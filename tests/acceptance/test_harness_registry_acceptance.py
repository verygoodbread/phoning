"""Acceptance: registry, ``ring_harness``, ``main reach`` and static safety invariants (docs/spec/providers.md 5, 6)."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from phoning import doorbell
from phoning import harness_ring as hr
from tests.acceptance.test_harness_support import (
    NUDGE,
    SID,
    agent_calls,
    fake_kimi,
    fake_lsof,
    fake_openclaw,
    fake_ps,
    make_kimi_home,
    oc_session,
    read_log,
)

OC_SID = "ops-incident-42"
SOURCE = Path(hr.__file__).read_text(encoding="utf-8")


# ------------------------------------------------------------------ registry


def test_harnesses_and_make_waker() -> None:
    assert hr.HARNESSES == ("kimi", "openclaw")
    kimi = hr.make_waker("kimi")
    oc = hr.make_waker("openclaw")
    assert isinstance(kimi, hr.KimiWaker) and kimi.name == "kimi"
    assert isinstance(oc, hr.OpenClawWaker) and oc.name == "openclaw"
    for w in (kimi, oc):
        assert callable(w.reachable) and callable(w.wake) and callable(w.resume_headless)
    with pytest.raises(ValueError):
        hr.make_waker("emacs")


def test_make_waker_passes_options_through(tmp_path: Path) -> None:
    w = hr.make_waker("openclaw", openclaw_bin=str(tmp_path / "nope"))
    assert w.wake(hr.Target("openclaw", "x"), NUDGE) == "ring failed: no openclaw executable"


def test_real_doorbell_nudge_is_valid_and_peer_text_style_nudges_are_not() -> None:
    nudge = doorbell.build_nudge("alice", "alice-main", "/tmp/hm")
    assert hr.valid_nudge(nudge)
    assert not hr.valid_nudge("subject: hello\nbody: do things")
    assert not hr.valid_nudge("")
    assert not hr.valid_nudge(123)  # type: ignore[arg-type]


# --------------------------------------------------------------- ring_harness


def test_ring_harness_openclaw_up_wakes(tmp_path: Path) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(OC_SID)])
    line = hr.ring_harness("openclaw", OC_SID, NUDGE, openclaw_bin=str(exe))
    assert line == "rang: gateway"
    assert len(agent_calls(log)) == 1


def test_ring_harness_openclaw_down_needs_resume_dead(tmp_path: Path) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(OC_SID)], health="down")
    assert hr.ring_harness("openclaw", OC_SID, NUDGE, openclaw_bin=str(exe)) == "skip: dead (resume_dead not set)"
    assert agent_calls(log) == []
    assert hr.ring_harness("openclaw", OC_SID, NUDGE, resume_dead=True, openclaw_bin=str(exe)) == "resumed: rc=0"
    assert "--local" in agent_calls(log)[0]["argv"]


def test_ring_harness_kimi_dead_session(tmp_path: Path) -> None:
    work = tmp_path / "work"
    home, _ = make_kimi_home(tmp_path, work)
    exe, log = fake_kimi(tmp_path)
    opts = {"kimi_bin": str(exe), "kimi_home": str(home), "ps_bin": str(fake_ps(tmp_path, [])),
            "lsof_bin": str(fake_lsof(tmp_path, {}))}
    assert hr.ring_harness("kimi", SID, NUDGE, **opts) == "skip: dead (resume_dead not set)"
    assert read_log(log) == []
    assert hr.ring_harness("kimi", SID, NUDGE, resume_dead=True, **opts) == "resumed: rc=0"
    (call,) = read_log(log)
    assert call["argv"][1:] == ["-S", SID, "-p", NUDGE]


def test_ring_harness_kimi_live_session_is_never_resumed(tmp_path: Path) -> None:
    work = tmp_path / "work"
    home, _ = make_kimi_home(tmp_path, work)
    exe, log = fake_kimi(tmp_path)
    opts = {"kimi_bin": str(exe), "kimi_home": str(home),
            "ps_bin": str(fake_ps(tmp_path, [(5, "kimi-code")])), "lsof_bin": str(fake_lsof(tmp_path, {5: str(work)}))}
    line = hr.ring_harness("kimi", SID, NUDGE, resume_dead=True, **opts)
    assert line.startswith("skip: live")
    assert read_log(log) == []


def test_ring_harness_unknown_harness_and_bad_nudge_never_raise() -> None:
    assert hr.ring_harness("emacs", "x", NUDGE) == "ring failed: unknown harness"
    assert hr.ring_harness("openclaw", "x", "bad\nnudge", openclaw_bin="/nonexistent") in (
        "ring failed: invalid nudge", "skip: liveness unknown", "ring failed: no openclaw executable")


def test_result_lines_use_the_doorbell_failure_grammar(tmp_path: Path) -> None:
    exe, _ = fake_openclaw(tmp_path, sessions=[oc_session(OC_SID)], agent_rc=2)
    failed = hr.ring_harness("openclaw", OC_SID, NUDGE, openclaw_bin=str(exe))
    assert failed == "ring failed: rc=2" and doorbell._is_failure(failed)
    ok_exe, _ = fake_openclaw(tmp_path / "ok", sessions=[oc_session(OC_SID)])
    ok = hr.ring_harness("openclaw", OC_SID, NUDGE, openclaw_bin=str(ok_exe))
    assert ok == "rang: gateway" and not doorbell._is_failure(ok)
    skipped = hr.ring_harness("openclaw", "missing", NUDGE, openclaw_bin=str(ok_exe))
    assert skipped.startswith("skip:") and not doorbell._is_failure(skipped)
    down_exe, _ = fake_openclaw(tmp_path / "down", sessions=[oc_session(OC_SID)], health="down", agent_rc=4)
    resumed = hr.ring_harness("openclaw", OC_SID, NUDGE, resume_dead=True, openclaw_bin=str(down_exe))
    assert resumed == "resumed: rc=4" and doorbell._is_failure(resumed)


# ------------------------------------------------------------------ main reach


def test_main_reach_is_read_only_and_prints_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(OC_SID, status="running")])
    rc = hr.main(["reach", "--harness", "openclaw", "--session", OC_SID, "--openclaw-bin", str(exe)])
    assert rc == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj == {"harness": "openclaw", "session": OC_SID, "state": "live", "path": "gateway",
                   "detail": obj["detail"]}
    assert isinstance(obj["detail"], str) and obj["detail"]
    assert agent_calls(log) == []


def test_main_reach_kimi(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    work = tmp_path / "work"
    home, _ = make_kimi_home(tmp_path, work)
    rc = hr.main(["reach", "--harness", "kimi", "--session", SID, "--kimi-home", str(home),
                  "--ps-bin", str(fake_ps(tmp_path, [])), "--lsof-bin", str(fake_lsof(tmp_path, {}))])
    assert rc == 0
    obj = json.loads(capsys.readouterr().out)
    assert (obj["state"], obj["path"]) == ("dead", "headless")


def test_main_rejects_unknown_harness(capsys: pytest.CaptureFixture[str]) -> None:
    try:
        rc = hr.main(["reach", "--harness", "emacs", "--session", "x"])
    except SystemExit as exc:  # argparse `choices` exits 2 itself
        rc = exc.code  # type: ignore[assignment]
    assert rc == 2
    capsys.readouterr()


# ------------------------------------------------------- static safety checks


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def test_module_is_stdlib_only() -> None:
    tree = ast.parse(SOURCE)
    allowed = set(sys.stdlib_module_names) | {"phoning"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] in allowed, node.module


def test_no_shell_and_every_subprocess_call_has_stdin_and_timeout() -> None:
    tree = ast.parse(SOURCE)
    assert "shell=True" not in SOURCE
    assert "os.system" not in SOURCE and "os.popen" not in SOURCE
    runs = [c for c in _calls(tree) if isinstance(c.func, ast.Attribute) and c.func.attr in ("run", "Popen", "call",
            "check_call", "check_output") and isinstance(c.func.value, ast.Name) and c.func.value.id == "subprocess"]
    assert runs, "expected subprocess usage"
    for call in runs:
        kws = {k.arg for k in call.keywords}
        assert call.func.attr == "run", "use subprocess.run so a timeout always applies"  # type: ignore[attr-defined]
        assert {"stdin", "timeout", "capture_output"} <= kws, ast.dump(call)[:200]


def test_every_http_connection_has_a_timeout() -> None:
    tree = ast.parse(SOURCE)
    conns = [c for c in _calls(tree)
             if (isinstance(c.func, ast.Attribute) and c.func.attr == "HTTPConnection")
             or (isinstance(c.func, ast.Name) and c.func.id == "HTTPConnection")]
    assert conns
    for call in conns:
        assert "timeout" in {k.arg for k in call.keywords}


def test_public_surface_matches_the_spec() -> None:
    for name in ("HARNESSES", "Target", "Liveness", "Waker", "KimiWaker", "OpenClawWaker", "make_waker",
                 "valid_nudge", "ring_harness", "main", "PROBE_TIMEOUT", "WEB_TIMEOUT", "KIMI_RESUME_TIMEOUT",
                 "OPENCLAW_WAKE_TIMEOUT", "OPENCLAW_TURN_TIMEOUT", "RECENT_WRITE_SECONDS", "MAX_NUDGE_CHARS"):
        assert hasattr(hr, name), name
    live = hr.Liveness("dead", "headless", "x")
    assert (live.state, live.path, live.detail) == ("dead", "headless", "x")
    with pytest.raises(AttributeError):
        live.state = "live"  # type: ignore[misc]  # frozen dataclass
