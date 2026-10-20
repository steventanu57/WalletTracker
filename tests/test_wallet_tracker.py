"""End-to-end tests for wallet_tracker.py (fake Telegram + fake Hypurrscan, no network).
Test data uses the zz- prefix."""

import contextlib
import io
import os
import re
import subprocess
import sys
import warnings
from unittest import mock

import aiohttp
from harness import BotRun, FakeHypurrscan, bot_dir
from telegram.warnings import PTBUserWarning

ADDR = "0x" + "ab" * 20  # a well-formed 42-character address
ADDR2 = "0x" + "cd" * 20
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
    run = BotRun({HOLDERS: (200, {})}).send("/get_top_holders", "usdc/../x")
    no_errors(run)
    assert FakeHypurrscan.requested == [
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
    run = BotRun({HOLDERS: (200, {})}).send("/get_top_holders", "zznotatoken")
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
    run = BotRun({ALIASES_URL: (200, {}), HOLDERS: (200, {})})
    run.send("/get_top_holders", "ZZ1", "ZZ2")
    no_errors(run)
    assert len(FakeHypurrscan.requested) == 2 and FakeHypurrscan.opened == 1


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
    run = BotRun({HOLDERS: (200, {})}).send("/get_top_holders", "ZZ")
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
