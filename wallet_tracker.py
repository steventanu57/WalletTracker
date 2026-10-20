import hashlib
import html
import logging
import os
import re
import sys
import time
import traceback
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
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.warnings import PTBUserWarning

# per_message=False is intended: the buttons belong to the user's conversation, not to one message
filterwarnings(
    action="ignore", message=r".*CallbackQueryHandler", category=PTBUserWarning
)

logger = logging.getLogger(__name__)

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

MESSAGE_LIMIT = 4000  # Telegram refuses messages over 4096 characters, so long lists are split below this
ALIAS_CACHE_SECONDS = (
    600  # The public names list (~30 KB) is downloaded at most once every 10 minutes
)
HOLDERS_PER_PAGE = 15
HOLDERS_MAX = (
    1000  # Paging stops here (the largest list size checked against the live API)
)
GLOBAL_ALIASES_PER_PAGE = 30
NAME_MATCHES_SHOWN = 10
NAME_SHOWN_CHARS = 40  # Long names are shortened inside page lists, so a page always fits in one message
REMOVE_BUTTONS_SHOWN = (
    50  # With more aliases than this, the rest can still be removed by typing the name
)
NO_PREVIEW = LinkPreviewOptions(
    is_disabled=True
)  # Don't show a website preview under messages with links

# Conversation states
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
START_BUTTONS = [
    START_REGISTER_ALIAS,
    START_REMOVE_ALIAS,
    START_SEARCH_ALIAS,
    START_GLOBAL_ALIAS,
    START_TOP_HOLDERS,
    START_VIEW_ALIAS,
]


def exact(*texts):
    # A pattern that matches only these button texts, so each CallbackQueryHandler only gets its own buttons
    return "^(" + "|".join(re.escape(text) for text in texts) + ")$"


# Commands shown in Telegram's Menu button (the bot sets them itself on startup) and by /help
COMMANDS = [
    ("start", "Show every feature as a button"),
    ("register_alias", "Save a name for a wallet address"),
    ("remove_alias", "Remove a saved name"),
    ("view_aliases", "List your saved names"),
    ("search_alias", "Find the public name of an address, or addresses by name"),
    ("get_global_aliases", "Browse the public names on Hypurrscan"),
    ("get_top_holders", "Show the top holders of a token, e.g. USDC"),
    ("help", "List the commands"),
    ("cancel", "Leave the current step"),
]

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


# IMPORTANT: Handler functions MUST be starting with async
# context.user_data is a TEMPORARY per-user dictionary, PERMANENT if using picklepersistence
# ConversationHandler is for emabling inputs from users after a follow-up reply.


# ======== Hypurrscan ========
http_session = None  # One connection to Hypurrscan, reused by every request (faster than a new one each time)
global_alias_cache = {"time": 0.0, "data": None}


async def fetch_json(url):
    """Returns the decoded JSON, or None when Hypurrscan can't be reached or answers with an error."""
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT)
    try:
        async with http_session.get(url) as response:
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


async def say(update, context, text, reply_markup=None):
    """Sends an HTML message to the chat the update came from (works for typed messages and button taps)."""
    return await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        link_preview_options=NO_PREVIEW,
    )


async def say_lines(update, context, lines, reply_markup=None):
    # Like say(), but long lists are split over several messages. Buttons go under the last one.
    parts = split_message(lines)
    for part in parts[:-1]:
        await say(update, context, part)
    await say(update, context, parts[-1], reply_markup)


async def show_typing(update, context):
    # Shows "typing..." while the bot waits for Hypurrscan
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )


def user_name(update):
    return update.effective_user.first_name if update.effective_user else "Someone"


def forget_unfinished(context):
    # Temporary values from a question that was left unanswered shouldn't stay in the saved data
    context.user_data.pop("address", None)
    context.user_data.pop("new_alias", None)


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
        f"👋 <b>Hello {html.escape(name)}!</b>\nWelcome to <b>Hyperliquid Wallet Storage</b> Bot!\n \n"
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

    return ConversationHandler.END


async def help_command(update: Update, context: CallbackContext) -> None:
    lines = ["<b>Commands</b>"]
    lines += [
        f"/{command} - {html.escape(description)}" for command, description in COMMANDS
    ]
    await say(update, context, "\n".join(lines))


async def set_bot_commands(app) -> None:
    # Runs once when the bot starts (app.post_init), so the Menu button always lists the commands
    await app.bot.set_my_commands(
        [BotCommand(command, description) for command, description in COMMANDS]
    )


# START_REGISTER_ALIAS
async def register_alias1(update: Update, context: CallbackContext) -> int:
    aliases = context.user_data.get("aliases", {})
    if aliases:
        await say_lines(update, context, alias_lines(aliases))

    await say(
        update,
        context,
        "Please enter a valid address to be added to the list of aliases (e.g 0xffffffffffffffffffffffffffffffffffffffff)",
        reply_markup=CANCEL_MARKUP,
    )

    return REGISTER_ALIAS1


async def register_alias2(update: Update, context: CallbackContext) -> int:
    address = update.message.text.strip().lower()

    print(f"{user_name(update)} is appending {address}")

    if not ADDRESS_RE.fullmatch(address):
        await say(
            update,
            context,
            "❌ That is not a valid address. It should be 0x followed by 40 characters (0-9, a-f).\nPlease try again",
            reply_markup=CANCEL_MARKUP,
        )
        return REGISTER_ALIAS1

    context.user_data["address"] = (
        address  # A temporary dictionary to be able to access this address in register_alias3
    )
    print(f"{user_name(update)} successfully appended {address}")

    await say(
        update,
        context,
        "✅ <b>Great</b>!\nWhat would you like to name this address?",
        reply_markup=CANCEL_MARKUP,
    )
    return REGISTER_ALIAS2


async def register_alias3(update: Update, context: CallbackContext) -> int:
    alias = update.message.text.strip()

    if not alias:
        await say(
            update,
            context,
            "❌ Try again. Please type a proper alias",
            reply_markup=CANCEL_MARKUP,
        )
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
    await say(
        update,
        context,
        "What would you like to name this address?",
        reply_markup=CANCEL_MARKUP,
    )
    return REGISTER_ALIAS2


async def save_alias(update, context, alias) -> int:
    address = context.user_data.pop(
        "address"
    )  # pop so the temporary address isn't saved forever

    aliases = context.user_data.setdefault("aliases", {})
    existing = find_alias(aliases, alias)
    if existing:
        del aliases[
            existing
        ]  # Same name with other capitals: keep only the new spelling
    aliases[alias] = address

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

    await say(
        update,
        context,
        "Please enter a valid address to be added to the list of aliases (e.g 0xffffffffffffffffffffffffffffffffffffffff)",
        reply_markup=CANCEL_MARKUP,
    )
    print(f"{user_name(update)} is trying to register another address")
    return REGISTER_ALIAS1


# START_REMOVE_ALIAS
def remove_button_data(alias):
    # Telegram limits button data to 64 bytes, so a long alias is identified by a short fingerprint
    return "rm:" + hashlib.sha256(alias.encode()).hexdigest()[:16]


def remove_markup(aliases):
    names = sorted(aliases, key=str.lower)[:REMOVE_BUTTONS_SHOWN]
    rows = [
        [InlineKeyboardButton(f"🗑 {alias}", callback_data=remove_button_data(alias))]
        for alias in names
    ]
    rows.append([InlineKeyboardButton(CANCEL_BUTTON, callback_data=CANCEL_BUTTON)])
    return InlineKeyboardMarkup(rows)


async def remove_alias1(update: Update, context: CallbackContext) -> int:
    aliases = context.user_data.get("aliases")
    if not aliases:
        await say(update, context, "❌ Please register an alias first")
        print(f"{user_name(update)} failed to remove aliases")
        return ConversationHandler.END

    await say_lines(update, context, alias_lines(aliases))
    await say(
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
        await say(
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
    alias = next((a for a in aliases if remove_button_data(a) == query.data), None)
    if alias is None:
        await query.answer("That alias was already removed")
        return REMOVE_ALIAS1

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    return await remove_and_ask_again(update, context, alias)


async def remove_and_ask_again(update, context, alias) -> int:
    aliases = context.user_data["aliases"]
    del aliases[alias]  # Removes the key and the value in the dict
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

    await say_lines(update, context, alias_lines(aliases))
    print(f"{user_name(update)} is viewing registered aliases")


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
    await say(
        update,
        context,
        "Send an address to find its public name (e.g 0xffffffffffffffffffffffffffffffffffffffff),\n"
        "or part of a name to find its addresses (e.g. HyperEVM)",
        reply_markup=CANCEL_MARKUP,
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

    if ADDRESS_RE.fullmatch(text.lower()):
        # An address: look up its public name
        address = text.lower()
        alias = data.get(address)
        if not alias:
            await say(
                update,
                context,
                "❌ No public name found for this address.\nSend another address or part of a name",
                reply_markup=CANCEL_MARKUP,
            )
            print(f"Could not find alias for {address}")
            return SEARCH_ALIAS1

        message_lines = [
            "✅ <b>Search successful!</b>",
            f"<b>Address</b>: {address_html(address)}",
            f"<b>Alias</b>: {html.escape(str(alias))}",
        ]
    else:
        # Not an address: find the public names that contain the text
        matches = [
            (addr, str(label))
            for addr, label in data.items()
            if text.lower() in str(label).lower()
        ]
        if not matches:
            await say(
                update,
                context,
                f"❌ No address or public name matches <b>{html.escape(text)}</b>.\n"
                "Send an address (0x followed by 40 characters) or part of a name",
                reply_markup=CANCEL_MARKUP,
            )
            return SEARCH_ALIAS1

        shown = matches[:NAME_MATCHES_SHOWN]
        header = (
            f"✅ <b>{len(matches)} public name(s) contain '{html.escape(text)}'</b>"
        )
        if len(matches) > len(shown):
            header += f" (showing the first {len(shown)})"
        message_lines = [header]
        message_lines += [
            f"• {html.escape(label)} — {address_html(addr)}" for addr, label in shown
        ]

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
    await say(
        update,
        context,
        "Which token would you like to search (e.g., USDC)?",
        reply_markup=CANCEL_MARKUP,
    )
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
        updated = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(data["lastUpdate"]))
        lines.append(f"<i>Data from {updated}</i>")
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
        await say(
            update,
            context,
            f"❌ No token called <b>{html.escape(token)}</b> found on Hyperliquid.\n"
            "Check the spelling and try again (e.g. USDC, HYPE)",
            reply_markup=CANCEL_MARKUP,
        )
        print(f"Could not find holder data for {token}")
        return TOP_HOLDERS1  # Retry

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
    app.post_init = set_bot_commands  # Runs once when the bot starts
    app.post_shutdown = close_http_session  # Runs once when the bot stops

    # Register handlers directly to the app (no dispatcher)
    app.add_handler(
        CommandHandler("get_global_aliases", get_global_aliases)
    )  # /get_global_aliases
    app.add_handler(CommandHandler("view_aliases", view_aliases))  # /view_aliases
    app.add_handler(CommandHandler("help", help_command))  # /help
    app.add_handler(
        CallbackQueryHandler(page_tap, pattern=r"^(ga|th:.+):\d+$")
    )  # ◀ / ▶ under lists

    # ConversationHandler consists of
    # entry_points: Starting the function with an input and returns an int
    # states: Depending on the returned int from entry_points, the MessageHandler will do according filter checks and functions
    # fallbacks: A list of handlers for unexpected input or early exits, like /cancel
    # Only new text messages: edited messages have no update.message and would crash the handlers
    text_reply = filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND
    # Each CallbackQueryHandler gets a pattern, so it only reacts to its own buttons
    menu_tap = CallbackQueryHandler(start_button_taps, pattern=exact(*START_BUTTONS))
    continue_tap = exact(CONTINUE_BUTTON)

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("get_top_holders", top_holders1),  # /get_top_holders
            CommandHandler("search_alias", search_alias1),  # /search_alias
            CommandHandler("register_alias", register_alias1),  # /register_alias
            CommandHandler("remove_alias", remove_alias1),  # /remove_alias
            CommandHandler("start", start),  # /start
            menu_tap,  # The /start menu buttons, from any step
        ],
        states={
            # MessageHandler reacts to user text input
            # filters.TEXT matches any of the input sent by the user
            # ~filters.COMMAND is a bitwise NOT operator which ignores any commands (anything starting with /)
            # filters.TEXT & ~filters.COMMAND = “Only match plain text messages that are not commands.”
            START_BUTTON_TAPS: [menu_tap],
            TOP_HOLDERS1: [MessageHandler(text_reply, top_holders2)],
            SEARCH_ALIAS1: [MessageHandler(text_reply, search_alias2)],
            REGISTER_ALIAS1: [MessageHandler(text_reply, register_alias2)],
            REGISTER_ALIAS2: [MessageHandler(text_reply, register_alias3)],
            REGISTER_CONFIRM: [
                CallbackQueryHandler(
                    register_confirm, pattern=exact(REPLACE_BUTTON, RENAME_BUTTON)
                )
            ],
            REMOVE_ALIAS1: [
                MessageHandler(text_reply, remove_alias2),
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
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_button, pattern=exact(CANCEL_BUTTON)),
        ],
        allow_reentry=True,  # A command typed mid-question restarts instead of being silently ignored
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
