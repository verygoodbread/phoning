"""Acceptance: the v0.2 surfaces are reachable through the `phoning` CLI (docs/SPEC-v0.2.md §5)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from phoning import cli, ops
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import ROOT


def phoning(tmp: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PHONING_HOME=str(tmp / "home"), PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", "phoning.cli", *args], env=env, input=stdin,
                          capture_output=True, text=True, timeout=60)


def test_help_lists_new_subcommands(tmp_path: Path) -> None:
    out = phoning(tmp_path, "--help").stdout
    for sub in ("up", "hook", "web", "mcp", "send", "own"):
        assert sub in out


def test_each_subcommand_delegates(tmp_path: Path) -> None:
    Mailbox(tmp_path / "home" / "alice")
    p = phoning(tmp_path, "hook", "print-config", "--for", "claude", "--name", "alice")
    assert p.returncode == 0 and "UserPromptSubmit" in json.loads(p.stdout)["hooks"]
    p = phoning(tmp_path, "hook", "user-prompt", "--name", "alice", stdin="{}")
    assert (p.returncode, p.stdout) == (0, "")
    cfg = tmp_path / "up.json"
    cfg.write_text(json.dumps({"owners": [{"home": str(tmp_path / "home"), "name": "alice", "session": "a"}]}))
    p = phoning(tmp_path, "up", "--config", str(cfg), "--dry-run")
    assert p.returncode == 0 and "alice" in p.stdout and "no-identity" in p.stdout
    assert phoning(tmp_path, "up", "--config", str(tmp_path / "missing.json")).returncode == 2
    assert phoning(tmp_path, "web", "nobody").returncode == 2
    assert phoning(tmp_path, "web", "alice", "--host", "0.0.0.0").returncode == 2
    req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18"}})
    p = phoning(tmp_path, "mcp", "alice", stdin=req + "\n")
    assert p.returncode == 0 and json.loads(p.stdout.splitlines()[0])["result"]["serverInfo"]["name"] == "phoning"


def test_cli_send_uses_ops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def fake_send(user_dir: str, name: str, session: str, **kw: object) -> str:
        calls.append({"user_dir": user_dir, "name": name, "session": session, **kw})
        return "e" * 32

    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ops, "send_message", fake_send)
    rc = cli.main(["send", "alice", "--to", "bob", "--type", "chat", "--subject", "hi", "--session", "s"])
    assert rc == 0 and calls and calls[0]["name"] == "alice" and calls[0]["to"] == "bob"
    assert calls[0]["msg_type"] == "chat" and calls[0]["subject"] == "hi" and calls[0]["session"] == "s"


def test_doorbell_intro_profile_and_sanitized_read_wait(tmp_path: Path) -> None:
    from phoning import envelope

    box = Mailbox(tmp_path / "home" / "alice")
    env = envelope.make("chat", "claude-lead", "s", "ok", "body")
    env["subject"], env["body"] = "evil\x1b[2Jsubj", "line1\r\nline2\x07"
    box.append({"direction": "in", "tid": "t1", "peer": "bob", "peer_fp": "a3f1c2d4" + "0" * 24, "room": None,
                "recv_ts": env["ts"], "raw": "RAW-WIRE-TEXT", "env": env})
    for args in (["read", "alice", "--reader", "r", "--sanitized", "--peek"],
                 ["wait", "alice", "--reader", "r", "--sanitized", "--timeout", "1"]):
        p = phoning(tmp_path, *args)
        assert p.returncode == 0, (args, p.stderr)
        rec = json.loads(p.stdout.splitlines()[0])
        assert rec["sender"] == "bob" and rec["claimed"] == "claude-lead" and rec["verified_fp8"] == "a3f1c2d4"
        assert "raw" not in rec and "RAW-WIRE-TEXT" not in p.stdout
        assert rec["env"]["subject"] == "evil[2Jsubj" and rec["env"]["body"] == "line1\nline2"
    raw = phoning(tmp_path, "read", "alice", "--reader", "r", "--json", "--peek")
    assert json.loads(raw.stdout.splitlines()[0])["raw"] == "RAW-WIRE-TEXT"  # plain --json unchanged
    p = phoning(tmp_path, "profile", "alice", "--role", "tester")
    assert p.returncode == 0 and json.loads(p.stdout)["role"] == "tester"
    assert phoning(tmp_path, "doorbell", "alice", "--once").returncode == 0
    assert phoning(tmp_path, "doorbell", "nobody", "--once").returncode == 2
    assert phoning(tmp_path, "intro", "--help").returncode == 0
