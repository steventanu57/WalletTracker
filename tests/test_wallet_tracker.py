"""End-to-end tests for wallet_tracker.py (fake Telegram + fake Hypurrscan, no network).
Test data uses the zz- prefix."""

import os
import subprocess
import sys
import warnings

import aiohttp
from harness import BotRun, bot_dir
from telegram.warnings import PTBUserWarning

ADDR = "0x" + "ab" * 20  # a well-formed 42-character address
ALIASES_URL = "api.hypurrscan.io/globalAliases"
ADDRESS_PAGE = (
    "hypurrscan.io/address/"  # returns HTTP 200 for ANY text, so it can't validate
)


def no_errors(run):
    assert run.errors == [], f"handler crashed: {[repr(e) for e in run.errors]}"


# ---------- features that must keep working ----------


def test_start_shows_menu():
    run = BotRun().send("/start")
    no_errors(run)
    assert "Welcome to <b>Hyperliquid Wallet Storage</b>" in run.telegram.texts()[0]


def test_register_valid_address_is_saved():
    run = BotRun({ADDRESS_PAGE: (200, None)}).send("/register_alias", ADDR, "zz-main")
    no_errors(run)
    assert run.saved_user_data()["aliases"] == {"zz-main": ADDR}


def test_top_holders_lists_15():
    holders = {f"0x{i:040x}": 1000.0 - i for i in range(30)}
    run = BotRun({"holdersWithLimit/USDC/": (200, {"holders": holders})}).send(
        "/get_top_holders", "usdc"
    )
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
    run = BotRun({ADDRESS_PAGE: (200, None)}).send(
        "/register_alias", "zz-not-an-address", "zz-name"
    )
    no_errors(run)
    assert "aliases" not in run.saved_user_data(), "garbage address was saved"


def test_alias_with_html_characters_still_gets_listed():
    run = BotRun({ADDRESS_PAGE: (200, None)}).send(
        "/register_alias", ADDR, "zz-<me&you>"
    )
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
    run = BotRun({"holdersWithLimit/USDC/": (200, {"holders": holders})}).send(
        "/get_top_holders", "USDC"
    )
    no_errors(run)
    listing = next(
        t for t in run.telegram.texts() if t.startswith("<b>Top USDC Holders")
    )
    lines = listing.splitlines()[1:]
    assert lines[0].startswith(f"1. <code>0x{29:040x}</code> - 29.00")
    assert lines[-1].startswith(f"15. <code>0x{15:040x}</code> - 15.00")


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
    run = BotRun({ADDRESS_PAGE: (200, None)}).send("/register_alias", ADDR, "zz-main")
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
