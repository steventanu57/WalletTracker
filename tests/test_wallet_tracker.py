"""End-to-end tests for wallet_tracker.py (fake Telegram + fake Hypurrscan, no network).
Test data uses the zz- prefix."""

import contextlib
import csv
import io
import os
import re
import subprocess
import sys
import time
import warnings
from unittest import mock

import aiohttp
import harness
from harness import GROUP, BotRun, FakeHypurrscan, bot_dir
from telegram.warnings import PTBUserWarning

ADDR = "0x" + "ab" * 20  # a well-formed 42-character address
ADDR2 = "0x" + "cd" * 20
ALIASES_URL = "api.hypurrscan.io/globalAliases"


def no_errors(run):
    assert run.errors == [], f"handler crashed: {[repr(e) for e in run.errors]}"


# ---------- features that must keep working ----------


def test_start_shows_menu():
    run = BotRun().send("/start")
    no_errors(run)
    assert "Welcome to <b>Hyperliquid Wallet Tracker</b>" in run.telegram.texts()[0]


def test_register_valid_address_is_saved():
    run = BotRun().send("/register_alias", ADDR, "zz-main")
    no_errors(run)
    assert run.saved_user_data()["aliases"] == {"zz-main": ADDR}


def test_top_holders_lists_15():
    holders = {f"0x{i:040x}": 1000.0 - i for i in range(30)}
    run = BotRun(
        {"holdersWithLimit/USDC/": (200, {"holders": holders}), ALIASES_URL: (200, {})}
    ).send("/get_top_holders", "usdc")
    no_errors(run)
    listing = next(
        t for t in run.telegram.texts() if t.startswith("<b>Top USDC Holders")
    )
    assert listing.count("\n") == 15


def test_search_alias_found():
    run = BotRun({ALIASES_URL: (200, {ADDR: "zz-Known Wallet"})}).send(
        "/search_alias", ADDR.upper().replace("0X", "0x")
    )
    no_errors(run)
    assert any("zz-Known Wallet" in t for t in run.telegram.texts())


# ---------- bug fixes ----------


def test_view_aliases_new_user_gets_a_reply():
    run = BotRun().send("/view_aliases")
    no_errors(run)
    assert run.telegram.texts() == ["❌ Please register an alias first"]


def test_remove_last_alias_then_continue():
    run = BotRun(saved_user_data={"aliases": {"zz-only": ADDR}})
    run.send("/remove_alias", "zz-only", ("tap", "Continue"))
    no_errors(run)
    assert "❌ No more aliases left to remove." in run.telegram.texts()


def test_garbage_address_is_rejected():
    run = BotRun().send("/register_alias", "zz-not-an-address", "zz-name")
    no_errors(run)
    assert "aliases" not in run.saved_user_data(), "garbage address was saved"


def test_alias_with_html_characters_still_gets_listed():
    run = BotRun().send("/register_alias", ADDR, "zz-<me&you>")
    no_errors(run)
    assert any("Registered aliases" in t for t in run.telegram.texts())


def test_edited_message_does_not_crash():
    run = BotRun().send(("edit", "zz-edited text"))
    no_errors(run)


def test_editing_a_message_while_bot_waits_for_an_answer():
    run = BotRun().send("/register_alias", ("edit", "zz-edited text"))
    no_errors(run)


def test_start_works_while_a_question_is_open():
    run = BotRun().send("/get_top_holders", "/start")
    no_errors(run)
    assert any("Welcome to" in t for t in run.telegram.texts()), (
        f"/start was ignored; bot replies: {run.telegram.texts()}"
    )


def test_top_holders_sorted_by_balance():
    # API returns smallest first: the bot must still list the biggest holder as #1
    holders = {f"0x{i:040x}": float(i) for i in range(30)}
    run = BotRun(
        {"holdersWithLimit/USDC/": (200, {"holders": holders}), ALIASES_URL: (200, {})}
    ).send("/get_top_holders", "USDC")
    no_errors(run)
    listing = next(
        t for t in run.telegram.texts() if t.startswith("<b>Top USDC Holders")
    )
    lines = [line for line in listing.splitlines() if re.match(r"\d+\. ", line)]
    assert lines[0].startswith(f"1. <code>0x{29:040x}</code>"), lines[0]
    assert "- 29.00 USDC" in lines[0]
    assert lines[-1].startswith(f"15. <code>0x{15:040x}</code>"), lines[-1]
    assert "- 15.00 USDC" in lines[-1]


def test_top_holders_failure_message():
    run = BotRun({"holdersWithLimit/": (500, None)}).send("/get_top_holders", "USDC")
    no_errors(run)
    assert "❌ Failed to fetch top holders." in run.telegram.texts()


def test_network_error_gets_a_reply():
    run = BotRun({ALIASES_URL: aiohttp.ClientConnectionError("zz-offline")}).send(
        "/get_global_aliases"
    )
    no_errors(run)
    assert any(t.startswith("❌") for t in run.telegram.texts()), "user got no reply"


def test_no_leftover_temporary_address_in_saved_data():
    run = BotRun().send("/register_alias", ADDR, "zz-main")
    no_errors(run)
    assert "address" not in run.saved_user_data()


def test_saves_every_5_seconds():
    assert BotRun().app.persistence.update_interval == 5


def test_no_warning_at_startup():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        BotRun()
    assert not [
        str(w.message) for w in caught if issubclass(w.category, PTBUserWarning)
    ]


def test_emoji_name_printed_to_a_log_file(tmp_path):
    probe = tmp_path / "zz_probe.py"
    probe.write_text(
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('wt', {str(bot_dir() / 'wallet_tracker.py')!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "print('zz-Steven \\U0001f680 wrote hi')\n",
        encoding="utf-8",
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONIOENCODING", "PYTHONUTF8")
    }
    with open(tmp_path / "zz-log.txt", "w") as log:
        result = subprocess.run(
            [sys.executable, "-B", str(probe)],
            stdout=log,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
    assert result.returncode == 0, (
        result.stderr.decode(errors="replace").strip().splitlines()[-1]
    )


# ---------- phase 1: bugs, misleading texts, accuracy, speed, UX ----------
HOLDERS = "holdersWithLimit/"


def answered(run):
    return run.telegram.count("answerCallbackQuery")


def test_old_menu_buttons_keep_working():  # W1
    run = BotRun().send(
        "/start", ("tap", "Find token top holders"), ("tap", "View registered aliases")
    )
    no_errors(run)
    assert answered(run) == 2
    assert run.telegram.texts()[-1] == "❌ Please register an alias first"


def test_button_of_an_old_message_is_answered():  # W1
    run = BotRun().send(("tap", "Continue"))
    no_errors(run)
    assert answered(run) == 1


def test_long_alias_list_is_split_under_telegram_limit():  # W2
    many = {f"zz-wallet-{i:03d}": "0x" + f"{i:040x}" for i in range(80)}
    run = BotRun(saved_user_data={"aliases": many}).send("/view_aliases")
    no_errors(run)
    sent = "\n".join(run.telegram.texts())
    assert all(f"zz-wallet-{i:03d}" in sent for i in range(80))


def test_token_with_spaces_is_trimmed():  # W4
    run = BotRun({HOLDERS: (200, {"holders": {ADDR: 5.0}}), ALIASES_URL: (200, {})})
    run.send("/get_top_holders", " usdc ")
    no_errors(run)
    assert any("/holdersWithLimit/USDC/" in url for url in FakeHypurrscan.requested)


def test_token_cannot_change_the_request_path():  # W4
    run = BotRun({HOLDERS: (200, {}), "info:spotMeta": (200, SPOT_META)})
    run.send("/get_top_holders", "usdc/../x")
    no_errors(run)
    assert [u for u in FakeHypurrscan.requested if HOLDERS in u] == [
        "https://api.hypurrscan.io/holdersWithLimit/USDC%2F..%2FX/15"
    ]


def test_unexpected_api_answer_gets_a_reply():  # W5
    run = BotRun({ALIASES_URL: (200, None)}).send("/search_alias", ADDR)
    no_errors(run)
    assert run.telegram.texts()[-1].startswith("❌")
    run = BotRun({HOLDERS: (200, [])}).send("/get_top_holders", "USDC")
    no_errors(run)
    assert run.telegram.texts()[-1] == "❌ Failed to fetch top holders."


def test_crash_in_a_handler_is_reported_to_the_user():  # W5
    run = BotRun(saved_user_data={"aliases": {"zz-main": ADDR}})
    with mock.patch.object(
        run.module.html, "escape", side_effect=RuntimeError("zz-boom")
    ):
        run.send("/view_aliases")
    assert run.errors, "the test needs a crash"
    assert run.telegram.texts()[-1].startswith("❌ Something went wrong")


def test_search_by_name_finds_addresses():  # W6, W29
    run = BotRun({ALIASES_URL: (200, {ADDR: "zz-HyperEVM", ADDR2: "zz-Other"})})
    run.send("/search_alias", "hyperevm")
    no_errors(run)
    reply = run.telegram.texts()[-1]
    assert "zz-HyperEVM" in reply and ADDR in reply and "zz-Other" not in reply


def test_search_that_matches_nothing_says_so():  # W7
    run = BotRun({ALIASES_URL: (200, {ADDR: "zz-Known"})})
    run.send("/search_alias", "zz-nothing")
    no_errors(run)
    assert "No address or public name matches" in run.telegram.texts()[-1]


def test_public_names_list_says_how_many_and_pages():  # W8, W32
    names = {"0x" + f"{i:040x}": f"zz-label-{i}" for i in range(463)}
    run = BotRun({ALIASES_URL: (200, names)}).send(
        "/get_global_aliases", ("tap", "ga:30")
    )
    no_errors(run)
    first, second = run.telegram.texts()
    assert "1-30 of 463" in first and "31-60 of 463" in second
    assert "zz-label-30" in second


def test_reusing_an_alias_name_asks_before_replacing():  # W9
    run = BotRun(saved_user_data={"aliases": {"zz-main": ADDR}})
    run.send("/register_alias", ADDR2, "zz-main")
    no_errors(run)
    assert run.saved_user_data()["aliases"] == {"zz-main": ADDR}
    assert "already points to" in run.telegram.texts()[-1]
    run.send(("tap", "Replace"))
    no_errors(run)
    assert run.saved_user_data()["aliases"] == {"zz-main": ADDR2}


def test_choosing_another_name_keeps_the_old_alias():  # W9
    run = BotRun(saved_user_data={"aliases": {"zz-main": ADDR}})
    run.send(
        "/register_alias",
        ADDR2,
        "zz-main",
        ("tap", "Choose another name"),
        "zz-second",
    )
    no_errors(run)
    assert run.saved_user_data()["aliases"] == {"zz-main": ADDR, "zz-second": ADDR2}


def test_alias_keeps_its_capitals_and_matches_any_case():  # W10
    run = BotRun().send("/register_alias", ADDR, "zz-Main Wallet")
    no_errors(run)
    assert run.saved_user_data()["aliases"] == {"zz-Main Wallet": ADDR}
    run.send("/remove_alias", "ZZ-MAIN WALLET")
    no_errors(run)
    assert run.saved_user_data()["aliases"] == {}


def test_bot_sets_its_menu_commands_on_startup():  # W11
    run = BotRun()
    assert run.app.post_init, "no startup step"
    run.call(run.app.post_init)
    sent = [p for e, p in run.telegram.calls if e == "setMyCommands"]
    assert sent and "register_alias" in str(sent[0]), sent


def test_unknown_token_says_so():  # W13
    run = BotRun({HOLDERS: (200, {}), "info:spotMeta": (200, SPOT_META)})
    run.send("/get_top_holders", "zznotatoken")
    no_errors(run)
    assert "No token called <b>ZZNOTATOKEN</b>" in run.telegram.texts()[-1]


def test_search_again_prompt_has_no_typo():  # W14
    run = BotRun({ALIASES_URL: (200, {ADDR: "zz-Known"})})
    run.send("/search_alias", ADDR, ("tap", "Continue"))
    no_errors(run)
    assert ")?" not in run.telegram.texts()[-1]


def test_terminal_log_names_the_user():  # W15
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        BotRun().send("/start", ("tap", "Remove a registered alias"))
    assert "zz-User failed to remove aliases" in out.getvalue(), out.getvalue()


def test_top_holders_show_names_and_system_addresses():  # W16, W31
    usdc_system = "0x2" + "0" * 39
    holders = {usdc_system: 9.0, ADDR: 8.0, ADDR2: 7.0}
    run = BotRun(
        {
            HOLDERS: (200, {"holders": holders}),
            ALIASES_URL: (200, {ADDR2: "zz-Public"}),
        },
        saved_user_data={"aliases": {"zz-mine": ADDR}},
    ).send("/get_top_holders", "USDC")
    no_errors(run)
    listing = next(t for t in run.telegram.texts() if t.startswith("<b>Top USDC"))
    assert "⭐ zz-mine" in listing and "🏷 zz-Public" in listing
    assert "⚙️ token system address" in listing


def test_top_holders_show_total_and_data_time():  # W17
    data = {"holders": {ADDR: 1.0}, "holdersCount": 1297595, "lastUpdate": 1792492882}
    run = BotRun({HOLDERS: (200, data), ALIASES_URL: (200, {})})
    run.send("/get_top_holders", "USDC")
    no_errors(run)
    listing = next(t for t in run.telegram.texts() if t.startswith("<b>Top USDC"))
    assert "of 1,297,595" in listing and "2026-10-20 10:41 UTC" in listing


def test_top_holders_asks_for_one_page_and_pages_on():  # W18, W32
    holders = {"0x" + f"{i:040x}": float(100 - i) for i in range(30)}
    data = {"holders": holders, "holdersCount": 30}
    run = BotRun({HOLDERS: (200, data), ALIASES_URL: (200, {})})
    run.send("/get_top_holders", "USDC", ("tap", "th:USDC:15"))
    no_errors(run)
    holder_urls = [u for u in FakeHypurrscan.requested if HOLDERS in u]
    assert holder_urls[0].endswith("/USDC/15") and holder_urls[1].endswith("/USDC/30")
    assert "16-30 of 30" in run.telegram.texts()[-1]


def test_public_names_downloaded_once_for_several_searches():  # W19
    run = BotRun({ALIASES_URL: (200, {ADDR: "zz-Known"})})
    run.send(
        "/search_alias", ADDR, ("tap", "Continue"), ADDR, ("tap", "Continue"), ADDR
    )
    no_errors(run)
    assert sum(ALIASES_URL in u for u in FakeHypurrscan.requested) == 1


def test_one_connection_is_reused():  # W20
    routes = {ALIASES_URL: (200, {}), HOLDERS: (200, {})}
    run = BotRun({**routes, "info:spotMeta": (200, SPOT_META)})
    run.send("/get_top_holders", "ZZ1", "ZZ2")
    no_errors(run)
    asked = FakeHypurrscan.requested
    assert len([u for u in asked if HOLDERS in u]) == 2 and FakeHypurrscan.opened == 1
    assert len([u for u in asked if "spotMeta" in u]) == 1  # Kept for an hour


def test_addresses_are_tap_to_copy_with_a_link():  # W21
    run = BotRun(saved_user_data={"aliases": {"zz-main": ADDR}}).send("/view_aliases")
    no_errors(run)
    text = run.telegram.texts()[0]
    assert f"<code>{ADDR}</code>" in text
    assert f'href="https://hypurrscan.io/address/{ADDR}"' in text


def test_questions_have_a_cancel_button():  # W22
    run = BotRun().send("/register_alias", ADDR)
    prompt = [p for e, p in run.telegram.calls if e == "sendMessage"][-1]
    assert "Cancel" in str(prompt.get("reply_markup"))
    run.send(("tap", "Cancel"), ADDR2)
    no_errors(run)
    assert "Operation cancelled." in run.telegram.texts()
    assert "address" not in run.saved_user_data()  # the unfinished answer is forgotten
    assert not run.saved_user_data().get("aliases")  # ADDR2 was not taken as an answer


def test_alias_can_be_removed_by_tapping_it():  # W23
    run = BotRun(saved_user_data={"aliases": {"zz-a": ADDR, "zz-b": ADDR2}})
    run.send("/remove_alias", ("tap", run.module.remove_button_data("zz-a")))
    no_errors(run)
    assert run.saved_user_data()["aliases"] == {"zz-b": ADDR2}


def test_no_more_silence():  # W24
    for text, expected in [
        ("/help", "/register_alias"),
        ("/cancel", "nothing to cancel"),
        ("zz-hello", "/start"),
        ("/zz_unknown", "don't know that command"),
    ]:
        run = BotRun().send(text)
        no_errors(run)
        assert run.telegram.texts() and expected in run.telegram.texts()[-1], (
            text,
            run.telegram.texts(),
        )


def test_typing_is_shown_while_waiting():  # W25
    run = BotRun({HOLDERS: (200, {}), "info:spotMeta": (200, SPOT_META)})
    run.send("/get_top_holders", "ZZ")
    no_errors(run)
    assert run.telegram.count("sendChatAction") == 1


def test_alias_list_is_counted_and_sorted():  # W26
    run = BotRun(saved_user_data={"aliases": {"zz-b": ADDR2, "zz-a": ADDR}})
    run.send("/view_aliases")
    no_errors(run)
    text = run.telegram.texts()[0]
    assert "Registered aliases (2)" in text
    assert text.index("zz-a") < text.index("zz-b")


def test_pages_fit_in_one_message_even_with_very_long_names():  # W8, W16
    long_names = {"0x" + f"{i:040x}": "zz-" + "x" * 500 for i in range(40)}
    run = BotRun({ALIASES_URL: (200, long_names)}).send("/get_global_aliases")
    no_errors(run)
    holders = {address: float(i) for i, address in enumerate(long_names)}
    mine = {f"zz-{i}-" + "y" * 500: address for i, address in enumerate(long_names)}
    run = BotRun(
        {HOLDERS: (200, {"holders": holders}), ALIASES_URL: (200, long_names)},
        saved_user_data={"aliases": mine},
    ).send("/get_top_holders", "USDC")
    no_errors(run)


# ---------- phase 2: timeouts, saved questions, wallets, alerts, alias tools, groups ----------
WALLET = "0x" + "ef" * 20
PERPS = {
    "time": 1792492882000,
    "marginSummary": {"accountValue": "12345.678"},
    "withdrawable": "100.5",
    "assetPositions": [
        {
            "type": "oneWay",
            "position": {
                "coin": "BTC",
                "szi": "-0.5",
                "entryPx": "78018.6",
                "positionValue": "39000.0",
                "unrealizedPnl": "-120.25",
                "returnOnEquity": "-0.0312",
                "leverage": {"type": "cross", "value": 5},
                "liquidationPx": "146670.9456",
            },
        }
    ],
}
SPOT = {
    "balances": [
        {"coin": "USDC", "total": "250.5"},
        {"coin": "HYPE", "total": "12.3456789"},
        {"coin": "PURR", "total": "0.0"},
    ]
}
WALLET_ROUTES = {
    "info:clearinghouseState": (200, PERPS),
    "info:spotClearinghouseState": (200, SPOT),
}
SPOT_META = {
    "universe": [{"name": "@107", "tokens": [150, 0], "index": 107}],
    "tokens": [{"name": "USDC", "index": 0}, {"name": "HYPE", "index": 150}],
}
LATER = 10_000_000.0  # A fixed "now" for alert tests (seconds)


def fill(tid, coin, side, size, px, start, pnl="0.0", time_ms=2000):
    return {
        "tid": tid,
        "coin": coin,
        "side": side,
        "sz": size,
        "px": px,
        "startPosition": start,
        "closedPnl": pnl,
        "time": time_ms,
        "dir": "zz",
    }


def alerts_on(**extra):
    # Saved data of a user with alerts on for WALLET since time 1000 (ms)
    return {
        "aliases": {"zz-whale": WALLET},
        "alert_wallets": {WALLET: 1000},
        "alert_every": 3600,
        **extra,
    }


def started(run):
    return run.call(run.app.post_init)  # Like starting the bot: post_init runs


def check_now(run, routes):
    FakeHypurrscan.routes = routes
    with mock.patch.object(run.module, "now", lambda: LATER):
        run.run_jobs("alerts:111")
    return run


def sent_text(run):
    return "\n".join(run.telegram.texts())


def test_late_answer_is_not_taken():  # W3
    run = BotRun({HOLDERS: (200, {"holders": {ADDR: 1.0}}), ALIASES_URL: (200, {})})
    run.send("/get_top_holders")
    with mock.patch.object(run.module, "now", lambda: time.time() + 601):
        run.send("zz-hi how are you")
    no_errors(run)
    assert run.telegram.texts()[-1].startswith("⌛")
    assert not [u for u in FakeHypurrscan.requested if HOLDERS in u]


def test_answer_in_time_is_taken():  # W3
    run = BotRun({HOLDERS: (200, {"holders": {ADDR: 1.0}}), ALIASES_URL: (200, {})})
    run.send("/get_top_holders")
    with mock.patch.object(run.module, "now", lambda: time.time() + 500):
        run.send("USDC")
    no_errors(run)
    assert any(t.startswith("<b>Top USDC Holders") for t in run.telegram.texts())


def test_open_question_survives_a_restart():  # W27
    run = BotRun({HOLDERS: (200, {"holders": {ADDR: 1.0}}), ALIASES_URL: (200, {})})
    run.send("/get_top_holders").restart().send("USDC")
    no_errors(run)
    assert any(t.startswith("<b>Top USDC Holders") for t in run.telegram.texts())


def test_late_answer_after_a_restart_is_not_taken():  # W3 + W27
    run = BotRun({HOLDERS: (200, {"holders": {ADDR: 1.0}}), ALIASES_URL: (200, {})})
    run.send("/get_top_holders").restart()
    with mock.patch.object(run.module, "now", lambda: time.time() + 601):
        run.send("USDC")
    no_errors(run)
    assert run.telegram.texts()[-1].startswith("⌛")


def test_wallet_shows_positions_and_balances():  # W28
    run = BotRun(WALLET_ROUTES, saved_user_data={"aliases": {"zz-whale": WALLET}})
    run.send("/wallet zz-whale")
    no_errors(run)
    text = sent_text(run)
    for expected in [
        "zz-whale",
        "SHORT 0.5 @ 78,018.6",
        "account value $12,345.68",
        "uPnL -$120.25 (-3.1%)",
        "5x cross",
        "liq 146,671",
        "<b>USDC</b> 250.5",
        "<b>HYPE</b> 12.3457",
    ]:
        assert expected in text, expected
    assert "PURR" not in text  # Empty balances aren't listed
    assert {"type": "clearinghouseState", "user": WALLET} in FakeHypurrscan.bodies


def test_wallet_can_be_picked_typed_or_tapped():  # W28
    run = BotRun(WALLET_ROUTES, saved_user_data={"aliases": {"zz-whale": WALLET}})
    run.send("/wallet", ("tap", run.module.button_data("w", "zz-whale")))
    no_errors(run)
    assert "SHORT 0.5" in sent_text(run)
    run = BotRun(WALLET_ROUTES).send("/wallet", WALLET.upper().replace("0X", "0x"))
    no_errors(run)
    assert "SHORT 0.5" in sent_text(run)


def test_wallet_unknown_name_is_asked_again():  # W28
    run = BotRun(WALLET_ROUTES).send("/wallet zz-nobody")
    no_errors(run)
    assert "No saved alias or address matches" in run.telegram.texts()[-1]


def test_wallet_failure_is_reported():  # W28
    run = BotRun(
        {
            "info:clearinghouseState": (500, None),
            "info:spotClearinghouseState": (200, SPOT),
        }
    ).send("/wallet", WALLET)
    no_errors(run)
    assert (
        run.telegram.texts()[-1] == "❌ Failed to fetch wallet data from Hyperliquid."
    )


def test_alerts_can_be_switched_on_and_off():  # W28
    run = BotRun(saved_user_data={"aliases": {"zz-whale": WALLET}})
    tap = ("tap", run.module.button_data("al", "zz-whale"))
    run.send("/alerts", tap)
    no_errors(run)
    assert len(run.jobs("alerts:111")) == 1
    assert WALLET in run.saved_user_data()["alert_wallets"]
    run.send(tap)
    no_errors(run)
    assert run.jobs("alerts:111") == ()
    assert run.saved_user_data()["alert_wallets"] == {}


def test_each_user_chooses_how_often():  # W28
    run = BotRun(saved_user_data={"aliases": {"zz-whale": WALLET}})
    run.send(
        "/alerts", ("tap", run.module.button_data("al", "zz-whale")), ("tap", "ai:900")
    )
    no_errors(run)
    assert run.saved_user_data()["alert_every"] == 900
    (job,) = run.jobs("alerts:111")
    assert job.job.trigger.interval.total_seconds() == 900
    assert "✅ Every 15 min" in str(run.telegram.calls[-1][1].get("reply_markup"))


def test_alerts_start_again_after_a_restart():  # W28
    run = BotRun(saved_user_data=alerts_on())
    assert run.jobs("alerts:111") == ()
    started(run)
    assert len(run.jobs("alerts:111")) == 1


def test_alert_summarises_all_trades_since_the_last_check():  # W28
    fills = [
        fill(1, "BTC", "A", "1.0", "78000", "0.0"),
        fill(2, "BTC", "A", "0.5", "78100", "-1.0"),
        fill(3, "ETH", "A", "2", "3000", "2", pnl="150.5"),
        fill(4, "@107", "B", "10", "40", "0"),
    ]
    run = started(BotRun(saved_user_data=alerts_on()))
    check_now(
        run, {"info:userFillsByTime": (200, fills), "info:spotMeta": (200, SPOT_META)}
    )
    no_errors(run)
    text = sent_text(run)
    for expected in [
        "<b>zz-whale</b> traded since",
        "<b>BTC</b> perp · 2 trade(s) · opened SHORT 1.5 · sold 1.5 (avg 78,033.3)",
        "<b>ETH</b> perp · 1 trade(s) · closed LONG · sold 2 (avg 3,000) · realised PnL +$150.50",
        "<b>HYPE/USDC</b> spot · 1 trade(s) · bought 10 (avg 40)",
    ]:
        assert expected in text, (expected, text)
    body = FakeHypurrscan.bodies[0]
    assert (body["startTime"], body["endTime"]) == (1000, int(LATER * 1000))
    # Next check starts right after this one: no gaps, no repeats
    assert run.saved_user_data()["alert_wallets"][WALLET] == int(LATER * 1000) + 1


def test_no_message_when_nothing_was_traded():  # W28
    run = started(BotRun(saved_user_data=alerts_on()))
    check_now(run, {"info:userFillsByTime": (200, [])})
    no_errors(run)
    assert run.telegram.count("sendMessage") == 0
    assert run.saved_user_data()["alert_wallets"][WALLET] == int(LATER * 1000) + 1


def test_failed_check_asks_for_the_same_period_again():  # W28
    run = started(BotRun(saved_user_data=alerts_on()))
    check_now(run, {"info:userFillsByTime": (500, None)})
    no_errors(run)
    assert run.telegram.count("sendMessage") == 0
    assert run.saved_user_data()["alert_wallets"][WALLET] == 1000


def test_many_trades_are_fetched_in_pages():  # W28
    first = [
        fill(i, "BTC", "B", "1", "10", str(i), time_ms=1000 + i) for i in range(2000)
    ]
    rest = [first[-1]] + [  # The last trade again (same time), then 5 new ones
        fill(2000 + i, "BTC", "B", "1", "10", str(2000 + i), time_ms=3000 + i)
        for i in range(5)
    ]

    def pages(body):
        return (200, first if body["startTime"] == 1000 else rest)

    run = started(BotRun(saved_user_data=alerts_on()))
    check_now(run, {"info:userFillsByTime": pages, "info:spotMeta": (200, SPOT_META)})
    no_errors(run)
    assert [
        b["startTime"] for b in FakeHypurrscan.bodies if b["type"] == "userFillsByTime"
    ] == [1000, 2999]
    assert "2005 trade(s)" in sent_text(run)


def test_removing_the_alias_stops_its_alerts():  # W28
    run = started(BotRun(saved_user_data=alerts_on()))
    run.send("/remove_alias", ("tap", run.module.remove_button_data("zz-whale")))
    no_errors(run)
    assert run.jobs("alerts:111") == ()
    assert run.saved_user_data()["alert_wallets"] == {}


def test_alerts_say_when_the_package_is_missing():  # W28
    run = BotRun(saved_user_data=alerts_on(), no_job_queue=True)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBUserWarning)  # PTB's own "no JobQueue" note
        started(run)
        run.send("/alerts")
    no_errors(run)
    assert "pip install -r requirements.txt" in run.telegram.texts()[-1]
    assert "Warning: wallet alerts are off" in out.getvalue()


def test_search_finds_your_own_aliases():  # W30
    run = BotRun(
        {ALIASES_URL: (200, {})},
        saved_user_data={"aliases": {"zz-Treasury Main": ADDR}},
    )
    run.send("/search_alias", "treasury")
    no_errors(run)
    assert "1 of your aliases contain" in sent_text(run)
    assert "zz-Treasury Main" in sent_text(run)


def test_search_by_address_shows_your_own_alias():  # W30
    run = BotRun(
        {ALIASES_URL: (200, {})}, saved_user_data={"aliases": {"zz-main": ADDR}}
    )
    run.send("/search_alias", ADDR)
    no_errors(run)
    assert "Saved by you as" in sent_text(run) and "zz-main" in sent_text(run)


def test_rename_by_tapping():  # W30
    run = BotRun(saved_user_data={"aliases": {"zz-old": ADDR}})
    run.send("/rename_alias", ("tap", run.module.button_data("rn", "zz-old")), "zz-New")
    no_errors(run)
    assert run.saved_user_data()["aliases"] == {"zz-New": ADDR}


def test_rename_refuses_a_name_already_in_use():  # W30
    run = BotRun(saved_user_data={"aliases": {"zz-a": ADDR, "zz-b": ADDR2}})
    run.send("/rename_alias", "zz-a", "ZZ-B")
    no_errors(run)
    assert "already used" in run.telegram.texts()[-1]
    assert run.saved_user_data()["aliases"] == {"zz-a": ADDR, "zz-b": ADDR2}


def test_export_sends_a_csv_file():  # W30
    run = BotRun(saved_user_data={"aliases": {"zz-b": ADDR2, "zz-a": ADDR}})
    run.send("/export_aliases")
    no_errors(run)
    ((filename, content),) = run.telegram.documents
    assert filename == "aliases.csv"
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    assert rows == [["alias", "address"], ["zz-a", ADDR], ["zz-b", ADDR2]]


def test_import_adds_aliases_and_reports_problems():  # W30
    content = (
        "alias,address\n"
        f"zz-new,{ADDR2}\n"
        f"ZZ-KEEP,{ADDR2}\n"
        "zz-bad,not-an-address\n"
        f",{WALLET}\n"
        f"zz-keep,{ADDR}\n"
    ).encode()
    run = BotRun(saved_user_data={"aliases": {"zz-keep": ADDR}})
    run.send("/import_aliases", ("document", "zz.csv", content))
    no_errors(run)
    assert run.saved_user_data()["aliases"] == {"zz-keep": ADDR, "zz-new": ADDR2}
    reply = run.telegram.texts()[-1]
    for expected in [
        "1 new alias(es) added",
        "1 were already saved",
        "row 4, 5",
        "kept as they were",
    ]:
        assert expected in reply, (expected, reply)


def test_import_reads_excel_csv_with_semicolons():  # W30
    content = f"alias;address\r\nzz-x;{ADDR}\r\n".encode("utf-8-sig")
    run = BotRun().send("/import_aliases", ("document", "zz.csv", content))
    no_errors(run)
    assert run.saved_user_data()["aliases"] == {"zz-x": ADDR}


def test_export_then_import_gives_the_same_aliases():  # W30
    aliases = {"zz-ñame 🚀": ADDR, "zz-other": ADDR2}
    exported = BotRun(saved_user_data={"aliases": aliases}).send("/export_aliases")
    ((_, content),) = exported.telegram.documents
    run = BotRun().send("/import_aliases", ("document", "aliases.csv", content))
    no_errors(run)
    assert run.saved_user_data()["aliases"] == aliases


def test_import_refuses_big_files_and_asks_for_a_file():  # W30
    run = BotRun().send("/import_aliases", ("document", "zz.csv", b"x" * 100_001))
    no_errors(run)
    assert "too big" in run.telegram.texts()[-1]
    run.send("zz-hello")
    no_errors(run)
    assert "send the CSV as a file" in run.telegram.texts()[-1]


def test_group_command_for_this_bot_points_to_a_private_chat():  # Group chats (option A)
    for command in ["/view_aliases@zz_bot", "/wallet"]:
        run = BotRun(saved_user_data={"aliases": {"zz-secret": ADDR}}, chat=GROUP)
        run.send(command)
        no_errors(run)
        (params,) = [p for e, p in run.telegram.calls if e == "sendMessage"]
        assert "private chats" in params["text"] and "zz-secret" not in params["text"]
        assert "https://t.me/zz_bot" in str(params["reply_markup"])


def test_group_chatter_and_other_bots_are_ignored():  # Group chats (option A)
    run = BotRun(chat=GROUP).send(
        "zz-gm everyone", "/start@zz_other_bot", "/zz_unknown"
    )
    no_errors(run)
    assert run.telegram.count("sendMessage") == 0


def test_group_button_tap_gets_a_private_chat_notice():  # Group chats (option A)
    run = BotRun(chat=GROUP).send(("tap", "Check a wallet"))
    no_errors(run)
    (answer,) = [p for e, p in run.telegram.calls if e == "answerCallbackQuery"]
    assert "private chats" in answer["text"]
    assert run.telegram.count("sendMessage") == 0


def test_rounding_noise_in_positions_is_ignored():  # W28
    # Hyperliquid sends positions with float noise, e.g. -1.0000000001 for -1 (seen live 2026-10-20)
    fills = [
        fill(1, "BTC", "B", "1", "78000", "-1.0000000001"),
        fill(2, "ETH", "A", "2", "3000", "0.0000000002"),
    ]
    run = started(BotRun(saved_user_data=alerts_on()))
    check_now(
        run, {"info:userFillsByTime": (200, fills), "info:spotMeta": (200, SPOT_META)}
    )
    no_errors(run)
    assert "<b>BTC</b> perp · 1 trade(s) · closed SHORT" in sent_text(run)
    assert "<b>ETH</b> perp · 1 trade(s) · opened SHORT 2" in sent_text(run)


# ---------- found by the end-to-end test (tests/e2e: real bot process, simulated Telegram) ----------
def many_aliases(count):
    return {f"zz-w{i:02d}": "0x" + f"{i:040x}" for i in range(count)}


def menus(run):
    # (text, buttons as text) of every message the bot sent or edited
    return [
        (p["text"], str(p.get("reply_markup")))
        for e, p in run.telegram.calls
        if e in ("sendMessage", "editMessageText")
    ]


def popups(run):
    return [p.get("text") for e, p in run.telegram.calls if e == "answerCallbackQuery"]


def test_tapping_the_interval_already_chosen_is_fine():  # W33
    run = BotRun(saved_user_data={**alerts_on(), "alert_every": 900})
    run.send("/alerts", ("tap", "ai:900"), ("tap", "ai:900"))
    no_errors(run)
    assert "Something went wrong" not in sent_text(run)
    assert popups(run) == ["Already checking every 15 min"] * 2


def test_tapping_a_page_button_twice_is_fine():  # W33
    names = {"0x" + f"{i:040x}": f"zz-label-{i}" for i in range(100)}
    run = BotRun({ALIASES_URL: (200, names)})
    run.send("/get_global_aliases", ("tap", "ga:30"), ("tap", "ga:30"))
    no_errors(run)
    assert "Something went wrong" not in sent_text(run)


def test_alerts_menu_without_pages_for_up_to_50_wallets():  # W34
    run = BotRun(saved_user_data={"aliases": many_aliases(50)})
    run.send("/alerts")
    no_errors(run)
    ((text, buttons),) = menus(run)
    assert "zz-w49" in buttons and "ap:" not in buttons and "of 50" not in text


def test_alerts_menu_pages_through_many_wallets():  # W34
    run = BotRun(saved_user_data={"aliases": many_aliases(60)})
    run.send("/alerts", ("tap", "ap:50"))
    no_errors(run)
    (text1, first), (text2, second) = menus(run)
    assert "1-50 of 60" in text1 and "zz-w49" in first and "zz-w50" not in first
    assert "ap:50" in first  # Next
    assert "51-60 of 60" in text2 and "zz-w59" in second and "zz-w49" not in second
    assert "ap:0" in second  # Previous


def test_alerts_for_a_wallet_on_a_later_page():  # W34
    aliases = many_aliases(60)
    run = BotRun(saved_user_data={"aliases": aliases})
    tap = ("tap", run.module.button_data("al", "zz-w55"))
    run.send("/alerts", tap, ("tap", "ai:900:50"))
    no_errors(run)
    saved = run.saved_user_data()
    assert aliases["zz-w55"] in saved["alert_wallets"] and saved["alert_every"] == 900
    for text, buttons in menus(run)[1:]:  # Both redraws stay on that wallet's page
        assert "51-60 of 60" in text and "🔔 zz-w55" in buttons


# ---------- easier to use: help, token names, clear questions, a safe terminal (U6-U8, U11) ----------
SPOT_TOKENS = {
    "universe": [{"name": "@142", "tokens": [197, 0], "index": 142}],
    "tokens": [
        {"name": "USDC", "index": 0, "fullName": None},
        {"name": "HYPE", "index": 150, "fullName": "Hyperliquid"},
        {"name": "UBTC", "index": 197, "fullName": "Unit Bitcoin"},
        {"name": "UETH", "index": 221, "fullName": "Unit Ethereum"},
    ],
}
NO_BTC = {
    "holdersWithLimit/BTC/": (
        200,
        {},
    ),  # Hyperliquid's spot market has no token called BTC
    "holdersWithLimit/UBTC/": (200, {"holders": {ADDR: 5.0}, "holdersCount": 1}),
    "holdersWithLimit/BITCOIN/": (200, {}),
    ALIASES_URL: (200, {}),
    "info:spotMeta": (200, SPOT_TOKENS),
}


def test_help_explains_addresses_and_token_names():  # U6
    run = BotRun().send("/help")
    no_errors(run)
    text = sent_text(run)
    for part in (
        "/register_alias",  # Still the list of commands
        "Finding wallet addresses",
        "leaderboard",
        "/search_alias",
        "UBTC",
        "Unit Bitcoin",
        "Perps (futures)",
        "alias,address",
    ):
        assert part in text, part


def test_welcome_and_menu_point_to_help():  # U6
    run = started(BotRun().send("/start"))
    welcome = run.telegram.texts()[0]
    assert "/help" in welcome and "wallet addresses" in welcome
    sent = [p for e, p in run.telegram.calls if e == "setMyCommands"]
    assert "how to find addresses and token names" in str(sent[0]), sent


def test_unknown_token_suggests_spot_names_as_buttons():  # U7
    run = BotRun(NO_BTC).send("/get_top_holders", "btc")
    no_errors(run)
    asked = run.telegram.calls[-1][1]
    assert "No token called <b>BTC</b>" in asked["text"]
    assert "tk:UBTC" in str(asked["reply_markup"])
    assert "UBTC (Unit Bitcoin)" in str(asked["reply_markup"])
    run.send(("tap", "tk:UBTC"))  # Nothing happens on its own: the user picks it
    no_errors(run)
    assert "Top UBTC Holders" in sent_text(run)


def test_full_names_find_spot_tokens_too():  # U7
    run = BotRun(NO_BTC).send("/get_top_holders", "bitcoin")
    no_errors(run)
    assert "tk:UBTC" in str(run.telegram.calls[-1][1]["reply_markup"])


def test_questions_point_to_help():  # U8
    asked = BotRun().send("/get_top_holders").telegram.texts()[-1]
    assert "UBTC" in asked and "/help" in asked
    assert "/help" in BotRun().send("/register_alias").telegram.texts()[-1]
    run = BotRun(saved_user_data={"aliases": {"zz-whale": WALLET}}).send("/wallet")
    assert "/help" in run.telegram.texts()[-1]


def test_help_during_a_question_keeps_the_question_open():  # U8
    run = BotRun().send("/register_alias", "/help", ADDR)
    no_errors(run)
    assert "name this address" in run.telegram.texts()[-1]


def test_messages_are_not_printed_in_the_terminal():  # U11
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        BotRun().send("zz-private words")
    assert "zz-private words" not in out.getvalue(), out.getvalue()


def test_log_lines_show_names_and_searches_safely():  # U11
    # Names and typed text come from users: control codes could change your terminal,
    # and a line break could fake a line of the log
    out = io.StringIO()
    routes = {HOLDERS: (200, {}), "info:spotMeta": (200, SPOT_META)}
    with (
        contextlib.redirect_stdout(out),
        mock.patch.dict(harness.USER, first_name="zz-\x1b]0;x\x07Evil\nName"),
    ):
        BotRun(routes).send("/get_top_holders", "zz\x1b[2J" + "q" * 100)
    lines = out.getvalue().splitlines()
    assert lines and not any("\x1b" in line or "\x07" in line for line in lines), lines
    assert any("Evil Name is searching for top holders" in line for line in lines), (
        lines
    )
    assert all(len(line) < 200 for line in lines), lines


def test_unknown_text_and_commands_get_one_hint_a_minute():  # U11
    run = BotRun()
    with mock.patch.object(run.module, "now", lambda: LATER):
        run.send("zz-1", "zz-2", "zz-3", "/zz_a", "/zz_b")
    texts = run.telegram.texts()
    assert sum("Send /start" in t for t in texts) == 1, texts
    assert sum("don't know that command" in t for t in texts) == 1, texts
    with mock.patch.object(run.module, "now", lambda: LATER + 61):
        run.send("zz-4")
    assert sum("Send /start" in t for t in run.telegram.texts()) == 2


# ---------- starting while Telegram can't be reached (U13) ----------
def test_startup_keeps_trying_when_telegram_cant_be_reached():  # U13
    # With -1, python-telegram-bot retries the start until Telegram answers (no internet at boot,
    # a short outage); a wrong token still stops it at once. tests/e2e checks both for real.
    assert BotRun().polling == {"bootstrap_retries": -1}


def test_startup_says_when_it_is_connected():  # U13
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        started(BotRun())
    assert (
        "✅ Connected to Telegram: the bot is running. Stop it with Ctrl+C."
        in out.getvalue()
    )


def test_startup_says_connected_also_when_alerts_are_off():  # U13
    run = BotRun(no_job_queue=True)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBUserWarning)  # PTB's own "no JobQueue" note
        started(run)
    assert "Warning: wallet alerts are off" in out.getvalue()
    assert "✅ Connected to Telegram: the bot is running" in out.getvalue()
