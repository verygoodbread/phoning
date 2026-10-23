"""Unit tests for doorbell ``--ring codex-queue`` and ``--resume-dead``."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from phoning import doorbell
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import inbound


def _write_rollout(path: Path, cwd: str | None) -> None:
    """Write a minimal rollout file with one session_meta line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"id": "tid-1"}
    if cwd is not None:
        payload["cwd"] = cwd
    path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n")


def test_thread_cwd_picks_newest(tmp_path: Path) -> None:
    """Newest rollout by mtime supplies the cwd."""
    tid = "11111111-2222-3333-4444-555555555555"
    older = tmp_path / "sessions" / "2026" / f"rollout-2026-01-01T00-00-00-{tid}.jsonl"
    newer = tmp_path / "sessions" / "2026" / "10" / f"rollout-2026-10-11T16-00-00-{tid}.jsonl"
    _write_rollout(older, "/old")
    _write_rollout(newer, "/new dir")
    now = time.time()
    os.utime(older, (now - 100.0, now - 100.0))
    os.utime(newer, (now, now))
    assert doorbell.thread_cwd(str(tmp_path), tid) == "/new dir"


def test_thread_cwd_falls_back_to_home(tmp_path: Path) -> None:
    """Missing files or missing cwd fall back to the home directory."""
    assert doorbell.thread_cwd(str(tmp_path), "nope-nope") == os.path.expanduser("~")
    tid = "22222222-2222-3333-4444-555555555555"
    _write_rollout(tmp_path / "sessions" / f"rollout-x-{tid}.jsonl", None)
    assert doorbell.thread_cwd(str(tmp_path), tid) == os.path.expanduser("~")


def test_codex_exe_prefers_explicit_bin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``--codex-bin`` wins; otherwise PATH is used."""
    monkeypatch.setattr(doorbell.shutil, "which", lambda _name: "/usr/bin/codex")
    assert doorbell._codex_exe(str(tmp_path / "other")) == str(tmp_path / "other")
    monkeypatch.setattr(doorbell.shutil, "which", lambda _name: None)
    assert doorbell._codex_exe(str(tmp_path / "other")) == str(tmp_path / "other")
    assert doorbell._codex_exe(None) is None
    monkeypatch.setattr(doorbell.shutil, "which", lambda _name: "/usr/bin/codex")
    assert doorbell._codex_exe(None) == "/usr/bin/codex"


class _Done:
    """Minimal stand-in for CompletedProcess."""

    def __init__(self, rc: int) -> None:
        self.returncode = rc
        self.stdout = b"SECRET-OUTPUT"
        self.stderr = b"SECRET-OUTPUT"


def _patch_run(monkeypatch: pytest.MonkeyPatch, rc: int, seen: dict[str, Any]) -> None:
    """Capture argv/kwargs and return ``rc`` with secret-marked output."""

    def _fake(
        argv: list[str],
        *,
        stdin: Any = None,
        stdout: Any = None,
        stderr: Any = None,
        capture_output: bool | None = None,
        timeout: float | None = None,
    ) -> _Done:
        seen["argv"] = list(argv)
        seen["stdin"] = stdin
        seen["stdout"] = stdout
        seen["stderr"] = stderr
        seen["capture_output"] = capture_output
        seen["timeout"] = timeout
        seen["shell"] = False
        return _Done(rc)

    monkeypatch.setattr(doorbell.subprocess, "run", _fake)


def test_queue_success_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Live queue uses list argv, DEVNULL stdin, capture and 30 s timeout."""
    monkeypatch.setattr(doorbell.shutil, "which", lambda _name: None)
    seen: dict[str, Any] = {}
    _patch_run(monkeypatch, 0, seen)
    home = str(tmp_path / "home")
    out = doorbell.ring_codex_queue(
        "alice", "alice-main", str(tmp_path), "tid-1", str(tmp_path / "codex"), False, home
    )
    assert out == "rang: queued"
    argv = seen["argv"]
    assert argv[:4] == [str(tmp_path / "codex"), "queue", "--thread", "tid-1"]
    assert argv[4] == "--message"
    assert argv[5].startswith("[phoning]") and "alice-main" in argv[5]
    assert f"PHONING_HOME={home}" in argv[5]
    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["capture_output"] is True
    assert seen["timeout"] == 30.0
    assert "SECRET-OUTPUT" not in out


def test_queue_failure_keeps_pending_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-zero queue exit reports rc without echoing subprocess output."""
    monkeypatch.setattr(doorbell.shutil, "which", lambda _name: None)
    seen: dict[str, Any] = {}
    _patch_run(monkeypatch, 3, seen)
    out = doorbell.ring_codex_queue(
        "alice", "alice-main", str(tmp_path), "tid-1", str(tmp_path / "codex"), False, None
    )
    assert out == "ring failed: rc=3"
    assert "SECRET-OUTPUT" not in out
    assert doorbell._is_failure(out) is True
    assert doorbell._is_failure("rang: queued") is False
    assert doorbell._is_failure("resumed: rc=0") is False
    assert doorbell._is_failure("resumed: rc=2") is True


def test_resume_dead_uses_exec_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Dead threads resume headless with the rollout cwd."""
    codex_home = tmp_path / "cx"
    tid = "0190abcd-1234-7abc-8def-0123456789ab"
    work = tmp_path / "proj"
    work.mkdir()
    _write_rollout(codex_home / "sessions" / f"rollout-2026-01-01T00-03-31-{tid}.jsonl", str(work))
    monkeypatch.setattr(doorbell.shutil, "which", lambda _name: None)
    seen: dict[str, Any] = {}
    _patch_run(monkeypatch, 0, seen)
    out = doorbell.ring_codex_queue(
        "alice",
        "alice-main",
        str(codex_home),
        tid,
        str(tmp_path / "codex"),
        True,
        str(tmp_path / "home"),
    )
    assert out == "resumed: rc=0"
    assert seen["argv"][:6] == [
        str(tmp_path / "codex"),
        "exec",
        "--skip-git-repo-check",
        "-C",
        str(work),
        "resume",
    ]
    assert seen["argv"][6] == tid
    assert seen["argv"][7].startswith("[phoning]")
    assert seen["capture_output"] is True
    assert seen["timeout"] == 600.0


def test_resume_dead_live_lock_queues(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An existing writer lock means the thread is live: queue as usual."""
    codex_home = tmp_path / "cx"
    locks = codex_home / "thread-writer-locks"
    locks.mkdir(parents=True)
    (locks / "tid-9.lock").write_text("")
    monkeypatch.setattr(doorbell.shutil, "which", lambda _name: None)
    seen: dict[str, Any] = {}
    _patch_run(monkeypatch, 0, seen)
    out = doorbell.ring_codex_queue(
        "alice", "alice-main", str(codex_home), "tid-9", str(tmp_path / "codex"), True, None
    )
    assert out == "rang: queued"
    assert seen["argv"][1] == "queue"


def test_queue_without_thread_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--ring codex-queue`` without ``--codex-thread`` is a usage error."""
    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    Mailbox(tmp_path / "home" / "alice")
    rc = doorbell.main(["alice", "--once", "--ring", "codex-queue", "--codex-bin", "/bin/false"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.strip() != "" and len(err.strip().splitlines()) == 1


def test_failed_queue_keeps_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed queue ring leaves pending set for the next interval."""
    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    box = Mailbox(tmp_path / "home" / "alice")
    inbound(box, "bob", "f" * 32, "ask", "q", "b")
    monkeypatch.setattr(doorbell.shutil, "which", lambda _name: None)
    seen: dict[str, Any] = {}
    _patch_run(monkeypatch, 3, seen)
    codex_home = tmp_path / "cx"
    codex_home.mkdir()
    rc = doorbell.main(
        [
            "alice",
            "--once",
            "--ring",
            "codex-queue",
            "--codex-bin",
            str(tmp_path / "codex"),
            "--codex-home",
            str(codex_home),
            "--codex-thread",
            "tid-1",
            "--min-interval",
            "0",
        ]
    )
    assert rc == 0
    assert "ring failed: rc=3" in capsys.readouterr().err
    state = json.loads((tmp_path / "home" / "alice" / "doorbell-state.json").read_text())
    assert state["pending"] is True
    assert time.time() >= state["last_ring"] - 5.0
