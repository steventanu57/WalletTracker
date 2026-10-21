# Architecture

## Tech stack

- **Python 3.12 or 3.13**
- **python-telegram-bot 22** with the `job-queue` extra: Telegram Bot API, long polling, conversation flows, and the scheduler (APScheduler) that runs wallet alerts
- **aiohttp**: HTTP requests to Hypurrscan and Hyperliquid
- **python-dotenv**: loads `BOT_TOKEN` from `.env`
- **Hyperliquid public info API**: wallets' positions, balances and trades
- **Hypurrscan public API**: public names of known wallets, and token holders

## How it works

```
 Telegram user ◀──▶ Telegram Bot API ◀──polling──▶ wallet_tracker.py ──HTTP──▶ api.hypurrscan.io
                                                          │            ──HTTP──▶ api.hyperliquid.xyz/info
                                                          │
                                       bot_data.pkl (aliases, alert settings, open questions)
```

- The bot polls Telegram for new messages (`run_polling`), so it needs no web server or open port.
- It works in private (1:1) chats only. See "Groups and channels" below.
- Lookups happen on demand. Wallet alerts run in the background, on a schedule each user chooses.

### Handlers

`main()` registers the handlers in this order:

0. **Group -1, before everything else:** `private_chats_only`. In a private chat it does nothing and the normal handlers run. In a group or channel it stops all further handling (see "Groups and channels").
1. One-off commands and buttons: `/get_global_aliases`, `/view_aliases`, `/help`, `/alerts`, the ◀ / ▶ page buttons under lists, and the 🔔 / 🔕, "Every …" and ◀ / ▶ buttons under the alerts menu. They reply and leave any open question as it was.
2. One `ConversationHandler` for everything that asks a question. It starts with `/start`, `/register_alias`, `/remove_alias`, `/rename_alias`, `/export_aliases`, `/import_aliases`, `/wallet`, `/search_alias`, `/get_top_holders` or a menu button. `/cancel` or a **Cancel** button ends it. Typing one of these commands, or tapping a menu button, mid-question starts that over (`allow_reentry=True`).
3. Replies for everything else, instead of silence:
   - `/cancel` with no open question: "There's nothing to cancel".
   - Unknown commands: a pointer to `/help`.
   - A button from an old message: "This button has expired" (as a small pop-up).
   - Any other message (`no_question_open`): the user is pointed to `/start`. The message itself isn't printed, so users' messages stay private and nothing they send reaches your terminal.
   - These two pointers go to each user at most once every `HINT_EVERY_SECONDS` (60) per kind (`hint_due()`, kept in memory only). Someone sending lots of messages the bot doesn't understand gets one pointer instead of a reply to each.

`/help` (`help_command`) sends the command list plus `HELP_GUIDE`: where to find wallet addresses (your wallet app, the Hyperliquid leaderboard, Hypurrscan, `/search_alias`, `/get_top_holders`), Hyperliquid's token names (perps `BTC`; spot `UBTC` "Unit Bitcoin", `UETH`, `USOL`) and the CSV format. The welcome message and the questions for an address or a token point to it. `/help` in the middle of a question answers and leaves the question open.

**The terminal:** activity lines like `Alice is checking wallet 0x…` show users' names and typed text through `clean()`, which turns control codes, line breaks and text-direction marks into spaces and shortens to 60 characters. Nobody can change your terminal (window title, clearing the screen) or fake a log line.

If a handler crashes, `on_error` prints the error to the terminal and tells the user "❌ Something went wrong".

On startup (`post_init` → `start_up`) the bot sets its own command list, which Telegram shows under the **Menu** button, and restarts the alert schedule of every user with alerts on. If APScheduler is missing, it prints a warning and alerts are switched off; everything else works. On shutdown (`post_shutdown`) it closes its HTTP connection.

Answers are only accepted from new text messages (and, for imports, files). Edited messages, photos and stickers are not taken as answers.

### Conversation steps

| Command | Steps (state constants in `wallet_tracker.py`) |
|---|---|
| `/start` | `START_BUTTON_TAPS`: waits for a menu button |
| `/register_alias` | `REGISTER_ALIAS1` address → `REGISTER_ALIAS2` name → (`REGISTER_CONFIRM` Replace / Choose another name, only when the name already points to another address) → `REGISTER_AGAIN` Continue/Cancel |
| `/remove_alias` | `REMOVE_ALIAS1` tap or type an alias → `REMOVE_AGAIN` Continue/Cancel |
| `/rename_alias` | `RENAME1` tap or type an alias → `RENAME2` new name |
| `/import_aliases` | `IMPORT1` a CSV file |
| `/wallet` | `WALLET1` tap a saved alias, or type an alias or address (skipped with `/wallet <alias or address>`) → `WALLET_AGAIN` Continue/Cancel |
| `/search_alias` | `SEARCH_ALIAS1` address or part of a name → `SEARCH_AGAIN` Continue/Cancel |
| `/get_top_holders` | `TOP_HOLDERS1` token (typed, or a "Did you mean …?" button `tk:<NAME>`) → `SHOW_TOP_HOLDERS_AGAIN` Continue/Cancel |

- Every question has a **Cancel** button. **Continue** asks the question again. **Cancel** deletes the button message, says "Operation cancelled." and forgets any half-finished answer.
- A wrong answer (bad address, unknown alias, nothing found, unknown token, not a file) asks the same question again.
- **Timeout:** `ask()` sends a question and saves the time in `asked_at`. Every typed-answer handler is wrapped in `answer_in_time()`: an answer more than 10 minutes (`QUESTION_TIMEOUT_SECONDS`) after the question gets "⌛ I asked that more than 10 minutes ago, so I closed the question" and ends the conversation. Because the time is saved with the user's data, this also works after a restart, without a background timer. Questions answered with buttons don't time out.
- **Saved questions:** the conversation is `persistent=True` (name `questions`), so the step each user is on is saved in `bot_data.pkl` and continues after a restart. State numbers are saved, so they must not change.
- The menu buttons work at any time, also on an older menu message.
- Each `CallbackQueryHandler` has a `pattern`, so it only reacts to its own buttons. Menu and Continue/Cancel buttons use the button's own text as `callback_data`. Buttons for one alias use a prefix plus a short fingerprint of the alias, because Telegram allows at most 64 bytes: `rm:` remove, `rn:` rename, `w:` wallet, `al:` alert on/off. Other buttons: `ai:<seconds>:<offset>` alert interval (alerts menus sent before there were pages have `ai:<seconds>`), `ap:<offset>` alerts menu page, `tk:<NAME>` a suggested token name, `ga:<offset>` public names page, `th:<TOKEN>:<offset>` top holders page.

### Groups and channels

`private_chats_only` runs first for every update. Outside a private chat:

- A command addressed to this bot (`/wallet@this_bot`, or one of its own commands without `@`) gets "I only work in private chats…" and a button that opens a private chat.
- A button tap gets the same note as a pop-up.
- Everything else is ignored, including chatter and other bots' commands.

Then it raises `ApplicationHandlerStop`, so no other handler runs. Alias lists, wallets and alerts never appear in a group.

### Data from Hypurrscan and Hyperliquid

| Feature | Request | Response used |
|---|---|---|
| Search and list public names | `GET api.hypurrscan.io/globalAliases` | `{address: name}`, about 30 KB and 463 entries (2026-10-20). Downloaded at most once every 10 minutes and kept in memory |
| Top holders of a token | `GET api.hypurrscan.io/holdersWithLimit/{TOKEN}/{offset + 15}` | `{"holders": {address: balance}, "holdersCount", "lastUpdate", ...}`. The bot sorts by balance and shows 15 per page (up to 1,000), with the total and the data's time. An unknown token returns `{}` |
| `/wallet` perps | `POST api.hyperliquid.xyz/info` `{"type": "clearinghouseState", "user": address}` | `marginSummary.accountValue`, `withdrawable`, `time`, and per position `coin`, `szi` (negative = short), `entryPx`, `positionValue`, `unrealizedPnl`, `returnOnEquity`, `leverage`, `liquidationPx` |
| `/wallet` spot | `POST …/info` `{"type": "spotClearinghouseState", "user": address}` | `balances[]`: `coin`, `total` (zero balances are left out) |
| Alerts | `POST …/info` `{"type": "userFillsByTime", "user", "startTime", "endTime"}` | Every trade (fill) in the window: `coin`, `side` (`B` buy, `A` sell), `sz`, `px`, `startPosition`, `closedPnl`, `time`, `tid` |
| Spot pair names in alerts, token suggestions | `POST …/info` `{"type": "spotMeta"}` | `universe` maps trade names like `@107` to `HYPE/USDC`; `tokens` gives each spot token's `name` and `fullName` (`UBTC`, "Unit Bitcoin"). `load_spot_meta()` keeps both for an hour |
| Register / remove / view / rename aliases | none | The user's own saved data |
| Address check when registering | none | Format check only (`0x` + 40 hex characters, any case) |

- One shared `aiohttp` session is reused for every request (about 0.2 s per request instead of 0.6 s with a new connection each time, measured 2026-10-20).
- The token is trimmed, uppercased and URL-encoded, so spaces or a `/` can't change which address is requested.
- **Token names:** Hypurrscan's holders only know spot names. On Hyperliquid's spot market Bitcoin is `UBTC`, Ether `UETH`, Solana `USOL` (perps use `BTC`, `ETH`, `SOL`); `BTC` returns `{}` (checked live 2026-10-21). For an unknown token, `suggest_tokens()` offers up to `TOKEN_SUGGESTIONS` (5) spot names as buttons, best first: `U` + what was typed (`BTC` → `UBTC`), then a full name containing it (`bitcoin` → `UBTC`), then names starting with or containing it. Nothing is switched automatically: the user taps one.
- A response that isn't the expected JSON shape counts as a failure. Requests time out after 15 seconds. If one fails, the user gets a ❌ message instead of silence.
- Hyperliquid sends numbers as text. The bot reads them as `Decimal`, so sums of sizes are exact.
- Top-holder lines are marked with the user's own aliases (⭐), Hypurrscan's public name (🏷) and Hyperliquid system addresses (⚙️). A system address is `0x20` + zeros + the token's index (USDC is `0x2000…0000`), or `0x2222…2222` for HYPE. These are used for HyperCore ↔ HyperEVM transfers, so they aren't ordinary holders.
- Telegram refuses messages over 4,096 characters. Alias lists, wallet views and alert summaries are split over several messages. In page lists, names longer than 40 characters are shortened so a page always fits in one message.
- Telegram also refuses an edit that changes nothing ("message is not modified"), for example when a button is tapped twice quickly. Menus are redrawn with `edit_menu()`, which treats that as fine: the menu already shows the right thing.

### Wallet alerts

- `/alerts` shows a menu: one 🔔 / 🔕 button per saved alias, and "Every 15 min / 1 h / 4 h". Alerts are kept per address, so two aliases of the same address share them. Removing the last alias of an address, or replacing its address, switches its alerts off.
- With more than 50 aliases (`ALIAS_BUTTONS_SHOWN`), the menu has pages: "(51-85 of 85)" in its title and ◀ / ▶ buttons. After a tap it stays on the same page: an interval button carries its page, and switching an alias shows that alias's page.
- Tapping the interval already chosen answers "Already checking every …" and keeps the current timer; any other change starts the timer again.
- Each user with alerts on has one repeating job, `alerts:<user id>`, in python-telegram-bot's `JobQueue`. `schedule_alerts()` creates, changes or removes it. Jobs aren't saved, so `start_up` creates them again from the saved settings.
- `check_alerts()` asks Hyperliquid for each wallet's trades from the last check to now. Both ends of the window are inclusive (checked live), so the next window starts at now + 1 ms: no gaps, no repeats. When Hyperliquid can't be reached, the window is kept and asked for again next time.
- Hyperliquid returns at most 2,000 trades per request, oldest first. `fetch_fills()` asks again from the last trade's time, skips trades already seen (by `tid`), and stops after 5 pages (10,000 trades, which is also how many Hyperliquid keeps). The summary says so if there were more.
- `trade_summary()` groups trades by coin. For perps it compares the position before the first trade (`startPosition`) with the position after the last one, giving "opened / added to / reduced / closed / flipped". Positions are rounded to 8 decimals first, because Hyperliquid's numbers carry float noise (e.g. `-1.0000000001`). Checked live on 2026-10-20: with that rounding, 4,081 of 4,081 consecutive trades lined up. Spot pairs show bought and sold amounts. A wallet that didn't trade gets no message.

## Saved data

Each user's data is in `context.user_data`:

- `aliases`: `{alias: address}`. Aliases keep the capitals they were typed with, and are found ignoring case. Addresses are lowercase. One address can have several aliases. Saving a name that already points to another address asks before replacing it. (Aliases saved before 2026-10-20 are all lowercase.)
- `alert_wallets`: `{address: time in ms}` for wallets with alerts on. The time is where the next check starts.
- `alert_every`: seconds between checks: 900, 3600 (the default) or 14400.
- `address`, `new_alias`, `rename_from` and `asked_at`: temporary. They hold answers between questions and the time a question was asked, and are removed when the question is answered or cancelled.

The open conversation step of each user is saved too (`ConversationHandler` `persistent=True`).

**Persistence:** python-telegram-bot's `PicklePersistence` keeps this data in memory and writes it to `bot_data.pkl` every 5 seconds (only when something changed) and on a clean shutdown. Run only one copy of the bot at a time: Telegram refuses a second poller with the same token, and both would write the same file.

## CSV import and export

- **Export:** `/export_aliases` sends `aliases.csv` with a header row `alias,address`, sorted by alias. It is UTF-8 with a byte-order mark, so Excel shows non-English characters correctly.
- **Import:** `/import_aliases` reads a CSV file of up to 100 KB and 1,000 rows. The separator is detected (`,`, `;` or tab), because Excel uses `;` in some countries. A header row `alias,address` is skipped.
- Rows without a name or with an invalid address are skipped and listed by row number.
- A name that already points to another address is kept as it was: an import never replaces an address.

## Files

| File | Role |
|---|---|
| `wallet_tracker.py` | The bot: handlers, conversation states, alerts, Hypurrscan and Hyperliquid calls |
| `tests/harness.py` | Fake Telegram server and fake Hypurrscan / Hyperliquid that run the real bot |
| `tests/test_wallet_tracker.py` | End-to-end tests |
| `tests/e2e/` | End-to-end test of the real bot process against a local Bot API simulator, with live data |
| `.env` (git-ignored) | `BOT_TOKEN` from @BotFather |
| `bot_data.pkl` (git-ignored) | Saved aliases, alert settings and open questions per user, created on first save |

## Setup and run

```
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env           # macOS/Linux: cp .env.example .env
```

1. Put your bot token from [@BotFather](https://t.me/BotFather) in `.env`.
2. Run `python wallet_tracker.py` and keep it running. Alerts only run while the bot is running; after a restart, the next check covers the time it was off.
3. Stop it with Ctrl+C, which saves data. Killing the process (closing the window, a crash) can lose the last 5 seconds of changes.

Output goes to the terminal through `print`: activity lines (with users' names and typed text cleaned), never the messages themselves. Logging is not configured, so only warnings and errors from the libraries appear.

**Settings:** your token goes in `.env`. Everything else you can change (alert intervals and the default, question timeout, page sizes, token suggestions, import limits, cache times, how often hints are sent, the HTTP timeout) is at the top of `wallet_tracker.py` under `SETTINGS YOU CAN CHANGE`. The values under `FIXED VALUES` follow Telegram's, Hypurrscan's and Hyperliquid's rules.

## Tests

```
pip install -r requirements-dev.txt
pytest
ruff check .
```

The tests need no bot token and no network. `tests/harness.py` runs the bot's real `main()` with:

- `FakeTelegram`, which answers Bot API calls locally and records every call. Like Telegram, it rejects bad HTML, messages over 4,096 visible characters and edits that change nothing. It also takes files from the test user (`files`) and records files the bot sends (`documents`).
- `FakeHypurrscan`, which replaces `aiohttp.ClientSession`. GET routes match a URL part; Hyperliquid POST routes are `"info:<type>"`. An answer is `(status, json)`, an exception, or a function of the request body. It records every request (`requested`), every POST body (`bodies`) and every session opened (`opened`).
- a temporary `bot_data.pkl`, so real data is never touched

`BotRun(...)` options:

- `saved_user_data`: start from saved data.
- `chat=GROUP`: talk from a group chat.
- `no_job_queue=True`: build the app the way python-telegram-bot does when APScheduler isn't installed.

`BotRun` methods:

- `.send(...)` plays a chat: text, commands, `("tap", button_data)`, `("edit", text)` or `("document", filename, bytes)`.
- `.call(func)` runs `func(app)` with the app started, for example `post_init`.
- `.run_jobs("alerts:111")` runs scheduled jobs now.
- `.restart()` reloads the code and app with the same saved data.

Test data uses a `zz-` prefix. Set `WT_DIR` to test a copy of `wallet_tracker.py` in another folder.

### End-to-end test

```
python tests/e2e/run_e2e.py            # everything, about 45 minutes
python tests/e2e/run_e2e.py --fast     # without the real waits, about 3 minutes
```

This runs the real `wallet_tracker.py` as its own process, as you do, and plays a user step by step. It isn't part of `pytest`.

- Telegram is replaced by `telegram_sim.py`, a local Bot API server. `start_bot.py` points python-telegram-bot at it with `base_url`. Like Telegram, it refuses:
  - bad HTML, texts over 4,096 characters, `callback_data` over 64 bytes
  - unchanged edits, and button answers after 15 s
  - taps on buttons that aren't on the message
- Like Telegram, it forgets updates once they are confirmed.
- Hypurrscan and Hyperliquid are the **real, live APIs**, so it needs internet. For the alert checks it switches alerts on for up to 3 wallets that traded in at least 20 of the last 30 minutes. If no alert comes, it asks Hyperliquid whether they traded at all, so a quiet wallet shows as "inconclusive", not as a bug.
- It also checks the newer parts: `/help`'s guide, `BTC` → **UBTC** buttons (live), at most one reply to five quick messages, and that no user message or control code reaches the bot's output.
- It needs no bot token and doesn't touch your `.env` or `bot_data.pkl`: the bot copy runs in a temporary folder with its own data.
- The full run also waits for a question to time out (10 min), for the first alert (15 min), and for an alert after a hard kill and restart.

It prints PASS/FAIL per check and exits with 1 if anything failed. The temporary folder is deleted when everything passed, and kept (with its path printed) when something failed. `WT_DIR` works here too.

Tests are grouped as "features that must keep working", "bug fixes", phase 1, phase 2, "found by the end-to-end test" and "easier to use" (U6-U8, U11), each marked with its finding or plan ID.
