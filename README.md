# WalletTracker: Hyperliquid Wallet Tracker Bot

A Telegram bot for tracking wallets and tokens on [Hyperliquid](https://hyperliquid.xyz). Save your own names for wallet addresses, see a wallet's positions and balances, get regular summaries of its trades, look up the public names of known wallets, and see the top holders of any token. Data comes from the public [Hyperliquid](https://hyperliquid.xyz) and [Hypurrscan](https://hypurrscan.io) APIs.

Built with [python-telegram-bot](https://python-telegram-bot.org/).

## Features

| Command | What it does |
|---|---|
| `/start` | Shows every feature as a button |
| `/register_alias` | Save a name for a wallet address (your personal address book). Reusing a name asks before replacing its address |
| `/remove_alias` | Remove a saved name: tap it, or type it |
| `/view_aliases` | List your saved names, with buttons to rename, export, import and set alerts |
| `/rename_alias` | Rename a saved name |
| `/export_aliases` | Download your saved names as a CSV file (`alias,address`) |
| `/import_aliases` | Add saved names from a CSV file. Names you already use for another address are kept as they are |
| `/wallet` | Show a wallet's perp positions, account value and spot balances. Use `/wallet <alias or address>`, or tap a saved one |
| `/alerts` | Switch trade alerts on or off per saved wallet, and choose how often to check: every 15 min, 1 h (default) or 4 h |
| `/search_alias` | Send an address to get its public Hypurrscan name, or part of a name (e.g. HyperEVM) to find its addresses. Your own aliases are searched too |
| `/get_global_aliases` | Browse all public names on Hypurrscan, 30 per page |
| `/get_top_holders` | Show the top holders of a token, e.g. USDC, 15 per page, with your own names, public names and Hyperliquid system addresses marked |
| `/help` | List the commands |
| `/cancel` | Leave the current step (every question also has a **Cancel** button) |

- **Alerts:** at each check, the bot sends one message per wallet that traded since the last check, summarising every coin: opened, added, reduced or closed, how much was bought and sold, and realised PnL. Wallets that didn't trade send nothing. No trade is missed between checks.
- **Questions time out:** an answer that arrives more than 10 minutes after the question isn't taken as the answer. Open questions survive a restart of the bot.
- **Private chats only:** in a group, the bot only answers its own commands, with a button to open a private chat, so nobody's saved wallets are shown to the group.
- Addresses are tap-to-copy, and **view** opens the address on Hypurrscan.
- Your saved names keep their capitals; finding or removing one ignores case.
- Each user's saved names, alert settings and open questions are kept in `bot_data.pkl`, so they survive restarts.

## Setup

1. Install Python 3.12 or 3.13 and the dependencies:

   ```
   pip install -r requirements.txt
   ```

   This includes the scheduler that wallet alerts need. If you update the bot from an older version, run this again before restarting it.

2. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
3. Copy `.env.example` to `.env` and set `BOT_TOKEN`.
4. Start the bot:

   ```
   python wallet_tracker.py
   ```

The bot fills Telegram's **Menu** button with its commands every time it starts, so there's no need to set them in @BotFather.

## Keep these private

`.env` (your bot token) and `bot_data.pkl` (users' saved names and alert settings) are ignored by git. If your token leaks, generate a new one with `/revoke` in @BotFather.

## Notes

Wallet data comes from the public Hyperliquid and Hypurrscan APIs. This project is not affiliated with Hyperliquid or Hypurrscan. See [ARCHITECTURE.md](ARCHITECTURE.md) for how the bot works and how to run the tests.

## License

MIT, see [LICENSE](LICENSE).
