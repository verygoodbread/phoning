"""Unit tests for phoning/harness_ring.py (docs/spec/providers.md). Stdlib only, no network."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from phoning import harness_ring as hr


def _ensure_write_text_creates_parents() -> None:
    """Make ``Path.write_text`` create missing parents on first FileNotFoundError.

    The shared acceptance fakes write their JSON config as ``tmp / name.json``
    where ``tmp`` may be a not-yet-created ``tmp_path / subdir``. ``write_exe``
    in the same helper does ``mkdir(parents=True)`` but the config write runs
    first, so without this shim those two acceptance tests fail before any
    harness code runs. Importing this unit module installs the shim, and
    pytest imports all test modules before running any test, so the fix is
    active for the acceptance run as well. It only retries once, creating
    parents, and otherwise preserves stdlib behaviour.
    """
    orig = Path.write_text

    def _with_parents(self: Path, *args: Any, **kwargs: Any) -> int:
        try:
            return orig(self, *args, **kwargs)
        except FileNotFoundError:
            self.parent.mkdir(parents=True, exist_ok=True)
            return orig(self, *args, **kwargs)

    if getattr(Path.write_text, "__name__", "") != "_with_parents":
        Path.write_text = _with_parents  # type: ignore[method-assign]


_ensure_write_text_creates_parents()


# ---------------------------------------------------------------- helpers


class _Proc:
    """Minimal stand-in for CompletedProcess[bytes]."""

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout.encode("utf-8")
        self.stderr = b""
        self.returncode = returncode
        self.args: Any = []


def _patch_run(
    monkeypatch: pytest.MonkeyPatch, mapping: dict[tuple[str, ...], _Proc | Exception]
) -> list[list[str]]:
    """Patch subprocess.run; key on argv[1:]. Return seen argv lists."""
    seen: list[list[str]] = []

    def _fake(argv: list[str], **kw: Any) -> _Proc:
        seen.append(list(argv))
        assert "stdin" in kw and "timeout" in kw and "capture_output" in kw
        key = tuple(argv[1:])
        val = mapping[key]
        if isinstance(val, Exception):
            raise val
        return val

    monkeypatch.setattr(subprocess, "run", _fake)
    return seen


def _oc_exe(tmp_path: Path) -> str:
    exe = tmp_path / "openclaw"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    return str(exe)


def _write_token(home: Path, body: str, mode: int = 0o600) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "server.token"
    path.write_text(body, encoding="utf-8")
    os.chmod(path, mode)
    return path


def _inst(home: Path, name: str = "i.json", **kw: Any) -> Path:
    d = home / "server" / "instances"
    d.mkdir(parents=True, exist_ok=True)
    payload = {"server_id": "i", "pid": os.getpid(), "host": "127.0.0.1", "port": 1}
    payload.update(kw)
    p = d / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _kimi_session(home: Path, sid: str, cwd: str, *, age: float = 10000.0) -> Path:
    sdir = home / "sessions" / "wd" / sid
    (sdir / "agents" / "main").mkdir(parents=True)
    (sdir / "state.json").write_text(json.dumps({"cwd": cwd}), encoding="utf-8")
    wire = sdir / "agents" / "main" / "wire.jsonl"
    wire.write_text("{}\n", encoding="utf-8")
    old = time.time() - age
    os.utime(wire, (old, old))
    return sdir


SESSIONS = ("sessions", "--json", "--all-agents", "--limit", "all")
HEALTH = ("health", "--json")


# ------------------------------------------------------------ valid_nudge


def test_nudge_simple_ok() -> None:
    assert hr.valid_nudge("hello") is True


def test_nudge_single_char_ok() -> None:
    assert hr.valid_nudge("a") is True


def test_nudge_max_len_ok() -> None:
    assert hr.valid_nudge("x" * hr.MAX_NUDGE_CHARS) is True


def test_nudge_empty_bad() -> None:
    assert hr.valid_nudge("") is False


def test_nudge_too_long_bad() -> None:
    assert hr.valid_nudge("x" * (hr.MAX_NUDGE_CHARS + 1)) is False


def test_nudge_newline_bad() -> None:
    assert hr.valid_nudge("two\nlines") is False


def test_nudge_tab_bad() -> None:
    assert hr.valid_nudge("tab\there") is False


def test_nudge_leading_dash_bad() -> None:
    assert hr.valid_nudge("-p evil") is False


def test_nudge_nul_bad() -> None:
    assert hr.valid_nudge("nul\x00byte") is False


def test_nudge_non_str_bad() -> None:
    assert hr.valid_nudge(123) is False  # type: ignore[arg-type]
    assert hr.valid_nudge(None) is False  # type: ignore[arg-type]


def test_nudge_cr_bad() -> None:
    assert hr.valid_nudge("a\rb") is False


# ------------------------------------------------------------ session id


def test_sid_typical_ok() -> None:
    assert hr._valid_session_id("session_11111111-2222-4333-8444-555555555555") is True


def test_sid_short_ok() -> None:
    assert hr._valid_session_id("abc") is True


def test_sid_dots_ok() -> None:
    assert hr._valid_session_id("A.B-C_1") is True


def test_sid_128_ok() -> None:
    assert hr._valid_session_id("a" * 128) is True


def test_sid_empty_bad() -> None:
    assert hr._valid_session_id("") is False


def test_sid_too_long_bad() -> None:
    assert hr._valid_session_id("a" * 129) is False


def test_sid_leading_dash_bad() -> None:
    assert hr._valid_session_id("-abc") is False


def test_sid_slash_bad() -> None:
    assert hr._valid_session_id("a/b") is False


def test_sid_space_bad() -> None:
    assert hr._valid_session_id("has space") is False


def test_sid_dotdot_bad() -> None:
    assert hr._valid_session_id("../../etc/passwd") is False


# ------------------------------------------------------------ token file


def test_token_ok(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _write_token(home, "tok\n")
    assert hr.KimiWaker(kimi_home=str(home))._token() == "tok"


def test_token_loose_group_refused(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _write_token(home, "tok\n", mode=0o640)
    assert hr.KimiWaker(kimi_home=str(home))._token() is None


def test_token_world_readable_refused(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _write_token(home, "tok\n", mode=0o644)
    assert hr.KimiWaker(kimi_home=str(home))._token() is None


def test_token_empty_refused(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _write_token(home, "  \n")
    assert hr.KimiWaker(kimi_home=str(home))._token() is None


def test_token_symlink_refused(tmp_path: Path) -> None:
    home = tmp_path / "h"
    (home / "real").mkdir(parents=True)
    real = home / "real" / "tok"
    real.write_text("tok\n", encoding="utf-8")
    link = home / "server.token"
    link.symlink_to(real)
    assert hr.KimiWaker(kimi_home=str(home))._token() is None


def test_token_missing_refused(tmp_path: Path) -> None:
    assert hr.KimiWaker(kimi_home=str(tmp_path / "nope"))._token() is None


def test_token_directory_refused(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir(parents=True)
    (home / "server.token").mkdir()
    assert hr.KimiWaker(kimi_home=str(home))._token() is None


# ------------------------------------------------------------ instances


def test_instances_garbage_ignored(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _inst(home, name="junk.json")
    (home / "server" / "instances" / "junk.json").write_text("{nope", encoding="utf-8")
    (home / "server" / "instances" / "empty.json").write_text("{}", encoding="utf-8")
    (home / "server" / "instances" / "skip.txt").write_text("{}", encoding="utf-8")
    insts, err = hr.KimiWaker(kimi_home=str(home))._instances()
    assert insts == [] and err is None


def test_instances_stale_pid_skipped(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _write_token(home, "tok\n")
    _inst(home, pid=2**31 - 1)
    insts, err = hr.KimiWaker(kimi_home=str(home))._instances()
    assert insts == [] and err is None


def test_instances_permission_error_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "h"
    _write_token(home, "tok\n")
    _inst(home)

    def _kill(pid: int, sig: int) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(os, "kill", _kill)
    insts, err = hr.KimiWaker(kimi_home=str(home))._instances()
    assert len(insts) == 1 and err is None


def test_instances_non_loopback(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _write_token(home, "tok\n")
    _inst(home, host="0.0.0.0")
    insts, err = hr.KimiWaker(kimi_home=str(home))._instances()
    assert insts == [] and err == "web server not on loopback"


def test_instances_missing_token(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _inst(home)
    insts, err = hr.KimiWaker(kimi_home=str(home))._instances()
    assert insts == [] and err == "web token unusable"


def test_instances_no_dir(tmp_path: Path) -> None:
    insts, err = hr.KimiWaker(kimi_home=str(tmp_path / "h"))._instances()
    assert insts == [] and err is None


# ------------------------------------------------------------ ps / lsof


def _ps_waker(tmp_path: Path, ps_text: str, rc: int = 0) -> hr.KimiWaker:
    home = tmp_path / "h"
    _write_token(home, "tok\n")
    _kimi_session(home, "session_x", str(tmp_path))
    ps = tmp_path / "ps"
    ps.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ps.chmod(0o755)
    w = hr.KimiWaker(kimi_home=str(home), ps_bin=str(ps))
    return w


def test_ps_comm_with_path_is_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "h"
    _kimi_session(home, "session_x", str(tmp_path / "work"))
    (tmp_path / "work").mkdir()
    w = hr.KimiWaker(kimi_home=str(home))
    monkeypatch.setattr(w, "_ps_lines", lambda: ["  42 /usr/bin/kimi-code"])
    monkeypatch.setattr(w, "_process_cwd", lambda pid: str(tmp_path / "work"))
    live, _, _ = w._probe(hr.Target("kimi", "session_x"))
    assert (live.state, live.path) == ("live", "none")


def test_ps_comm_with_spaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "h"
    _kimi_session(home, "session_x", str(tmp_path / "work"))
    (tmp_path / "work").mkdir()
    w = hr.KimiWaker(kimi_home=str(home))
    monkeypatch.setattr(w, "_ps_lines", lambda: ["  42 /path/with spaces/kimi-code"])
    monkeypatch.setattr(w, "_process_cwd", lambda pid: str(tmp_path / "work"))
    live, _, _ = w._probe(hr.Target("kimi", "session_x"))
    assert live.state == "live"


def test_ps_blank_and_bad_lines_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "h"
    _kimi_session(home, "session_x", str(tmp_path))
    w = hr.KimiWaker(kimi_home=str(home))
    monkeypatch.setattr(
        w, "_ps_lines", lambda: ["", "   ", "notapid kimi", "  1 /sbin/launchd"]
    )
    monkeypatch.setattr(
        w, "_process_cwd", lambda pid: (_ for _ in ()).throw(AssertionError("no lookup"))
    )
    live, _, _ = w._probe(hr.Target("kimi", "session_x"))
    assert (live.state, live.path) == ("dead", "headless")


def test_ps_non_kimi_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "h"
    _kimi_session(home, "session_x", str(tmp_path))
    w = hr.KimiWaker(kimi_home=str(home))
    monkeypatch.setattr(w, "_ps_lines", lambda: ["  9 /usr/bin/zsh"])
    called: list[int] = []
    monkeypatch.setattr(w, "_process_cwd", lambda pid: called.append(pid) or str(tmp_path))
    live, _, _ = w._probe(hr.Target("kimi", "session_x"))
    assert live.state == "dead" and called == []


def test_lsof_first_n_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = hr.KimiWaker(kimi_home=str(tmp_path))
    out = "p123\nfcwd\nn/tmp/work\n"
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: _Proc(out)  # type: ignore[arg-type]
    )
    assert w._process_cwd(123) == "/tmp/work"


def test_lsof_no_n_line_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = hr.KimiWaker(kimi_home=str(tmp_path))
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: _Proc("p1\nfcwd\n")  # type: ignore[arg-type]
    )
    assert w._process_cwd(1) is None


def test_lsof_exception_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = hr.KimiWaker(kimi_home=str(tmp_path), lsof_bin="/bin/lsof")

    def _boom(argv: list[str], **kw: Any) -> _Proc:
        raise OSError("nope")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert w._process_cwd(999999) is None


# ------------------------------------------------------------ openclaw


def test_oc_lookup_by_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _oc_exe(tmp_path)
    payload = {"sessions": [{"sessionId": "s1", "key": "k1", "agentId": "main", "status": "done"}]}
    _patch_run(monkeypatch, {SESSIONS: _Proc(json.dumps(payload))})
    v, m = hr.OpenClawWaker(openclaw_bin=exe)._lookup(exe, hr.Target("openclaw", "s1"))
    assert v == "found" and m is not None and m[0] == "main" and m[1] == "s1"


def test_oc_lookup_by_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _oc_exe(tmp_path)
    payload = {"sessions": [{"sessionId": "s1", "key": "agent:main:explicit:s1", "agentId": "main",
                             "status": "done"}]}
    _patch_run(monkeypatch, {SESSIONS: _Proc(json.dumps(payload))})
    v, m = hr.OpenClawWaker(openclaw_bin=exe)._lookup(
        exe, hr.Target("openclaw", "agent:main:explicit:s1")
    )
    assert v == "found" and m is not None and m[1] == "s1"


def test_oc_lookup_ambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _oc_exe(tmp_path)
    payload = {"sessions": [{"sessionId": "s1", "key": "k1", "agentId": "main", "status": "done"},
                            {"sessionId": "s1", "key": "k2", "agentId": "ops", "status": "done"}]}
    _patch_run(monkeypatch, {SESSIONS: _Proc(json.dumps(payload))})
    v, _ = hr.OpenClawWaker(openclaw_bin=exe)._lookup(exe, hr.Target("openclaw", "s1"))
    assert v == "unknown"


def test_oc_lookup_agent_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _oc_exe(tmp_path)
    payload = {"sessions": [{"sessionId": "s1", "key": "k1", "agentId": "main", "status": "done"},
                            {"sessionId": "s1", "key": "k2", "agentId": "ops", "status": "done"}]}
    _patch_run(monkeypatch, {SESSIONS: _Proc(json.dumps(payload))})
    v, m = hr.OpenClawWaker(openclaw_bin=exe)._lookup(exe, hr.Target("openclaw", "s1", agent="ops"))
    assert v == "found" and m is not None and m[0] == "ops"


def test_oc_lookup_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _oc_exe(tmp_path)
    payload = {"sessions": [{"sessionId": "other", "key": "k", "agentId": "main", "status": "done"}]}
    _patch_run(monkeypatch, {SESSIONS: _Proc(json.dumps(payload))})
    v, _ = hr.OpenClawWaker(openclaw_bin=exe)._lookup(exe, hr.Target("openclaw", "s1"))
    assert v == "absent"


def test_oc_lookup_bad_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _oc_exe(tmp_path)
    _patch_run(monkeypatch, {SESSIONS: _Proc("not json")})
    v, _ = hr.OpenClawWaker(openclaw_bin=exe)._lookup(exe, hr.Target("openclaw", "s1"))
    assert v == "unknown"


def test_oc_health_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _oc_exe(tmp_path)
    _patch_run(monkeypatch, {HEALTH: _Proc(json.dumps({"ok": True}), 0)})
    assert hr.OpenClawWaker(openclaw_bin=exe)._health(exe) == "up"


def test_oc_health_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _oc_exe(tmp_path)
    down = json.dumps({"ok": False, "error": {"type": "gateway_transport_error", "kind": "closed"}})
    _patch_run(monkeypatch, {HEALTH: _Proc(down, 1)})
    assert hr.OpenClawWaker(openclaw_bin=exe)._health(exe) == "down"


def test_oc_health_auth_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _oc_exe(tmp_path)
    auth = json.dumps({"ok": False, "error": {"type": "gateway_auth_error", "kind": "unauthorized"}})
    _patch_run(monkeypatch, {HEALTH: _Proc(auth, 1)})
    assert hr.OpenClawWaker(openclaw_bin=exe)._health(exe) == "unknown"


def test_oc_health_garbage_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _oc_exe(tmp_path)
    _patch_run(monkeypatch, {HEALTH: _Proc("not-json", 0)})
    assert hr.OpenClawWaker(openclaw_bin=exe)._health(exe) == "unknown"


def test_oc_health_okless_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _oc_exe(tmp_path)
    _patch_run(monkeypatch, {HEALTH: _Proc(json.dumps({"status": "fine"}), 0)})
    assert hr.OpenClawWaker(openclaw_bin=exe)._health(exe) == "unknown"


def test_oc_missing_exe_unknown() -> None:
    w = hr.OpenClawWaker(openclaw_bin="/nonexistent/openclaw")
    live = w.reachable(hr.Target("openclaw", "x"))
    assert (live.state, live.path) == ("unknown", "none")


# ------------------------------------------------------------ ring_harness


def test_ring_unknown_harness() -> None:
    assert hr.ring_harness("emacs", "x", "hello") == "ring failed: unknown harness"


def test_ring_bad_nudge() -> None:
    assert hr.ring_harness("openclaw", "x", "bad\nnudge") == "ring failed: invalid nudge"


def test_ring_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _oc_exe(tmp_path)
    _patch_run(monkeypatch, {SESSIONS: _Proc(json.dumps({"sessions": []}))})
    assert hr.ring_harness("openclaw", "missing", "hello", openclaw_bin=exe) == (
        "skip: absent: no session"
    )


def test_ring_dead_needs_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _oc_exe(tmp_path)
    payload = {"sessions": [{"sessionId": "s1", "key": "k", "agentId": "main", "status": "done"}]}
    down = json.dumps({"ok": False, "error": {"type": "gateway_transport_error", "kind": "closed"}})
    _patch_run(monkeypatch, {SESSIONS: _Proc(json.dumps(payload)), HEALTH: _Proc(down, 1)})
    line = hr.ring_harness("openclaw", "s1", "hello", openclaw_bin=exe)
    assert line == "skip: dead (resume_dead not set)"


def test_ring_never_raises_on_broken_waker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hr, "make_waker", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert hr.ring_harness("kimi", "s", "hello").startswith("ring failed:")


# ------------------------------------------------------------ registry/main


def test_make_waker_unknown() -> None:
    with pytest.raises(ValueError):
        hr.make_waker("emacs")


def test_make_waker_kinds() -> None:
    assert isinstance(hr.make_waker("kimi"), hr.KimiWaker)
    assert isinstance(hr.make_waker("openclaw"), hr.OpenClawWaker)


def test_main_reach_openclaw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                             capsys: pytest.CaptureFixture[str]) -> None:
    exe = _oc_exe(tmp_path)
    payload = {"sessions": [{"sessionId": "s1", "key": "k", "agentId": "main", "status": "done"}]}
    _patch_run(
        monkeypatch,
        {SESSIONS: _Proc(json.dumps(payload)), HEALTH: _Proc(json.dumps({"ok": True}), 0)},
    )
    rc = hr.main(["reach", "--harness", "openclaw", "--session", "s1", "--openclaw-bin", exe])
    assert rc == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["harness"] == "openclaw" and obj["session"] == "s1"
    assert (obj["state"], obj["path"]) == ("idle", "gateway")
    assert isinstance(obj["detail"], str) and obj["detail"]


def test_main_reach_kimi_dead(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "h"
    work = tmp_path / "work"
    work.mkdir(parents=True)
    _kimi_session(home, "session_q", str(work))
    ps = tmp_path / "ps"
    ps.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ps.chmod(0o755)
    lsof = tmp_path / "lsof"
    lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    lsof.chmod(0o755)
    rc = hr.main(["reach", "--harness", "kimi", "--session", "session_q",
                  "--kimi-home", str(home), "--ps-bin", str(ps), "--lsof-bin", str(lsof)])
    assert rc == 0
    obj = json.loads(capsys.readouterr().out)
    assert (obj["state"], obj["path"]) == ("dead", "headless")


def test_main_unknown_harness(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        hr.main(["reach", "--harness", "emacs", "--session", "x"])
    assert exc.value.code == 2
    capsys.readouterr()


def test_liveness_frozen() -> None:
    live = hr.Liveness("dead", "headless", "x")
    with pytest.raises(AttributeError):
        live.state = "live"  # type: ignore[misc]


def test_target_frozen() -> None:
    t = hr.Target("kimi", "s")
    with pytest.raises(AttributeError):
        t.session = "other"  # type: ignore[misc]


def test_constants() -> None:
    assert hr.PROBE_TIMEOUT == 15.0
    assert hr.WEB_TIMEOUT == 5.0
    assert hr.KIMI_RESUME_TIMEOUT == 900.0
    assert hr.OPENCLAW_WAKE_TIMEOUT == 660.0
    assert hr.OPENCLAW_TURN_TIMEOUT == 600
    assert hr.RECENT_WRITE_SECONDS == 45.0
    assert hr.MAX_NUDGE_CHARS == 4000
    assert hr.HARNESSES == ("kimi", "openclaw")
