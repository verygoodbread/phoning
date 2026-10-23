"""Acceptance: session binding and self-arming Claude sessions (docs/SPEC-v0.2.md §7.0-7.1)."""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path

import pytest

from phoning import hooks
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import inbound

FP = "a3f1c2d4e5b60718293a4b5c6d7e8f90"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PHONING_SESSIONS", str(tmp_path / "sessions.json"))
    for k in ("PHONING_READER", "PHONING_NAME", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path / "home"


def test_session_start_arms_the_waker(home: Path, monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    Mailbox(home / "alice")
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert hooks.main(["session-start", "--name", "alice", "--reader", "main", "--arm-waker"]) == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "phoning waker:" in ctx and "wait alice --reader main --timeout 3600" in ctx
    assert "read alice --reader main" in ctx and "run_in_background" in ctx
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert hooks.main(["user-prompt", "--name", "alice", "--reader", "main"]) == 0
    assert capsys.readouterr().out == ""  # only session-start arms; nothing new -> silent
    assert hooks.main(["print-config", "--for", "claude", "--name", "alice", "--reader", "main"]) == 0
    cfg = json.loads(capsys.readouterr().out)["hooks"]
    assert "--arm-waker" in cfg["SessionStart"][0]["hooks"][0]["command"]
    assert "--arm-waker" not in cfg["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert hooks.main(["print-config", "--for", "codex", "--name", "alice", "--reader", "main"]) == 0
    assert "--arm-waker" not in capsys.readouterr().out


def test_session_binding_keeps_shared_project_hooks_safe(home: Path, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    inbound(box, "bob", FP, "ask", "for alice only")
    monkeypatch.delenv("PHONING_NAME", raising=False)

    def event(argv: list[str], sid: str) -> str:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": sid})))
        assert hooks.main(argv) == 0
        return capsys.readouterr().out

    # identity-free command, as installed in a shared project's settings
    assert event(["user-prompt"], "sess-other") == ""          # unbound session: inert
    assert box.cursor("sess-other") == 0 and not list((home / "alice" / "mailbox" / "cursors").glob("*.json"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-alice")
    assert hooks.main(["bind", "--name", "alice", "--reader", "alice-main"]) == 0
    entry = json.loads(capsys.readouterr().out)
    assert entry["name"] == "alice" and entry["reader"] == "alice-main" and entry["home"] == str(home)
    reg = tmp_path / "sessions.json"
    assert stat.S_IMODE(reg.stat().st_mode) == 0o600 and "sess-alice" in json.loads(reg.read_text())
    out = event(["user-prompt"], "sess-alice")
    assert "for alice only" in out and box.cursor("alice-main") == 1
    assert event(["user-prompt"], "sess-other") == ""          # still inert
    armed = event(["session-start", "--arm-waker"], "sess-alice")
    assert "wait alice --reader alice-main" in armed
    assert event(["session-start", "--arm-waker"], "sess-other") == ""  # no waker for unbound sessions
    assert hooks.main(["bind", "--name", "nobody"]) == 2        # no mailbox: refuse
    capsys.readouterr()
    assert hooks.main(["bindings"]) == 0 and "sess-alice" in capsys.readouterr().out
    assert hooks.main(["unbind"]) == 0
    capsys.readouterr()
    inbound(box, "bob", FP, "ask", "after unbind")
    assert event(["user-prompt"], "sess-alice") == ""
    assert hooks.main(["print-config", "--for", "claude"]) == 0
    cfg = json.loads(capsys.readouterr().out)["hooks"]
    cmds = [cfg[e][0]["hooks"][0]["command"] for e in cfg]
    assert all("--name" not in c and "--reader" not in c and "--home" not in c for c in cmds)
    assert "--arm-waker" in cfg["SessionStart"][0]["hooks"][0]["command"]


def test_waker_and_hints_carry_the_bound_home(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    other = tmp_path / "Codex work" / "phoning"  # non-default home, with a space
    box = Mailbox(other / "orch")
    for i in range(3):
        inbound(box, "bob", FP, "ask", f"q{i}")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-orch")
    assert hooks.main(["bind", "--name", "orch", "--home", str(other), "--reader", "orch-main"]) == 0
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "sess-orch"})))
    assert hooks.main(["session-start", "--arm-waker", "--limit", "1"]) == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    quoted = f"PHONING_HOME='{other}'"
    assert f"{quoted} " in ctx
    for cmd in ("wait orch --reader orch-main --timeout 3600", "read orch --reader orch-main --sanitized",
                "(+2 more unread: "):
        line = next(ln for ln in ctx.splitlines() if cmd in ln)
        assert quoted in line, line
