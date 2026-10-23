"""Acceptance: KimiWaker (docs/spec/providers.md section 3). Fake kimi, ps, lsof and a fake ``kimi web`` server."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from phoning import harness_ring as hr
from tests.acceptance.test_harness_support import (
    NUDGE,
    SID,
    TOKEN,
    FakeKimiWeb,
    dead_pid,
    fake_kimi,
    fake_lsof,
    fake_ps,
    make_kimi_home,
    read_log,
)


@pytest.fixture
def rig(tmp_path: Path) -> dict[str, Path]:
    work = tmp_path / "work"
    home, sdir = make_kimi_home(tmp_path, work)
    exe, log = fake_kimi(tmp_path)
    return {"tmp": tmp_path, "work": work, "home": home, "sdir": sdir, "kimi": exe, "log": log,
            "ps": fake_ps(tmp_path, [(1, "/sbin/launchd"), (200, "/usr/bin/zsh")]),
            "lsof": fake_lsof(tmp_path, {})}


@pytest.fixture
def web() -> Iterator[FakeKimiWeb]:
    srv = FakeKimiWeb(subscribed=[SID])
    yield srv
    srv.close()


def waker(rig: dict[str, Path]) -> hr.KimiWaker:
    return hr.KimiWaker(kimi_bin=str(rig["kimi"]), kimi_home=str(rig["home"]), ps_bin=str(rig["ps"]),
                        lsof_bin=str(rig["lsof"]))


def target(rig: dict[str, Path], **kw: str | None) -> hr.Target:
    return hr.Target("kimi", SID, **kw)  # type: ignore[arg-type]


# ----------------------------------------------------------------- liveness


def test_dead_session_is_reachable_headless(rig: dict[str, Path]) -> None:
    live = waker(rig).reachable(target(rig))
    assert (live.state, live.path) == ("dead", "headless")


def test_unknown_and_malicious_session_ids_are_absent(rig: dict[str, Path]) -> None:
    w = waker(rig)
    for bad in ("session_nope", "../../etc/passwd", "a/b", "", "-S", "x" * 300):
        live = w.reachable(hr.Target("kimi", bad))
        assert (live.state, live.path) == ("absent", "none"), bad


def test_broken_state_file_is_unknown(rig: dict[str, Path]) -> None:
    (rig["sdir"] / "state.json").write_text("{not json", encoding="utf-8")
    assert waker(rig).reachable(target(rig)).state == "unknown"
    (rig["sdir"] / "state.json").write_text('{"cwd": ""}', encoding="utf-8")
    assert waker(rig).reachable(target(rig)).state == "unknown"


def test_kimi_process_in_session_cwd_is_live(rig: dict[str, Path]) -> None:
    rig["ps"] = fake_ps(rig["tmp"], [(4242, "/Users/x/.kimi-code/bin/kimi-code"), (1, "/sbin/launchd")])
    rig["lsof"] = fake_lsof(rig["tmp"], {4242: str(rig["work"])})
    live = waker(rig).reachable(target(rig))
    assert (live.state, live.path) == ("live", "none")


def test_kimi_process_elsewhere_does_not_block(rig: dict[str, Path]) -> None:
    other = rig["tmp"] / "elsewhere"
    other.mkdir()
    rig["ps"] = fake_ps(rig["tmp"], [(4242, "kimi-code")])
    rig["lsof"] = fake_lsof(rig["tmp"], {4242: str(other)})
    assert waker(rig).reachable(target(rig)).state == "dead"


def test_recent_wire_write_is_live(rig: dict[str, Path]) -> None:
    wire = rig["sdir"] / "agents" / "main" / "wire.jsonl"
    now = time.time() - 5
    os.utime(wire, (now, now))
    live = waker(rig).reachable(target(rig))
    assert (live.state, live.path) == ("live", "none")


def test_process_scan_failure_fails_closed(rig: dict[str, Path]) -> None:
    rig["ps"] = fake_ps(rig["tmp"], [], rc=1)
    live = waker(rig).reachable(target(rig))
    assert (live.state, live.path) == ("unknown", "none")


def test_unreadable_process_cwd_fails_closed(rig: dict[str, Path]) -> None:
    rig["ps"] = fake_ps(rig["tmp"], [(4242, "kimi-code")])
    rig["lsof"] = fake_lsof(rig["tmp"], {})  # exits 1 for every pid
    assert waker(rig).reachable(target(rig)).state == "unknown"


def test_missing_ps_binary_fails_closed(rig: dict[str, Path]) -> None:
    w = hr.KimiWaker(kimi_bin=str(rig["kimi"]), kimi_home=str(rig["home"]), ps_bin=str(rig["tmp"] / "nope"),
                     lsof_bin=str(rig["lsof"]))
    assert w.reachable(target(rig)).state == "unknown"


# ------------------------------------------------------------ resume_headless


def test_resume_headless_runs_the_exact_argv_in_the_session_cwd(rig: dict[str, Path]) -> None:
    line = waker(rig).resume_headless(target(rig), NUDGE, None)
    assert line == "resumed: rc=0"
    (call,) = read_log(rig["log"])
    assert call["argv"][1:] == ["-S", SID, "-p", NUDGE]
    assert os.path.realpath(call["cwd"]) == os.path.realpath(rig["work"])
    assert call["stdin"] == ""
    assert call["kimi_home"] == str(rig["home"])


def test_resume_headless_reports_nonzero_rc_and_never_echoes_output(rig: dict[str, Path]) -> None:
    exe, log = fake_kimi(rig["tmp"] / "bad", rc=3)
    rig["kimi"], rig["log"] = exe, log
    line = waker(rig).resume_headless(target(rig), NUDGE, None)
    assert line == "resumed: rc=3"
    assert "assistant text" not in line


@pytest.mark.parametrize("case", ["process", "recent", "psfail"])
def test_resume_headless_refuses_unless_provably_dead(rig: dict[str, Path], case: str) -> None:
    if case == "process":
        rig["ps"] = fake_ps(rig["tmp"], [(7, "kimi-code")])
        rig["lsof"] = fake_lsof(rig["tmp"], {7: str(rig["work"])})
    elif case == "recent":
        now = time.time()
        os.utime(rig["sdir"] / "agents" / "main" / "wire.jsonl", (now, now))
    else:
        rig["ps"] = fake_ps(rig["tmp"], [], rc=2)
    line = waker(rig).resume_headless(target(rig), NUDGE, None)
    assert line.startswith("skip: ")
    assert read_log(rig["log"]) == []


def test_resume_headless_refuses_absent_session(rig: dict[str, Path]) -> None:
    line = waker(rig).resume_headless(hr.Target("kimi", "session_missing"), NUDGE, None)
    assert line == "skip: absent: no session"
    assert read_log(rig["log"]) == []


def test_resume_headless_cwd_mismatch_is_skipped(rig: dict[str, Path]) -> None:
    other = rig["tmp"] / "other"
    other.mkdir()
    line = waker(rig).resume_headless(target(rig), NUDGE, str(other))
    assert line == "skip: cwd mismatch"
    assert read_log(rig["log"]) == []
    # the matching cwd is accepted
    assert waker(rig).resume_headless(target(rig), NUDGE, str(rig["work"])) == "resumed: rc=0"


def test_resume_headless_timeout(rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    exe, log = fake_kimi(rig["tmp"] / "slow", sleep=5)
    rig["kimi"], rig["log"] = exe, log
    monkeypatch.setattr(hr, "KIMI_RESUME_TIMEOUT", 0.5)
    t0 = time.monotonic()
    assert waker(rig).resume_headless(target(rig), NUDGE, None) == "ring failed: timed out"
    assert time.monotonic() - t0 < 4


def test_resume_headless_missing_binary(rig: dict[str, Path]) -> None:
    w = hr.KimiWaker(kimi_bin=str(rig["tmp"] / "no-such-kimi"), kimi_home=str(rig["home"]), ps_bin=str(rig["ps"]),
                     lsof_bin=str(rig["lsof"]))
    assert w.resume_headless(target(rig), NUDGE, None) == "ring failed: no kimi executable"


# ----------------------------------------------------------------- web path


def test_web_subscribed_session_is_woken_through_the_rest_api(rig: dict[str, Path], web: FakeKimiWeb) -> None:
    web.register(rig["home"])
    live = waker(rig).reachable(target(rig))
    assert (live.state, live.path) == ("live", "web")
    assert waker(rig).wake(target(rig), NUDGE) == "rang: web"
    posts = [r for r in web.requests if r["method"] == "POST"]
    assert len(posts) == 1
    post = posts[0]
    assert post["path"] == f"/api/v1/sessions/{SID}/prompts"
    assert post["auth"] == f"Bearer {TOKEN}"
    assert post["ctype"] == "application/json"
    assert post["body"] == {"content": [{"type": "text", "text": NUDGE}]}
    assert read_log(rig["log"]) == []  # no kimi process was started


def test_web_wake_never_uses_headless_resume_on_a_hosted_session(rig: dict[str, Path], web: FakeKimiWeb) -> None:
    web.register(rig["home"])
    line = waker(rig).resume_headless(target(rig), NUDGE, None)
    assert line.startswith("skip: live")
    assert read_log(rig["log"]) == []
    assert [r for r in web.requests if r["method"] == "POST"] == []


def test_web_server_error_code_is_reported_without_server_text(rig: dict[str, Path]) -> None:
    srv = FakeKimiWeb(subscribed=[SID], prompt_code=40110)
    try:
        srv.register(rig["home"])
        line = waker(rig).wake(target(rig), NUDGE)
    finally:
        srv.close()
    assert line == "ring failed: web code=40110"
    assert "SECRET" not in line


def test_web_busy_but_unsubscribed_session_is_live_and_not_woken(rig: dict[str, Path]) -> None:
    srv = FakeKimiWeb(subscribed=[], busy=True)
    try:
        srv.register(rig["home"])
        live = waker(rig).reachable(target(rig))
        line = waker(rig).wake(target(rig), NUDGE)
    finally:
        srv.close()
    assert (live.state, live.path) == ("live", "none")
    assert line.startswith("skip: live")
    assert [r for r in srv.requests if r["method"] == "POST"] == []


def test_idle_unsubscribed_web_server_does_not_block_a_dead_session(rig: dict[str, Path]) -> None:
    srv = FakeKimiWeb(subscribed=["session_other"])
    try:
        srv.register(rig["home"])
        live = waker(rig).reachable(target(rig))
    finally:
        srv.close()
    assert (live.state, live.path) == ("dead", "headless")


def test_stale_instance_file_is_ignored(rig: dict[str, Path], web: FakeKimiWeb) -> None:
    web.register(rig["home"], pid=dead_pid())
    assert waker(rig).reachable(target(rig)).state == "dead"
    assert web.requests == []


def test_garbage_instance_file_is_ignored(rig: dict[str, Path]) -> None:
    inst = rig["home"] / "server" / "instances"
    inst.mkdir(parents=True)
    (inst / "junk.json").write_text("{nope", encoding="utf-8")
    (inst / "empty.json").write_text("{}", encoding="utf-8")
    assert waker(rig).reachable(target(rig)).state == "dead"


def test_unreachable_live_web_server_fails_closed(rig: dict[str, Path]) -> None:
    srv = FakeKimiWeb(subscribed=[SID])
    srv.register(rig["home"])
    srv.close()  # port now refuses connections, pid (this test process) is alive
    live = waker(rig).reachable(target(rig))
    assert (live.state, live.path) == ("unknown", "none")
    assert waker(rig).resume_headless(target(rig), NUDGE, None).startswith("skip: liveness unknown")
    assert read_log(rig["log"]) == []


def test_web_error_envelope_fails_closed(rig: dict[str, Path]) -> None:
    srv = FakeKimiWeb(subscribed=[SID], connections_code=50001)
    try:
        srv.register(rig["home"])
        assert waker(rig).reachable(target(rig)).state == "unknown"
    finally:
        srv.close()


def test_wrong_token_fails_closed(rig: dict[str, Path]) -> None:
    srv = FakeKimiWeb(token="a-different-token", subscribed=[SID])
    try:
        srv.register(rig["home"])
        (rig["home"] / "server.token").write_text("stale-token\n", encoding="utf-8")
        (rig["home"] / "server.token").chmod(0o600)
        assert waker(rig).reachable(target(rig)).state == "unknown"
    finally:
        srv.close()


def test_loose_token_file_mode_is_refused(rig: dict[str, Path], web: FakeKimiWeb) -> None:
    web.register(rig["home"], token_mode=0o644)
    assert waker(rig).reachable(target(rig)).state == "unknown"
    assert web.requests == []  # the token was never even sent


def test_non_loopback_instance_is_refused(rig: dict[str, Path], web: FakeKimiWeb) -> None:
    web.register(rig["home"], host="0.0.0.0")
    assert waker(rig).reachable(target(rig)).state == "unknown"
    assert web.requests == []


def test_missing_token_file_fails_closed(rig: dict[str, Path], web: FakeKimiWeb) -> None:
    web.register(rig["home"])
    (rig["home"] / "server.token").unlink()
    assert waker(rig).reachable(target(rig)).state == "unknown"


# ------------------------------------------------------------ wake on non-web


def test_wake_on_dead_session_points_at_resume(rig: dict[str, Path]) -> None:
    assert waker(rig).wake(target(rig), NUDGE) == "skip: dead (use resume_headless)"
    assert read_log(rig["log"]) == []


def test_wake_on_tui_session_is_a_skip_not_a_resume(rig: dict[str, Path]) -> None:
    rig["ps"] = fake_ps(rig["tmp"], [(9, "kimi-code")])
    rig["lsof"] = fake_lsof(rig["tmp"], {9: str(rig["work"])})
    line = waker(rig).wake(target(rig), NUDGE)
    assert line.startswith("skip: live")
    assert read_log(rig["log"]) == []


def test_wake_on_absent_and_unknown(rig: dict[str, Path]) -> None:
    assert waker(rig).wake(hr.Target("kimi", "session_missing"), NUDGE) == "skip: absent: no session"
    rig["ps"] = fake_ps(rig["tmp"], [], rc=1)
    assert waker(rig).wake(target(rig), NUDGE) == "skip: liveness unknown"


# ----------------------------------------------------------- nudge validation


@pytest.mark.parametrize("bad", ["", "line one\nline two", "tab\there", "-p evil", "x" * 5000, "nul\x00byte"])
def test_invalid_nudges_never_reach_the_harness(rig: dict[str, Path], web: FakeKimiWeb, bad: str) -> None:
    web.register(rig["home"])
    w = waker(rig)
    assert w.wake(target(rig), bad) == "ring failed: invalid nudge"
    assert w.resume_headless(target(rig), bad, None) == "ring failed: invalid nudge"
    assert read_log(rig["log"]) == []
    assert [r for r in web.requests if r["method"] == "POST"] == []
