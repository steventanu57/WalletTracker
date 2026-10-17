# Architecture

## Tech stack

- **Python 3.12**
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
- Each command starts a conversation (`ConversationHandler`): the bot asks a question, waits for the reply, then offers **Continue** / **Cancel** buttons.

| Feature | Where the data comes from |
|---|---|
| Register / remove / view aliases | The user's own saved data |
| Search and list public aliases | `GET api.hypurrscan.io/globalAliases` |
| Top holders of a token | `GET api.hypurrscan.io/holdersWithLimit/{TOKEN}/1000` (top 15 shown) |
| Address check when registering | Format check only (`0x` + 40 hex characters), no request |

Hypurrscan requests time out after 15 seconds. If one fails, the user gets a ❌ message instead of silence.

**Persistence:** python-telegram-bot's `PicklePersistence` keeps each user's aliases in memory and writes them to `bot_data.pkl` every 5 seconds (only when something changed) and on a clean shutdown. Open conversations are not saved, so a restart cancels any question in progress.

## Files

| File | Role |
|---|---|
| `wallet_tracker.py` | The bot: handlers, conversation states, Hypurrscan calls |
| `tests/` | End-to-end tests against a fake Telegram server and fake Hypurrscan (no token, no network) |
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

## Tests

```
pip install -r requirements-dev.txt
pytest
ruff check .
```
