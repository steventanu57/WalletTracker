import asyncio
import csv
import functools
import hashlib
import html
import io
import os
import re
import sys
import time
import traceback
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote
from warnings import filterwarnings

import aiohttp
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatAction, ChatType, MessageEntityType
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    TypeHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

# per_message=False is intended: the buttons belong to the user's conversation, not to one message
filterwarnings(
    action="ignore", message=r".*CallbackQueryHandler", category=PTBUserWarning
)

# Personal settings are read from .env (copy .env.example to .env and fill it in)
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Names with emoji would crash print() when output goes to a log file on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
ADDRESS_RE = re.compile(
    r"0x[0-9a-f]{40}"
)  # Hyperliquid addresses: 0x + 40 hex characters

# Hypurrscan's public API (no key needed)
GLOBAL_ALIASES_URL = "https://api.hypurrscan.io/globalAliases"
HOLDERS_URL = "https://api.hypurrscan.io/holdersWithLimit/{token}/{limit}"
ADDRESS_PAGE_URL = "https://hypurrscan.io/address/{address}"
# Hyperliquid's public API (no key needed): positions, balances and trades of any wallet
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"

MESSAGE_LIMIT = 4000  # Telegram refuses messages over 4096 characters, so long lists are split below this
ALIAS_CACHE_SECONDS = (
    600  # The public names list (~30 KB) is downloaded at most once every 10 minutes
)
SPOT_NAMES_CACHE_SECONDS = 3600  # Hyperliquid's list of spot pairs changes rarely
HOLDERS_PER_PAGE = 15
HOLDERS_MAX = (
    1000  # Paging stops here (the largest list size checked against the live API)
)
GLOBAL_ALIASES_PER_PAGE = 30
NAME_MATCHES_SHOWN = 10
NAME_SHOWN_CHARS = 40  # Long names are shortened inside page lists, so a page always fits in one message
ALIAS_BUTTONS_SHOWN = (
    50  # With more aliases than this, the rest can still be picked by typing the name
)
QUESTION_TIMEOUT_SECONDS = (
    600  # A typed answer that comes later than this isn't taken as the answer
)
ALERT_INTERVALS = {900: "15 min", 3600: "1 h", 14400: "4 h"}  # Seconds -> label
DEFAULT_ALERT_INTERVAL = 3600
FILLS_PER_REQUEST = 2000  # Hyperliquid returns at most this many trades per request
FILLS_MAX_PAGES = 5  # So at most 10,000 trades per wallet per check (Hyperliquid keeps the last 10,000)
IMPORT_MAX_BYTES = 100_000
IMPORT_MAX_ROWS = 1000
NO_PREVIEW = LinkPreviewOptions(
    is_disabled=True
)  # Don't show a website preview under messages with links

# Conversation states (saved in bot_data.pkl, so keep the numbers)
START_BUTTON_TAPS = 0
REGISTER_ALIAS1 = 1
REGISTER_ALIAS2 = 2
REGISTER_AGAIN = 3
REMOVE_ALIAS1 = 4
REMOVE_AGAIN = 5
SEARCH_ALIAS1 = 6
SEARCH_AGAIN = 7
TOP_HOLDERS1 = 8
SHOW_TOP_HOLDERS_AGAIN = 9
REGISTER_CONFIRM = 10
WALLET1 = 11
WALLET_AGAIN = 12
RENAME1 = 13
RENAME2 = 14
IMPORT1 = 15

# Continue and Cancel Texts
CONTINUE_BUTTON = "Continue"
CANCEL_BUTTON = "Cancel"
REPLACE_BUTTON = "Replace"
RENAME_BUTTON = "Choose another name"

# All-in-One Start Buttons' Texts
START_REGISTER_ALIAS = "Register an alias"
START_REMOVE_ALIAS = "Remove a registered alias"
START_SEARCH_ALIAS = "Search public names"
START_GLOBAL_ALIAS = "List of public names"
START_TOP_HOLDERS = "Find token top holders"
START_VIEW_ALIAS = "View registered aliases"
START_WALLET = "Check a wallet"
START_ALERTS = "Wallet alerts"
START_BUTTONS = [
    START_REGISTER_ALIAS,
    START_REMOVE_ALIAS,
    START_SEARCH_ALIAS,
    START_GLOBAL_ALIAS,
    START_TOP_HOLDERS,
    START_VIEW_ALIAS,
    START_WALLET,
    START_ALERTS,
]
# Buttons under the list of your aliases (handled like the menu buttons)
START_RENAME = "Rename an alias"
START_EXPORT = "Export aliases (CSV)"
START_IMPORT = "Import aliases (CSV)"
MENU_ACTIONS = [*START_BUTTONS, START_RENAME, START_EXPORT, START_IMPORT]


def exact(*texts):
    # A pattern that matches only these button texts, so each CallbackQueryHandler only gets its own buttons
    return "^(" + "|".join(re.escape(text) for text in texts) + ")$"


# Commands shown in Telegram's Menu button (the bot sets them itself on startup) and by /help
COMMANDS = [
    ("start", "Show every feature as a button"),
    ("register_alias", "Save a name for a wallet address"),
    ("remove_alias", "Remove a saved name"),
    ("view_aliases", "List your saved names"),
    ("rename_alias", "Rename a saved name"),
    ("export_aliases", "Download your saved names as a CSV file"),
    ("import_aliases", "Add saved names from a CSV file"),
    ("wallet", "Show a wallet's positions and balances"),
    ("alerts", "Get regular summaries of your saved wallets' trades"),
    ("search_alias", "Find the public name of an address, or addresses by name"),
    ("get_global_aliases", "Browse the public names on Hypurrscan"),
    ("get_top_holders", "Show the top holders of a token, e.g. USDC"),
    ("help", "List the commands"),
    ("cancel", "Leave the current step"),
]
KNOWN_COMMANDS = {command for command, _ in COMMANDS}

# Markups for buttons
ASK_MARKUP = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton(CONTINUE_BUTTON, callback_data=CONTINUE_BUTTON)],
        [InlineKeyboardButton(CANCEL_BUTTON, callback_data=CANCEL_BUTTON)],
    ]
)

# Shown under every question, so leaving doesn't require typing /cancel
CANCEL_MARKUP = InlineKeyboardMarkup(
    [[InlineKeyboardButton(CANCEL_BUTTON, callback_data=CANCEL_BUTTON)]]
)

REPLACE_MARKUP = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton(REPLACE_BUTTON, callback_data=REPLACE_BUTTON)],
        [InlineKeyboardButton(RENAME_BUTTON, callback_data=RENAME_BUTTON)],
        [InlineKeyboardButton(CANCEL_BUTTON, callback_data=CANCEL_BUTTON)],
    ]
)

AIO_MARKUP = InlineKeyboardMarkup(
    [[InlineKeyboardButton(text, callback_data=text)] for text in START_BUTTONS]
)

VIEW_MARKUP = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✏️ Rename", callback_data=START_RENAME),
            InlineKeyboardButton("🔔 Alerts", callback_data=START_ALERTS),
        ],
        [
            InlineKeyboardButton("📤 Export CSV", callback_data=START_EXPORT),
            InlineKeyboardButton("📥 Import CSV", callback_data=START_IMPORT),
        ],
    ]
)


# IMPORTANT: Handler functions MUST be starting with async
# context.user_data is a TEMPORARY per-user dictionary, PERMANENT if using picklepersistence
# ConversationHandler is for enabling inputs from users after a follow-up reply.


def now():
    return (
        time.time()
    )  # Seconds since 1970 (the tests move this forward to check timeouts)


def now_ms():
    return int(now() * 1000)  # Hyperliquid counts time in milliseconds


def utc(seconds):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(seconds))


# ======== Hypurrscan and Hyperliquid ========
http_session = (
    None  # One connection, reused by every request (faster than a new one each time)
)
global_alias_cache = {"time": 0.0, "data": None}
spot_names_cache = {"time": 0.0, "data": None}


def get_http_session():
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT)
    return http_session


async def fetch_json(url):
    """Returns the decoded JSON, or None when Hypurrscan can't be reached or answers with an error."""
    try:
        async with get_http_session().get(url) as response:
            if response.status != 200:
                return None
            return await response.json()
    except (aiohttp.ClientError, TimeoutError):
        return None


async def hyperliquid(body):
    """Asks Hyperliquid's info API (a POST with a JSON body). None when it can't be reached."""
    try:
        async with get_http_session().post(HYPERLIQUID_INFO_URL, json=body) as response:
            if response.status != 200:
                return None
            return await response.json()
    except (aiohttp.ClientError, TimeoutError):
        return None


async def close_http_session(app) -> None:
    # Runs when the bot stops (app.post_shutdown)
    if http_session is not None:
        await http_session.close()


async def get_global_alias_data():
    """Hypurrscan's public names as {address: name}, cached for ALIAS_CACHE_SECONDS. None if unavailable."""
    cached = global_alias_cache["data"]
    if (
        cached is not None
        and time.monotonic() - global_alias_cache["time"] < ALIAS_CACHE_SECONDS
    ):
        return cached

    data = await fetch_json(GLOBAL_ALIASES_URL)
    if not isinstance(data, dict):  # An unexpected answer counts as a failure
        return None
    global_alias_cache.update(time=time.monotonic(), data=data)
    return data


async def get_spot_names():
    """Spot pairs' readable names, e.g. {"@107": "HYPE/USDC"} (trades use the "@107" form). {} if unavailable."""
    cached = spot_names_cache["data"]
    if (
        cached is not None
        and time.monotonic() - spot_names_cache["time"] < SPOT_NAMES_CACHE_SECONDS
    ):
        return cached

    meta = await hyperliquid({"type": "spotMeta"})
    if not isinstance(meta, dict):
        return {}
    tokens = {
        token.get("index"): token.get("name")
        for token in meta.get("tokens", [])
        if isinstance(token, dict)
    }
    names = {}
    for pair in meta.get("universe", []):
        try:
            names[pair["name"]] = "/".join(tokens[index] for index in pair["tokens"])
        except (KeyError, TypeError):
            continue  # A pair with missing details keeps its "@123" name
    spot_names_cache.update(time=time.monotonic(), data=names)
    return names


def system_label(address):
    # Hyperliquid gives every token a "system address" for HyperCore <-> HyperEVM transfers:
    # 0x20, then zeros, then the token's index (USDC is 0x2000...0000). HYPE's is 0x2222...2222.
    if address == "0x" + "22" * 20:
        return "HYPE system address (HyperEVM bridge)"
    if (
        ADDRESS_RE.fullmatch(address)
        and address.startswith("0x20")
        and int(address[4:], 16) < 2**32
    ):
        return "token system address (HyperEVM bridge)"
    return None


# ======== Numbers ========
def to_decimal(value):
    """Hyperliquid sends numbers as text ("2468.19423"). Decimal keeps them exact. None if not a number."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def fmt(number):
    # 1234567.50 -> "1,234,567.5"
    text = f"{number:,f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def significant(number, digits=6):
    return number.quantize(Decimal(1).scaleb(number.adjusted() - digits + 1))


def price(number):
    return fmt(significant(number)) if number else "0"


def amount(number):
    # Up to 4 decimals for normal amounts, 6 significant digits for tiny ones
    if not number:
        return "0"
    return fmt(
        number.quantize(Decimal("0.0001")) if abs(number) >= 1 else significant(number)
    )


def clean_position(number):
    # Hyperliquid's position numbers carry float noise (e.g. -1.0000000001 for -1).
    # Rounding to 8 decimals removes it; real sizes never have more decimals than that.
    return number.quantize(Decimal("0.00000001"))


def usd(number, sign=False):
    text = f"${abs(number):,.2f}"
    if sign:
        return ("+" if number >= 0 else "-") + text
    return ("-" if number < 0 else "") + text


# ======== Message helpers ========
def address_html(address):
    # <code> makes the address tap-to-copy in Telegram; the link opens it on Hypurrscan
    url = ADDRESS_PAGE_URL.format(address=quote(address))
    return (
        f'<code>{html.escape(address)}</code> (<a href="{html.escape(url)}">view</a>)'
    )


def alias_lines(aliases):
    lines = [f"<b>Registered aliases ({len(aliases)}):</b>"]
    for alias in sorted(aliases, key=str.lower):
        lines.append(f"• <b>{html.escape(alias)}</b> — {address_html(aliases[alias])}")
    return lines


def shorten(name):
    name = str(name)
    return name if len(name) <= NAME_SHOWN_CHARS else name[: NAME_SHOWN_CHARS - 1] + "…"


def find_alias(aliases, name):
    # Alias names keep the capitals they were typed with, but are matched without caring about case
    for alias in aliases:
        if alias.lower() == name.lower():
            return alias
    return None


def names_for(aliases, address):
    # All your aliases that point to this address
    return [
        alias for alias in sorted(aliases, key=str.lower) if aliases[alias] == address
    ]


def split_message(lines):
    # Joins lines into as few messages as possible, each under Telegram's length limit
    parts, current = [], ""
    for line in lines:
        if current and len(current) + 1 + len(line) > MESSAGE_LIMIT:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    parts.append(current)
    return parts


async def send(bot, chat_id, text, reply_markup=None):
    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        link_preview_options=NO_PREVIEW,
    )


async def send_lines(bot, chat_id, lines, reply_markup=None):
    # Long lists are split over several messages. Buttons go under the last one.
    parts = split_message(lines)
    for part in parts[:-1]:
        await send(bot, chat_id, part)
    await send(bot, chat_id, parts[-1], reply_markup)


async def say(update, context, text, reply_markup=None):
    """Sends an HTML message to the chat the update came from (works for typed messages and button taps)."""
    return await send(context.bot, update.effective_chat.id, text, reply_markup)


async def say_lines(update, context, lines, reply_markup=None):
    await send_lines(context.bot, update.effective_chat.id, lines, reply_markup)


async def ask(update, context, text, reply_markup=CANCEL_MARKUP):
    """Asks a question that waits for a typed answer. Every question has a Cancel button.
    The time is remembered, so an answer that comes much later isn't taken (see answer_in_time)."""
    context.user_data["asked_at"] = now()
    return await say(update, context, text, reply_markup)


def answer_in_time(handler):
    """Wraps a handler for a typed answer: an answer that comes more than
    QUESTION_TIMEOUT_SECONDS after the question closes it instead of being taken.
    This also works after a restart, because the time is saved with the user's data."""

    @functools.wraps(handler)
    async def checked(update, context):
        if now() - context.user_data.get("asked_at", 0) > QUESTION_TIMEOUT_SECONDS:
            forget_unfinished(context)
            await say(
                update,
                context,
                f"⌛ I asked that more than {QUESTION_TIMEOUT_SECONDS // 60} minutes ago, so I closed the question.\n"
                "Send /start to begin again.",
            )
            return ConversationHandler.END
        return await handler(update, context)

    return checked


async def show_typing(update, context):
    # Shows "typing..." while the bot waits for Hypurrscan or Hyperliquid
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )


def user_name(update):
    return update.effective_user.first_name if update.effective_user else "Someone"


def forget_unfinished(context):
    # Temporary values from a question that was left unanswered shouldn't stay in the saved data
    for key in ("address", "new_alias", "rename_from", "asked_at"):
        context.user_data.pop(key, None)


def button_data(prefix, text):
    # Telegram limits button data to 64 bytes, so a long alias is identified by a short fingerprint
    return f"{prefix}:" + hashlib.sha256(text.encode()).hexdigest()[:16]


def remove_button_data(alias):
    return button_data("rm", alias)


def alias_from_button(aliases, prefix, data):
    return next((a for a in aliases if button_data(prefix, a) == data), None)


def pick_markup(aliases, prefix, icon):
    # One button per alias (tap to pick it), plus Cancel
    names = sorted(aliases, key=str.lower)[:ALIAS_BUTTONS_SHOWN]
    rows = [
        [
            InlineKeyboardButton(
                f"{icon} {alias}", callback_data=button_data(prefix, alias)
            )
        ]
        for alias in names
    ]
    rows.append([InlineKeyboardButton(CANCEL_BUTTON, callback_data=CANCEL_BUTTON)])
    return InlineKeyboardMarkup(rows)


def remove_markup(aliases):
    return pick_markup(aliases, "rm", "🗑")


# ======== Private chats only ========
def command_for_me(update, context):
    # A command addressed to this bot: /wallet@this_bot, or one of its own commands without a @name
    message = update.message
    if not message or not message.text or not message.entities:
        return False
    first = message.entities[0]
    if first.type != MessageEntityType.BOT_COMMAND or first.offset != 0:
        return False
    command, _, mention = message.text[1 : first.length].partition("@")
    if mention:
        return mention.lower() == (context.bot.username or "").lower()
    return command.lower() in KNOWN_COMMANDS


async def private_chats_only(update: object, context: CallbackContext) -> None:
    """Runs before every other handler. In a group or channel the bot only points people to a
    private chat, so nobody's saved wallets or alerts are shown to the group."""
    if not isinstance(update, Update):
        return
    chat = update.effective_chat
    if chat is None or chat.type == ChatType.PRIVATE:
        return  # A private chat: the normal handlers take it from here

    if update.callback_query:
        await update.callback_query.answer(
            "I only work in private chats. Open a chat with me to use this.",
            show_alert=True,
        )
    elif command_for_me(update, context):
        await say(
            update,
            context,
            "👋 I only work in private chats, so your saved wallets stay private.\n"
            "Tap below to open a chat with me.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Open a private chat",
                            url=f"https://t.me/{context.bot.username}",
                        )
                    ]
                ]
            ),
        )
    raise ApplicationHandlerStop  # Nothing else runs for updates from groups and channels


# Main handler functions
# Make the /start display all the buttons in a compact table
async def start(update: Update, context: CallbackContext) -> int:
    user = update.effective_user
    name = (
        user.first_name or "there"
    )  # "there" is a fallback incase the name is not returned

    await say(
        update,
        context,
        f"👋 <b>Hello {html.escape(name)}!</b>\nWelcome to <b>Hyperliquid Wallet Tracker</b> Bot!\n \n"
        "Click the buttons below to access commands\n"
        "OR\n"
        "Click the <b>Menu</b> button beside the chat bar",
        reply_markup=AIO_MARKUP,
    )

    return START_BUTTON_TAPS


async def start_button_taps(update: Update, context: CallbackContext) -> int:
    # The menu buttons work at any time, also after another question was started (it's an entry point)
    query = update.callback_query
    await query.answer()
    choice = query.data
    forget_unfinished(context)

    # If checks for conversation states
    if choice == START_REGISTER_ALIAS:
        return await register_alias1(update, context)  # Call function immediately
    elif choice == START_REMOVE_ALIAS:
        return await remove_alias1(update, context)
    elif choice == START_SEARCH_ALIAS:
        return await search_alias1(update, context)
    elif choice == START_GLOBAL_ALIAS:
        return await get_global_aliases(update, context)
    elif choice == START_TOP_HOLDERS:
        return await top_holders1(update, context)
    elif choice == START_VIEW_ALIAS:
        await view_aliases(update, context)
        return START_BUTTON_TAPS
    elif choice == START_WALLET:
        return await wallet1(update, context)
    elif choice == START_ALERTS:
        await alerts_command(update, context)
        return ConversationHandler.END
    elif choice == START_RENAME:
        return await rename_alias1(update, context)
    elif choice == START_EXPORT:
        return await export_aliases(update, context)
    elif choice == START_IMPORT:
        return await import_aliases1(update, context)

    return ConversationHandler.END


async def help_command(update: Update, context: CallbackContext) -> None:
    lines = ["<b>Commands</b>"]
    lines += [
        f"/{command} - {html.escape(description)}" for command, description in COMMANDS
    ]
    await say(update, context, "\n".join(lines))


async def set_bot_commands(app) -> None:
    # Fills Telegram's Menu button with the commands
    await app.bot.set_my_commands(
        [BotCommand(command, description) for command, description in COMMANDS]
    )


async def start_up(app) -> None:
    # Runs once when the bot starts (app.post_init)
    await set_bot_commands(app)
    if app.job_queue is None:
        print(
            "Warning: wallet alerts are off because a package is missing. "
            "Run: pip install -r requirements.txt"
        )
        return
    # Alert checks aren't saved, so start them again for everyone who had alerts on
    for user_id, user_data in app.user_data.items():
        schedule_alerts(app.job_queue, user_id, user_data)


# START_REGISTER_ALIAS
async def register_alias1(update: Update, context: CallbackContext) -> int:
    aliases = context.user_data.get("aliases", {})
    if aliases:
        await say_lines(update, context, alias_lines(aliases))

    await ask(
        update,
        context,
        "Please enter a valid address to be added to the list of aliases (e.g 0xffffffffffffffffffffffffffffffffffffffff)",
    )

    return REGISTER_ALIAS1


async def register_alias2(update: Update, context: CallbackContext) -> int:
    address = update.message.text.strip().lower()

    print(f"{user_name(update)} is appending {address}")

    if not ADDRESS_RE.fullmatch(address):
        await ask(
            update,
            context,
            "❌ That is not a valid address. It should be 0x followed by 40 characters (0-9, a-f).\nPlease try again",
        )
        return REGISTER_ALIAS1

    context.user_data["address"] = (
        address  # A temporary dictionary to be able to access this address in register_alias3
    )
    print(f"{user_name(update)} successfully appended {address}")

    await ask(
        update,
        context,
        "✅ <b>Great</b>!\nWhat would you like to name this address?",
    )
    return REGISTER_ALIAS2


async def register_alias3(update: Update, context: CallbackContext) -> int:
    alias = update.message.text.strip()

    if not alias:
        await ask(update, context, "❌ Try again. Please type a proper alias")
        print(f"{user_name(update)} did not type a proper alias")
        return REGISTER_ALIAS2

    aliases = context.user_data.get("aliases", {})
    existing = find_alias(aliases, alias)
    if existing and aliases[existing] != context.user_data["address"]:
        # Saving would silently replace the address this name points to, so ask first
        context.user_data["new_alias"] = alias
        await say(
            update,
            context,
            f"⚠️ <b>{html.escape(existing)}</b> already points to {address_html(aliases[existing])}.\n"
            f"Replace it with {address_html(context.user_data['address'])}?",
            reply_markup=REPLACE_MARKUP,
        )
        return REGISTER_CONFIRM

    return await save_alias(update, context, alias)


async def register_confirm(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(
        reply_markup=None
    )  # The question is answered: remove its buttons

    if query.data == REPLACE_BUTTON:
        return await save_alias(update, context, context.user_data.pop("new_alias"))

    context.user_data.pop("new_alias", None)
    await ask(update, context, "What would you like to name this address?")
    return REGISTER_ALIAS2


async def save_alias(update, context, alias) -> int:
    address = context.user_data.pop(
        "address"
    )  # pop so the temporary address isn't saved forever
    context.user_data.pop("asked_at", None)

    aliases = context.user_data.setdefault("aliases", {})
    existing = find_alias(aliases, alias)
    replaced_address = aliases.get(existing) if existing else None
    if existing:
        del aliases[
            existing
        ]  # Same name with other capitals: keep only the new spelling
    aliases[alias] = address
    if replaced_address and replaced_address != address:
        drop_unused_alert(update, context, replaced_address)

    await say(
        update,
        context,
        f"✅ Alias <b>{html.escape(alias)}</b> saved for address {address_html(address)}",
    )
    print(f"{user_name(update)} registered alias {alias} for address {address}")

    await say_lines(update, context, alias_lines(aliases))
    await say(
        update,
        context,
        "Would you like to register another address and alias?",
        reply_markup=ASK_MARKUP,
    )

    return REGISTER_AGAIN


async def register_again(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)

    await ask(
        update,
        context,
        "Please enter a valid address to be added to the list of aliases (e.g 0xffffffffffffffffffffffffffffffffffffffff)",
    )
    print(f"{user_name(update)} is trying to register another address")
    return REGISTER_ALIAS1


# START_REMOVE_ALIAS
async def remove_alias1(update: Update, context: CallbackContext) -> int:
    aliases = context.user_data.get("aliases")
    if not aliases:
        await say(update, context, "❌ Please register an alias first")
        print(f"{user_name(update)} failed to remove aliases")
        return ConversationHandler.END

    await say_lines(update, context, alias_lines(aliases))
    await ask(
        update,
        context,
        "Which alias would you like to remove?\nTap it below, or type its name",
        reply_markup=remove_markup(aliases),
    )
    print(f"{user_name(update)} is trying to remove an alias")

    return REMOVE_ALIAS1


async def remove_alias2(update: Update, context: CallbackContext) -> int:
    aliases = context.user_data.get("aliases", {})
    alias = find_alias(aliases, update.message.text.strip())

    if alias is None:
        await ask(
            update,
            context,
            "❌ The alias is not found in the registered aliases.\nPlease input another valid alias",
            reply_markup=remove_markup(aliases),
        )
        print(
            f"{user_name(update)} failed to remove alias {update.message.text.strip()}"
        )
        return REMOVE_ALIAS1

    return await remove_and_ask_again(update, context, alias)


async def remove_tap(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    aliases = context.user_data.get("aliases", {})
    alias = alias_from_button(aliases, "rm", query.data)
    if alias is None:
        await query.answer("That alias was already removed")
        return REMOVE_ALIAS1

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    return await remove_and_ask_again(update, context, alias)


async def remove_and_ask_again(update, context, alias) -> int:
    aliases = context.user_data["aliases"]
    address = aliases.pop(alias)  # Removes the key and the value in the dict
    context.user_data.pop("asked_at", None)
    drop_unused_alert(update, context, address)
    print(f"{user_name(update)} has successfully removed {alias} from aliases")

    if aliases:
        await say_lines(update, context, alias_lines(aliases))
    await say(
        update,
        context,
        f"✅ Alias <b>{html.escape(alias)}</b> has been removed.\nWould you like to remove another alias?",
        reply_markup=ASK_MARKUP,  # This will just display "Continue" and "Cancel" buttons beneath the text above
    )

    return REMOVE_AGAIN


async def remove_again_choice(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()

    if not context.user_data.get("aliases"):
        await query.edit_message_text(
            "❌ No more aliases left to remove."
        )  # query.edit_message_text EDITS text, instead of sending a new text
        print(f"{user_name(update)} has no more aliases left to remove")
        return ConversationHandler.END

    await query.edit_message_reply_markup(reply_markup=None)
    print(f"{user_name(update)} is trying to remove another alias")
    return await remove_alias1(update, context)


# START_VIEW_ALIAS
async def view_aliases(update: Update, context: CallbackContext) -> None:
    aliases = context.user_data.get("aliases")
    if not aliases:
        await say(update, context, "❌ Please register an alias first")
        print(f"{user_name(update)} failed to view registered aliases")
        return

    await say_lines(update, context, alias_lines(aliases), reply_markup=VIEW_MARKUP)
    print(f"{user_name(update)} is viewing registered aliases")


# START_RENAME
async def rename_alias1(update: Update, context: CallbackContext) -> int:
    aliases = context.user_data.get("aliases")
    if not aliases:
        await say(update, context, "❌ Please register an alias first")
        return ConversationHandler.END

    await say_lines(update, context, alias_lines(aliases))
    await ask(
        update,
        context,
        "Which alias would you like to rename?\nTap it below, or type its name",
        reply_markup=pick_markup(aliases, "rn", "✏️"),
    )
    return RENAME1


async def rename_alias2(update: Update, context: CallbackContext) -> int:
    aliases = context.user_data.get("aliases", {})
    alias = find_alias(aliases, update.message.text.strip())
    if alias is None:
        await ask(
            update,
            context,
            "❌ The alias is not found in the registered aliases.\nPlease input another valid alias",
            reply_markup=pick_markup(aliases, "rn", "✏️"),
        )
        return RENAME1
    return await ask_new_name(update, context, alias)


async def rename_tap(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    alias = alias_from_button(context.user_data.get("aliases", {}), "rn", query.data)
    if alias is None:
        await query.answer("That alias no longer exists")
        return RENAME1

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    return await ask_new_name(update, context, alias)


async def ask_new_name(update, context, alias) -> int:
    context.user_data["rename_from"] = alias
    await ask(
        update, context, f"What should <b>{html.escape(alias)}</b> be called now?"
    )
    return RENAME2


async def rename_alias3(update: Update, context: CallbackContext) -> int:
    aliases = context.user_data.get("aliases", {})
    old = context.user_data.get("rename_from")
    new = update.message.text.strip()

    if old not in aliases:  # Removed in the meantime
        forget_unfinished(context)
        await say(update, context, "❌ That alias no longer exists.")
        return ConversationHandler.END
    if not new:
        await ask(update, context, "❌ Try again. Please type a proper alias")
        return RENAME2
    clash = find_alias(aliases, new)
    if clash and clash != old:
        await ask(
            update,
            context,
            f"❌ <b>{html.escape(clash)}</b> is already used for {address_html(aliases[clash])}.\n"
            "Please choose another name",
        )
        return RENAME2

    aliases[new] = aliases.pop(old)
    forget_unfinished(context)
    await say(
        update,
        context,
        f"✅ Renamed <b>{html.escape(old)}</b> to <b>{html.escape(new)}</b>",
    )
    await say_lines(update, context, alias_lines(aliases))
    print(f"{user_name(update)} renamed alias {old} to {new}")
    return ConversationHandler.END


# START_EXPORT / START_IMPORT
async def export_aliases(update: Update, context: CallbackContext) -> int:
    aliases = context.user_data.get("aliases")
    if not aliases:
        await say(update, context, "❌ Please register an alias first")
        return ConversationHandler.END

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["alias", "address"])
    for alias in sorted(aliases, key=str.lower):
        writer.writerow([alias, aliases[alias]])
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        # utf-8-sig adds a marker that lets Excel show non-English characters correctly
        document=out.getvalue().encode("utf-8-sig"),
        filename="aliases.csv",
        caption=f"📤 Your {len(aliases)} saved aliases. Edit the file and send it back with /import_aliases to add names.",
    )
    print(f"{user_name(update)} exported {len(aliases)} aliases")
    return ConversationHandler.END


async def import_aliases1(update: Update, context: CallbackContext) -> int:
    await ask(
        update,
        context,
        "📥 Send me a CSV file with two columns: <b>alias,address</b>\n"
        "(The file from /export_aliases has this format. A header row is fine.)",
    )
    return IMPORT1


def read_csv_rows(text):
    # Excel saves CSV with ";" or tabs in some countries, so detect the separator
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text), dialect))


async def import_file(update: Update, context: CallbackContext) -> int:
    document = update.message.document
    too_big = (
        "❌ That file is too big (the limit is 100 KB). Please send a smaller CSV file"
    )
    if document.file_size and document.file_size > IMPORT_MAX_BYTES:
        await ask(update, context, too_big)
        return IMPORT1

    raw = await (await document.get_file()).download_as_bytearray()
    if len(raw) > IMPORT_MAX_BYTES:
        await ask(update, context, too_big)
        return IMPORT1
    try:
        text = bytes(raw).decode("utf-8-sig")
    except UnicodeDecodeError:
        await ask(
            update,
            context,
            "❌ I couldn't read that file. Save it as CSV (UTF-8) and send it again",
        )
        return IMPORT1

    numbered = [
        (number, row)
        for number, row in enumerate(read_csv_rows(text), 1)
        if any(cell.strip() for cell in row)
    ]
    if numbered and [c.strip().lower() for c in numbered[0][1][:2]] == [
        "alias",
        "address",
    ]:
        numbered = numbered[1:]  # The header row
    if len(numbered) > IMPORT_MAX_ROWS:
        await ask(
            update,
            context,
            f"❌ That file has more than {IMPORT_MAX_ROWS} rows. Please split it into smaller files",
        )
        return IMPORT1

    aliases = context.user_data.setdefault("aliases", {})
    added, unchanged, skipped, kept = 0, 0, [], []
    for number, row in numbered:
        alias = row[0].strip()
        address = row[1].strip().lower() if len(row) > 1 else ""
        if not alias or not ADDRESS_RE.fullmatch(address):
            skipped.append(str(number))
            continue
        existing = find_alias(aliases, alias)
        if existing is None:
            aliases[alias] = address
            added += 1
        elif aliases[existing] == address:
            unchanged += 1
        else:
            kept.append(existing)  # Never replace an address without asking

    forget_unfinished(context)
    lines = ["📥 <b>Import finished</b>", f"✅ {added} new alias(es) added"]
    if unchanged:
        lines.append(f"• {unchanged} were already saved")
    if skipped:
        rows = ", ".join(skipped[:10]) + ("…" if len(skipped) > 10 else "")
        lines.append(
            f"• {len(skipped)} row(s) skipped (no name, or not a valid address): row {rows}"
        )
    if kept:
        names = ", ".join(html.escape(shorten(name)) for name in kept[:10])
        lines.append(
            f"• {len(kept)} kept as they were, because the name already points to another address: {names}"
            + ("…" if len(kept) > 10 else "")
        )
    lines.append("Send /view_aliases to see them all.")
    await say_lines(update, context, lines)
    print(f"{user_name(update)} imported {added} aliases")
    return ConversationHandler.END


async def import_not_a_file(update: Update, context: CallbackContext) -> int:
    await ask(
        update,
        context,
        "Please send the CSV as a file (📎 → File), or tap Cancel",
    )
    return IMPORT1


# START_WALLET
async def wallet_lines(context, address):
    """A wallet's perp positions, account value and spot balances, or None if Hyperliquid can't be reached."""
    perps, spot = await asyncio.gather(
        hyperliquid({"type": "clearinghouseState", "user": address}),
        hyperliquid({"type": "spotClearinghouseState", "user": address}),
    )
    if not isinstance(perps, dict) or not isinstance(spot, dict):
        return None

    names = names_for(context.user_data.get("aliases", {}), address)
    lines = [
        f"👛 <b>{html.escape(names[0])}</b>" if names else "👛 <b>Wallet</b>",
        address_html(address),
    ]
    if isinstance(perps.get("time"), int):
        lines.append(f"<i>Data from {utc(perps['time'] / 1000)}</i>")

    summary = perps.get("marginSummary") or {}
    value = to_decimal(summary.get("accountValue"))
    withdrawable = to_decimal(perps.get("withdrawable"))
    header = "\n<b>Perps</b>"
    if value is not None:
        header += f" · account value {usd(value)}"
    if withdrawable is not None:
        header += f" · withdrawable {usd(withdrawable)}"
    lines.append(header)

    positions = [
        entry.get("position")
        for entry in perps.get("assetPositions") or []
        if isinstance(entry, dict) and isinstance(entry.get("position"), dict)
    ]
    shown = 0
    for position in positions:
        size = to_decimal(position.get("szi"))
        if not size:
            continue
        shown += 1
        line = f"• <b>{html.escape(str(position.get('coin')))}</b> {'LONG' if size > 0 else 'SHORT'} {amount(abs(size))}"
        entry_px = to_decimal(position.get("entryPx"))
        if entry_px:
            line += f" @ {price(entry_px)}"
        position_value = to_decimal(position.get("positionValue"))
        if position_value is not None:
            line += f" · value {usd(position_value)}"
        pnl = to_decimal(position.get("unrealizedPnl"))
        if pnl is not None:
            line += f" · uPnL {usd(pnl, sign=True)}"
            roe = to_decimal(position.get("returnOnEquity"))
            if roe is not None:
                line += f" ({roe * 100:+.1f}%)"
        leverage = position.get("leverage") or {}
        if isinstance(leverage, dict) and leverage.get("value"):
            line += f" · {leverage['value']}x {html.escape(str(leverage.get('type', '')))}".rstrip()
        liquidation = to_decimal(position.get("liquidationPx"))
        if liquidation:
            line += f" · liq {price(liquidation)}"
        lines.append(line)
    if not shown:
        lines.append("No open positions")

    lines.append("\n<b>Spot</b>")
    balances = [
        (str(entry.get("coin")), to_decimal(entry.get("total")))
        for entry in spot.get("balances") or []
        if isinstance(entry, dict)
    ]
    balances = [(coin, total) for coin, total in balances if total]
    for coin, total in balances:
        lines.append(f"• <b>{html.escape(coin)}</b> {amount(total)}")
    if not balances:
        lines.append("No spot balances")

    wallets = context.user_data.get("alert_wallets") or {}
    if address in wallets:
        every = ALERT_INTERVALS.get(
            context.user_data.get("alert_every", DEFAULT_ALERT_INTERVAL), "1 h"
        )
        lines.append(f"\n🔔 Alerts on (every {every}). Change them with /alerts")
    elif names:
        lines.append("\n🔕 Alerts off. Switch them on with /alerts")
    else:
        lines.append(
            "\nSave this address with /register_alias to get trade alerts for it."
        )
    return lines


def resolve_wallet(context, text):
    # An address, or the name of one of your aliases. None if it's neither.
    text = text.strip()
    if ADDRESS_RE.fullmatch(text.lower()):
        return text.lower()
    aliases = context.user_data.get("aliases", {})
    alias = find_alias(aliases, text)
    return aliases[alias] if alias else None


async def wallet1(update: Update, context: CallbackContext) -> int:
    # /wallet <alias or address> shows it right away; /wallet alone asks which one
    if context.args:
        return await wallet_answer(update, context, " ".join(context.args))

    aliases = context.user_data.get("aliases", {})
    await ask(
        update,
        context,
        "Which wallet? Send one of your aliases or an address"
        + ("\n(or tap a saved one below)" if aliases else ""),
        reply_markup=pick_markup(aliases, "w", "👛"),
    )
    return WALLET1


async def wallet2(update: Update, context: CallbackContext) -> int:
    return await wallet_answer(update, context, update.message.text)


async def wallet_answer(update, context, text) -> int:
    address = resolve_wallet(context, text)
    if address is None:
        await ask(
            update,
            context,
            f"❌ No saved alias or address matches <b>{html.escape(text.strip())}</b>.\n"
            "Send one of your aliases or an address (0x followed by 40 characters)",
            reply_markup=pick_markup(context.user_data.get("aliases", {}), "w", "👛"),
        )
        return WALLET1
    return await show_wallet(update, context, address)


async def wallet_tap(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    aliases = context.user_data.get("aliases", {})
    alias = alias_from_button(aliases, "w", query.data)
    if alias is None:
        await query.answer("That alias no longer exists")
        return WALLET1

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    return await show_wallet(update, context, aliases[alias])


async def show_wallet(update, context, address) -> int:
    context.user_data.pop("asked_at", None)
    print(f"{user_name(update)} is checking wallet {address}")
    await show_typing(update, context)
    lines = await wallet_lines(context, address)
    if lines is None:
        await say(update, context, "❌ Failed to fetch wallet data from Hyperliquid.")
        return ConversationHandler.END

    await say_lines(update, context, lines)
    await say(
        update,
        context,
        "Would you like to check another wallet?",
        reply_markup=ASK_MARKUP,
    )
    return WALLET_AGAIN


async def wallet_again(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    return await wallet1(update, context)


# START_ALERTS
def schedule_alerts(job_queue, user_id, user_data):
    """(Re)starts the repeating trade check for one user, or stops it when no wallet has alerts on."""
    for job in job_queue.get_jobs_by_name(f"alerts:{user_id}"):
        job.schedule_removal()
    if user_data.get("alert_wallets"):
        every = user_data.get("alert_every", DEFAULT_ALERT_INTERVAL)
        job_queue.run_repeating(
            check_alerts,
            interval=every,
            first=every,
            name=f"alerts:{user_id}",
            chat_id=user_id,  # In a private chat, the chat ID is the user's ID
            user_id=user_id,
        )


def drop_unused_alert(update, context, address):
    # No alias points to this address any more: stop its alerts too
    if address in context.user_data.get("aliases", {}).values():
        return
    wallets = context.user_data.get("alert_wallets") or {}
    if wallets.pop(address, None) is not None and context.job_queue:
        schedule_alerts(context.job_queue, update.effective_user.id, context.user_data)


def alerts_menu(context):
    aliases = context.user_data.get("aliases", {})
    wallets = context.user_data.get("alert_wallets") or {}
    every = context.user_data.get("alert_every", DEFAULT_ALERT_INTERVAL)
    text = (
        "<b>Wallet alerts</b>\n"
        "At each check you get one summary of every trade a wallet made since the last check.\n"
        f"Checked every <b>{ALERT_INTERVALS.get(every, '1 h')}</b>. "
        "Tap a wallet to switch its alerts on 🔔 or off 🔕:"
    )
    rows = [
        [
            InlineKeyboardButton(
                f"{'🔔' if aliases[alias] in wallets else '🔕'} {alias}",
                callback_data=button_data("al", alias),
            )
        ]
        for alias in sorted(aliases, key=str.lower)[:ALIAS_BUTTONS_SHOWN]
    ]
    rows.append(
        [
            InlineKeyboardButton(
                f"{'✅ ' if seconds == every else ''}Every {label}",
                callback_data=f"ai:{seconds}",
            )
            for seconds, label in ALERT_INTERVALS.items()
        ]
    )
    return text, InlineKeyboardMarkup(rows)


ALERTS_MISSING = "⚠️ Alerts aren't available: the bot is missing a package. Run pip install -r requirements.txt and restart it."


async def alerts_command(update: Update, context: CallbackContext) -> None:
    if context.job_queue is None:
        await say(update, context, ALERTS_MISSING)
        return
    if not context.user_data.get("aliases"):
        await say(
            update,
            context,
            "❌ Please register an alias first. Alerts are for your saved wallets.",
        )
        return
    text, markup = alerts_menu(context)
    await say(update, context, text, reply_markup=markup)


async def alerts_tap(update: Update, context: CallbackContext) -> None:
    # 🔔 / 🔕 and "Every ..." buttons under the alerts menu: change the setting and redraw the menu
    query = update.callback_query
    if context.job_queue is None:
        await query.answer(ALERTS_MISSING, show_alert=True)
        return

    if query.data.startswith("ai:"):
        seconds = int(query.data[3:])
        if seconds not in ALERT_INTERVALS:
            await query.answer()
            return
        context.user_data["alert_every"] = seconds
        note = f"Checking every {ALERT_INTERVALS[seconds]}"
    else:
        aliases = context.user_data.get("aliases", {})
        alias = alias_from_button(aliases, "al", query.data)
        if alias is None:
            await query.answer("That alias no longer exists")
            return
        wallets = context.user_data.setdefault("alert_wallets", {})
        address = aliases[alias]
        if address in wallets:
            del wallets[address]
            note = f"🔕 Alerts off for {alias}"
        else:
            wallets[address] = (
                now_ms()
            )  # Report trades from now on, not the wallet's history
            note = f"🔔 Alerts on for {alias}"

    schedule_alerts(context.job_queue, update.effective_user.id, context.user_data)
    await query.answer(note)
    text, markup = alerts_menu(context)
    await query.edit_message_text(
        text, parse_mode="HTML", reply_markup=markup, link_preview_options=NO_PREVIEW
    )


async def fetch_fills(address, start, end):
    """Every trade of a wallet with start <= time <= end (milliseconds), oldest first, and
    whether that's all of them. (None, False) if Hyperliquid can't be reached."""
    fills, seen = [], set()
    for _ in range(FILLS_MAX_PAGES):
        page = await hyperliquid(
            {
                "type": "userFillsByTime",
                "user": address,
                "startTime": start,
                "endTime": end,
            }
        )
        if not isinstance(page, list):
            return None, False
        new = [f for f in page if isinstance(f, dict) and f.get("tid") not in seen]
        seen.update(f.get("tid") for f in new)
        fills += new
        if len(page) < FILLS_PER_REQUEST:
            return fills, True
        last = page[-1].get("time") if isinstance(page[-1], dict) else None
        if not new or not isinstance(last, int):
            break
        start = (
            last  # The window includes this time again; trades seen already are skipped
        )
    return fills, False


def describe_position(start, end):
    # How a perp position changed during the period, e.g. "opened SHORT 13.6"
    def side(size):
        return "LONG" if size > 0 else "SHORT"

    if start == end:
        return "position unchanged"
    if start == 0:
        return f"opened {side(end)} {amount(abs(end))}"
    if end == 0:
        return f"closed {side(start)}"
    if (start > 0) != (end > 0):
        return f"flipped to {side(end)} {amount(abs(end))}"
    if abs(end) > abs(start):
        return f"added to {side(end)} (now {amount(abs(end))})"
    return f"reduced {side(end)} (now {amount(abs(end))})"


async def trade_summary(address, aliases, fills, since_ms, complete, every):
    """One message's lines summarising a wallet's trades, per coin."""
    spot_names = await get_spot_names()
    coins = {}
    for fill in fills:
        coin = str(fill.get("coin"))
        size, fill_price = to_decimal(fill.get("sz")), to_decimal(fill.get("px"))
        if size is None or fill_price is None:
            continue
        info = coins.setdefault(
            coin,
            {
                "trades": 0,
                "bought": Decimal(0),
                "sold": Decimal(0),
                "bought_value": Decimal(0),
                "sold_value": Decimal(0),
                "pnl": Decimal(0),
                "start": to_decimal(fill.get("startPosition")) or Decimal(0),
            },
        )
        info["trades"] += 1
        if fill.get("side") == "B":
            info["bought"] += size
            info["bought_value"] += size * fill_price
        else:
            info["sold"] += size
            info["sold_value"] += size * fill_price
        info["pnl"] += to_decimal(fill.get("closedPnl")) or 0

    names = names_for(aliases, address)
    who = f"<b>{html.escape(names[0])}</b>" if names else "A wallet with alerts on"
    lines = [f"🔔 {who} traded since {utc(since_ms / 1000)}", address_html(address)]
    by_value = sorted(
        coins.items(),
        key=lambda item: item[1]["bought_value"] + item[1]["sold_value"],
        reverse=True,
    )
    for coin, info in by_value:
        spot = coin.startswith("@") or "/" in coin
        name = spot_names.get(coin, coin) if spot else coin
        line = f"• <b>{html.escape(name)}</b> {'spot' if spot else 'perp'} · {info['trades']} trade(s)"
        if not spot:
            start = clean_position(info["start"])
            end = clean_position(info["start"] + info["bought"] - info["sold"])
            line += f" · {describe_position(start, end)}"
        if info["bought"]:
            line += f" · bought {amount(info['bought'])} (avg {price(info['bought_value'] / info['bought'])})"
        if info["sold"]:
            line += f" · sold {amount(info['sold'])} (avg {price(info['sold_value'] / info['sold'])})"
        if info["pnl"]:
            line += f" · realised PnL {usd(info['pnl'], sign=True)}"
        lines.append(line)
    if not complete:
        lines.append(
            f"⚠️ More than {len(fills):,} trades in this period: only the first {len(fills):,} are summarised."
        )
    lines.append(
        f"<i>Checked every {ALERT_INTERVALS.get(every, '1 h')} · /alerts to change</i>"
    )
    return lines


async def check_alerts(context: CallbackContext) -> None:
    """Runs every few minutes or hours for each user with alerts on: one summary per wallet that traded."""
    user_data = context.user_data
    wallets = user_data.get("alert_wallets") or {}
    aliases = user_data.get("aliases", {})
    every = user_data.get("alert_every", DEFAULT_ALERT_INTERVAL)
    until = now_ms()
    for address, since in list(wallets.items()):
        fills, complete = await fetch_fills(address, since, until)
        if fills is None:
            continue  # Hyperliquid couldn't be reached: the same period is asked for again next time
        wallets[address] = (
            until + 1
        )  # Next time, start right after this period (no gaps, no repeats)
        if fills:
            lines = await trade_summary(address, aliases, fills, since, complete, every)
            await send_lines(context.bot, context.job.chat_id, lines)


# START_GLOBAL_ALIAS
def global_aliases_page(data, offset):
    # One page of public names, with ◀ / ▶ buttons that edit the same message (see page_tap)
    entries = list(data.items())
    page = entries[offset : offset + GLOBAL_ALIASES_PER_PAGE]
    lines = [
        f"<b>Public names on Hypurrscan</b> ({offset + 1}-{offset + len(page)} of {len(entries)})"
    ]
    for addr, label in page:
        lines.append(f"{address_html(addr)}: {html.escape(shorten(label))}")
    return "\n".join(lines), page_markup(
        "ga", offset, GLOBAL_ALIASES_PER_PAGE, len(entries)
    )


def page_markup(prefix, offset, per_page, total):
    buttons = []
    if offset > 0:
        buttons.append(
            InlineKeyboardButton(
                "◀ Previous", callback_data=f"{prefix}:{max(offset - per_page, 0)}"
            )
        )
    if offset + per_page < total:
        buttons.append(
            InlineKeyboardButton(
                "Next ▶", callback_data=f"{prefix}:{offset + per_page}"
            )
        )
    return InlineKeyboardMarkup([buttons]) if buttons else None


async def get_global_aliases(update: Update, context: CallbackContext) -> int:
    await show_typing(update, context)
    data = await get_global_alias_data()
    if data is None:
        await say(update, context, "❌ Failed to fetch global aliases.")
        return ConversationHandler.END

    if not data:
        await say(update, context, "❌ No aliases found.")
        return ConversationHandler.END

    text, markup = global_aliases_page(data, 0)
    await say(update, context, text, reply_markup=markup)
    return ConversationHandler.END


async def page_tap(update: Update, context: CallbackContext) -> None:
    # ◀ / ▶ under a list: show the requested page in the same message
    query = update.callback_query
    prefix, _, rest = query.data.partition(":")

    if prefix == "ga":
        data = await get_global_alias_data()
        if not data:
            await query.answer("Couldn't load the public names. Please try again")
            return
        text, markup = global_aliases_page(data, int(rest))
    else:  # "th:<token>:<offset>"
        token, _, offset = rest.rpartition(":")
        page = await holders_page(context, token, int(offset))
        if not isinstance(page, tuple):
            await query.answer("Couldn't load the holders. Please try again")
            return
        text, markup = page

    await query.answer()
    await query.edit_message_text(
        text, parse_mode="HTML", reply_markup=markup, link_preview_options=NO_PREVIEW
    )


# START_SEARCH_ALIAS
async def search_alias1(update: Update, context: CallbackContext) -> int:
    await ask(
        update,
        context,
        "Send an address to find its public name (e.g 0xffffffffffffffffffffffffffffffffffffffff),\n"
        "or part of a name to find its addresses (e.g. HyperEVM). Your own aliases are searched too.",
    )
    return SEARCH_ALIAS1


async def search_alias2(update: Update, context: CallbackContext) -> int:
    # Gets the message from the user
    text = update.message.text.strip()
    print(f"{user_name(update)} is searching public names for {text}")

    await show_typing(update, context)
    data = await get_global_alias_data()
    if data is None:
        await say(update, context, "❌ Failed to fetch global aliases.")
        return ConversationHandler.END

    aliases = context.user_data.get("aliases", {})
    if ADDRESS_RE.fullmatch(text.lower()):
        # An address: look up its public name, and whether you saved it yourself
        address = text.lower()
        alias = data.get(address)
        mine = names_for(aliases, address)
        if not alias and not mine:
            await ask(
                update,
                context,
                "❌ No public name found for this address, and it isn't one of your aliases.\n"
                "Send another address or part of a name",
            )
            print(f"Could not find alias for {address}")
            return SEARCH_ALIAS1

        message_lines = [
            "✅ <b>Search successful!</b>",
            f"<b>Address</b>: {address_html(address)}",
        ]
        if alias:
            message_lines.append(f"<b>Alias</b>: {html.escape(str(alias))}")
        if mine:
            message_lines.append(
                "<b>Saved by you as</b>: "
                + ", ".join(f"⭐ {html.escape(name)}" for name in mine)
            )
    else:
        # Not an address: find your aliases and the public names that contain the text
        needle = text.lower()
        my_matches = [
            (alias, aliases[alias])
            for alias in sorted(aliases, key=str.lower)
            if needle in alias.lower()
        ]
        matches = [
            (addr, str(label))
            for addr, label in data.items()
            if needle in str(label).lower()
        ]
        if not matches and not my_matches:
            await ask(
                update,
                context,
                f"❌ No address or public name matches <b>{html.escape(text)}</b>, in your aliases or on Hypurrscan.\n"
                "Send an address (0x followed by 40 characters) or part of a name",
            )
            return SEARCH_ALIAS1

        message_lines = []
        if my_matches:
            header = f"✅ <b>{len(my_matches)} of your aliases contain '{html.escape(text)}'</b>"
            if len(my_matches) > NAME_MATCHES_SHOWN:
                header += f" (showing the first {NAME_MATCHES_SHOWN})"
            message_lines.append(header)
            message_lines += [
                f"• ⭐ <b>{html.escape(alias)}</b> — {address_html(addr)}"
                for alias, addr in my_matches[:NAME_MATCHES_SHOWN]
            ]
        if matches:
            shown = matches[:NAME_MATCHES_SHOWN]
            header = (
                f"✅ <b>{len(matches)} public name(s) contain '{html.escape(text)}'</b>"
            )
            if len(matches) > len(shown):
                header += f" (showing the first {len(shown)})"
            message_lines.append(header)
            message_lines += [
                f"• {html.escape(label)} — {address_html(addr)}"
                for addr, label in shown
            ]

    context.user_data.pop("asked_at", None)
    message_lines.append("Would you like to search again?")
    await say_lines(update, context, message_lines, reply_markup=ASK_MARKUP)
    print(f"{user_name(update)} successfully searched public names for {text}")
    return SEARCH_AGAIN


async def search_again(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)

    print(f"{user_name(update)} is searching for another address's alias")
    return await search_alias1(update, context)


# START_TOP_HOLDERS
async def top_holders1(
    update: Update, context: CallbackContext
) -> int:  # Has to return an int
    # Initiates the question and awaits input
    await ask(update, context, "Which token would you like to search (e.g., USDC)?")
    return TOP_HOLDERS1


async def holders_page(context, token, offset):
    """One page of a token's top holders as (text, buttons), or "failed" / "unknown" when there's none."""
    # quote(): the token goes into the web address, so a "/" or space can't change which page is asked for
    url = HOLDERS_URL.format(
        token=quote(token, safe=""), limit=offset + HOLDERS_PER_PAGE
    )
    data = await fetch_json(url)
    if not isinstance(data, dict):
        return "failed"

    holders = {}
    raw_holders = data.get("holders")
    for addr, balance in (raw_holders if isinstance(raw_holders, dict) else {}).items():
        try:
            holders[addr] = float(balance)
        except (TypeError, ValueError):
            continue  # Skip a holder whose balance isn't a number
    if not holders:
        return "unknown"

    # Largest balance first (don't rely on the API's order)
    ranked = sorted(holders.items(), key=lambda item: item[1], reverse=True)
    page = ranked[offset : offset + HOLDERS_PER_PAGE]
    if not page:  # Asked for a page past the end of the list
        return "unknown"
    total = data.get("holdersCount")
    total = total if isinstance(total, int) else len(holders)

    # Names to show next to addresses: your own aliases first, then Hypurrscan's public names
    my_names = {}
    for alias, address in context.user_data.get("aliases", {}).items():
        my_names.setdefault(address, []).append(alias)
    public_names = await get_global_alias_data() or {}

    lines = [
        f"<b>Top {html.escape(token)} Holders:</b> {offset + 1}-{offset + len(page)} of {total:,}"
    ]
    if isinstance(data.get("lastUpdate"), int):
        lines.append(f"<i>Data from {utc(data['lastUpdate'])}</i>")
    for i, (addr, balance) in enumerate(page, offset + 1):
        line = f"{i}. {address_html(addr)} - {balance:,.2f} {html.escape(token)}"
        addr = addr.lower()  # Saved and public names use lowercase addresses
        notes = [
            f"⭐ {html.escape(shorten(name))}" for name in my_names.get(addr, [])[:2]
        ]
        if public_names.get(addr):
            notes.append(f"🏷 {html.escape(shorten(public_names[addr]))}")
        if system_label(addr):
            notes.append(f"⚙️ {system_label(addr)}")
        if notes:
            line += "\n    " + " · ".join(notes)
        lines.append(line)

    markup = None
    if (
        len(f"th:{token}:{offset + HOLDERS_PER_PAGE}".encode()) <= 64
    ):  # Telegram's button data limit
        markup = page_markup(
            f"th:{token}", offset, HOLDERS_PER_PAGE, min(total, HOLDERS_MAX)
        )
    return "\n".join(lines), markup


async def top_holders2(update: Update, context: CallbackContext) -> int:
    # Gets the message from the user
    token = update.message.text.strip().upper()
    print(f"{user_name(update)} is searching for top holders of {token}")

    await show_typing(update, context)
    page = await holders_page(context, token, 0) if token else "unknown"
    if page == "failed":
        await say(update, context, "❌ Failed to fetch top holders.")
        return ConversationHandler.END

    if page == "unknown":
        await ask(
            update,
            context,
            f"❌ No token called <b>{html.escape(token)}</b> found on Hyperliquid.\n"
            "Check the spelling and try again (e.g. USDC, HYPE)",
        )
        print(f"Could not find holder data for {token}")
        return TOP_HOLDERS1  # Retry

    context.user_data.pop("asked_at", None)
    text, markup = page
    await say(update, context, text, reply_markup=markup)
    print(f"{user_name(update)} successfully found holders of {token}")

    await say(
        update,
        context,
        "✅ Search <b>successful</b>\n"
        "Would you like to find the top holders of another token?",
        reply_markup=ASK_MARKUP,
    )

    return SHOW_TOP_HOLDERS_AGAIN


async def show_top_holders_again(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)

    print(f"{user_name(update)} is searching for top holders of another token")
    return await top_holders1(update, context)


async def cancel(update: Update, context: CallbackContext) -> int:
    forget_unfinished(context)
    await say(update, context, "Operation cancelled.")
    print(f"{user_name(update)} cancelled operation")
    return ConversationHandler.END


async def cancel_button(update: Update, context: CallbackContext) -> int:
    # The Cancel button under any question: same as typing /cancel
    query = update.callback_query
    await query.answer()
    await query.message.delete()  # Deletes the message and the buttons
    return await cancel(update, context)


# Side handler functions
async def nothing_to_cancel(update: Update, context: CallbackContext) -> None:
    await say(update, context, "There's nothing to cancel. Send /start to begin.")


async def unknown_command(update: Update, context: CallbackContext) -> None:
    await say(update, context, "I don't know that command. Send /help to see them all.")


async def expired_button(update: Update, context: CallbackContext) -> None:
    # A button from an old message that no longer belongs to a question
    await update.callback_query.answer(
        "This button has expired. Send /start to begin again."
    )


async def console(update: Update, context: CallbackContext) -> None:
    """
    This function would be added to the dispatcher as a handler for messages coming from the Bot API
    """
    # Print to console
    print(f"{user_name(update)} wrote {update.message.text}")
    await say(
        update, context, "Send /start to see what I can do, or /help for the commands."
    )


async def on_error(update: object, context: CallbackContext) -> None:
    # Something went wrong in a handler: print the details for you, and tell the user instead of going silent
    traceback.print_exception(context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await say(
                update,
                context,
                "❌ Something went wrong. Please try again, or send /start.",
            )
        except Exception:  # noqa: BLE001 - the error report itself must never fail loudly
            print("Could not tell the user about the error")


def main() -> None:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise SystemExit("Missing BOT_TOKEN in .env (see .env.example)")

    # Save changed data every 5 seconds (default 60), so a crash or closed window loses at most 5 seconds
    persistence = PicklePersistence(
        filepath=BASE_DIR / "bot_data.pkl", update_interval=5
    )
    app = ApplicationBuilder().token(bot_token).persistence(persistence).build()
    app.post_init = start_up  # Runs once when the bot starts
    app.post_shutdown = close_http_session  # Runs once when the bot stops

    # Runs before everything else (group -1): outside private chats, only point people to a private chat
    app.add_handler(TypeHandler(Update, private_chats_only), group=-1)

    # Register handlers directly to the app (no dispatcher)
    app.add_handler(
        CommandHandler("get_global_aliases", get_global_aliases)
    )  # /get_global_aliases
    app.add_handler(CommandHandler("view_aliases", view_aliases))  # /view_aliases
    app.add_handler(CommandHandler("help", help_command))  # /help
    app.add_handler(CommandHandler("alerts", alerts_command))  # /alerts
    app.add_handler(
        CallbackQueryHandler(page_tap, pattern=r"^(ga|th:.+):\d+$")
    )  # ◀ / ▶ under lists
    app.add_handler(
        CallbackQueryHandler(alerts_tap, pattern=r"^(al:[0-9a-f]+|ai:\d+)$")
    )  # 🔔 / 🔕 and "Every ..." under the alerts menu

    # ConversationHandler consists of
    # entry_points: Starting the function with an input and returns an int
    # states: Depending on the returned int from entry_points, the MessageHandler will do according filter checks and functions
    # fallbacks: A list of handlers for unexpected input or early exits, like /cancel
    # Only new text messages: edited messages have no update.message and would crash the handlers
    text_reply = filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND
    # Each CallbackQueryHandler gets a pattern, so it only reacts to its own buttons
    menu_tap = CallbackQueryHandler(start_button_taps, pattern=exact(*MENU_ACTIONS))
    continue_tap = exact(CONTINUE_BUTTON)

    def typed(callback):
        # A typed answer: answer_in_time() closes the question when the answer comes too late
        return MessageHandler(text_reply, answer_in_time(callback))

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("get_top_holders", top_holders1),  # /get_top_holders
            CommandHandler("search_alias", search_alias1),  # /search_alias
            CommandHandler("register_alias", register_alias1),  # /register_alias
            CommandHandler("remove_alias", remove_alias1),  # /remove_alias
            CommandHandler("rename_alias", rename_alias1),  # /rename_alias
            CommandHandler("export_aliases", export_aliases),  # /export_aliases
            CommandHandler("import_aliases", import_aliases1),  # /import_aliases
            CommandHandler("wallet", wallet1),  # /wallet or /wallet <alias or address>
            CommandHandler("start", start),  # /start
            menu_tap,  # The /start menu buttons, from any step
        ],
        states={
            # MessageHandler reacts to user text input
            # filters.TEXT matches any of the input sent by the user
            # ~filters.COMMAND is a bitwise NOT operator which ignores any commands (anything starting with /)
            # filters.TEXT & ~filters.COMMAND = “Only match plain text messages that are not commands.”
            START_BUTTON_TAPS: [menu_tap],
            TOP_HOLDERS1: [typed(top_holders2)],
            SEARCH_ALIAS1: [typed(search_alias2)],
            REGISTER_ALIAS1: [typed(register_alias2)],
            REGISTER_ALIAS2: [typed(register_alias3)],
            REGISTER_CONFIRM: [
                CallbackQueryHandler(
                    register_confirm, pattern=exact(REPLACE_BUTTON, RENAME_BUTTON)
                )
            ],
            REMOVE_ALIAS1: [
                typed(remove_alias2),
                CallbackQueryHandler(remove_tap, pattern=r"^rm:"),
            ],
            REMOVE_AGAIN: [
                CallbackQueryHandler(remove_again_choice, pattern=continue_tap)
            ],
            REGISTER_AGAIN: [
                CallbackQueryHandler(register_again, pattern=continue_tap)
            ],
            SHOW_TOP_HOLDERS_AGAIN: [
                CallbackQueryHandler(show_top_holders_again, pattern=continue_tap)
            ],
            SEARCH_AGAIN: [CallbackQueryHandler(search_again, pattern=continue_tap)],
            WALLET1: [typed(wallet2), CallbackQueryHandler(wallet_tap, pattern=r"^w:")],
            WALLET_AGAIN: [CallbackQueryHandler(wallet_again, pattern=continue_tap)],
            RENAME1: [
                typed(rename_alias2),
                CallbackQueryHandler(rename_tap, pattern=r"^rn:"),
            ],
            RENAME2: [typed(rename_alias3)],
            IMPORT1: [
                MessageHandler(
                    filters.UpdateType.MESSAGE & filters.Document.ALL,
                    answer_in_time(import_file),
                ),
                typed(import_not_a_file),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_button, pattern=exact(CANCEL_BUTTON)),
        ],
        allow_reentry=True,  # A command typed mid-question restarts instead of being silently ignored
        name="questions",  # Open questions are saved in bot_data.pkl, so a restart doesn't lose them
        persistent=True,
    )

    app.add_handler(conv_handler)

    # Everything the conversation didn't take: replies instead of silence
    app.add_handler(
        CommandHandler("cancel", nothing_to_cancel)
    )  # /cancel with no question open
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(CallbackQueryHandler(expired_button))  # Buttons of old messages
    # This is for all messages, it will print them in the terminal and point the user to /start
    app.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND, console)
    )
    app.add_error_handler(on_error)

    # Start the Bot
    app.run_polling()


if __name__ == "__main__":
    main()
