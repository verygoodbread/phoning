"""Unit tests for docs/spec/muse.md: the Muse Code waker adapter (phoning/muse_ring.py)."""

from __future__ import annotations

import ast
import fcntl
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from phoning import muse_ring

SID = "01a1c0de-1111-7651-8222-333344445555"
SID2 = "5eed0001-2222-4941-a333-444455556666"
NUDGE = "[phoning] New actionable phoning mail for alice arrived while this session was idle. Read it."
OUT_SECRET = "SECRET-CHILD-OUTPUT"


class _Done:
    """Minimal CompletedProcess stand-in."""

    def __init__(self, returncode: int = 0, stdout: bytes | str = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Runner:
    """Recording replacement for subprocess.run."""

    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.result: Any = result if result is not None else _Done(0, b"{}")

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append((list(argv), kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> _Runner:
    r = _Runner()
    monkeypatch.setattr(muse_ring.subprocess, "run", r)
    monkeypatch.delenv("PHONING_HOME", raising=False)
    return r


@pytest.fixture
def exe(tmp_path: Path) -> str:
    path = tmp_path / "bin" / "muse"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    home = tmp_path / "musehome"
    (home / "sessions").mkdir(parents=True)
    return home


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    path = tmp_path / "ws dir"
    path.mkdir()
    return path


def make_session(
    home: Path,
    sid: str = SID,
    *,
    workspace: Path | str | None = None,
    provider: str | None = "echo",
    name: str | None = "quiet-harbor",
    day: str = "11",
    lock: bool = True,
    junk_before: int = 0,
) -> Path:
    d = home / "sessions" / "2026" / "10" / day / sid
    d.mkdir(parents=True)
    lines: list[str] = ['{"retained_frame": "session_permission_transaction", "children": []}', "not json at all {"]
    lines += ['{"payload_type": "runtime.session.route_facts", "payload": {"record": {}}}'] * junk_before
    meta: dict[str, Any] = {}
    if workspace is not None:
        meta["workspace_root"] = str(workspace)
    if provider is not None:
        meta["provider_id"] = provider
    lines.append(json.dumps({"payload_type": "runtime.session.metadata", "payload": {"kind": "metadata", "record": meta}}))
    if name is not None:
        lines.append(json.dumps({"payload_type": "session.name.changed",
                                 "payload": {"session_id": sid, "previous_name": None, "new_name": name}}))
    (d / "session.jsonl").write_text("\n".join(lines) + "\n")
    if lock:
        (d / ".session.lock").write_text("")
    return d


@contextmanager
def held(session_dir: Path) -> Iterator[None]:
    fd = os.open(str(session_dir / ".session.lock"), os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def home_arg(store: Path) -> dict[str, Any]:
    return {"muse_home": str(store)}


# ---------------------------------------------------------------- helpers


def test_muse_home_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert muse_ring.muse_home(str(tmp_path / "x")) == str(tmp_path / "x")
    assert muse_ring.muse_home(None) == os.path.expanduser("~/.local/share/muse")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert muse_ring.muse_home(None) == str(tmp_path / "xdg" / "muse")
    monkeypatch.setenv("XDG_DATA_HOME", "")
    assert muse_ring.muse_home(None) == os.path.expanduser("~/.local/share/muse")
    assert muse_ring.muse_home("~/somewhere") == os.path.expanduser("~/somewhere")


def test_muse_exe_rules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exe: str) -> None:
    assert muse_ring.muse_exe(exe) == exe
    plain = tmp_path / "notexec"
    plain.write_text("x")
    plain.chmod(0o644)
    monkeypatch.setattr(muse_ring.shutil, "which", lambda name: exe)
    assert muse_ring.muse_exe(str(plain)) is None  # explicit but unusable: never substitute the PATH muse
    assert muse_ring.muse_exe(str(tmp_path / "missing")) is None
    monkeypatch.setattr(muse_ring.shutil, "which", lambda name: None)
    assert muse_ring.muse_exe(None) is None
    monkeypatch.setattr(muse_ring.shutil, "which", lambda name: exe if name == "muse" else None)
    assert muse_ring.muse_exe(None) == exe


def test_find_session_dir(store: Path, ws: Path) -> None:
    d = make_session(store, workspace=ws)
    assert muse_ring.find_session_dir(str(store), SID) == str(d)
    assert muse_ring.find_session_dir(str(store), SID.upper()) == str(d)
    assert muse_ring.find_session_dir(str(store), SID2) is None
    for bad in ("", "quiet-harbor", "../" + SID, SID + "x", "*"):
        assert muse_ring.find_session_dir(str(store), bad) is None
    # the same id under two dates is ambiguous: fail closed
    dup = store / "sessions" / "2026" / "10" / "12" / SID
    dup.mkdir(parents=True)
    (dup / "session.jsonl").write_text("{}\n")
    assert muse_ring.find_session_dir(str(store), SID) is None
    # a directory without session.jsonl is not a session
    (store / "sessions" / "2026" / "10" / "11" / SID2).mkdir()
    assert muse_ring.find_session_dir(str(store), SID2) is None


def test_missing_store_is_missing_not_an_error(tmp_path: Path) -> None:
    r = muse_ring.reachable(SID, muse_home=str(tmp_path / "nope"))
    assert r.state == "missing" and r.session_id is None and not r.resumable


# ------------------------------------------------------- resolve_target


def test_resolve_uuid_and_name(store: Path, ws: Path) -> None:
    make_session(store, SID, workspace=ws, name="quiet-harbor")
    make_session(store, SID2, workspace=ws, name="calm-meadow", day="12")
    assert muse_ring.resolve_target(SID, str(store)) == (SID, "")
    assert muse_ring.resolve_target(f"  {SID.upper()}  ", str(store)) == (SID, "")
    assert muse_ring.resolve_target("calm-meadow", str(store)) == (SID2, "")
    assert muse_ring.resolve_target("Calm-Meadow", str(store)) == (None, "no such session")
    assert muse_ring.resolve_target("nobody", str(store)) == (None, "no such session")
    assert muse_ring.resolve_target(SID.replace("01a1", "01a2"), str(store)) == (None, "no such session")


def test_resolve_last_name_wins_and_ambiguity(store: Path, ws: Path) -> None:
    d = make_session(store, SID, workspace=ws, name="old-name")
    with (d / "session.jsonl").open("a") as fh:
        fh.write(json.dumps({"payload_type": "session.name.changed",
                             "payload": {"previous_name": "old-name", "new_name": "new-name"}}) + "\n")
    assert muse_ring.resolve_target("old-name", str(store)) == (None, "no such session")
    assert muse_ring.resolve_target("new-name", str(store)) == (SID, "")
    make_session(store, SID2, workspace=ws, name="new-name", day="12")
    assert muse_ring.resolve_target("new-name", str(store)) == (None, "ambiguous name")


def test_resolve_ignores_subagent_logs_and_cleared_names(store: Path, ws: Path) -> None:
    d = make_session(store, SID, workspace=ws, name="shared-name")
    sub = d / "subagent" / SID2
    sub.mkdir(parents=True)
    (sub / "session.jsonl").write_text(json.dumps(
        {"payload_type": "session.name.changed", "payload": {"new_name": "shared-name"}}) + "\n")
    assert muse_ring.resolve_target("shared-name", str(store)) == (SID, "")
    with (d / "session.jsonl").open("a") as fh:
        fh.write(json.dumps({"payload_type": "session.name.changed", "payload": {"new_name": None}}) + "\n")
    assert muse_ring.resolve_target("shared-name", str(store)) == (None, "no such session")


@pytest.mark.parametrize("bad", ["", "   ", "a\x00b", "line\nbreak", "x" * 129, "\x1b[31m"])
def test_resolve_rejects_bad_targets(store: Path, bad: str) -> None:
    assert muse_ring.resolve_target(bad, str(store)) == (None, "bad target")


# ----------------------------------------------------------- reachable


def test_reachable_dead_reads_facts(store: Path, ws: Path) -> None:
    d = make_session(store, workspace=ws, provider="meta", name="quiet-harbor", junk_before=3)
    r = muse_ring.reachable("quiet-harbor", muse_home=str(store))
    assert (r.state, r.session_id, r.name, r.workspace, r.provider) == ("dead", SID, "quiet-harbor", str(ws), "meta")
    assert r.session_dir == str(d) and r.resumable and r.reason == "session lock free"
    assert r.as_dict()["resumable"] is True and r.as_dict()["state"] == "dead"


def test_reachable_live_when_the_lock_is_held(store: Path, ws: Path) -> None:
    d = make_session(store, workspace=ws)
    with held(d):
        r = muse_ring.reachable(SID, muse_home=str(store))
        assert r.state == "live" and r.reason == "session lock held" and not r.resumable
    assert muse_ring.reachable(SID, muse_home=str(store)).state == "dead"


def test_probe_never_creates_or_keeps_a_lock(store: Path, ws: Path) -> None:
    d = make_session(store, workspace=ws, lock=False)
    r = muse_ring.reachable(SID, muse_home=str(store))
    assert r.state == "unknown" and r.reason == "no lock file" and not r.resumable
    assert not (d / ".session.lock").exists()
    (d / ".session.lock").write_text("")
    assert muse_ring.reachable(SID, muse_home=str(store)).state == "dead"
    fd = os.open(str(d / ".session.lock"), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # not left shared-locked by the probe
    finally:
        os.close(fd)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes")
def test_unreadable_lock_is_unknown(store: Path, ws: Path) -> None:
    d = make_session(store, workspace=ws)
    (d / ".session.lock").chmod(0)
    try:
        r = muse_ring.reachable(SID, muse_home=str(store))
        assert r.state == "unknown" and r.reason == "lock unreadable" and not r.resumable
    finally:
        (d / ".session.lock").chmod(0o600)


def test_lock_probe_oserror_is_unknown(store: Path, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_session(store, workspace=ws)

    def boom(fd: int, op: int) -> None:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(muse_ring.fcntl, "flock", boom)
    r = muse_ring.reachable(SID, muse_home=str(store))
    assert r.state == "unknown" and r.reason == "lock probe failed"


def test_metadata_beyond_the_first_400_lines_is_ignored(store: Path, ws: Path) -> None:
    make_session(store, workspace=ws, junk_before=450)
    r = muse_ring.reachable(SID, muse_home=str(store))
    assert r.state == "dead" and r.workspace is None and r.provider is None and not r.resumable


def test_resumable_matrix(store: Path, ws: Path, tmp_path: Path) -> None:
    make_session(store, SID, workspace=ws, provider="local")
    make_session(store, SID2, workspace=tmp_path / "gone", provider="echo", day="12")
    sid3 = "5eedc0de-0000-7a10-9d33-1c2d3e4f5a6b"
    make_session(store, sid3, workspace=ws, provider=None, day="13")
    assert not muse_ring.reachable(SID, muse_home=str(store)).resumable
    assert not muse_ring.reachable(SID2, muse_home=str(store)).resumable
    assert not muse_ring.reachable(sid3, muse_home=str(store)).resumable


# ---------------------------------------------------------------- wake


def _env(call: tuple[list[str], dict[str, Any]]) -> dict[str, str]:
    env = call[1]["env"]
    assert isinstance(env, dict)
    return env


def test_wake_live_success(store: Path, ws: Path, exe: str, runner: _Runner) -> None:
    d = make_session(store, workspace=ws)
    runner.result = _Done(0, json.dumps({"schema_version": 1, "status": "delivered", "receipts": []}).encode())
    with held(d):
        out = muse_ring.wake("quiet-harbor", NUDGE, muse_bin=exe, **home_arg(store))
    assert out == "rang: delivered"
    argv, kw = runner.calls[0]
    assert argv == [exe, "session-message", "send", "--target", SID, "--json"]  # resolved uuid, not the name
    assert kw["input"] == NUDGE.encode() and "stdin" not in kw
    assert kw.get("shell") in (None, False) and isinstance(kw["timeout"], (int, float)) and kw["timeout"] > 0
    assert kw["capture_output"] is True
    env = _env(runner.calls[0])
    assert env["MUSE_EXPERIMENTAL_EXTERNAL_AGENT_INGRESS"] == "1"
    assert env["MUSE_NO_AUTO_UPDATE"] == "1" and env["MUSE_LOGIN"] == "0"


@pytest.mark.parametrize("stdout, expected", [
    (b'{"status": "Not A Word!"}', "rang: sent"),
    (b'{"status": 5}', "rang: sent"),
    (b'{"schema_version": 1}', "rang: sent"),
    (('{"status": "' + "a" * 40 + '"}').encode(), "rang: sent"),
])
def test_wake_status_word_is_sanitized(store: Path, ws: Path, exe: str, runner: _Runner,
                                       stdout: bytes, expected: str) -> None:
    d = make_session(store, workspace=ws)
    runner.result = _Done(0, stdout)
    with held(d):
        assert muse_ring.wake(SID, NUDGE, muse_bin=exe, **home_arg(store)) == expected


@pytest.mark.parametrize("rc, payload, expected", [
    (1, {"status": "unavailable", "error_code": "sender_unverified"}, "skip: sender_unverified"),
    (1, {"status": "unavailable", "error_code": "external_agent_ingress_closed"},
     "skip: external_agent_ingress_closed"),
    (1, {"status": "target_gone", "error_code": "target_not_found"}, "skip: target_not_found"),
    (0, {"status": "x", "error_code": "ambiguous_target_name"}, "skip: ambiguous_target_name"),
    (1, {"error_code": "Bad Code; rm -rf"}, "ring failed: rc=1"),
    (1, {"error_code": "x" * 60}, "ring failed: rc=1"),
    (1, {"error_code": 7}, "ring failed: rc=1"),
    (2, {"status": "ok"}, "ring failed: rc=2"),
])
def test_wake_refusals_and_failures(store: Path, ws: Path, exe: str, runner: _Runner,
                                    rc: int, payload: dict[str, Any], expected: str) -> None:
    d = make_session(store, workspace=ws)
    runner.result = _Done(rc, json.dumps(payload).encode())
    with held(d):
        assert muse_ring.wake(SID, NUDGE, muse_bin=exe, **home_arg(store)) == expected


@pytest.mark.parametrize("stdout", [b"", b"not json", b"[1, 2]", b'"str"', b"\xff\xfe"])
def test_wake_unparseable_reply_is_a_failure_whatever_the_rc(store: Path, ws: Path, exe: str,
                                                            runner: _Runner, stdout: bytes) -> None:
    d = make_session(store, workspace=ws)
    for rc in (0, 1):
        runner.result = _Done(rc, stdout)
        with held(d):
            assert muse_ring.wake(SID, NUDGE, muse_bin=exe, **home_arg(store)) == f"ring failed: rc={rc}"


def test_wake_never_echoes_child_output(store: Path, ws: Path, exe: str, runner: _Runner) -> None:
    d = make_session(store, workspace=ws)
    runner.result = _Done(1, json.dumps({"status": OUT_SECRET, "error_code": OUT_SECRET, "message": OUT_SECRET}).encode(),
                          OUT_SECRET.encode())
    with held(d):
        out = muse_ring.wake(SID, NUDGE, muse_bin=exe, **home_arg(store))
    assert OUT_SECRET not in out and out == "ring failed: rc=1"


def test_wake_exceptions_become_one_line(store: Path, ws: Path, exe: str, runner: _Runner) -> None:
    d = make_session(store, workspace=ws)
    with held(d):
        runner.result = subprocess.TimeoutExpired(cmd="muse", timeout=1)
        assert muse_ring.wake(SID, NUDGE, muse_bin=exe, timeout=1, **home_arg(store)) == "ring failed: timed out"
        runner.result = OSError("secret path /x/y")
        assert muse_ring.wake(SID, NUDGE, muse_bin=exe, **home_arg(store)) == "ring failed: OSError"
        runner.result = ValueError("boom")
        assert muse_ring.wake(SID, NUDGE, muse_bin=exe, **home_arg(store)) == "ring failed: ValueError"


def test_wake_runs_nothing_unless_the_session_is_live(store: Path, ws: Path, exe: str, runner: _Runner) -> None:
    make_session(store, SID, workspace=ws)  # dead
    make_session(store, SID2, workspace=ws, lock=False, day="12")  # unknown
    assert muse_ring.wake(SID, NUDGE, muse_bin=exe, **home_arg(store)) == \
        "skip: not running (resume_headless wakes ended sessions)"
    assert muse_ring.wake(SID2, NUDGE, muse_bin=exe, **home_arg(store)) == "skip: liveness unknown"
    assert muse_ring.wake("nobody", NUDGE, muse_bin=exe, **home_arg(store)) == "skip: unreachable: no such session"
    assert muse_ring.wake("", NUDGE, muse_bin=exe, **home_arg(store)) == "skip: unreachable: bad target"
    assert runner.calls == []


# ------------------------------------------------------- resume_headless


def test_resume_dead_session_exact_command(store: Path, ws: Path, exe: str, runner: _Runner,
                                           tmp_path: Path) -> None:
    make_session(store, workspace=ws, provider="meta")
    runner.result = _Done(0, OUT_SECRET.encode(), OUT_SECRET.encode())
    out = muse_ring.resume_headless("quiet-harbor", NUDGE, muse_bin=exe, home=str(tmp_path / "hh"), **home_arg(store))
    assert out == "resumed: rc=0"
    argv, kw = runner.calls[0]
    assert argv == [exe, "exec", "--json", "--provider", "meta", "--workspace", str(ws),
                    "--session-id", SID, "--", NUDGE]
    assert kw["stdin"] == subprocess.DEVNULL and "input" not in kw
    assert kw["cwd"] == str(ws) and kw["capture_output"] is True
    assert kw.get("shell") in (None, False) and kw["timeout"] > 0
    env = _env(runner.calls[0])
    assert env["PHONING_HOME"] == str(tmp_path / "hh")
    assert env["MUSE_NO_AUTO_UPDATE"] == "1" and env["MUSE_LOGIN"] == "0"
    assert env.get("MUSE_EXPERIMENTAL_EXTERNAL_AGENT_INGRESS") == os.environ.get("MUSE_EXPERIMENTAL_EXTERNAL_AGENT_INGRESS")
    assert OUT_SECRET not in out


def test_resume_env_has_no_phoning_home_unless_given(store: Path, ws: Path, exe: str, runner: _Runner) -> None:
    make_session(store, workspace=ws)
    muse_ring.resume_headless(SID, NUDGE, muse_bin=exe, **home_arg(store))
    assert "PHONING_HOME" not in _env(runner.calls[0])
    muse_ring.resume_headless(SID, NUDGE, muse_bin=exe, home="~/hh-x", **home_arg(store))
    assert _env(runner.calls[1])["PHONING_HOME"] == os.path.abspath(os.path.expanduser("~/hh-x"))


@pytest.mark.parametrize("rc", [1, 3, 137])
def test_resume_reports_the_return_code(store: Path, ws: Path, exe: str, runner: _Runner, rc: int) -> None:
    make_session(store, workspace=ws)
    runner.result = _Done(rc)
    assert muse_ring.resume_headless(SID, NUDGE, muse_bin=exe, **home_arg(store)) == f"resumed: rc={rc}"


def test_resume_never_touches_live_or_unknown_sessions(store: Path, ws: Path, exe: str, runner: _Runner) -> None:
    live = make_session(store, SID, workspace=ws)
    make_session(store, SID2, workspace=ws, lock=False, day="12", name="other-name")
    with held(live):
        assert muse_ring.resume_headless(SID, NUDGE, muse_bin=exe, **home_arg(store)) == \
            "skip: live (a running Muse session is never resumed)"
        assert muse_ring.resume_headless("quiet-harbor", NUDGE, muse_bin=exe, **home_arg(store)).startswith("skip: live")
    assert muse_ring.resume_headless(SID2, NUDGE, muse_bin=exe, **home_arg(store)) == "skip: liveness unknown"
    assert runner.calls == []


def test_resume_never_creates_a_session_for_an_unknown_target(store: Path, exe: str, runner: _Runner) -> None:
    for t in (SID, "typo-name", "../../etc", ""):
        out = muse_ring.resume_headless(t, NUDGE, muse_bin=exe, **home_arg(store))
        assert out.startswith("skip: unreachable: ")
    assert runner.calls == []


def test_resume_refuses_what_exec_cannot_do(store: Path, ws: Path, tmp_path: Path, exe: str,
                                            runner: _Runner) -> None:
    make_session(store, SID, workspace=ws, provider="local")
    make_session(store, SID2, workspace=tmp_path / "gone", day="12")
    sid3 = "5eedc0de-0000-7a10-9d33-1c2d3e4f5a6b"
    make_session(store, sid3, workspace=None, provider="echo", day="13")
    assert muse_ring.resume_headless(SID, NUDGE, muse_bin=exe, **home_arg(store)) == \
        "skip: unreachable: provider cannot be resumed headlessly"
    assert muse_ring.resume_headless(SID2, NUDGE, muse_bin=exe, **home_arg(store)) == \
        "skip: unreachable: workspace gone"
    assert muse_ring.resume_headless(sid3, NUDGE, muse_bin=exe, **home_arg(store)) == \
        "skip: unreachable: workspace gone"
    assert runner.calls == []


def test_resume_cwd_override_keeps_workspace_flag(store: Path, ws: Path, tmp_path: Path, exe: str,
                                                  runner: _Runner) -> None:
    make_session(store, workspace=ws)
    other = tmp_path / "other"
    other.mkdir()
    assert muse_ring.resume_headless(SID, NUDGE, str(other), muse_bin=exe, **home_arg(store)) == "resumed: rc=0"
    argv, kw = runner.calls[0]
    assert kw["cwd"] == str(other) and argv[argv.index("--workspace") + 1] == str(ws)
    assert muse_ring.resume_headless(SID, NUDGE, str(tmp_path / "gone"), muse_bin=exe, **home_arg(store)) == \
        "skip: unreachable: cwd gone"
    assert len(runner.calls) == 1


def test_resume_extra_args_go_before_the_separator(store: Path, ws: Path, exe: str, runner: _Runner) -> None:
    make_session(store, workspace=ws)
    out = muse_ring.resume_headless(SID, NUDGE, muse_bin=exe, extra_args=["--approval-mode", "never"],
                                    **home_arg(store))
    assert out == "resumed: rc=0"
    argv = runner.calls[0][0]
    sep = argv.index("--")
    assert argv[sep - 2:sep] == ["--approval-mode", "never"] and argv[sep + 1:] == [NUDGE]


@pytest.mark.parametrize("bad", [
    ["--session-id", "x"], ["--session-id=x"], ["--workspace", "/x"], ["--workspace=/x"],
    ["--provider", "meta"], ["--provider=meta"], ["ok", "a\x00b"], [5],
])
def test_resume_rejects_dangerous_extra_args(store: Path, ws: Path, exe: str, runner: _Runner,
                                             bad: list[Any]) -> None:
    make_session(store, workspace=ws)
    out = muse_ring.resume_headless(SID, NUDGE, muse_bin=exe, extra_args=bad, **home_arg(store))
    assert out == "ring failed: invalid extra args" and runner.calls == []


def test_resume_exceptions_become_one_line(store: Path, ws: Path, exe: str, runner: _Runner) -> None:
    make_session(store, workspace=ws)
    runner.result = subprocess.TimeoutExpired(cmd="muse", timeout=1)
    assert muse_ring.resume_headless(SID, NUDGE, muse_bin=exe, timeout=1, **home_arg(store)) == \
        "ring failed: timed out"
    runner.result = PermissionError("x")
    assert muse_ring.resume_headless(SID, NUDGE, muse_bin=exe, **home_arg(store)) == "ring failed: PermissionError"


# ------------------------------------------------- nudge/exe validation


@pytest.mark.parametrize("bad", ["", "-flag", "--session-id x", "two\nlines", "tab\there", "nul\x00", "x\x7f",
                                 "esc\x1b[0m", "x" * (muse_ring.NUDGE_MAX + 1)])
def test_invalid_nudges_run_nothing(store: Path, ws: Path, exe: str, runner: _Runner, bad: str) -> None:
    d = make_session(store, workspace=ws)
    with held(d):
        assert muse_ring.wake(SID, bad, muse_bin=exe, **home_arg(store)) == "ring failed: invalid nudge"
    assert muse_ring.resume_headless(SID, bad, muse_bin=exe, **home_arg(store)) == "ring failed: invalid nudge"
    assert muse_ring.ring_muse(SID, bad, muse_bin=exe, **home_arg(store)) == "ring failed: invalid nudge"
    assert runner.calls == []


def test_non_string_nudge_is_invalid(store: Path, ws: Path, exe: str, runner: _Runner) -> None:
    make_session(store, workspace=ws)
    assert muse_ring.resume_headless(SID, 5, muse_bin=exe, **home_arg(store)) == "ring failed: invalid nudge"  # type: ignore[arg-type]
    assert runner.calls == []


def test_nudge_of_exactly_the_maximum_length_is_accepted(store: Path, ws: Path, exe: str, runner: _Runner) -> None:
    make_session(store, workspace=ws)
    nudge = "n" * muse_ring.NUDGE_MAX
    assert muse_ring.resume_headless(SID, nudge, muse_bin=exe, **home_arg(store)) == "resumed: rc=0"


def test_missing_executable(store: Path, ws: Path, runner: _Runner, monkeypatch: pytest.MonkeyPatch,
                            tmp_path: Path) -> None:
    make_session(store, workspace=ws)
    monkeypatch.setattr(muse_ring.shutil, "which", lambda name: None)
    for fn in (muse_ring.wake, muse_ring.resume_headless, muse_ring.ring_muse):
        assert fn(SID, NUDGE, muse_bin=str(tmp_path / "nope"), **home_arg(store)) == "ring failed: no muse executable"
        assert fn(SID, NUDGE, **home_arg(store)) == "ring failed: no muse executable"
    assert runner.calls == []


# -------------------------------------------------------------- ring_muse


def test_ring_muse_routes_by_liveness(store: Path, ws: Path, exe: str, runner: _Runner) -> None:
    d = make_session(store, SID, workspace=ws)
    make_session(store, SID2, workspace=ws, lock=False, day="12")
    runner.result = _Done(0, b'{"status": "delivered"}')
    with held(d):  # live: session-message, never exec
        assert muse_ring.ring_muse(SID, NUDGE, muse_bin=exe, **home_arg(store)) == "rang: delivered"
    assert [c[0][1] for c in runner.calls] == ["session-message"]
    runner.calls.clear()
    assert muse_ring.ring_muse(SID, NUDGE, muse_bin=exe, **home_arg(store)) == "resumed: rc=0"  # dead: exec
    assert [c[0][1] for c in runner.calls] == ["exec"]
    runner.calls.clear()
    assert muse_ring.ring_muse(SID, NUDGE, muse_bin=exe, resume_dead=False, **home_arg(store)) == \
        "skip: not running (resume not enabled)"
    assert muse_ring.ring_muse(SID2, NUDGE, muse_bin=exe, **home_arg(store)) == "skip: liveness unknown"
    assert muse_ring.ring_muse("nobody", NUDGE, muse_bin=exe, **home_arg(store)) == "skip: unreachable: no such session"
    assert runner.calls == []


def test_ring_muse_passes_options_through(store: Path, ws: Path, tmp_path: Path, exe: str, runner: _Runner) -> None:
    make_session(store, workspace=ws)
    other = tmp_path / "o"
    other.mkdir()
    out = muse_ring.ring_muse(SID, NUDGE, muse_bin=exe, cwd=str(other), home=str(tmp_path / "hh"),
                              extra_args=["--permission-profile", "p"], **home_arg(store))
    assert out == "resumed: rc=0"
    argv, kw = runner.calls[0]
    assert kw["cwd"] == str(other) and _env(runner.calls[0])["PHONING_HOME"] == str(tmp_path / "hh")
    assert argv[argv.index("--") - 2:argv.index("--")] == ["--permission-profile", "p"]


def test_ring_muse_wake_refusal_is_a_skip_and_never_falls_through_to_resume(store: Path, ws: Path, exe: str,
                                                                          runner: _Runner) -> None:
    d = make_session(store, workspace=ws)
    runner.result = _Done(1, b'{"status": "unavailable", "error_code": "sender_unverified"}')
    with held(d):
        assert muse_ring.ring_muse(SID, NUDGE, muse_bin=exe, **home_arg(store)) == "skip: sender_unverified"
    assert all(c[0][1] == "session-message" for c in runner.calls) and len(runner.calls) == 1


# ------------------------------------------------------------------ main


def test_main_reach_prints_json(store: Path, ws: Path, capsys: pytest.CaptureFixture[str],
                                monkeypatch: pytest.MonkeyPatch) -> None:
    d = make_session(store, workspace=ws, provider="meta")

    def no_spawn(*a: Any, **k: Any) -> Any:
        raise AssertionError("reach must never start a process")

    monkeypatch.setattr(muse_ring.subprocess, "run", no_spawn)
    assert muse_ring.main(["reach", "quiet-harbor", "--muse-home", str(store)]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["state"] == "dead" and row["session_id"] == SID and row["provider"] == "meta"
    assert row["resumable"] is True and row["workspace"] == str(ws)
    with held(d):
        assert muse_ring.main(["reach", SID, "--muse-home", str(store)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "live"
    (d / ".session.lock").unlink()
    assert muse_ring.main(["reach", SID, "--muse-home", str(store)]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "unknown"
    assert muse_ring.main(["reach", "nobody", "--muse-home", str(store)]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "missing"


def test_module_runs_as_a_script(store: Path, ws: Path) -> None:
    make_session(store, workspace=ws)
    root = str(Path(muse_ring.__file__).resolve().parent.parent)
    done = subprocess.run(
        [sys.executable, "-m", "phoning.muse_ring", "reach", SID, "--muse-home", str(store)],
        env={**os.environ, "PYTHONPATH": root}, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["state"] == "dead"


def test_main_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert muse_ring.main([]) == 2
    assert muse_ring.main(["reach"]) == 2
    assert muse_ring.main(["wake", "x"]) == 2
    assert muse_ring.main(["reach", "x", "--bogus"]) == 2
    capsys.readouterr()


# ----------------------------------------------------------- source rules


def test_source_has_no_shell_and_no_back_imports() -> None:
    src = Path(muse_ring.__file__).read_text()
    assert not re.search(r"shell\s*=\s*True", src)
    assert "os.system" not in src and "os.popen" not in src
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {m for m in imported if m == "phoning" or m.startswith("phoning.")}, imported
    assert "os.O_CREAT" not in src and "fcntl.LOCK_EX" not in src
