"""Shared fixtures and fake binaries for the doorbell-rules acceptance tests (docs/spec/rules.md)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from phoning import doorbell
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import inbound

FP = "a3f1c2d4e5b60718293a4b5c6d7e8f90"
PEER_TEXT = "SECRET-PEER-TEXT-must-never-be-injected"
TID = "0190abcd-1234-7abc-8def-0123456789ab"
SID = "3f0c9e2a-5b7d-4c1e-9a8f-2d6b4e1c7a90"


@pytest.fixture(name="home")
def home_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PHONING_SESSIONS", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex-home"))
    monkeypatch.delenv("PHONING_READER", raising=False)
    return tmp_path / "home"


def fake_codex(tmp: Path, rc: int = 0) -> tuple[Path, Path]:
    log = tmp / "codex-calls.jsonl"
    exe = tmp / "fakebin" / "codex"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                   f"open({str(log)!r},'a').write(json.dumps({{'argv':sys.argv[1:],'cwd':os.getcwd()}})+'\\n')\n"
                   f"sys.exit({rc})\n")
    exe.chmod(0o755)
    return exe, log


def fake_claude(tmp: Path, agents: list[dict[str, Any]]) -> tuple[Path, Path]:
    log = tmp / "claude-calls.jsonl"
    exe = tmp / "fakeclaude" / "claude"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\n"
        "if sys.argv[1:3] == ['agents','--json']:\n"
        f"    print(json.dumps({agents!r})); sys.exit(0)\n"
        f"open({str(log)!r},'a').write(json.dumps({{'argv':sys.argv[1:],'cwd':os.getcwd(),"
        "'tok':os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'),'home':os.environ.get('PHONING_HOME')})+'\\n')\n")
    exe.chmod(0o755)
    return exe, log


def fake_osascript(tmp: Path, rc: int = 0) -> tuple[Path, Path]:
    log = tmp / "osascript-calls.jsonl"
    exe = tmp / "fakeosa" / "osascript"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                   f"open({str(log)!r},'a').write(json.dumps({{'argv':sys.argv[1:]}})+'\\n')\n"
                   f"sys.exit({rc})\n")
    exe.chmod(0o755)
    return exe, log


def calls(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def codex_home_with_thread(tmp: Path, tid: str = TID, *, locked: bool = False,
                           contexts: list[dict[str, Any]] | None = None, raw_tail: str = "") -> tuple[Path, Path]:
    """A fake CODEX_HOME holding one rollout (session_meta + optional turn_context lines).

    Returns ``(codex_home, workdir)``. ``locked`` creates the writer lock (the thread is live elsewhere).
    """
    chome = tmp / "codexhome"
    (chome / "thread-writer-locks").mkdir(parents=True, exist_ok=True)
    sess = chome / "sessions" / "2026" / "10" / "11"
    sess.mkdir(parents=True, exist_ok=True)
    workdir = tmp / "proj dir"
    workdir.mkdir(exist_ok=True)
    lines = [json.dumps({"type": "session_meta", "payload": {"id": tid, "cwd": str(workdir)}})]
    for ctx in contexts or []:
        lines.append(json.dumps({"type": "event_msg", "payload": {"type": "noise"}}))
        lines.append(json.dumps({"type": "turn_context", "payload": ctx}))
    (sess / f"rollout-2026-01-01T00-03-31-{tid}.jsonl").write_text("\n".join(lines) + "\n" + raw_tail)
    if locked:
        (chome / "thread-writer-locks" / f"{tid}.lock").write_text("")
    return chome, workdir


def claude_home_with_session(tmp: Path, sid: str = SID) -> tuple[Path, Path]:
    chome = tmp / "claudehome"
    proj = chome / "projects" / "-x"
    proj.mkdir(parents=True, exist_ok=True)
    workdir = tmp / "work dir"
    workdir.mkdir(exist_ok=True)
    (proj / f"{sid}.jsonl").write_text(json.dumps({"type": "user", "cwd": str(workdir)}) + "\n")
    return chome, workdir


def token_file(tmp: Path) -> Path:
    tok = tmp / "claude-token"
    tok.write_text("tok-SECRET-123\n")
    tok.chmod(0o600)
    return tok


def mail(home: Path, typ: str = "ask", subject: str = "q", **kw: Any) -> None:
    """Append one inbound direct record to alice's mailbox."""
    inbound(Mailbox(home / "alice"), "bob", FP, typ, subject, **kw)


def bell(*args: str) -> int:
    """One non-blocking pass of ``phoning doorbell alice``."""
    return doorbell.main(["alice", "--once", *args])


def state(home: Path) -> dict[str, Any]:
    out: dict[str, Any] = json.loads((home / "alice" / "doorbell-state.json").read_text())
    return out


def write_state(home: Path, data: dict[str, Any]) -> None:
    path = home / "alice" / "doorbell-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    path.chmod(0o600)


def approvals(home: Path) -> list[dict[str, Any]]:
    path = home / "alice" / "doorbell-approvals.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def assert_mode_0600(path: Path) -> None:
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def cfg_pairs(argv: list[str]) -> dict[str, str]:
    """``-c key=value`` pairs of a codex argv, with TOML quotes stripped from values."""
    out: dict[str, str] = {}
    for i, a in enumerate(argv[:-1]):
        if a in ("-c", "--config") and "=" in argv[i + 1]:
            k, v = argv[i + 1].split("=", 1)
            out[k] = v.strip().strip('"').strip("'")
    return out


def effective_sandbox(argv: list[str]) -> str | None:
    """Sandbox mode a codex argv asks for, via ``-s/--sandbox X`` or ``-c sandbox_mode=X``."""
    for i, a in enumerate(argv[:-1]):
        if a in ("-s", "--sandbox"):
            return argv[i + 1]
    return cfg_pairs(argv).get("sandbox_mode")
