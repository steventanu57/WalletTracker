"""Starts a copy of the REAL wallet_tracker.py, unchanged, with python-telegram-bot pointed at the
local Bot API simulator instead of api.telegram.org (the supported "{token}" base_url form).
Started by run_e2e.py: start_bot.py <wallet_tracker.py copy> <simulator URL>"""

import runpy
import sys

from telegram import ext

script, sim_url = sys.argv[1], sys.argv[2]
real_token = ext.ApplicationBuilder.token


def token(self, value):
    return (
        real_token(self, value)
        .base_url(f"{sim_url}/bot{{token}}")
        .base_file_url(f"{sim_url}/file/bot{{token}}")
    )


ext.ApplicationBuilder.token = token
sys.argv = [script]
runpy.run_path(script, run_name="__main__")
