"""A local stand-in for Telegram's Bot API server (https://core.telegram.org/bots/api), strict
where Telegram is strict, so the REAL wallet_tracker.py can run end to end against it (run_e2e.py).

Telegram's rules that it applies (from the Bot API docs):
- HTML style: only b/strong/i/em/u/ins/s/strike/del/span/tg-spoiler/a href/tg-emoji/code/pre/
  blockquote; only &lt; &gt; &amp; &quot; and numeric entities; tags must nest.
- Message text 1-4096 characters after the HTML is read (UTF-16 units); captions 0-1024.
- callback_data 1-64 bytes; button text not empty; url buttons need http(s) or tg URLs; at most
  100 buttons.
- answerCallbackQuery: text up to 200 characters, once, within 15 s.
- editMessageText / editMessageReplyMarkup with exactly the same content: "message is not modified".
- setMyCommands: 1-100 commands of 1-32 characters a-z, 0-9, _; descriptions 1-256 characters.
- getUpdates: an offset confirms every update before it, and confirmed updates are forgotten.
The user's side: user_text, user_document and tap (only buttons that are really on the message)."""

import asyncio
import html
import itertools
import json
import re
import time

from aiohttp import web

ALLOWED = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "span",
    "tg-spoiler",
    "a",
    "tg-emoji",
    "code",
    "pre",
    "blockquote",
}


class BadRequest(Exception):
    pass


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def parse_html(text):
    """Returns the visible text, or raises BadRequest like Telegram's HTML parser."""
    out, stack, i = [], [], 0
    while i < len(text):
        ch = text[i]
        if ch == "<":
            end = text.find(">", i)
            if end == -1:
                raise BadRequest(
                    "Bad Request: can't parse entities: unclosed start tag"
                )
            raw = text[i + 1 : end].strip()
            closing = raw.startswith("/")
            name = (
                raw[1:].strip().lower()
                if closing
                else re.split(r"\s", raw, maxsplit=1)[0].lower()
            )
            if name not in ALLOWED:
                raise BadRequest(
                    f'Bad Request: can\'t parse entities: unsupported start tag "{name}"'
                )
            if closing:
                if not stack or stack[-1] != name:
                    raise BadRequest(
                        f'Bad Request: can\'t parse entities: unmatched end tag "{name}"'
                    )
                stack.pop()
            else:
                if name == "a" and not re.search(r'href\s*=\s*"[^"]*"', raw):
                    raise BadRequest(
                        "Bad Request: can't parse entities: a tag without href"
                    )
                if (
                    stack
                    and stack[-1] in ("code", "pre")
                    and not (stack[-1] == "pre" and name == "code")
                ):
                    raise BadRequest(
                        "Bad Request: can't parse entities: entities can't be nested in code"
                    )
                stack.append(name)
            i = end + 1
        elif ch == "&":
            m = re.match(r"&(#\d+|#x[0-9a-fA-F]+|lt|gt|amp|quot);", text[i:])
            if not m:
                raise BadRequest(
                    "Bad Request: can't parse entities: unsupported entity or stray &"
                )
            out.append(html.unescape(m.group(0)))
            i += len(m.group(0))
        elif ch == ">":
            raise BadRequest("Bad Request: can't parse entities: stray >")
        else:
            out.append(ch)
            i += 1
    if stack:
        raise BadRequest(
            f"Bad Request: can't parse entities: can't find end tag corresponding to start tag \"{stack[-1]}\""
        )
    return "".join(out)


def check_markup(markup):
    if markup is None:
        return None
    rows = markup.get("inline_keyboard")
    if not isinstance(rows, list):
        raise BadRequest("Bad Request: invalid reply markup")
    count = 0
    for row in rows:
        for button in row:
            count += 1
            if not button.get("text"):
                raise BadRequest("Bad Request: button text is empty")
            if "callback_data" in button:
                size = len(button["callback_data"].encode())
                if not 1 <= size <= 64:
                    raise BadRequest("Bad Request: BUTTON_DATA_INVALID")
            elif "url" in button:
                if not re.match(r"(https?|tg)://", button["url"]):
                    raise BadRequest("Bad Request: BUTTON_URL_INVALID")
            else:
                raise BadRequest("Bad Request: button without action")
    if count > 100:
        raise BadRequest("Bad Request: too many buttons")
    return markup


class BotApiSim:
    def __init__(self, bot_id=999, bot_username="zz_sim_bot"):
        self.bot = {
            "id": bot_id,
            "is_bot": True,
            "first_name": "zz-SimBot",
            "username": bot_username,
        }
        self.updates, self.update_ids = [], itertools.count(1)
        self.message_ids = itertools.count(1000)
        self.messages = {}  # (chat_id, message_id) -> dict
        self.log = []  # every Bot API call: (method, params)
        self.errors = []  # every request Telegram would have refused
        self.callbacks = {}  # callback query id -> {"answered": bool, "answer": params}
        self.files, self.commands = {}, []
        self.new_update = asyncio.Event()

    # ---------- the user side ----------
    def chat(self, chat_id, kind="private"):
        if kind == "private":
            return {"id": chat_id, "type": "private", "first_name": "zz-User"}
        return {"id": chat_id, "type": kind, "title": "zz-Group"}

    def user(self, user_id):
        return {"id": user_id, "is_bot": False, "first_name": "zz-User"}

    def _push(self, kind, payload):
        self.updates.append({"update_id": next(self.update_ids), kind: payload})
        self.new_update.set()

    def user_text(self, text, user_id=111, chat_id=None, chat_kind="private"):
        chat_id = chat_id or user_id
        mid = next(self.message_ids)
        msg = {
            "message_id": mid,
            "date": int(time.time()),
            "chat": self.chat(chat_id, chat_kind),
            "from": self.user(user_id),
            "text": text,
        }
        if text.startswith("/"):
            msg["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
        self.messages[(chat_id, mid)] = {"from": "user", "text": text, "markup": None}
        self._push("message", msg)
        return mid

    def user_document(self, name, content, user_id=111):
        mid, fid = next(self.message_ids), f"zz-file-{len(self.files)}"
        self.files[fid] = content
        msg = {
            "message_id": mid,
            "date": int(time.time()),
            "chat": self.chat(user_id),
            "from": self.user(user_id),
            "document": {
                "file_id": fid,
                "file_unique_id": fid + "u",
                "file_name": name,
                "mime_type": "text/csv",
                "file_size": len(content),
            },
        }
        self._push("message", msg)
        return mid

    def tap(self, message_id, data, user_id=111, chat_id=None, chat_kind="private"):
        """Taps a button the bot really sent; the data must be on that message (like a real client)."""
        chat_id = chat_id or user_id
        stored = self.messages.get((chat_id, message_id))
        assert stored and stored["from"] == "bot", f"no bot message {message_id}"
        datas = [
            b.get("callback_data")
            for row in (stored["markup"] or {}).get("inline_keyboard", [])
            for b in row
        ]
        assert data in datas, f"button {data!r} is not on message {message_id}: {datas}"
        qid = f"zz-cq-{next(self.update_ids)}"
        self.callbacks[qid] = {"answered": False, "answer": None, "sent": time.time()}
        self._push(
            "callback_query",
            {
                "id": qid,
                "from": self.user(user_id),
                "chat_instance": "zz",
                "data": data,
                "message": {
                    "message_id": message_id,
                    "date": int(time.time()),
                    "chat": self.chat(chat_id, chat_kind),
                    "from": self.bot,
                    "text": stored["text"],
                    **({"reply_markup": stored["markup"]} if stored["markup"] else {}),
                },
            },
        )
        return qid

    def bot_messages(self, chat_id=111):
        return [
            (mid, m)
            for (cid, mid), m in sorted(self.messages.items())
            if cid == chat_id and m["from"] == "bot" and not m.get("deleted")
        ]

    # ---------- the Bot API ----------
    async def handle(self, request):
        method = request.match_info["method"]
        form = await request.post()
        params = {}
        for key, value in form.items():
            if isinstance(value, web.FileField):
                params[key] = {"filename": value.filename, "content": value.file.read()}
            else:
                params[key] = value
        self.log.append((method, params))
        try:
            result = await getattr(self, "m_" + method)(params)
        except BadRequest as e:
            self.errors.append((method, str(e), params))
            return web.json_response(
                {"ok": False, "error_code": 400, "description": str(e)}, status=400
            )
        except AttributeError:
            self.errors.append((method, "unknown method", params))
            return web.json_response(
                {
                    "ok": False,
                    "error_code": 404,
                    "description": "Not Found: method not found",
                },
                status=404,
            )
        return web.json_response({"ok": True, "result": result})

    async def file(self, request):
        return web.Response(body=self.files[request.match_info["file_id"]])

    @staticmethod
    def j(params, key, default=None):
        value = params.get(key)
        if value is None:
            return default
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value

    async def m_getMe(self, p):
        return self.bot

    async def m_deleteWebhook(self, p):
        return True

    async def m_getUpdates(self, p):
        offset = int(p.get("offset", 0) or 0)
        timeout = float(p.get("timeout", 0) or 0)
        limit = int(p.get("limit", 100) or 100)
        if offset > 0:
            # Like Telegram: an offset confirms every update before it, and confirmed updates are forgotten
            # (a restarted bot that starts again from offset 0 only gets what was never confirmed)
            self.updates = [u for u in self.updates if u["update_id"] >= offset]
        deadline = time.time() + timeout
        while True:
            pending = [u for u in self.updates if u["update_id"] >= offset][:limit]
            if pending or time.time() >= deadline:
                return pending
            self.new_update.clear()
            try:
                await asyncio.wait_for(
                    self.new_update.wait(), max(0.05, deadline - time.time())
                )
            except TimeoutError:
                pass

    async def m_setMyCommands(self, p):
        commands = self.j(p, "commands")
        if not 1 <= len(commands) <= 100:
            raise BadRequest("Bad Request: too many or no commands")
        for c in commands:
            if not re.fullmatch(r"[a-z0-9_]{1,32}", c["command"]):
                raise BadRequest(f"Bad Request: BOT_COMMAND_INVALID {c['command']}")
            if not 1 <= len(c["description"]) <= 256:
                raise BadRequest("Bad Request: BOT_COMMAND_DESCRIPTION_INVALID")
        self.commands = commands
        return True

    async def m_getMyCommands(self, p):
        return self.commands

    async def m_sendChatAction(self, p):
        return True

    def _text_of(self, p, key="text", limit=4096, allow_empty=False):
        text = p.get(key, "")
        visible = parse_html(text) if p.get("parse_mode") == "HTML" else text
        if not allow_empty and not visible.strip():
            raise BadRequest("Bad Request: message text is empty")
        if utf16_len(visible) > limit:
            raise BadRequest(
                "Bad Request: message is too long"
                if key == "text"
                else "Bad Request: message caption is too long"
            )
        return visible

    def _message(self, chat_id, mid, stored):
        msg = {
            "message_id": mid,
            "date": int(time.time()),
            "chat": self.chat(chat_id, "private" if chat_id > 0 else "supergroup"),
            "from": self.bot,
        }
        if stored.get("document"):
            msg["document"] = stored["document"]
            if stored["text"]:
                msg["caption"] = stored["text"]
        else:
            msg["text"] = stored["text"]
        if stored["markup"]:
            msg["reply_markup"] = stored["markup"]
        return msg

    async def m_sendMessage(self, p):
        chat_id = int(p["chat_id"])
        visible = self._text_of(p)
        markup = check_markup(self.j(p, "reply_markup"))
        mid = next(self.message_ids)
        self.messages[(chat_id, mid)] = {
            "from": "bot",
            "text": visible,
            "html": p.get("text"),
            "markup": markup,
        }
        return self._message(chat_id, mid, self.messages[(chat_id, mid)])

    def _stored(self, p):
        key = (int(p["chat_id"]), int(p["message_id"]))
        if key not in self.messages or self.messages[key].get("deleted"):
            raise BadRequest("Bad Request: message to edit not found")
        if self.messages[key]["from"] != "bot":
            raise BadRequest("Bad Request: message can't be edited")
        return key, self.messages[key]

    async def m_editMessageText(self, p):
        key, stored = self._stored(p)
        visible = self._text_of(p)
        markup = check_markup(self.j(p, "reply_markup"))
        if visible == stored["text"] and markup == stored["markup"]:
            raise BadRequest(
                "Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message"
            )
        stored.update(text=visible, html=p.get("text"), markup=markup)
        return self._message(*key, stored)

    async def m_editMessageReplyMarkup(self, p):
        key, stored = self._stored(p)
        markup = check_markup(self.j(p, "reply_markup"))
        if markup == stored["markup"]:
            raise BadRequest(
                "Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message"
            )
        stored["markup"] = markup
        return self._message(*key, stored)

    async def m_deleteMessage(self, p):
        key = (int(p["chat_id"]), int(p["message_id"]))
        if key not in self.messages or self.messages[key].get("deleted"):
            raise BadRequest("Bad Request: message to delete not found")
        self.messages[key]["deleted"] = True
        return True

    async def m_answerCallbackQuery(self, p):
        qid = p["callback_query_id"]
        query = self.callbacks.get(qid)
        if query is None:
            raise BadRequest(
                "Bad Request: query is too old and response timeout expired or query ID is invalid"
            )
        if query["answered"]:
            raise BadRequest(
                "Bad Request: query is too old and response timeout expired or query ID is invalid"
            )
        if time.time() - query["sent"] > 15:
            raise BadRequest(
                "Bad Request: query is too old and response timeout expired or query ID is invalid"
            )
        if len(p.get("text", "")) > 200:
            raise BadRequest("Bad Request: MESSAGE_TOO_LONG (callback answer text)")
        query.update(answered=True, answer=p)
        return True

    async def m_sendDocument(self, p):
        chat_id = int(p["chat_id"])
        caption = (
            self._text_of(p, "caption", 1024, allow_empty=True)
            if p.get("caption")
            else ""
        )
        doc = p["document"]
        fid = f"zz-sent-{len(self.files)}"
        self.files[fid] = doc["content"]
        mid = next(self.message_ids)
        self.messages[(chat_id, mid)] = {
            "from": "bot",
            "text": caption,
            "markup": None,
            "document": {
                "file_id": fid,
                "file_unique_id": fid + "u",
                "file_name": doc["filename"],
                "file_size": len(doc["content"]),
            },
        }
        return self._message(chat_id, mid, self.messages[(chat_id, mid)])

    async def m_getFile(self, p):
        fid = p["file_id"]
        if fid not in self.files:
            raise BadRequest("Bad Request: invalid file_id")
        return {
            "file_id": fid,
            "file_unique_id": fid + "u",
            "file_size": len(self.files[fid]),
            "file_path": fid,
        }

    def app(self):
        application = web.Application(client_max_size=50 * 1024 * 1024)
        application.router.add_post("/bot{token}/{method}", self.handle)
        application.router.add_get("/bot{token}/{method}", self.handle)
        application.router.add_get("/file/bot{token}/{file_id}", self.file)
        return application
