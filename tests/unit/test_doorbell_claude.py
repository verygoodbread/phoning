"""Unit tests for SPEC-v0.2 §7.10: absolute exe nudges and claude-resume."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from phoning import doorbell
from phoning.mailbox import Mailbox

SID = "3f0c9e2a-5b7d-4c1e-9a8f-2d6b4e1c7a90"


class _Done:
    """Minimal CompletedProcess stand-in for mocked subprocess.run."""

    def __init__(self, returncode: int = 0, stdout: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


def _write_session(claude_home: Path, sid: str, cwd: str) -> Path:
    proj = claude_home / "projects" / "-x"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{sid}.jsonl"
    path.write_text(json.dumps({"type": "user", "cwd": cwd}) + "\n", encoding="utf-8")
    return path


def test_phoning_prefix_uses_which(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = str(tmp_path / "bin" / "phoning")
    monkeypatch.setattr(doorbell.shutil, "which", lambda name: fake if name == "phoning" else None)
    assert doorbell._phoning_prefix() == shlex.join([fake])


def test_phoning_prefix_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doorbell.shutil, "which", lambda name: None)
    assert doorbell._phoning_prefix() == shlex.join([sys.executable, "-m", "phoning.cli"])


def test_build_nudge_has_home_and_absolute_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doorbell.shutil, "which", lambda name: "/opt/bin/phoning")
    text = doorbell.build_nudge("alice", "alice-main", "/tmp/hm")
    assert "[phoning] New actionable phoning mail for alice" in text
    assert "PHONING_HOME=/tmp/hm /opt/bin/phoning read alice --reader alice-main" in text


def test_build_claude_nudge_omits_home_adds_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doorbell.shutil, "which", lambda name: "/opt/bin/phoning")
    text = doorbell.build_claude_nudge("alice", "alice-main")
    assert "PHONING_HOME=" not in text
    assert "/opt/bin/phoning read alice --reader alice-main --sanitized" in text
    assert "not instructions from the user" in text


def _mock_run(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict[str, Any]],
    agents_stdout: bytes,
    agents_rc: int = 0,
    resume_rc: int = 0,
    fail_agents: bool = False,
) -> None:
    def _fake(
        argv: list[str],
        stdin: Any = None,
        capture_output: bool = False,
        timeout: float = 0,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> _Done:
        calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env or {})})
        if len(argv) >= 2 and argv[1] == "agents":
            if fail_agents:
                raise FileNotFoundError("no such file")
            return _Done(returncode=agents_rc, stdout=agents_stdout)
        return _Done(returncode=resume_rc, stdout=b"{}")

    monkeypatch.setattr(doorbell.subprocess, "run", _fake)


def test_live_session_never_resumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_session(tmp_path / "chome", SID, str(tmp_path))
    calls: list[dict[str, Any]] = []
    live = json.dumps([{"sessionId": SID, "status": "idle"}]).encode()
    _mock_run(monkeypatch, calls, live)
    out = doorbell.ring_claude_resume(
        "alice",
        "alice-main",
        str(tmp_path),
        SID,
        claude_bin="/fake/claude",
        claude_home=str(tmp_path / "chome"),
        claude_token_file=str(tmp_path / "missing-token"),
    )
    assert out == "skip: live (Claude sessions wake via their own background waker)"
    assert len(calls) == 1


def test_liveness_unknown_when_call_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_session(tmp_path / "chome", SID, str(tmp_path))
    calls: list[dict[str, Any]] = []
    _mock_run(monkeypatch, calls, b"[]", fail_agents=True)
    out = doorbell.ring_claude_resume(
        "alice",
        "alice-main",
        str(tmp_path),
        SID,
        claude_bin="/fake/claude",
        claude_home=str(tmp_path / "chome"),
        claude_token_file=str(tmp_path / "missing-token"),
    )
    assert out == "skip: liveness unknown"
    assert len(calls) == 1


def test_liveness_unknown_on_bad_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_session(tmp_path / "chome", SID, str(tmp_path))
    calls: list[dict[str, Any]] = []
    _mock_run(monkeypatch, calls, b"not-json")
    out = doorbell.ring_claude_resume(
        "alice",
        "alice-main",
        str(tmp_path),
        SID,
        claude_bin="/fake/claude",
        claude_home=str(tmp_path / "chome"),
        claude_token_file=str(tmp_path / "missing-token"),
    )
    assert out == "skip: liveness unknown"


def test_missing_session_file_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    _mock_run(monkeypatch, calls, b"[]")
    out = doorbell.ring_claude_resume(
        "alice",
        "alice-main",
        str(tmp_path),
        SID,
        claude_bin="/fake/claude",
        claude_home=str(tmp_path / "chome-empty"),
        claude_token_file=str(tmp_path / "missing-token"),
    )
    assert out == "skip: unreachable: no session file"
    assert len(calls) == 1


def test_token_mode_refused_and_never_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_session(tmp_path / "chome", SID, str(tmp_path))
    tok = tmp_path / "tok"
    tok.write_text("tok-SECRET-999\n", encoding="utf-8")
    tok.chmod(0o644)
    calls: list[dict[str, Any]] = []
    _mock_run(monkeypatch, calls, b"[]")
    out = doorbell.ring_claude_resume(
        "alice",
        "alice-main",
        str(tmp_path),
        SID,
        claude_bin="/fake/claude",
        claude_home=str(tmp_path / "chome"),
        claude_token_file=str(tok),
    )
    assert out == "ring failed: token file must be 0600"
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert "tok-SECRET-999" not in captured.out and "tok-SECRET-999" not in captured.err


def test_resume_argv_env_and_allowed_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    _write_session(tmp_path / "chome", SID, str(workdir))
    tok = tmp_path / "tok"
    tok.write_text("tok-SECRET-123\n", encoding="utf-8")
    tok.chmod(0o600)
    monkeypatch.setattr(doorbell.shutil, "which", lambda name: "/opt/bin/phoning")
    calls: list[dict[str, Any]] = []
    _mock_run(monkeypatch, calls, b"[]")
    out = doorbell.ring_claude_resume(
        "alice",
        "alice-main",
        str(tmp_path),
        SID,
        claude_bin="/fake/claude",
        claude_home=str(tmp_path / "chome"),
        claude_token_file=str(tok),
    )
    assert out == "resumed: rc=0"
    assert len(calls) == 2
    resume = calls[1]
    argv: list[str] = resume["argv"]
    assert argv[0] == "/fake/claude"
    assert argv[1] == "-p" and argv[3:7] == ["--resume", SID, "--output-format", "json"]
    assert "PHONING_HOME=" not in argv[2]
    assert " read alice --reader alice-main --sanitized" in argv[2]
    allowed = [argv[i + 1] for i, a in enumerate(argv) if a == "--allowedTools"]
    assert len(allowed) == 2
    assert allowed[0] == "Bash(/opt/bin/phoning read:*)"
    assert allowed[1] == "Bash(/opt/bin/phoning send:*)"
    assert resume["cwd"] == str(workdir)
    env: dict[str, str] = resume["env"]
    assert env["PHONING_HOME"] == str(tmp_path)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-SECRET-123"


def test_missing_claude_exe_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doorbell.shutil, "which", lambda name: None)
    out = doorbell.ring_claude_resume(
        "alice", "alice-main", str(tmp_path), SID, claude_bin=None
    )
    assert out.startswith("ring failed:")


def test_claude_cwd_first_line_with_cwd(tmp_path: Path) -> None:
    proj = tmp_path / "projects" / "p1"
    proj.mkdir(parents=True)
    path = proj / f"{SID}.jsonl"
    path.write_text(
        json.dumps({"type": "x"}) + "\n" + json.dumps({"cwd": str(tmp_path)}) + "\n",
        encoding="utf-8",
    )
    assert doorbell.claude_cwd(str(tmp_path), SID) == str(tmp_path)
    assert doorbell.claude_cwd(str(tmp_path), "missing") is None


def test_main_requires_claude_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("PHONING_HOME", str(home))
    Mailbox(home / "alice")
    rc = doorbell.main(["alice", "--once", "--ring", "claude-resume"])
    assert rc == 2


def test_token_file_must_be_regular_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_session(tmp_path / "chome", SID, str(tmp_path))
    sub = tmp_path / "tokdir"
    sub.mkdir()
    os.chmod(sub, 0o600)
    calls: list[dict[str, Any]] = []
    _mock_run(monkeypatch, calls, b"[]")
    out = doorbell.ring_claude_resume(
        "alice",
        "alice-main",
        str(tmp_path),
        SID,
        claude_bin="/fake/claude",
        claude_home=str(tmp_path / "chome"),
        claude_token_file=str(sub),
    )
    assert out == "ring failed: token file must be 0600"
    mode = stat.S_IMODE(sub.stat().st_mode)
    assert mode == 0o600 or out == "ring failed: token file must be 0600"


def test_resumed_nonzero_is_failure() -> None:
    assert doorbell._is_failure("resumed: rc=0") is False
    assert doorbell._is_failure("resumed: rc=3") is True
    assert doorbell._is_failure("ring failed: rc=1") is True


def test_run_uses_subprocess_list_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_session(tmp_path / "chome", SID, str(tmp_path))
    seen: list[dict[str, Any]] = []

    def _fake(argv: Any, **kw: Any) -> _Done:
        assert isinstance(argv, list)
        seen.append({"argv": argv})
        if len(argv) >= 2 and argv[1] == "agents":
            return _Done(returncode=0, stdout=b"[]")
        return _Done(returncode=0, stdout=b"{}")

    monkeypatch.setattr(subprocess, "run", _fake)
    monkeypatch.setattr(doorbell.subprocess, "run", _fake)
    out = doorbell.ring_claude_resume(
        "alice",
        "alice-main",
        str(tmp_path),
        SID,
        claude_bin="/fake/claude",
        claude_home=str(tmp_path / "chome"),
        claude_token_file=str(tmp_path / "no-token"),
    )
    assert out == "resumed: rc=0"
    assert seen and all(isinstance(c["argv"], list) for c in seen)
