"""End-to-end test: runs the real wallet_tracker.py (a copy, unchanged, as its own process) against a
local Bot API simulator (telegram_sim.py), and plays a user step by step. Hypurrscan and Hyperliquid
are the real, live APIs, so it needs internet; no bot token and none of your data are used.

    python tests/e2e/run_e2e.py            # everything, about 45 minutes (real 10 and 15 min waits)
    python tests/e2e/run_e2e.py --fast     # without the waits, about 3 minutes

Prints PASS/FAIL per check and exits with 1 if anything failed. Its files go to a temporary
folder, deleted when everything passed (kept, and its path printed, when something failed).
Set WT_DIR to test the wallet_tracker.py of another folder (default: this repo)."""

import asyncio
import csv
import hashlib
import io
import json
import os
import pickle
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from pathlib import Path

from aiohttp import web
from telegram_sim import BotApiSim

HERE = Path(__file__).resolve().parent
SOURCE = Path(
    os.environ.get("WT_DIR") or HERE.parents[1]
)  # Where the bot under test is
WORK = Path(
    tempfile.mkdtemp(prefix="zz-wt-e2e-")
)  # The bot copy and its own bot_data.pkl
FAST = "--fast" in sys.argv[1:]
ME, GROUP = 111, -100500
ADDR_A, ADDR_B = "0x" + "a1" * 20, "0x" + "b2" * 20
# A real Hyperliquid wallet with perps and spot balances (for /wallet)
BUSY = "0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36"
NOT_MODIFIED = "message is not modified"
results = []


def hyperliquid(body):
    request = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def trades_between(address, start, end):
    """How many trades a wallet made from start to end (seconds), asked from Hyperliquid."""
    body = {"type": "userFillsByTime", "user": address}
    return len(
        hyperliquid(
            {**body, "startTime": int(start * 1000), "endTime": int(end * 1000)}
        )
    )


def pick_active_wallets(wanted=3):
    """Wallets that trade all the time: alerts are only sent when a wallet traded, so the alert
    test needs some, and several, so one going quiet doesn't leave the check without an alert.
    Taken from the latest public trades: those that traded in at least 20 of the last 30
    minutes, without being so busy that a summary gets huge. Not BUSY: the scenario saves that
    one under another name, and alerts are kept per address."""
    picked = []
    seen = Counter()
    for coin in ("BTC", "ETH", "SOL", "HYPE"):
        for trade in hyperliquid({"type": "recentTrades", "coin": coin}):
            seen.update(trade.get("users", []))
    since = int((time.time() - 30 * 60) * 1000)
    for address, _ in seen.most_common(25):
        if address.lower() == BUSY:
            continue
        fills = hyperliquid(
            {"type": "userFillsByTime", "user": address, "startTime": since}
        )
        minutes = {f["time"] // 60_000 for f in fills}
        if len(minutes) >= 20 and len(fills) < 3000:
            print(f"Alert test wallet: {address} ({len(fills)} trades in 30 min)")
            picked.append(address)
            if len(picked) == wanted:
                break
    if not picked:
        raise SystemExit("No steadily trading wallet found right now; try again later")
    return picked


async def no_alert_detail(addresses, start, end):
    # Without trades there's nothing to report: then a missing alert proves nothing.
    # Asked on a thread, so the simulated Telegram keeps answering the bot meanwhile.
    count = await asyncio.to_thread(
        lambda: sum(trades_between(address, start, end) for address in addresses)
    )
    if count == 0:
        return "inconclusive: the test wallets made no trades in that window"
    return f"the wallets made {count} trades in that window, but no alert came"


def check(item, name, ok, detail=""):
    results.append((item, name, bool(ok), detail))
    line = f"{'PASS' if ok else 'FAIL'} [{item}] {name}"
    print(line + (f" -- {detail}" if detail and not ok else ""), flush=True)


def fp(prefix, alias):
    # The bot's button_data() for one alias
    return f"{prefix}:" + hashlib.sha256(alias.encode()).hexdigest()[:16]


def all_buttons(message):
    rows = (message["markup"] or {}).get("inline_keyboard", [])
    return [b for row in rows for b in row]


def buttons(message):
    return [b.get("callback_data") or b.get("url") for b in all_buttons(message)]


def labels(message):
    return [b["text"] for b in all_buttons(message)]


def data_of(message, label):
    """The callback data of the button whose text contains label."""
    return next(b["callback_data"] for b in all_buttons(message) if label in b["text"])


class Run:
    def __init__(self, sim, url):
        self.sim, self.url, self.proc, self.starts = sim, url, None, 0

    def start_bot(self):
        self.starts += 1
        env = {**os.environ, "BOT_TOKEN": "123456:zz-sim-token"}
        self.log = open(WORK / f"bot_output_{self.starts}.txt", "w", encoding="utf-8")  # noqa: SIM115
        # -u: every printed line reaches the output file at once, also when the bot is killed
        command = [sys.executable, "-B", "-u", str(HERE / "start_bot.py")]
        command += [str(WORK / "wallet_tracker.py"), self.url]
        self.proc = subprocess.Popen(
            command, cwd=WORK, env=env, stdout=self.log, stderr=subprocess.STDOUT
        )

    def kill_bot(self):  # Like closing the window: no clean shutdown
        self.proc.kill()
        self.proc.wait()
        self.log.close()

    async def until(self, condition, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            value = condition()
            if value:
                return value
            await asyncio.sleep(0.2)
        return None

    def last_mid(self, chat=ME):
        msgs = self.sim.bot_messages(chat)
        return msgs[-1][0] if msgs else 0

    async def reply(self, after, contains=None, chat=ME, timeout=40):
        """The first bot message after message id `after` (whose text contains `contains`)."""

        def find():
            for mid, m in self.sim.bot_messages(chat):
                if mid > after and (
                    contains is None
                    or contains in m["text"]
                    or contains in (m.get("html") or "")
                ):
                    return mid, m
            return None

        return await self.until(find, timeout)

    async def say(self, text, contains=None, timeout=40, chat=ME, kind="private"):
        mark = self.last_mid(chat)
        self.sim.user_text(text, chat_id=chat if chat != ME else None, chat_kind=kind)
        return await self.reply(mark, contains, chat, timeout)

    async def tap(self, mid, data, contains=None, timeout=40):
        mark = self.last_mid()
        qid = self.sim.tap(mid, data)
        await self.until(lambda: self.sim.callbacks[qid]["answered"], 15)
        found = await self.reply(mark, contains, timeout=timeout) if contains else None
        return qid, found

    def answered(self, qid):
        return self.sim.callbacks[qid]["answered"]

    def answer_text(self, qid):
        return (self.sim.callbacks[qid]["answer"] or {}).get("text", "")

    def edited(self, mid):
        return self.sim.messages[(ME, mid)]

    def went_wrong_after(self, mid):
        return any(
            "Something went wrong" in m["text"]
            for m_id, m in self.sim.bot_messages()
            if m_id > mid
        )


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


async def startup_checks():
    """Starting while Telegram can't be reached yet (no internet at boot, say): the bot should wait
    and start by itself once Telegram answers. A wrong token should still stop it at once."""

    def start(name, port):
        folder = WORK / name
        folder.mkdir()
        shutil.copy(SOURCE / "wallet_tracker.py", folder / "wallet_tracker.py")
        log = open(folder / "bot_output.txt", "w", encoding="utf-8")  # noqa: SIM115
        command = [sys.executable, "-B", "-u", str(HERE / "start_bot.py")]
        command += [str(folder / "wallet_tracker.py"), f"http://127.0.0.1:{port}"]
        env = {**os.environ, "BOT_TOKEN": "123456:zz-sim-token"}
        proc = subprocess.Popen(
            command, cwd=folder, env=env, stdout=log, stderr=subprocess.STDOUT
        )
        return proc, log, folder / "bot_output.txt"

    def stop(proc, log):
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        log.close()

    port = free_port()
    proc, log, output = start("startup_offline", port)
    await asyncio.sleep(10)  # Nothing answers on that port: Telegram can't be reached
    waited = proc.poll() is None
    sim = BotApiSim()
    runner = web.AppRunner(sim.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()  # Now Telegram answers
    end = time.time() + 60
    while time.time() < end and "Connected" not in output.read_text(encoding="utf-8"):
        await asyncio.sleep(0.5)
    text = output.read_text(encoding="utf-8")
    stop(proc, log)
    await runner.cleanup()
    check(
        "U13",
        "Telegram can't be reached when the bot starts: it keeps waiting instead of stopping",
        waited,
        text[-300:],
    )
    check(
        "U13",
        "once Telegram answers, it starts by itself and says it's connected",
        "✅ Connected to Telegram: the bot is running" in text and sim.commands,
        text[-300:],
    )

    async def unauthorized(request):  # What Telegram answers to a wrong token
        body = {"ok": False, "error_code": 401, "description": "Unauthorized"}
        return web.json_response(body, status=401)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", unauthorized)
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    proc, log, output = start("startup_wrong_token", port)
    end = time.time() + 30
    while time.time() < end and proc.poll() is None:
        await asyncio.sleep(0.2)
    stopped = proc.poll() is not None
    stop(proc, log)
    await runner.cleanup()
    text = output.read_text(encoding="utf-8")
    check(
        "U13",
        "a wrong token still stops it at once, saying the token is invalid",
        stopped and "Invalid token" in text,
        text[-300:],
    )


async def main():
    await startup_checks()
    # The real bot, unchanged, with no .env next to it
    shutil.copy(SOURCE / "wallet_tracker.py", WORK / "wallet_tracker.py")
    sim = BotApiSim()
    runner = web.AppRunner(sim.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    r = Run(sim, url)
    r.start_bot()
    try:
        await scenario(r, sim)
    except Exception as e:  # noqa: BLE001 - reported as a failed check
        check(
            "harness",
            "the scenario ran to the end",
            False,
            f"{type(e).__name__}: {e}"[:300],
        )
    finally:
        if r.proc and r.proc.poll() is None:
            r.kill_bot()
        await runner.cleanup()

    # A repeated tap redraws a menu that already shows the right thing; Telegram refuses that
    # edit and the bot ignores it on purpose (edit_menu). Any other refusal is a bug.
    refused = [e for e in sim.errors if NOT_MODIFIED not in e[1].lower()]
    for method, error, params in refused:
        shown = {k: (v if k != "document" else "<file>") for k, v in params.items()}
        print(f"  refused: {method}: {error} | {str(shown)[:300]}")
    check(
        "TL",
        f"Telegram would have accepted every request ({len(sim.log)} requests, "
        f"{len(sim.errors) - len(refused)} harmless 'not modified' edits)",
        not refused,
        f"{len(refused)} refused",
    )
    tracebacks = []
    for out in sorted(WORK.glob("bot_output_*.txt")):
        text = out.read_text(encoding="utf-8", errors="replace")
        pattern = r"Traceback \(most recent call last\):.*?(?=\n\S|\Z)"
        for block in re.findall(pattern, text, re.DOTALL):
            tracebacks.append(f"{out.name}: " + " | ".join(block.splitlines()[-3:]))
    for t in tracebacks:
        print(f"  traceback: {t[:400]}")
    check("core", "no errors in the bot's own output", not tracebacks)
    printed = "".join(
        out.read_text(encoding="utf-8", errors="replace")
        for out in WORK.glob("bot_output_*.txt")
    )
    check(
        "U11",
        "users' messages aren't printed, and no control codes reach the terminal",
        "zz-hello" not in printed
        and "zz spam" not in printed
        and "\x1b" not in printed,
    )

    passed = sum(1 for *_, ok, _ in results if ok)
    print(f"\n==== {passed}/{len(results)} checks passed ====")
    for item, name, ok, detail in results:
        if not ok:
            print(f"  FAIL [{item}] {name}: {detail}")
    if passed == len(results):
        shutil.rmtree(WORK, ignore_errors=True)
        return 0
    print(f"\nFiles kept for a look: {WORK}")
    return 1


async def scenario(r, sim):
    # The lookup takes a few seconds; on a thread, so the simulated Telegram keeps answering
    # the bot meanwhile (the bot gives up on a request after 5 s)
    actives = (
        ["0x" + "d4" * 20] if FAST else await asyncio.to_thread(pick_active_wallets)
    )
    mm_names = [f"zz mm {i}" for i in range(1, len(actives) + 1)]

    async def alert_after(mark, timeout):
        # An alert summary for one of the test wallets, after message id `mark`
        def find():
            for mid, m in sim.bot_messages():
                html_text = m.get("html") or ""
                if mid > mark and any(
                    f"{n}</b> traded since" in html_text for n in mm_names
                ):
                    return mid, m
            return None

        return await r.until(find, timeout)

    # ---- Startup ----
    started = await r.until(lambda: sim.commands, 60)
    names = [c["command"] for c in sim.commands]
    wanted = {"wallet", "alerts", "rename_alias", "export_aliases", "import_aliases"}
    check(
        "W11",
        "bot sets its own menu commands on startup",
        started and wanted | {"help"} <= set(names),
        names,
    )

    # ---- /start and the menu ----
    found = await r.say("/start", "Welcome")
    check(
        "W12",
        "welcome says 'Hyperliquid Wallet Tracker'",
        found and "Hyperliquid Wallet Tracker" in found[1]["text"],
    )
    check(
        "U6",
        "the welcome points new users to /help",
        found and "New here? Send /help" in found[1]["text"],
    )
    menu_mid, menu = found
    check("W12", "menu has 8 buttons", len(buttons(menu)) == 8, buttons(menu))
    q1, found = await r.tap(menu_mid, "Find token top holders", "Which token")
    check(
        "W22", "question has a Cancel button", found and "Cancel" in buttons(found[1])
    )
    q2, found = await r.tap(
        menu_mid, "View registered aliases", "register an alias first"
    )
    check(
        "W1",
        "second tap on the same (older) menu works and is answered",
        r.answered(q1) and r.answered(q2) and found,
    )

    # ---- Top holders (live Hypurrscan) ----
    actions_before = sum(1 for m, _ in sim.log if m == "sendChatAction")
    await r.say("/get_top_holders", "Which token")
    found = await r.say(" usdc ", "Top USDC Holders", timeout=60)
    check("W4", "' usdc ' with spaces finds USDC", found)
    if found:
        lmid, listing = found
        text = listing["text"]
        check(
            "W18",
            "15 holders on the first page",
            len(re.findall(r"^\d+\. ", text, re.MULTILINE)) == 15,
            text[:200],
        )
        check(
            "W17",
            "total holders and data time shown",
            re.search(r"1-15 of [\d,]+", text) and "Data from" in text,
            text[:120],
        )
        check(
            "W16",
            "#1 USDC holder marked as a system address",
            "token system address" in text,
        )
        check(
            "W21",
            "addresses are tap-to-copy with a Hypurrscan link",
            "<code>0x" in listing["html"]
            and 'href="https://hypurrscan.io/address/' in listing["html"],
        )
        check(
            "W25",
            "'typing…' shown while waiting",
            sum(1 for m, _ in sim.log if m == "sendChatAction") > actions_before,
        )
        await r.tap(lmid, "th:USDC:15")
        await r.until(lambda: "16-30" in r.edited(lmid)["text"], 30)
        check(
            "W32",
            "Next ▶ edits the same message to 16-30",
            "16-30" in r.edited(lmid)["text"],
        )
        await r.tap(lmid, "th:USDC:0")
        await r.until(lambda: "1-15" in r.edited(lmid)["text"], 30)
        check("W32", "◀ Previous goes back to 1-15", "1-15" in r.edited(lmid)["text"])
    found = await r.until(
        lambda: next(
            (
                (mid, m)
                for mid, m in reversed(sim.bot_messages())
                if "find the top holders of another token" in m["text"]
            ),
            None,
        ),
        10,
    )
    if found:
        await r.tap(found[0], "Continue", "Which token")
    found = await r.say("zznotatoken", "No token called")
    check(
        "W13",
        "unknown token gets a clear message",
        found and "ZZNOTATOKEN" in found[1]["text"],
    )
    if found:
        deletes_before = sum(1 for m, _ in sim.log if m == "deleteMessage")
        _, cancelled = await r.tap(found[0], "Cancel", "Operation cancelled")
        check(
            "W22",
            "Cancel button ends the question",
            cancelled
            and sum(1 for m, _ in sim.log if m == "deleteMessage") > deletes_before,
        )
    await r.say("/get_top_holders", "Which token")
    found = await r.say("btc", "No token called")
    check(
        "U7",
        "'btc' isn't a spot token name: UBTC is suggested as a button",
        found
        and "tk:UBTC" in buttons(found[1])
        and any("Unit Bitcoin" in label for label in labels(found[1])),
        found and labels(found[1]),
    )
    if found and "tk:UBTC" in buttons(found[1]):
        _, shown = await r.tap(found[0], "tk:UBTC", "Top UBTC Holders", timeout=60)
        check("U7", "tapping UBTC shows its top holders (live)", shown)
        again = await r.until(
            lambda: next(
                (
                    (mid, m)
                    for mid, m in reversed(sim.bot_messages())
                    if "another token" in m["text"] and "Cancel" in buttons(m)
                ),
                None,
            ),
            10,
        )
        if again:
            await r.tap(again[0], "Cancel", "Operation cancelled")
    found = await r.say("zz-hello", "/start")
    check("W24", "text with no question open gets a pointer to /start", found)
    mark = r.last_mid()
    for i in range(
        5
    ):  # Quick messages, with terminal control codes and a fake log line
        sim.user_text(f"zz spam {i}\x1b]0;zz-HIJACKED\x07\nzz-User wrote fake")
    await asyncio.sleep(4)
    replies = [m["text"][:40] for mid, m in sim.bot_messages() if mid > mark]
    check(
        "U11",
        "5 quick messages get at most one reply (one hint a minute)",
        len(replies) <= 1,
        replies,
    )

    # ---- Public names and search (live) ----
    found = await r.say("/get_global_aliases", "Public names on Hypurrscan", timeout=60)
    if found:
        gmid, gmsg = found
        check(
            "W8",
            "public names page says '1-30 of N'",
            re.search(r"1-30 of \d+", gmsg["text"]),
            gmsg["text"][:80],
        )
        mark = r.last_mid()
        # A quick double tap: both taps carry the same button, from before the page changed
        double = [sim.tap(gmid, "ga:30"), sim.tap(gmid, "ga:30")]
        await r.until(lambda: all(r.answered(q) for q in double), 20)
        await r.until(lambda: "31-60" in r.edited(gmid)["text"], 30)
        await asyncio.sleep(2)
        check("W8/W32", "Next ▶ shows 31-60", "31-60" in r.edited(gmid)["text"])
        check(
            "W33",
            "a quick double tap on Next ▶ is fine (no 'Something went wrong')",
            all(r.answered(q) for q in double) and not r.went_wrong_after(mark),
        )
    else:
        check("W8", "public names page", False, "no reply")
    await r.say("/search_alias", "Send an address")
    found = await r.say("hyperevm", "public name(s) contain", timeout=60)
    check(
        "W6/W29",
        "search by name finds addresses",
        found and "HyperEVM" in found[1]["text"] and "0x" in found[1]["text"],
    )
    if found:
        _, again = await r.tap(found[0], "Continue", "Send an address")
        check(
            "W14",
            "search-again prompt has no '?)?' typo",
            again and ")?" not in again[1]["text"],
        )
    found = await r.say("zzzz-nothing", "No address or public name matches", timeout=60)
    check("W7", "search that matches nothing says so", found)
    await r.say("/cancel", "Operation cancelled")

    # ---- Register, replace, rename ----
    await r.say("/register_alias", "valid address")
    found = await r.say("0x123", "not a valid address")
    check("core", "invalid address is refused", found)
    await r.say(ADDR_A, "name this address")
    found = await r.say("zz Main Wallet", "Registered aliases (1)")
    check(
        "W10/W26",
        "alias saved with its capitals, list counted",
        found and "zz Main Wallet" in found[1]["text"],
    )
    cont = await r.until(
        lambda: next(
            (
                (mid, m)
                for mid, m in reversed(sim.bot_messages())
                if "register another address" in m["text"]
            ),
            None,
        ),
        10,
    )
    await r.tap(cont[0], "Continue", "valid address")
    await r.say(ADDR_B, "name this address")
    found = await r.say("ZZ MAIN WALLET", "already points to")
    check(
        "W9",
        "reusing a name asks before replacing",
        found and set(buttons(found[1])) >= {"Replace", "Choose another name"},
    )
    _, found = await r.tap(found[0], "Choose another name", "name this address")
    found = await r.say("zz Second", "Registered aliases (2)")
    check(
        "W9",
        "'Choose another name' keeps the old alias",
        found
        and "zz Main Wallet" in found[1]["text"]
        and "zz Second" in found[1]["text"],
    )
    found = await r.say("/view_aliases", "Registered aliases (2)")
    check(
        "W26",
        "list is sorted",
        found
        and found[1]["text"].index("zz Main Wallet")
        < found[1]["text"].index("zz Second"),
    )
    view_buttons = {
        "Rename an alias",
        "Wallet alerts",
        "Export aliases (CSV)",
        "Import aliases (CSV)",
    }
    check(
        "W30",
        "view has Rename/Alerts/Export/Import buttons",
        found and view_buttons <= set(buttons(found[1])),
    )
    _, ask = await r.tap(
        found[0], "Rename an alias", "Which alias would you like to rename"
    )
    _, ask = await r.tap(ask[0], fp("rn", "zz Second"), "be called now")
    found = await r.say("zz main wallet", "already used")
    check("W30", "rename to a name in use is refused", found)
    found = await r.say("zz Renamed", "Renamed")
    check("W30", "rename works", found)

    # ---- Export and import ----
    sends = len([1 for m, _ in sim.log if m == "sendDocument"])
    await r.say("/export_aliases")
    await r.until(
        lambda: len([1 for m, _ in sim.log if m == "sendDocument"]) > sends, 20
    )
    docs = [p for m, p in sim.log if m == "sendDocument"]
    content = docs[-1]["document"]["content"].decode("utf-8-sig") if docs else ""
    rows = list(csv.reader(io.StringIO(content)))
    check(
        "W30",
        "export sends aliases.csv with both aliases",
        docs
        and docs[-1]["document"]["filename"] == "aliases.csv"
        and rows[0] == ["alias", "address"]
        and len(rows) == 3,
        rows,
    )
    await r.say("/import_aliases", "Send me a CSV file")
    found = await r.say("zz not a file", "send the CSV as a file")
    check("W30", "text instead of a file is refused", found)
    upload = content + "zz Imported,0x" + "c3" * 20 + "\r\nzz Bad,not-an-address\r\n"
    mark = r.last_mid()
    sim.user_document("aliases.csv", upload.encode("utf-8-sig"))
    found = await r.reply(mark, "Import finished")
    report = found[1]["text"] if found else ""
    check(
        "W30",
        "import adds new rows and reports problems",
        "1 new alias(es) added" in report
        and "2 were already saved" in report
        and "row 5" in report,
        report,
    )
    await r.say("/search_alias", "Send an address")
    found = await r.say("renamed", "of your aliases contain", timeout=60)
    check("W30", "search finds your own alias", found)
    await r.say("/cancel", "Operation cancelled")

    # ---- A long list (W2) ----
    many = "alias,address\r\n" + "".join(
        f"zz-long-{i:03d},0x{i:040x}\r\n" for i in range(80)
    )
    await r.say("/import_aliases", "Send me a CSV file")
    mark = r.last_mid()
    sim.user_document("many.csv", many.encode())
    await r.reply(mark, "Import finished")
    mark = r.last_mid()
    sim.user_text("/view_aliases")
    await r.until(
        lambda: sum(1 for mid, m in sim.bot_messages() if mid > mark) >= 2, 30
    )
    await asyncio.sleep(2)
    parts = [m["text"] for mid, m in sim.bot_messages() if mid > mark]
    joined = "\n".join(parts)
    check(
        "W2",
        "84-alias list split over several messages, nothing missing",
        len(parts) >= 2 and all(f"zz-long-{i:03d}" in joined for i in range(80)),
        f"{len(parts)} parts",
    )

    # ---- Wallet (live Hyperliquid) ----
    await r.say("/register_alias", "valid address")
    await r.say(BUSY, "name this address")
    await r.say("zz busy", "Registered aliases")
    found = await r.say("/wallet zz busy", "<b>Perps</b>", timeout=60)
    check(
        "W28",
        "/wallet shows account value, positions and spot",
        found and "account value $" in found[1]["text"] and "Spot" in found[1]["text"],
        found and found[1]["text"][:200],
    )
    found = await r.say("/wallet", "Which wallet")
    if found:
        _, shown = await r.tap(found[0], fp("w", "zz busy"), "Perps", timeout=60)
        check("W28", "/wallet then tapping a saved wallet works", shown)
    found = await r.say("/wallet zz-nobody", "No saved alias or address matches")
    check("W28", "/wallet with an unknown name is asked again", found)
    await r.say("/cancel", "Operation cancelled")
    for name, address in zip(mm_names, actives, strict=True):
        await r.say("/register_alias", "valid address")
        await r.say(address, "name this address")
        await r.say(name, "Registered aliases")
        await r.say(
            "/cancel", "Operation cancelled"
        )  # Closes "register another address?"

    # ---- The alerts menu has pages: 85 saved wallets, 50 per page (W34) ----
    found = await r.say("/alerts", "Wallet alerts")
    amid, menu = found
    total = re.search(r"\(1-50 of (\d+)\)", menu["text"])
    check(
        "W34",
        "with more than 50 wallets the menu says '1-50 of N' and has Next ▶",
        total and int(total[1]) > 50 and "ap:50" in buttons(menu),
        menu["text"][:80],
    )
    total = int(total[1]) if total else 0
    second_page = f"(51-{total} of {total})"
    await r.tap(amid, "ap:50")
    await r.until(lambda: second_page in r.edited(amid)["text"], 15)
    page = r.edited(amid)
    check(
        "W34",
        "Next ▶ shows the rest, with ◀ Previous",
        second_page in page["text"]
        and "🔕 zz-long-079" in labels(page)
        and "ap:0" in buttons(page),
        page["text"][:80],
    )
    await r.tap(amid, fp("al", "zz-long-079"))
    await r.until(lambda: "🔔 zz-long-079" in labels(r.edited(amid)), 15)
    check(
        "W34",
        "a wallet on the second page can be switched on, and the menu stays there",
        "🔔 zz-long-079" in labels(r.edited(amid))
        and second_page in r.edited(amid)["text"],
    )
    await r.tap(amid, fp("al", "zz-long-079"))  # And off again: a made-up address
    await r.until(lambda: "🔕 zz-long-079" in labels(r.edited(amid)), 15)
    await r.tap(amid, "ap:0")
    await r.until(lambda: "(1-50 of" in r.edited(amid)["text"], 15)

    # ---- Alerts on, every 15 minutes ----
    answers = []
    for name in mm_names:
        qa, _ = await r.tap(amid, fp("al", name))
        answers.append(r.answer_text(qa))
    check(
        "W28",
        "switching alerts on is confirmed",
        all(f"Alerts on for {n}" in a for n, a in zip(mm_names, answers, strict=True)),
        answers,
    )
    await r.tap(amid, data_of(r.edited(amid), "Every 15 min"))
    await r.until(lambda: "15 min</b>" in (r.edited(amid).get("html") or ""), 10)
    check(
        "W28",
        "choosing 'every 15 min' updates the menu",
        "Checked every <b>15 min</b>" in (r.edited(amid).get("html") or ""),
    )
    alerts_on_at, msgs_before = time.time(), r.last_mid()
    qi, _ = await r.tap(amid, data_of(r.edited(amid), "Every 15 min"))  # Again
    await asyncio.sleep(3)
    check(
        "W33",
        "tapping the interval already chosen says so, with no 'Something went wrong'",
        r.answer_text(qi) == "Already checking every 15 min"
        and not r.went_wrong_after(msgs_before),
        r.answer_text(qi),
    )

    # ---- Group chat (option A) ----
    found = await r.say(
        "/view_aliases@zz_sim_bot",
        "only work in private chats",
        chat=GROUP,
        kind="supergroup",
    )
    check(
        "Groups",
        "command for this bot in a group points to a private chat",
        found
        and "https://t.me/zz_sim_bot" in buttons(found[1])
        and "zz Main" not in found[1]["text"],
    )
    mark = r.last_mid(GROUP)
    sim.user_text("zz gm everyone", chat_id=GROUP, chat_kind="supergroup")
    sim.user_text("/start@zz_other_bot", chat_id=GROUP, chat_kind="supergroup")
    await asyncio.sleep(4)
    check(
        "Groups",
        "group chatter and other bots' commands are ignored",
        r.last_mid(GROUP) == mark,
    )
    old_menu = {"text": "Check a wallet", "callback_data": "Check a wallet"}
    sim.messages[(GROUP, 1)] = {
        "from": "bot",
        "text": "zz old menu",
        "markup": {"inline_keyboard": [[old_menu]]},
    }
    qg = sim.tap(1, "Check a wallet", chat_id=GROUP, chat_kind="supergroup")
    await r.until(lambda: sim.callbacks[qg]["answered"], 15)
    check(
        "Groups",
        "button tap in a group gets a private-chat pop-up",
        "private chats" in r.answer_text(qg),
    )

    # ---- Replies instead of silence, old buttons, edited messages ----
    found = await r.say("/help", "/register_alias")
    check("W24", "/help lists the commands", found)
    guide = "\n".join(
        m["text"] for mid, m in sim.bot_messages() if found and mid >= found[0]
    )
    check(
        "U6",
        "/help explains where to find wallet addresses and the token names",
        all(
            part in guide
            for part in (
                "Finding wallet addresses",
                "leaderboard",
                "UBTC",
                "Unit Bitcoin",
            )
        ),
        guide[-300:],
    )
    found = await r.say("/cancel", "nothing to cancel")
    check("W24", "/cancel with nothing open", found)
    found = await r.say("/zz_unknown", "don't know that command")
    check("W24", "unknown command", found)
    old_continue = next(
        (
            mid
            for mid, m in sim.bot_messages()
            if "search again" in m["text"] and "Continue" in buttons(m)
        ),
        None,
    )
    if old_continue:
        qo = sim.tap(old_continue, "Continue")
        await r.until(lambda: sim.callbacks[qo]["answered"], 15)
        check(
            "W1",
            "Continue on an old, finished question says it expired",
            "expired" in r.answer_text(qo),
            r.answer_text(qo),
        )
    mark = r.last_mid()
    now = int(time.time())
    sim._push(
        "edited_message",
        {
            "message_id": 5,
            "date": now,
            "edit_date": now,
            "chat": sim.chat(ME),
            "from": sim.user(ME),
            "text": "zz edited",
        },
    )
    await asyncio.sleep(3)
    check("core", "an edited message causes no reply or error", r.last_mid() == mark)

    if FAST:
        print("--fast: skipping the real-time waits (W3 timeout, alerts, restart)")
        return

    # ---- W3: an answer 10+ minutes late ----
    await r.say("/get_top_holders", "Which token")
    asked_at = time.time()
    print("... waiting 10.5 minutes for the question to time out (W3)", flush=True)
    await asyncio.sleep(max(0, 630 - (time.time() - asked_at)))
    found = await r.say("USDC", "I asked that more than 10 minutes ago")
    check("W3", "an answer 10+ minutes late isn't taken", found)

    # ---- The first alert, 15 minutes after switching on ----
    print("... waiting for the first alert (15 min after switching on)", flush=True)
    wait = max(60, 900 + 120 - (time.time() - alerts_on_at))
    alert = await alert_after(msgs_before, wait)
    check(
        "W28",
        "alert summary arrives after 15 minutes",
        alert,
        ""
        if alert
        else await no_alert_detail(actives, alerts_on_at, alerts_on_at + 900),
    )
    first_check_at = time.time() if alert else alerts_on_at + 900
    if alert:
        check(
            "W28",
            "summary has one line per coin with trades",
            re.search(r"• .* (perp|spot) · \d+ trade\(s\)", alert[1]["text"]),
            alert[1]["text"][:300],
        )

    # ---- W27: kill the bot with a question open, start it again ----
    await r.say("/get_top_holders", "Which token")
    await asyncio.sleep(7)  # Data is saved every 5 seconds
    r.kill_bot()
    r.start_bot()
    restarted_at = time.time()
    await r.until(lambda: len([1 for m, _ in sim.log if m == "getMe"]) >= 2, 60)
    await asyncio.sleep(3)
    found = await r.say("USDC", "Top USDC Holders", timeout=60)
    check("W27", "a question survives a hard kill and restart", found)
    found = await r.say("/view_aliases", "Registered aliases")
    check(
        "core",
        "saved aliases survive a hard kill",
        found and "zz Renamed" in found[1]["text"],
    )
    print("... waiting for an alert after the restart", flush=True)
    mark = r.last_mid()
    alert = await alert_after(mark, 900 + 180)
    check(
        "W28",
        "alerts start again after a restart",
        alert,
        ""
        if alert
        else await no_alert_detail(actives, first_check_at, restarted_at + 900),
    )

    # ---- Removing the alias stops its alerts ----
    found = await r.say("/remove_alias", "Which alias would you like to remove")
    _, removed = await r.tap(found[0], fp("rm", mm_names[0]), "has been removed")
    check("W23", "an alias can be removed by tapping it", removed)
    await asyncio.sleep(7)
    # Safe to unpickle: the bot_data.pkl written by this test's own copy of the bot
    data = pickle.loads((WORK / "bot_data.pkl").read_bytes())
    user = data["user_data"][ME]
    check(
        "W28",
        "removing the alias switched its alerts off (and only its)",
        actives[0] not in user.get("alert_wallets", {})
        and all(a in user.get("alert_wallets", {}) for a in actives[1:]),
        user.get("alert_wallets"),
    )
    check(
        "W22",
        "no half-finished answers left in saved data",
        not {"address", "new_alias", "rename_from"} & set(user),
        sorted(user),
    )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"WalletTracker end-to-end test of {SOURCE} (files in {WORK})")
    sys.exit(asyncio.run(main()))
