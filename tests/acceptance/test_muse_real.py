"""Opt-in acceptance against the REAL `muse` binary (docs/research/muse.md, docs/spec/muse.md).

Run with PHONING_TEST_REAL_MUSE=1. Everything happens in a throwaway XDG store with `--provider echo`: no
login, no network model call, no existing Muse session is read or written.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import termios
import time
import uuid
from pathlib import Path

import pytest

from phoning import muse_ring

MUSE = shutil.which("muse")
pytestmark = pytest.mark.skipif(
    os.environ.get("PHONING_TEST_REAL_MUSE") != "1" or MUSE is None,
    reason="opt-in: set PHONING_TEST_REAL_MUSE=1 with `muse` on PATH",
)

NUDGE = "[phoning] New actionable phoning mail for alice arrived while this session was idle. Say ok."


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    dirs = {"data": tmp_path / "data", "config": tmp_path / "config", "run": tmp_path / "run",
            "ws": tmp_path / "ws"}
    for d in dirs.values():
        d.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(dirs["data"]))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(dirs["config"]))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(dirs["run"]))
    monkeypatch.setenv("MUSE_NO_AUTO_UPDATE", "1")
    monkeypatch.setenv("MUSE_LOGIN", "0")
    monkeypatch.delenv("PHONING_HOME", raising=False)
    return dirs


def _muse(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(MUSE), *args], cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=120)


def test_ended_session_is_revived_with_its_context(xdg: dict[str, Path]) -> None:
    sid = str(uuid.uuid4())
    first = _muse(["exec", "--provider", "echo", "--json", "--workspace", str(xdg["ws"]),
                   "--session-id", sid, "first turn PINEAPPLE"], xdg["ws"])
    assert first.returncode == 0
    home = muse_ring.muse_home(None)
    assert home == str(xdg["data"] / "muse")
    reach = muse_ring.reachable(sid, muse_home=home)
    assert reach.state == "dead" and reach.provider == "echo" and reach.resumable
    assert reach.workspace is not None and os.path.realpath(reach.workspace) == os.path.realpath(xdg["ws"])
    assert reach.name, "Muse allocates an automatic session name"
    # by NAME, from a different cwd: the adapter resolves the uuid and passes --workspace
    out = muse_ring.resume_headless(reach.name, NUDGE, str(xdg["run"]), muse_home=home)
    assert out == "resumed: rc=0"
    exported = xdg["run"] / "export.json"
    assert _muse(["export", "--session", sid, "--out", str(exported)], xdg["ws"]).returncode == 0
    doc = json.loads(exported.read_text())
    assert [s["turn_count"] for s in doc["sessions"]] == [2]
    log = muse_ring.find_session_dir(home, sid)
    assert log is not None
    trace = _muse(["trace", "inspect", "--session-log", os.path.join(log, "session.jsonl"), "--all-runs",
                   "--render-mode", "verbose", "--format", "text"], xdg["ws"])
    assert "external_history=true" in trace.stdout, "the prior turns must be sent to the model"


def test_unknown_id_is_not_resumed_and_creates_nothing(xdg: dict[str, Path]) -> None:
    home = muse_ring.muse_home(None)
    out = muse_ring.ring_muse(str(uuid.uuid4()), NUDGE, muse_home=home)
    assert out == "skip: unreachable: no such session"
    assert list((xdg["data"] / "muse").glob("sessions/*/*/*/*")) == []


def test_a_live_tui_session_is_seen_live_and_not_resumed(xdg: dict[str, Path]) -> None:
    env = {**os.environ, "TERM": "xterm-256color"}
    pid, fd = pty.fork()
    if pid == 0:  # child: the TUI
        os.chdir(xdg["ws"])
        os.execvpe(str(MUSE), [str(MUSE), "--provider", "echo", "--trust-workspace"], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    try:
        home = muse_ring.muse_home(None)
        sid = None
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and sid is None:
            ready, _, _ = select.select([fd], [], [], 0.5)  # keep draining the TUI's output
            if ready:
                try:
                    os.read(fd, 65536)
                except OSError:
                    break
            sid = _muse_list(xdg["ws"], env)
        assert sid is not None, "the TUI never registered as a live session"
        reach = muse_ring.reachable(sid, muse_home=home)
        assert reach.state == "live"
        assert muse_ring.resume_headless(sid, NUDGE, muse_home=home).startswith("skip: ")
        # Muse 1.4.6 refuses every sender that is not a model tool call inside a Muse session (docs/research/muse.md E7).
        # If a later build opens local ingress this becomes "rang: ..."; either way a live session is not resumed.
        woke = muse_ring.ring_muse(sid, NUDGE, muse_home=home)
        assert woke == "skip: sender_unverified" or woke.startswith("rang: "), woke
    finally:
        os.kill(pid, signal.SIGKILL)
        _drain_until_eof(fd)  # a tty close blocks on macOS until its output is read
        os.close(fd)
        os.waitpid(pid, 0)


def _muse_list(cwd: Path, env: dict[str, str]) -> str | None:
    done = subprocess.run([str(MUSE), "session-message", "list", "--json"], cwd=cwd, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=30,
                          env={**env, "MUSE_EXPERIMENTAL_EXTERNAL_AGENT_INGRESS": "1"})
    if done.returncode != 0:
        return None
    rows = json.loads(done.stdout).get("sessions", [])
    return str(rows[0]["session_id"]) if rows else None


def _drain_until_eof(fd: int) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if not ready:
            continue
        try:
            if not os.read(fd, 65536):
                return
        except OSError:
            return
