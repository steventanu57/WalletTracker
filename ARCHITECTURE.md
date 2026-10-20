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
- Everything happens on demand. Despite the name, the bot does not watch wallets or send alerts.

### Handlers

`main()` registers the handlers in this order. The first one that matches an update handles it:

1. `/get_global_aliases` and `/view_aliases`: one-off commands. They reply and leave any open question as it was.
2. One `ConversationHandler` for everything that asks a question. It starts with `/start`, `/register_alias`, `/remove_alias`, `/search_alias` or `/get_top_holders`, and `/cancel` ends it. Typing one of these commands mid-question starts that command over (`allow_reentry=True`).
3. A catch-all that prints any other non-command message to the terminal as `<name> wrote <text>`.

Answers are only accepted from new text messages. Edited messages, photos and stickers are ignored. Unknown commands, and `/cancel` with no open question, get no reply.

### Conversation steps

| Command | Steps (state constants in `wallet_tracker.py`) |
|---|---|
| `/start` | `START_BUTTON_TAPS`: waits for a menu button |
| `/register_alias` | `REGISTER_ALIAS1` address → `REGISTER_ALIAS2` name → `REGISTER_AGAIN` Continue/Cancel |
| `/remove_alias` | `REMOVE_ALIAS1` alias → `REMOVE_AGAIN` Continue/Cancel |
| `/search_alias` | `SEARCH_ALIAS1` address → `SEARCH_AGAIN` Continue/Cancel |
| `/get_top_holders` | `TOP_HOLDERS1` token → `SHOW_TOP_HOLDERS_AGAIN` Continue/Cancel |

- **Continue** asks the question again. **Cancel** deletes the button message and ends the conversation.
- A wrong answer (bad address, unknown alias, address with no public name, token with no holders) asks the same question again.
- The `/start` menu buttons only work while the menu is the current step. Once a button starts another flow, or after **List of global aliases**, taps on the old menu are ignored, so send `/start` again. **View registered aliases** keeps the menu active.
- Button `callback_data` is the button's own text.

### Data from Hypurrscan

| Feature | Request | Response used |
|---|---|---|
| Search and list public aliases | `GET api.hypurrscan.io/globalAliases` | `{address: name}`, about 30 KB, fetched again on every request |
| Top holders of a token | `GET api.hypurrscan.io/holdersWithLimit/{TOKEN}/1000` | `{"holders": {address: balance}}`, sorted by balance and the top 15 shown. An unknown token returns `{}` |
| Register / remove / view aliases | none | The user's own saved data |
| Address check when registering | none | Format check only (`0x` + 40 hex characters, any case) |

Hypurrscan requests time out after 15 seconds. If one fails, the user gets a ❌ message instead of silence.

## Saved data

Each user's data is in `context.user_data`:

- `aliases`: `{alias: address}`, both lowercased. The alias is the key, so saving an existing alias again replaces its address. One address can have several aliases.
- `address`: temporary. It holds the address between the two registration questions and is removed once the name is saved.

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

- `FakeTelegram`, which answers Bot API calls locally, records every message the bot sends, and rejects HTML that Telegram would reject
- `FakeHypurrscan`, which replaces `aiohttp.ClientSession` and returns canned responses or errors per URL
- a temporary `bot_data.pkl`, so real data is never touched

`BotRun(...).send(...)` plays a chat: text, commands, `("tap", button_text)` or `("edit", text)`. Test data uses a `zz-` prefix. Set `WT_DIR` to test a copy of `wallet_tracker.py` in another folder.
