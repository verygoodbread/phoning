"""Acceptance: OpenClawWaker (docs/spec/providers.md section 4). A fake ``openclaw`` executable logs every call."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from phoning import harness_ring as hr
from tests.acceptance.test_harness_support import (
    NUDGE,
    agent_calls,
    fake_openclaw,
    oc_session,
    read_log,
)

SID = "ops-incident-42"


def waker(exe: Path) -> hr.OpenClawWaker:
    return hr.OpenClawWaker(openclaw_bin=str(exe))


def tgt(**kw: str | None) -> hr.Target:
    return hr.Target("openclaw", SID, **kw)  # type: ignore[arg-type]


def test_idle_session_with_gateway_up_is_woken(tmp_path: Path) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID)])
    w = waker(exe)
    live = w.reachable(tgt())
    assert (live.state, live.path) == ("idle", "gateway")
    assert w.wake(tgt(), NUDGE) == "rang: gateway"
    (call,) = agent_calls(log)
    assert call["argv"][1:] == ["agent", "--agent", "main", "--session-id", SID, "--message", NUDGE, "--json",
                                "--timeout", str(hr.OPENCLAW_TURN_TIMEOUT)]
    assert call["stdin"] == ""


def test_wake_never_delivers_or_runs_locally(tmp_path: Path) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID)])
    waker(exe).wake(tgt(), NUDGE)
    argv = agent_calls(log)[0]["argv"]
    for forbidden in ("--deliver", "--local", "--channel", "--to", "--reply-to", "--reply-channel", "--reply-account"):
        assert forbidden not in argv


def test_running_session_is_live_and_still_woken(tmp_path: Path) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID, status="running")])
    w = waker(exe)
    live = w.reachable(tgt())
    assert (live.state, live.path) == ("live", "gateway")
    assert w.wake(tgt(), NUDGE) == "rang: gateway"
    assert len(agent_calls(log)) == 1


def test_agent_id_comes_from_the_session_lookup(tmp_path: Path) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID, agent="ops")])
    assert waker(exe).wake(tgt(), NUDGE) == "rang: gateway"
    argv = agent_calls(log)[0]["argv"]
    assert argv[argv.index("--agent") + 1] == "ops"


def test_session_can_be_named_by_full_key(tmp_path: Path) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID)])
    key = f"agent:main:explicit:{SID}"
    assert waker(exe).wake(hr.Target("openclaw", key), NUDGE) == "rang: gateway"
    argv = agent_calls(log)[0]["argv"]
    assert argv[argv.index("--session-id") + 1] == SID  # resolved from the lookup, not the raw key


def test_unknown_session_is_never_created(tmp_path: Path) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session("someone-else")])
    w = waker(exe)
    live = w.reachable(tgt())
    assert (live.state, live.path) == ("absent", "none")
    assert w.wake(tgt(), NUDGE) == "skip: absent: no session"
    assert w.resume_headless(tgt(), NUDGE, None) == "skip: absent: no session"
    assert agent_calls(log) == []


def test_lookup_uses_the_documented_sessions_command(tmp_path: Path) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID)])
    waker(exe).reachable(tgt())
    first = read_log(log)[0]["argv"][1:]
    assert first == ["sessions", "--json", "--all-agents", "--limit", "all"]


def test_gateway_down_means_dead_headless(tmp_path: Path) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID)], health="down")
    w = waker(exe)
    live = w.reachable(tgt())
    assert (live.state, live.path) == ("dead", "headless")
    assert w.wake(tgt(), NUDGE) == "skip: gateway down (use resume_headless)"
    assert agent_calls(log) == []


def test_resume_headless_runs_local_only_when_the_gateway_is_proven_down(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID)], health="down")
    assert waker(exe).resume_headless(tgt(), NUDGE, str(work)) == "resumed: rc=0"
    (call,) = agent_calls(log)
    assert call["argv"][1:] == ["agent", "--local", "--agent", "main", "--session-id", SID, "--message", NUDGE,
                                "--json", "--timeout", str(hr.OPENCLAW_TURN_TIMEOUT)]
    assert Path(call["cwd"]).resolve() == work.resolve()
    assert "--deliver" not in call["argv"]


def test_resume_headless_refuses_while_the_gateway_is_up(tmp_path: Path) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID)], health="up")
    assert waker(exe).resume_headless(tgt(), NUDGE, None).startswith("skip: live")
    assert agent_calls(log) == []


@pytest.mark.parametrize("health", ["auth", "garbage", "okless"])
def test_ambiguous_gateway_state_fails_closed(tmp_path: Path, health: str) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID)], health=health)
    w = waker(exe)
    live = w.reachable(tgt())
    assert (live.state, live.path) == ("unknown", "none")
    assert w.wake(tgt(), NUDGE) == "skip: liveness unknown"
    assert w.resume_headless(tgt(), NUDGE, None) == "skip: liveness unknown"
    assert agent_calls(log) == []


def test_health_timeout_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID)])
    slow = tmp_path / "slowoc" / "openclaw"
    slow.parent.mkdir()
    slow.write_text(f"#!/bin/sh\nif [ \"$1\" = health ]; then sleep 5; fi\nexec {exe} \"$@\"\n", encoding="utf-8")
    slow.chmod(0o755)
    monkeypatch.setattr(hr, "PROBE_TIMEOUT", 0.5)
    t0 = time.monotonic()
    assert waker(slow).reachable(tgt()).state == "unknown"
    assert time.monotonic() - t0 < 4
    assert agent_calls(log) == []


@pytest.mark.parametrize("out", ["not json", "[]", '{"nosessions": 1}', '{"sessions": "x"}'])
def test_bad_sessions_output_fails_closed(tmp_path: Path, out: str) -> None:
    exe, log = fake_openclaw(tmp_path, sessions_stdout=out)
    w = waker(exe)
    assert w.reachable(tgt()).state == "unknown"
    assert w.wake(tgt(), NUDGE) == "skip: liveness unknown"
    assert agent_calls(log) == []


def test_sessions_command_failure_fails_closed(tmp_path: Path) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID)], sessions_rc=1)
    assert waker(exe).wake(tgt(), NUDGE) == "skip: liveness unknown"
    assert agent_calls(log) == []


def test_same_id_under_two_agents_is_ambiguous_until_the_agent_is_named(tmp_path: Path) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID, agent="main"), oc_session(SID, agent="ops")])
    w = waker(exe)
    assert w.reachable(tgt()).state == "unknown"
    assert w.wake(tgt(), NUDGE) == "skip: liveness unknown"
    assert agent_calls(log) == []
    assert w.wake(tgt(agent="ops"), NUDGE) == "rang: gateway"
    argv = agent_calls(log)[0]["argv"]
    assert argv[argv.index("--agent") + 1] == "ops"


def test_agent_failure_and_timeout_are_reported_without_output(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    exe, _ = fake_openclaw(tmp_path / "fail", sessions=[oc_session(SID)], agent_rc=1)
    line = waker(exe).wake(tgt(), NUDGE)
    assert line == "ring failed: rc=1"
    assert "assistant text" not in line
    slow, _ = fake_openclaw(tmp_path / "slow", sessions=[oc_session(SID)], agent_sleep=5)
    monkeypatch.setattr(hr, "OPENCLAW_WAKE_TIMEOUT", 0.5)
    t0 = time.monotonic()
    assert waker(slow).wake(tgt(), NUDGE) == "ring failed: timed out"
    assert time.monotonic() - t0 < 4


def test_missing_binary(tmp_path: Path) -> None:
    w = hr.OpenClawWaker(openclaw_bin=str(tmp_path / "no-such-openclaw"))
    assert w.wake(tgt(), NUDGE) == "ring failed: no openclaw executable"
    assert w.resume_headless(tgt(), NUDGE, None) == "ring failed: no openclaw executable"
    live = w.reachable(tgt())
    assert (live.state, live.path) == ("unknown", "none")


@pytest.mark.parametrize("bad", ["", "two\nlines", "-m evil", "y" * 4001])
def test_invalid_nudges_never_run_anything(tmp_path: Path, bad: str) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(SID)])
    w = waker(exe)
    assert w.wake(tgt(), bad) == "ring failed: invalid nudge"
    assert w.resume_headless(tgt(), bad, None) == "ring failed: invalid nudge"
    assert read_log(log) == []


def test_nothing_in_a_result_line_leaks_peer_or_server_text(tmp_path: Path) -> None:
    exe, _ = fake_openclaw(tmp_path, sessions=[oc_session(SID)])
    line = waker(exe).wake(tgt(), NUDGE)
    assert line == "rang: gateway"
    assert json.dumps(line).count(SID) == 0
