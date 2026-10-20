# Architecture

## Tech stack

- **Python 3.12 or 3.13**
- **python-telegram-bot 22**: Telegram Bot API, long polling, conversation flows
- **aiohttp**: HTTP requests to Hypurrscan
- **python-dotenv**: loads `BOT_TOKEN` from `.env`
- **Hypurrscan public API**: Hyperliquid wallet and token data

## How it works

```
 Telegram user ◀──▶ Telegram Bot API ◀──polling──▶ wallet_tracker.py ──HTTP──▶ api.hypurrscan.io
                                                          │
                                                   bot_data.pkl (saved aliases)
```

- The bot polls Telegram for new messages (`run_polling`), so it needs no web server or open port.
- It is meant for private (1:1) chats. Questions that follow a button tap are sent to the user's private chat.
- Everything happens on demand. Despite the name, the bot does not watch wallets or send alerts yet.

### Handlers

`main()` registers the handlers in this order. The first one that matches an update handles it:

1. One-off commands and buttons: `/get_global_aliases`, `/view_aliases`, `/help`, and the ◀ / ▶ page buttons under lists. They reply and leave any open question as it was.
2. One `ConversationHandler` for everything that asks a question. It starts with `/start`, `/register_alias`, `/remove_alias`, `/search_alias`, `/get_top_holders` or a `/start` menu button, and `/cancel` or a **Cancel** button ends it. Typing one of these commands, or tapping a menu button, mid-question starts that over (`allow_reentry=True`).
3. Replies for everything else, instead of silence:
   - `/cancel` with no open question: "There's nothing to cancel".
   - Unknown commands: a pointer to `/help`.
   - A button from an old message: "This button has expired" (as a small pop-up).
   - Any other message: printed to the terminal as `<name> wrote <text>`, and the user is pointed to `/start`.

If a handler crashes, `on_error` prints the error to the terminal and tells the user "❌ Something went wrong".

On startup (`post_init`) the bot sets its own command list, which Telegram shows under the **Menu** button. On shutdown (`post_shutdown`) it closes its Hypurrscan connection.

Answers are only accepted from new text messages. Edited messages, photos and stickers are not taken as answers.

### Conversation steps

| Command | Steps (state constants in `wallet_tracker.py`) |
|---|---|
| `/start` | `START_BUTTON_TAPS`: waits for a menu button |
| `/register_alias` | `REGISTER_ALIAS1` address → `REGISTER_ALIAS2` name → (`REGISTER_CONFIRM` Replace / Choose another name, only when the name already points to another address) → `REGISTER_AGAIN` Continue/Cancel |
| `/remove_alias` | `REMOVE_ALIAS1` tap or type an alias → `REMOVE_AGAIN` Continue/Cancel |
| `/search_alias` | `SEARCH_ALIAS1` address or part of a name → `SEARCH_AGAIN` Continue/Cancel |
| `/get_top_holders` | `TOP_HOLDERS1` token → `SHOW_TOP_HOLDERS_AGAIN` Continue/Cancel |

- Every question has a **Cancel** button. **Continue** asks the question again. **Cancel** deletes the button message, says "Operation cancelled." and forgets any half-finished answer.
- A wrong answer (bad address, unknown alias, nothing found, unknown token) asks the same question again.
- The `/start` menu buttons work at any time, also on an older menu message.
- Each `CallbackQueryHandler` has a `pattern`, so it only reacts to its own buttons. Menu and Continue/Cancel buttons use the button's own text as `callback_data`. Remove buttons use `rm:` + a short fingerprint of the alias, because Telegram allows at most 64 bytes. Page buttons use `ga:<offset>` (public names) and `th:<TOKEN>:<offset>` (top holders).

### Data from Hypurrscan

| Feature | Request | Response used |
|---|---|---|
| Search and list public names | `GET api.hypurrscan.io/globalAliases` | `{address: name}`, about 30 KB and 463 entries (2026-10-20). Downloaded at most once every 10 minutes and kept in memory |
| Top holders of a token | `GET api.hypurrscan.io/holdersWithLimit/{TOKEN}/{offset + 15}` | `{"holders": {address: balance}, "holdersCount", "lastUpdate", ...}`. The bot sorts by balance and shows 15 per page (up to 1,000), with the total and the data's time. An unknown token returns `{}` |
| Register / remove / view aliases | none | The user's own saved data |
| Address check when registering | none | Format check only (`0x` + 40 hex characters, any case) |

- One shared `aiohttp` session is reused for every request (about 0.2 s per request instead of 0.6 s with a new connection each time, measured 2026-10-20).
- The token is trimmed, uppercased and URL-encoded, so spaces or a `/` can't change which address is requested.
- A response that isn't the expected JSON shape counts as a failure. Requests time out after 15 seconds. If one fails, the user gets a ❌ message instead of silence.
- Top-holder lines are marked with the user's own aliases (⭐), Hypurrscan's public name (🏷) and Hyperliquid system addresses (⚙️). A system address is `0x20` + zeros + the token's index (USDC is `0x2000…0000`), or `0x2222…2222` for HYPE. These are used for HyperCore ↔ HyperEVM transfers, so they aren't ordinary holders.
- Telegram refuses messages over 4,096 characters. Alias lists are split over several messages. In page lists, names longer than 40 characters are shortened so a page always fits in one message.

## Saved data

Each user's data is in `context.user_data`:

- `aliases`: `{alias: address}`. Aliases keep the capitals they were typed with, and are found ignoring case. Addresses are lowercase. One address can have several aliases. Saving a name that already points to another address asks before replacing it. (Aliases saved before 2026-10-20 are all lowercase.)
- `address` and `new_alias`: temporary. They hold answers between registration questions, and are removed once the alias is saved or the question is cancelled.

**Persistence:** python-telegram-bot's `PicklePersistence` keeps this data in memory and writes it to `bot_data.pkl` every 5 seconds (only when something changed) and on a clean shutdown. Open conversations are not saved, so a restart cancels any question in progress. Run only one copy of the bot at a time: Telegram refuses a second poller with the same token, and both would write the same file.

## Files

| File | Role |
|---|---|
| `wallet_tracker.py` | The bot: handlers, conversation states, Hypurrscan calls |
| `tests/harness.py` | Fake Telegram server and fake Hypurrscan that run the real bot |
| `tests/test_wallet_tracker.py` | End-to-end tests |
| `.env` (git-ignored) | `BOT_TOKEN` from @BotFather |
| `bot_data.pkl` (git-ignored) | Saved aliases per user, created on first save |

## Setup and run

```
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env           # macOS/Linux: cp .env.example .env
```

1. Put your bot token from [@BotFather](https://t.me/BotFather) in `.env`.
2. Run `python wallet_tracker.py` and keep it running.
3. Stop it with Ctrl+C, which saves data. Killing the process (closing the window, a crash) can lose the last 5 seconds of changes.

Output goes to the terminal through `print`. Logging is not configured, so only warnings and errors from the libraries appear.

## Tests

```
pip install -r requirements-dev.txt
pytest
ruff check .
```

The tests need no bot token and no network. `tests/harness.py` runs the bot's real `main()` with:

- `FakeTelegram`, which answers Bot API calls locally and records every call. Like Telegram, it rejects bad HTML and messages over 4,096 visible characters
- `FakeHypurrscan`, which replaces `aiohttp.ClientSession` and returns canned responses or errors per URL. It records every URL asked for (`requested`) and every session opened (`opened`)
- a temporary `bot_data.pkl`, so real data is never touched

`BotRun(...).send(...)` plays a chat: text, commands, `("tap", button_data)` or `("edit", text)`. `BotRun(...).call(func)` runs `func(app)` with the app started, for example `post_init`. Test data uses a `zz-` prefix. Set `WT_DIR` to test a copy of `wallet_tracker.py` in another folder.

Tests are grouped as "features that must keep working", "bug fixes", and phase 1 (each marked with its finding ID, W1 to W32).
