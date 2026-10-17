# WalletTracker: Hyperliquid Wallet Storage Bot

A Telegram bot for looking up wallets and tokens on [Hyperliquid](https://hyperliquid.xyz), using the public [Hypurrscan](https://hypurrscan.io) API. Save your own names for wallet addresses, look up the public names of known wallets, and see the top holders of any token.

Built with [python-telegram-bot](https://python-telegram-bot.org/).

## Features

| Command | What it does |
|---|---|
| `/start` | Shows every feature as a button |
| `/register_alias` | Save a name for a wallet address (your personal address book) |
| `/remove_alias` | Remove a saved name |
| `/view_aliases` | List your saved names |
| `/search_alias` | Look up the public Hypurrscan name of an address |
| `/get_global_aliases` | Show the first 30 public names on Hypurrscan |
| `/get_top_holders` | Show the top 15 holders of a token, e.g. USDC |
| `/cancel` | Leave the current step |

Each user's saved names are kept in `bot_data.pkl`, so they survive restarts.

## Setup

1. Install Python 3.12 or 3.13 and the dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
3. Copy `.env.example` to `.env` and set `BOT_TOKEN`.
4. Start the bot:

   ```
   python wallet_tracker.py
   ```

To list the commands under Telegram's **Menu** button, send `/setcommands` to @BotFather and paste the commands from the table above.

## Keep these private

`.env` (your bot token) and `bot_data.pkl` (users' saved names) are ignored by git. If your token leaks, generate a new one with `/revoke` in @BotFather.

## Notes

Wallet data comes from Hypurrscan's public API. This project is not affiliated with Hyperliquid or Hypurrscan. See [ARCHITECTURE.md](ARCHITECTURE.md) for how the bot works and how to run the tests.

## License

MIT, see [LICENSE](LICENSE).
