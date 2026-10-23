"""Unit tests for ``phoning.cli`` (docs/SPEC-v0.2.md §5, §7.6, §7.7)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from phoning import cli, ops
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import ROOT


def phoning(tmp: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``phoning`` as a subprocess against a throwaway home."""
    env = dict(_environ(tmp), PYTHONPATH=str(ROOT))
    return subprocess.run(
        [sys.executable, "-m", "phoning.cli", *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _environ(tmp: Path) -> dict[str, str]:
    """Environment with ``PHONING_HOME`` pointed at ``tmp/home``."""
    import os

    return dict(os.environ, PHONING_HOME=str(tmp / "home"))


def test_help_lists_v02_subcommands() -> None:
    parser = cli.build_parser()
    text = parser.format_help()
    for sub in ("up", "hook", "web", "mcp", "doorbell", "intro", "profile"):
        assert sub in text


def test_short_circuit_table_maps_modules() -> None:
    assert cli._DELEGATED["doorbell"] == ("phoning.doorbell", "main")
    assert cli._DELEGATED["intro"] == ("phoning.intro", "main")
    assert cli._DELEGATED["profile"] == ("phoning.intro", "profile_main")
    assert cli._DELEGATED["up"] == ("phoning.up", "main")


def test_doorbell_delegates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from phoning import doorbell as doorbell_mod

    seen: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        seen.append(list(argv))
        return 7

    monkeypatch.setattr(doorbell_mod, "main", fake_main)
    assert cli.main(["doorbell", "alice", "--once"]) == 7
    assert seen == [["alice", "--once"]]


def test_profile_delegates_to_profile_main(monkeypatch: pytest.MonkeyPatch) -> None:
    from phoning import intro as intro_mod

    seen: list[list[str]] = []

    def fake_profile(argv: list[str]) -> int:
        seen.append(list(argv))
        return 4

    monkeypatch.setattr(intro_mod, "profile_main", fake_profile)
    assert cli.main(["profile", "alice", "--role", "x"]) == 4
    assert seen == [["alice", "--role", "x"]]


def test_sanitized_record_strips_controls() -> None:
    env = {
        "eid": "e" * 32,
        "type": "chat",
        "from": {"principal": "claude-lead", "session": "s"},
        "subject": "evil\x1b[2Jsubj",
        "body": "line1\r\nline2\x07",
        "ts": "t",
    }
    rec = {
        "seq": 1,
        "direction": "in",
        "peer": "bob",
        "peer_fp": "a3f1c2d4" + "0" * 24,
        "room": None,
        "recv_ts": "t",
        "raw": "RAW",
        "env": env,
    }
    out = cli._sanitized_record(rec, "alice")
    assert out["sender"] == "bob" and out["claimed"] == "claude-lead"
    assert out["verified_fp8"] == "a3f1c2d4"
    assert "raw" not in out
    assert out["env"]["subject"] == "evil[2Jsubj"
    assert out["env"]["body"] == "line1\nline2"


def test_read_and_wait_sanitized_cli(tmp_path: Path) -> None:
    box = Mailbox(tmp_path / "home" / "alice")
    env = {
        "eid": "e" * 32,
        "type": "chat",
        "from": {"principal": "claude-lead", "session": "s"},
        "subject": "sub\x1bj",
        "body": "b\x07\nc",
        "ts": "t",
    }
    box.append(
        {
            "direction": "in",
            "tid": "t1",
            "peer": "bob",
            "peer_fp": "a3f1c2d4" + "0" * 24,
            "room": None,
            "recv_ts": "t",
            "raw": "RAW-WIRE-TEXT",
            "env": env,
        }
    )
    p = phoning(tmp_path, "read", "alice", "--reader", "r", "--sanitized", "--peek")
    assert p.returncode == 0, p.stderr
    rec = json.loads(p.stdout.splitlines()[0])
    assert rec["sender"] == "bob" and "raw" not in rec and "RAW-WIRE-TEXT" not in p.stdout
    assert rec["env"]["subject"] == "subj" and rec["env"]["body"] == "b\nc"
    p = phoning(tmp_path, "wait", "alice", "--reader", "r", "--sanitized", "--timeout", "1")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout.splitlines()[0])["env"]["subject"] == "subj"


def test_plain_json_output_unchanged(tmp_path: Path) -> None:
    box = Mailbox(tmp_path / "home" / "alice")
    env = {"eid": "e" * 32, "type": "chat", "subject": "s", "body": "b", "ts": "t"}
    box.append(
        {
            "direction": "in",
            "tid": "t1",
            "peer": "bob",
            "peer_fp": None,
            "room": None,
            "recv_ts": "t",
            "raw": "RAW-WIRE-TEXT",
            "env": env,
        }
    )
    p = phoning(tmp_path, "read", "alice", "--reader", "r", "--json", "--peek")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout.splitlines()[0])["raw"] == "RAW-WIRE-TEXT"


def test_send_uses_ops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def fake_send(user_dir: str, name: str, session: str, **kw: object) -> str:
        calls.append({"user_dir": user_dir, "name": name, "session": session, **kw})
        return "e" * 32

    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ops, "send_message", fake_send)
    rc = cli.main(
        ["send", "alice", "--to", "bob", "--type", "chat", "--subject", "hi", "--session", "s"]
    )
    assert rc == 0 and calls and calls[0]["name"] == "alice"
    assert calls[0]["to"] == "bob" and calls[0]["msg_type"] == "chat"
    assert calls[0]["subject"] == "hi" and calls[0]["session"] == "s"
