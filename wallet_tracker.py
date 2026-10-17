import html
import logging
import os
import re
import sys
from pathlib import Path
from warnings import filterwarnings

import aiohttp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

# Continue and Cancel Texts
CONTINUE_BUTTON = "Continue"
CANCEL_BUTTON = "Cancel"

# All-in-One Start Buttons' Texts
START_REGISTER_ALIAS = "Register an alias"
START_REMOVE_ALIAS = "Remove a registered alias"
START_SEARCH_ALIAS = "Search amongst global aliases"
START_GLOBAL_ALIAS = "List of global aliases"
START_TOP_HOLDERS = "Find token top holders"
START_VIEW_ALIAS = "View registered aliases"

# Markups for buttons
ASK_MARKUP = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton(CONTINUE_BUTTON, callback_data=CONTINUE_BUTTON)],
        [InlineKeyboardButton(CANCEL_BUTTON, callback_data=CANCEL_BUTTON)],
    ]
)

AIO_MARKUP = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton(
                START_REGISTER_ALIAS, callback_data=START_REGISTER_ALIAS
            )
        ],
        [InlineKeyboardButton(START_REMOVE_ALIAS, callback_data=START_REMOVE_ALIAS)],
        [InlineKeyboardButton(START_SEARCH_ALIAS, callback_data=START_SEARCH_ALIAS)],
        [InlineKeyboardButton(START_GLOBAL_ALIAS, callback_data=START_GLOBAL_ALIAS)],
        [InlineKeyboardButton(START_TOP_HOLDERS, callback_data=START_TOP_HOLDERS)],
        [InlineKeyboardButton(START_VIEW_ALIAS, callback_data=START_VIEW_ALIAS)],
    ]
)


# FOR TUTORIALS
# Store bot screaming status
screaming = False

# IMPORTANT: Handler functions MUST be starting with async
# context.user_data is a TEMPORARY per-user dictionary, PERMANENT if using picklepersistence
# ConversationHandler is for emabling inputs from users after a follow-up reply.


async def fetch_json(url):
    """Returns (status, data). status is None when Hypurrscan can't be reached."""
    try:
        async with (
            aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session,
            session.get(url) as response,
        ):
            if response.status != 200:
                return response.status, None
            return response.status, await response.json()
    except (aiohttp.ClientError, TimeoutError):
        return None, None


# Main handler functions
# Make the /start display all the buttons in a compact table
async def start(update: Update, context: CallbackContext) -> int:
    user = update.effective_user
    name = (
        user.first_name or "there"
    )  # "there" is a fallback incase the name is not returned

    await update.message.reply_text(
        f"👋 <b>Hello {name}!</b>\nWelcome to <b>Hyperliquid Wallet Storage</b> Bot!\n \n"
        "Click the buttons below to access commands\n"
        "OR\n"
        "Click the <b>Menu</b> button beside the chat bar",
        reply_markup=AIO_MARKUP,
        parse_mode="HTML",
    )

    return START_BUTTON_TAPS


async def start_button_taps(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data

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
        return await view_aliases(update, context)

    return ConversationHandler.END


# START_REGISTER_ALIAS
async def register_alias1(update: Update, context: CallbackContext) -> int:
    # USE THIS VARIABLE FOR THE START OF EVERY FUNCTION
    message = update.message or update.callback_query.message

    aliases = context.user_data.get("aliases", {})
    if aliases:
        message_lines = ["<b>Registered aliases: </b>"]
        for alias, address in aliases.items():
            message_lines.append(f"{html.escape(address)} -> {html.escape(alias)}")
        await message.reply_text("\n".join(message_lines), parse_mode="HTML")

    await message.reply_text(
        "Please enter a valid address to be added to the list of aliases (e.g 0xffffffffffffffffffffffffffffffffffffffff)"
    )

    return REGISTER_ALIAS1


async def register_alias2(update: Update, context: CallbackContext) -> int:
    address = update.message.text.strip().lower()

    print(f"{update.message.from_user.first_name} is appending {address}")

    if not ADDRESS_RE.fullmatch(address):
        await update.message.reply_text(
            "❌ That is not a valid address. It should be 0x followed by 40 characters (0-9, a-f).\nPlease try again"
        )
        return REGISTER_ALIAS1

    context.user_data["address"] = (
        address  # A temporary dictionary to be able to access this address in register_alias3
    )
    print(f"{update.message.from_user.first_name} successfully appended {address}")

    await update.message.reply_text(
        "✅ <b>Great</b>!\nWhat would you like to name this address?", parse_mode="HTML"
    )
    return REGISTER_ALIAS2


async def register_alias3(update: Update, context: CallbackContext) -> int:
    alias = update.message.text.strip().lower()

    if not alias:
        await update.message.reply_text("❌ Try again. Please type a proper alias")
        print(f"{update.message.from_user.first_name} did not type a proper alias")
        return REGISTER_ALIAS2

    address = context.user_data.pop(
        "address"
    )  # pop so the temporary address isn't saved forever

    if "aliases" not in context.user_data:
        context.user_data["aliases"] = {}
    context.user_data["aliases"][alias] = address

    await update.message.reply_text(f"✅ Alias '{alias}' saved for address {address}")
    print(
        f"{update.message.from_user.first_name} registered alias {alias} for address {address}"
    )

    aliases = context.user_data.get("aliases", {})
    message_lines = ["<b>Registered aliases: </b>"]
    for alias, address in aliases.items():
        message_lines.append(f"{html.escape(address)} -> {html.escape(alias)}")
    await update.message.reply_text("\n".join(message_lines), parse_mode="HTML")

    await update.message.reply_text(
        "Would you like to register another address and alias?", reply_markup=ASK_MARKUP
    )

    return REGISTER_AGAIN


async def register_again(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    choice = query.data
    await query.answer()

    if choice == CONTINUE_BUTTON:
        # Show the current alias list again
        aliases = context.user_data.get("aliases", {})
        message_lines = ["<b>Registered aliases:</b>"]
        for alias, address in aliases.items():
            message_lines.append(f"{html.escape(address)} -> {html.escape(alias)}")

        await query.edit_message_text("\n".join(message_lines), parse_mode="HTML")
        await context.bot.send_message(  # Can't use update.message.reply_text since it is from a query
            chat_id=query.from_user.id,
            text="Please enter a valid address to be added to the list of aliases (e.g 0xffffffffffffffffffffffffffffffffffffffff)",
        )
        print(f"{query.from_user.first_name} is trying to register another address")
        return REGISTER_ALIAS1

    elif choice == CANCEL_BUTTON:
        await update.callback_query.message.delete()
        return ConversationHandler.END


# START_REMOVE_ALIAS
async def remove_alias1(update: Update, context: CallbackContext) -> int:
    message = update.message or update.callback_query.message

    method = None

    if message == update.message:
        method = update.message
    elif message == update.callback_query.message:
        method = update.callback_query

    if not context.user_data.get("aliases"):
        await message.reply_text("❌ Please register an alias first")
        print(f"{message.from_user.first_name} failed to remove aliases")
        return ConversationHandler.END

    message_lines = ["<b>Registered aliases:</b>"]
    aliases = context.user_data.get("aliases", {})
    for alias, address in aliases.items():
        message_lines.append(f"{html.escape(address)} -> {html.escape(alias)}")
    await message.reply_text("\n".join(message_lines), parse_mode="HTML")

    await message.reply_text(
        "Which alias would you like to remove?\nPlease type the alias"
    )
    print(f"{method.from_user.first_name} is trying to remove an alias")

    return REMOVE_ALIAS1


async def remove_alias2(update: Update, context: CallbackContext) -> int:
    aliases = context.user_data.get("aliases", {})
    alias = update.message.text.strip().lower()

    if alias not in aliases:
        await update.message.reply_text(
            "❌ The alias is not found in the registered aliases.\nPlease input another valid alias"
        )
        print(f"{update.message.from_user.first_name} failed to remove alias {alias}")
        return REMOVE_ALIAS1

    del aliases[alias]  # Removes the key and the value in the dict
    print(
        f"{update.message.from_user.first_name} has succesfully removed {alias} from aliases"
    )

    message_lines = ["<b>Registered aliases: </b>"]
    aliases = context.user_data.get("aliases", {})
    for alias, address in aliases.items():
        message_lines.append(f"{html.escape(address)} -> {html.escape(alias)}")
    await update.message.reply_text("\n".join(message_lines), parse_mode="HTML")

    alias = update.message.text.strip().lower()
    await update.message.reply_text(
        f"✅ Alias '{alias}' has been removed.\nWould you like to remove another alias?",
        reply_markup=ASK_MARKUP,  # This will just display "Continue" and "Cancel" buttons beneath the text above
    )

    return REMOVE_AGAIN


async def remove_again_choice(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data

    if choice == CONTINUE_BUTTON:
        # Show the current alias list again
        aliases = context.user_data.get("aliases", {})
        if not aliases:
            await query.edit_message_text(
                "❌ No more aliases left to remove."
            )  # query.edit_message_text EDITS text, instead of sending a new text
            print(f"{query.from_user.first_name} has no more aliases left to remove")
            return ConversationHandler.END

        message_lines = ["<b>Registered aliases: </b>"]
        for alias, address in aliases.items():
            message_lines.append(f"{html.escape(address)} -> {html.escape(alias)}")

        await query.edit_message_text("\n".join(message_lines), parse_mode="HTML")
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="Which alias would you like to remove? Type the alias",
        )
        print(f"{query.from_user.first_name} is trying to remove another alias")
        return REMOVE_ALIAS1

    elif choice == CANCEL_BUTTON:
        await (
            update.callback_query.message.delete()
        )  # Deletes the message and the buttons
        return ConversationHandler.END


# START_VIEW_ALIAS
async def view_aliases(update: Update, context: CallbackContext):
    message = update.message or update.callback_query.message

    method = None

    if message == update.message:
        method = update.message
    elif message == update.callback_query.message:
        method = update.callback_query

    if not context.user_data.get("aliases"):
        await message.reply_text("❌ Please register an alias first")
        print(f"{message.from_user.first_name} failed to view registered aliases")
        return ConversationHandler.END

    message_lines = ["<b>Registered aliases: </b>"]
    aliases = context.user_data.get("aliases", {})
    for alias, address in aliases.items():
        message_lines.append(f"{html.escape(address)} -> {html.escape(alias)}")

    await message.reply_text("\n".join(message_lines), parse_mode="HTML")
    print(f"{method.from_user.first_name} is viewing registered aliases")


# START_GLOBAL_ALIAS
async def get_global_aliases(update: Update, context: CallbackContext) -> None:
    message = update.message or update.callback_query.message

    url = "https://api.hypurrscan.io/globalAliases"

    status, data = await fetch_json(url)
    if status != 200:
        await message.reply_text("❌ Failed to fetch global aliases.")
        return

    if not data:
        await message.reply_text("❌ No aliases found.")
        return

    # Format the message
    message_lines = ["<b>Global Wallet Aliases:</b>"]
    for addr, label in list(data.items())[:30]:  # show only top 30 for readability
        message_lines.append(f"<code>{html.escape(addr)}</code>: {html.escape(label)}")

    await message.reply_text("\n".join(message_lines), parse_mode="HTML")
    return ConversationHandler.END


# START_SEARCH_ALIAS
async def search_alias1(update: Update, context: CallbackContext) -> int:
    message = update.message or update.callback_query.message

    await message.reply_text(
        "Which address's alias would you like to search for? (e.g 0xffffffffffffffffffffffffffffffffffffffff)"
    )
    return SEARCH_ALIAS1


async def search_alias2(update: Update, context: CallbackContext) -> int:
    # Gets the message from the user
    address = update.message.text.strip().lower()

    url = "https://api.hypurrscan.io/globalAliases"
    print(f"{update.message.from_user.first_name} is searching for alias of {address}")

    status, data = await fetch_json(url)
    if status != 200:
        await update.message.reply_text(
            "❌ Failed to fetch global alias for this address"
        )
        return ConversationHandler.END

    alias = data.get(address)

    if not alias:
        await update.message.reply_text(
            "❌ Alias not found for this address. Please try again!"
        )
        print(f"Could not find alias for {address}")
        return SEARCH_ALIAS1

    message_lines = [
        f"✅ <b>Search successful!</b>\n<b>Address</b>: {html.escape(address)}"
    ]
    message_lines.append(f"<b>Alias</b>: {html.escape(alias)}")
    message_lines.append("Would you like to search for another address's alias?")

    await update.message.reply_text(
        "\n".join(message_lines), parse_mode="HTML", reply_markup=ASK_MARKUP
    )
    print(
        f"{update.message.from_user.first_name} successfully found alias for {address}"
    )
    return SEARCH_AGAIN


async def search_again(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data

    if choice == CONTINUE_BUTTON:
        await context.bot.send_message(  # Can't use update.message.reply_text since it is from a query
            chat_id=query.from_user.id,  # To tell the bot which chat id to send the message to
            text="Which address's alias would you like to search for? (e.g 0xffffffffffffffffffffffffffffffffffffffff)?",
        )
        print(
            f"{query.from_user.first_name} is searching for top holders of another token"
        )
        return SEARCH_ALIAS1

    elif choice == CANCEL_BUTTON:
        await (
            update.callback_query.message.delete()
        )  # Deletes the message and the buttons
        return ConversationHandler.END


# START_TOP_HOLDERS
async def top_holders1(
    update: Update, context: CallbackContext
) -> int:  # Has to return an int
    message = update.message or update.callback_query.message

    # Initiates the question and awaits input
    await message.reply_text("Which token would you like to search (e.g., USDC)?")
    return TOP_HOLDERS1


async def top_holders2(update: Update, context: CallbackContext) -> int:
    # Gets the message from the user
    token = update.message.text.upper()

    # Search the URL
    url = f"https://api.hypurrscan.io/holdersWithLimit/{token}/1000"
    print(
        f"{update.message.from_user.first_name} is searching for top holders of {token}"
    )

    status, data = await fetch_json(url)
    if status != 200:
        await update.message.reply_text("❌ Failed to fetch top holders.")
        return ConversationHandler.END

    addresses = data.get("holders", {})
    if not addresses:
        await update.message.reply_text("❌ No holder data available. Please try again")
        print(f"Could not find holder data for {token}")
        token = update.message.text.upper()
        return TOP_HOLDERS1  # Retry

    # Format the top 15 holders
    message_lines = [f"<b>Top {html.escape(token)} Holders:</b>"]
    for i, (addr, balance) in enumerate(list(addresses.items())[:15], 1):
        message_lines.append(
            f"{i}. <code>{html.escape(addr)}</code> - {balance:,.2f} {html.escape(token)}"
        )

    # When done with everything,
    await update.message.reply_text("\n".join(message_lines), parse_mode="HTML")
    print(
        f"{update.message.from_user.first_name} successfully found holders of {token}"
    )

    await update.message.reply_text(
        "✅ Search <b>successful</b>\n"
        "Would you like to find the top holders of another token?",
        parse_mode="HTML",
        reply_markup=ASK_MARKUP,
    )

    return SHOW_TOP_HOLDERS_AGAIN


async def show_top_holders_again(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    choice = query.data
    await query.answer()

    if choice == CONTINUE_BUTTON:
        await context.bot.send_message(  # Can't use update.message.reply_text since it is from a query
            chat_id=query.from_user.id,
            text="Which token would you like to search (e.g., USDC)?",
        )
        print(
            f"{query.from_user.first_name} is searching for top holders of another token"
        )
        return TOP_HOLDERS1

    elif choice == CANCEL_BUTTON:
        await update.callback_query.message.delete()
        return ConversationHandler.END


async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Operation cancelled.")
    print(f"{update.message.from_user.first_name} cancelled operation")
    return ConversationHandler.END


# Side handler functions
async def console(update: Update, context: CallbackContext) -> None:
    """
    This function would be added to the dispatcher as a handler for messages coming from the Bot API
    """
    # Print to console
    print(f"{update.message.from_user.first_name} wrote {update.message.text}")


def main() -> None:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise SystemExit("Missing BOT_TOKEN in .env (see .env.example)")

    # Save changed data every 5 seconds (default 60), so a crash or closed window loses at most 5 seconds
    persistence = PicklePersistence(
        filepath=BASE_DIR / "bot_data.pkl", update_interval=5
    )
    app = ApplicationBuilder().token(bot_token).persistence(persistence).build()

    # Register handlers directly to the app (no dispatcher)
    app.add_handler(
        CommandHandler("get_global_aliases", get_global_aliases)
    )  # /get_global_aliases
    app.add_handler(CommandHandler("view_aliases", view_aliases))  # /view_aliases

    # ConversationHandler consists of
    # entry_points: Starting the function with an input and returns an int
    # states: Depending on the returned int from entry_points, the MessageHandler will do according filter checks and functions
    # fallbacks: A list of handlers for unexpected input or early exits, like /cancel
    # Only new text messages: edited messages have no update.message and would crash the handlers
    text_reply = filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("get_top_holders", top_holders1),  # /get_top_holders
            CommandHandler("search_alias", search_alias1),  # /search_alias
            CommandHandler("register_alias", register_alias1),  # /register_alias
            CommandHandler("remove_alias", remove_alias1),  # /remove_alias
            CommandHandler("start", start),  # /start
        ],
        states={
            # MessageHandler reacts to user text input
            # filters.TEXT matches any of the input sent by the user
            # ~filters.COMMAND is a bitwise NOT operator which ignores any commands (anything starting with /)
            # filters.TEXT & ~filters.COMMAND = “Only match plain text messages that are not commands.”
            START_BUTTON_TAPS: [CallbackQueryHandler(start_button_taps)],
            TOP_HOLDERS1: [MessageHandler(text_reply, top_holders2)],
            SEARCH_ALIAS1: [MessageHandler(text_reply, search_alias2)],
            REGISTER_ALIAS1: [MessageHandler(text_reply, register_alias2)],
            REGISTER_ALIAS2: [MessageHandler(text_reply, register_alias3)],
            REMOVE_ALIAS1: [MessageHandler(text_reply, remove_alias2)],
            REMOVE_AGAIN: [CallbackQueryHandler(remove_again_choice)],
            REGISTER_AGAIN: [CallbackQueryHandler(register_again)],
            SHOW_TOP_HOLDERS_AGAIN: [CallbackQueryHandler(show_top_holders_again)],
            SEARCH_AGAIN: [CallbackQueryHandler(search_again)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,  # A command typed mid-question restarts instead of being silently ignored
    )

    app.add_handler(conv_handler)

    # This is for all messages, it will just echo it back to you through the terminal
    app.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND, console)
    )

    # Start the Bot
    app.run_polling()


if __name__ == "__main__":
    main()
