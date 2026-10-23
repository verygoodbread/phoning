"""Unit tests for PHONING_HOME-prefixed hook commands (SPEC-v0.2 §7.8)."""

from __future__ import annotations

import io
import json
import os
import shlex
from pathlib import Path

import pytest

from phoning import hooks
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import inbound

FP = "a3f1c2d4e5b60718293a4b5c6d7e8f90"


def _event(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> None:
    """Point stdin at ``payload`` encoded as JSON."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def test_agent_cmd_prefixes_bare_command() -> None:
    """The helper renders exactly ``PHONING_HOME=<quoted> <command>``."""
    assert hooks._agent_cmd("/tmp/hh", "phoning read alice --reader main") == (
        "PHONING_HOME=/tmp/hh phoning read alice --reader main"
    )


def test_agent_cmd_quotes_home_with_space() -> None:
    """Homes with spaces are shell-quoted."""
    home = "/tmp/Codex work/phoning"
    got = hooks._agent_cmd(home, "phoning read orch --reader r")
    assert got == f"PHONING_HOME={shlex.quote(home)} phoning read orch --reader r"
    assert got.startswith("PHONING_HOME='/tmp/Codex work/phoning' ")


def test_agent_cmd_always_prefixes_default_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prefix is present even for the resolved default home."""
    monkeypatch.setenv("PHONING_HOME", "/tmp/default-home")
    got = hooks._agent_cmd(hooks._resolve_home(None), "phoning read a --reader r")
    assert got == "PHONING_HOME=/tmp/default-home phoning read a --reader r"


def test_arm_waker_carries_home() -> None:
    """Wait and read lines in the waker paragraph carry the bound home."""
    home = "/tmp/Codex work/phoning"
    text = hooks._arm_waker_paragraph("orch", "orch-main", home)
    quoted = shlex.quote(os.path.abspath(home))
    assert f"PHONING_HOME={quoted} " in text
    for cmd in ("wait orch --reader orch-main --timeout 3600", "read orch --reader orch-main"):
        line = next(ln for ln in text.splitlines() if cmd in ln)
        assert f"PHONING_HOME={quoted}" in line


def test_arm_waker_uses_resolved_home_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no explicit home the resolved ``$PHONING_HOME`` is used."""
    monkeypatch.setenv("PHONING_HOME", "/tmp/wh")
    text = hooks._arm_waker_paragraph("amy", "sess1")
    for cmd in ("wait amy --reader sess1 --timeout 3600", "read amy --reader sess1 --sanitized"):
        seg = next(s for s in text.split("`") if cmd in s)
        assert seg.startswith("PHONING_HOME=/tmp/wh /") and seg.endswith(cmd)


def _rec(seq: int, body: str, reply_to: str | None = None) -> dict[str, object]:
    """Build a minimal inbound record for formatting tests."""
    env: dict[str, object] = {
        "eid": "a" * 32,
        "type": "ask",
        "from": {"principal": "bob", "session": "s"},
        "subject": "q",
        "body": body,
        "ts": "2026-10-11T00:00:00Z",
    }
    if reply_to is not None:
        env["reply_to"] = reply_to
    return {
        "seq": seq,
        "direction": "in",
        "peer": "bob",
        "peer_fp": FP,
        "recv_ts": "2026-10-11T00:00:00Z",
        "env": env,
    }


def test_build_text_hints_and_stop_carry_home() -> None:
    """More-unread and stop lines go through the home helper."""
    home = "/tmp/hh"
    rec = _rec(1, "hi")
    text = hooks._build_text(
        2, [rec], "alice", "main", 600, 1, False, home  # type: ignore[arg-type]
    )
    assert "(+1 more unread: PHONING_HOME=/tmp/hh phoning read alice --reader main)" in text
    stop = hooks._build_text(
        1, [rec], "alice", "main", 600, 20, True, home  # type: ignore[arg-type]
    )
    assert "PHONING_HOME=/tmp/hh phoning send alice --to <peer> ..." in stop
    empty_stop = hooks._build_text(0, [], "alice", "main", 600, 20, True, home)
    assert "PHONING_HOME=/tmp/hh phoning send alice" in empty_stop


def test_format_truncation_hint_carries_home() -> None:
    """Long bodies truncate with a home-prefixed read hint."""
    home = "/tmp/hh"
    rec = _rec(3, "x" * 50)
    lines = hooks._format_one(rec, "alice", "main", 10, home)  # type: ignore[arg-type]
    joined = "\n".join(lines)
    assert "[+40 chars: PHONING_HOME=/tmp/hh phoning read alice --reader main]" in joined


def test_format_body_lines_prefixed() -> None:
    """Body lines are indented with the ``  | `` marker."""
    lines = hooks._format_one(_rec(1, "hello\nworld"), "alice", "main", 600, "/tmp/hh")
    assert "  | hello" in lines and "  | world" in lines


def test_stop_event_carries_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Stop block names the prefixed send command."""
    home = tmp_path / "home"
    monkeypatch.setenv("PHONING_HOME", str(home))
    monkeypatch.delenv("PHONING_READER", raising=False)
    monkeypatch.delenv("PHONING_NAME", raising=False)
    box = Mailbox(home / "alice")
    inbound(box, "bob", FP, "ask", "need answer")
    _event(monkeypatch, {})
    assert hooks.main(["stop", "--name", "alice", "--reader", "main"]) == 0
    out = capsys.readouterr().out
    reason = json.loads(out)["reason"]
    assert f"PHONING_HOME={home} phoning send alice --to <peer> ..." in reason


def test_more_unread_end_to_end_carries_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The (+K more unread) line carries the non-default home."""
    other = tmp_path / "Codex work" / "phoning"
    box = Mailbox(other / "orch")
    for i in range(3):
        inbound(box, "bob", FP, "ask", f"q{i}")
    _event(monkeypatch, {})
    assert (
        hooks.main(
            [
                "user-prompt",
                "--name",
                "orch",
                "--home",
                str(other),
                "--reader",
                "r",
                "--limit",
                "1",
            ]
        )
        == 0
    )
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    quoted = f"PHONING_HOME={shlex.quote(str(other))} "
    assert quoted in ctx
    line = next(ln for ln in ctx.splitlines() if "(+2 more unread:" in ln)
    assert quoted.strip() in line


def test_truncation_hint_end_to_end_carries_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truncated body's read hint carries the non-default home."""
    other = tmp_path / "Codex work" / "phoning"
    box = Mailbox(other / "orch")
    inbound(box, "bob", FP, "ask", "s", "x" * 1000)
    _event(monkeypatch, {})
    assert (
        hooks.main(
            [
                "user-prompt",
                "--name",
                "orch",
                "--home",
                str(other),
                "--reader",
                "r",
                "--body-chars",
                "10",
            ]
        )
        == 0
    )
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    quoted = f"PHONING_HOME={shlex.quote(str(other))} phoning read orch --reader r"
    assert f"[+990 chars: {quoted}]" in ctx
