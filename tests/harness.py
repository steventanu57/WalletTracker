"""Runs the real wallet_tracker.py handlers end to end against a fake Telegram server and a
fake Hypurrscan, so tests need no network, no bot token and no real data.

Set WT_DIR to test a copy of wallet_tracker.py somewhere else (default: the repo root)."""

import asyncio
import html
import importlib.util
import json
import os
import re
import tempfile
from pathlib import Path
from typing import ClassVar
from unittest import mock

import aiohttp
from telegram import Update
from telegram.ext import Application, PicklePersistence
from telegram.request import BaseRequest

# Set before the bot loads .env: load_dotenv never overrides it, so tests never read the real token
os.environ["BOT_TOKEN"] = "123456:zz-fake-token"

BOT = {"id": 999, "is_bot": True, "first_name": "zz-bot", "username": "zz_bot"}
USER = {"id": 111, "is_bot": False, "first_name": "zz-User"}
CHAT = {"id": 111, "type": "private", "first_name": "zz-User"}

# Telegram's HTML mode (https://core.telegram.org/bots/api#html-style) supports only these tags.
# A "<" that doesn't start one of them, or unbalanced tags, makes Telegram reject the message.
TAGS = "b|strong|i|em|u|ins|s|strike|del|span|tg-spoiler|a|code|pre|blockquote|tg-emoji"
TAG_RE = re.compile(rf"</?(?:{TAGS})(?:\s[^<>]*)?>")


def bot_dir():
    return Path(os.environ.get("WT_DIR") or Path(__file__).resolve().parents[1])


def telegram_accepts_html(text):
    if "<" in TAG_RE.sub("", text):
        return False
    opened = []
    for m in re.finditer(rf"<(/?)({TAGS})\b", text):
        if m.group(1):
            if not opened or opened.pop() != m.group(2):
                return False
        else:
            opened.append(m.group(2))
    return not opened


def visible_length(params):
    # Telegram counts the limit after HTML is turned into formatting: tags and link targets don't count
    text = params["text"]
    if params.get("parse_mode") == "HTML":
        text = html.unescape(TAG_RE.sub("", text))
    return len(text)


class FakeTelegram(BaseRequest):
    """Answers Bot API calls locally and records every message the bot tries to send."""

    def __init__(self):
        self.calls, self._next_id = [], 5000

    @property
    def read_timeout(self):
        return 5.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(
        self,
        url,
        method,
        request_data=None,
        read_timeout=None,
        write_timeout=None,
        connect_timeout=None,
        pool_timeout=None,
    ):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((endpoint, params))
        if endpoint == "getMe":
            result = BOT
        elif endpoint in ("sendMessage", "editMessageText"):
            if visible_length(params) > 4096:  # Telegram's limit
                return 400, json.dumps(
                    {
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: message is too long",
                    }
                ).encode()
            if params.get("parse_mode") == "HTML" and not telegram_accepts_html(
                params["text"]
            ):
                return 400, json.dumps(
                    {
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: can't parse entities",
                    }
                ).encode()
            self._next_id += 1
            result = {
                "message_id": self._next_id,
                "date": 0,
                "chat": CHAT,
                "from": BOT,
                "text": params["text"],
            }
        else:  # answerCallbackQuery, deleteMessage, ...
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def texts(self):
        return [
            p["text"] for e, p in self.calls if e in ("sendMessage", "editMessageText")
        ]

    def count(self, endpoint):
        return sum(1 for e, _ in self.calls if e == endpoint)


class FakeHypurrscan:
    """Stands in for aiohttp.ClientSession. routes: {url_substring: (status, json) or Exception}.
    requested lists every URL asked for; opened counts the sessions (connections) created."""

    routes: ClassVar[dict] = {}
    requested: ClassVar[list] = []
    opened: ClassVar[int] = 0
    closed = False  # The bot reuses one session while it's open

    def __init__(self, *a, **k):
        FakeHypurrscan.opened += 1

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, *a, **k):
        self.requested.append(url)
        for key, answer in self.routes.items():
            if key in url:
                if isinstance(answer, Exception):
                    raise answer
                return _Response(*answer)
        raise AssertionError(f"unexpected URL {url}")


class _Response:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, *a, **k):
        return self._payload


def load_bot_module():
    path = bot_dir() / "wallet_tracker.py"
    spec = importlib.util.spec_from_file_location(
        f"wallet_tracker_{abs(hash(str(path)))}", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BotRun:
    """Builds the app exactly as the bot's own main() does, but wired to the fakes."""

    def __init__(self, hypurrscan_routes=None, saved_user_data=None):
        # Deleted automatically once the run is no longer used (at the latest when Python exits)
        self._tmp = tempfile.TemporaryDirectory(
            prefix="zz-wt-", ignore_cleanup_errors=True
        )
        self.tmp = Path(self._tmp.name)
        self.telegram = FakeTelegram()
        self.errors = []
        self._update_id = 0
        FakeHypurrscan.routes = hypurrscan_routes or {}
        FakeHypurrscan.requested = []
        FakeHypurrscan.opened = 0
        self.module = load_bot_module()
        self.app = self._build()
        if saved_user_data:
            asyncio.run(self._seed(saved_user_data))

    def _build(self):
        captured = {}
        real_builder = self.module.ApplicationBuilder
        fake_tg, tmp = self.telegram, self.tmp

        class Builder:
            def __init__(self):
                self.b = (
                    real_builder().request(fake_tg).get_updates_request(FakeTelegram())
                )

            def token(self, _):
                self.b = self.b.token("123456:zz-fake-token")
                return self

            def persistence(self, persistence):
                persistence.filepath = (
                    tmp / "zz-bot_data.pkl"
                )  # keep the bot's settings, use a temp file
                self.b = self.b.persistence(persistence)
                return self

            def build(self):
                return self.b.build()

        with (
            mock.patch.object(self.module, "ApplicationBuilder", Builder),
            mock.patch.object(
                Application,
                "run_polling",
                lambda app, *a, **k: captured.setdefault("app", app),
            ),
            mock.patch.dict(os.environ, {"BOT_TOKEN": "123456:zz-fake-token"}),
        ):
            self.module.main()
        app = captured["app"]

        async def record(update, context):
            self.errors.append(context.error)

        app.add_error_handler(record)
        return app

    async def _seed(self, saved_user_data):
        # Write a complete pickle file first; the app loads it in initialize()
        seed = PicklePersistence(filepath=self.tmp / "zz-bot_data.pkl")
        for load in (
            seed.get_user_data,
            seed.get_chat_data,
            seed.get_bot_data,
            seed.get_callback_data,
        ):
            await load()
        await seed.update_user_data(USER["id"], saved_user_data)

    def _next(self):
        self._update_id += 1
        return self._update_id

    def _message(self, text):
        msg = {
            "message_id": self._next(),
            "date": 0,
            "chat": CHAT,
            "from": USER,
            "text": text,
        }
        if text.startswith("/"):
            msg["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
        return msg

    def send(self, *steps):
        """steps: plain text / commands, ("tap", button_text) or ("edit", text)."""

        async def go():
            with mock.patch.object(aiohttp, "ClientSession", FakeHypurrscan):
                await self.app.initialize()
                for step in steps:
                    if isinstance(step, tuple) and step[0] == "tap":
                        upd = {
                            "update_id": self._next(),
                            "callback_query": {
                                "id": f"zz-cq{self._update_id}",
                                "from": USER,
                                "chat_instance": "zz",
                                "data": step[1],
                                "message": {
                                    "message_id": 4000,
                                    "date": 0,
                                    "chat": CHAT,
                                    "from": BOT,
                                    "text": "zz",
                                },
                            },
                        }
                    elif isinstance(step, tuple) and step[0] == "edit":
                        upd = {
                            "update_id": self._next(),
                            "edited_message": {
                                **self._message(step[1]),
                                "edit_date": 1,
                            },
                        }
                    else:
                        upd = {
                            "update_id": self._next(),
                            "message": self._message(step),
                        }
                    await self.app.process_update(Update.de_json(upd, self.app.bot))
                await self.app.shutdown()

        asyncio.run(go())
        return self

    def call(self, func):
        """Runs func(app) with the app started, e.g. the bot's post_init."""

        async def go():
            await self.app.initialize()
            await func(self.app)
            await self.app.shutdown()

        asyncio.run(go())
        return self

    def saved_user_data(self):
        async def read():
            return await PicklePersistence(
                filepath=self.tmp / "zz-bot_data.pkl"
            ).get_user_data()

        return asyncio.run(read()).get(USER["id"], {})
