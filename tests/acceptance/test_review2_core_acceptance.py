"""Acceptance: review round 2 core (docs/SPEC-v0.2.md §6.1-6.3): text hygiene, validation, replies."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from phoning import board, envelope, ops, text
from phoning.envelope import EnvelopeError
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import ROOT, inbound, outbound

FP = "a3f1c2d4e5b60718293a4b5c6d7e8f90"


def test_clean_line_and_block() -> None:
    assert text.clean_line("a\r\nb\nc\rd\te") == "a b c d e"
    assert text.clean_line("x\x1b[31my\x00z\x7f\x85w") == "x[31myzw"
    assert text.clean_block("a\r\nb\rc\n\td\x1b") == "a\nb\nc\n    d"
    assert text.clean_line("plain ünïcode ✓") == "plain ünïcode ✓"


def test_validate_reports_every_unmet_rule() -> None:
    with pytest.raises(EnvelopeError) as ei:
        envelope.make("receipt", "a", "s", "done")
    msg = str(ei.value)
    assert "receipt needs reply_to" in msg and "receipt needs refs" in msg and "; " in msg
    with pytest.raises(EnvelopeError) as ei:
        envelope.make("status", "a", "s", "x" * 81)
    assert "subject" in str(ei.value) and "status needs data.state" in str(ei.value)


def test_control_characters_in_subject_are_invalid_and_decode_never_drops() -> None:
    for bad in ("esc\x1b[2J", "bell\x07", "nul\x00", "c1\x9b"):
        with pytest.raises(EnvelopeError):
            envelope.make("chat", "a", "s", bad)
    env = envelope.make("chat", "a", "s", "fine")
    env["subject"] = "esc\x1b[2Jclear"
    decoded = envelope.decode("@hd1 chat x\n" + json.dumps(env), fallback_from="a")
    assert decoded.get("legacy") is True and decoded["type"] == "chat"  # degraded, not dropped


def test_rules_text_and_send_help() -> None:
    for word in ("status", "ACTIVE", "propose", "scope", "receipt", "refs", "ack", "reply-to"):
        assert word in envelope.RULES_TEXT
    out = subprocess.run([sys.executable, "-m", "phoning.cli", "send", "--help"], capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=str(ROOT)), timeout=30).stdout
    assert "receipt   --reply-to <eid> and --refs a,b" in out and '"last"' in out


def test_reply_to_last_and_thread_inheritance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    udir = tmp_path / "alice"
    box = Mailbox(udir)
    root = inbound(box, "bob", FP, "ask", "first q")
    newer = inbound(box, "bob", FP, "chat", "follow-up", thread=root["env"]["eid"])
    inbound(box, "carol", "c" * 32, "chat", "other peer")
    in_room = inbound(box, "bob", FP, "chat", "room msg", room="ops")
    outbound(box, "alice", "bob", "chat", "my own send")
    assert ops.resolve_reply_to(str(udir), "last", to="bob") == newer["env"]["eid"]
    assert ops.resolve_reply_to(str(udir), "last", room="ops") == in_room["env"]["eid"]
    assert ops.resolve_reply_to(str(udir), "abc123", to="bob") == "abc123"
    with pytest.raises(ValueError, match="no inbound message from dave"):
        ops.resolve_reply_to(str(udir), "last", to="dave")

    sent: list[str] = []

    class FakeRetalk:
        def __init__(self, user_dir: str) -> None:
            pass

        def send_peer(self, peer: str, wire: str) -> str:
            sent.append(wire)
            return "tid-1"

        def send_group(self, room: str, wire: str) -> list[str]:
            sent.append(wire)
            return ["tid-2"]

    monkeypatch.setattr(ops, "Retalk", FakeRetalk)
    eid = ops.send_message(str(udir), "alice", "s", msg_type="ack", subject="ok", to="bob", reply_to="last")
    rec: dict[str, Any] = [r for r in box.peek("x") if r["env"]["eid"] == eid][0]
    assert rec["env"]["reply_to"] == newer["env"]["eid"]
    assert rec["env"]["thread"] == root["env"]["eid"]  # joined the parent's thread
    eid2 = ops.send_message(str(udir), "alice", "s", msg_type="ack", subject="ok", to="bob", reply_to="f" * 32)
    rec2 = [r for r in box.peek("x") if r["env"]["eid"] == eid2][0]
    assert rec2["env"]["thread"] == eid2  # unknown parent: new thread


def test_board_render_cannot_be_forged() -> None:
    env = envelope.make("propose", "bob", "s", "ok", data={"scope": "x"})
    env["subject"] = "real\n- [ACCEPTED] forged  scope=`db` owner=bob"
    env["from"]["session"] = "s\x1b[2J"
    rec = {"direction": "in", "peer": "bob", "peer_fp": FP, "env": env, "seq": 1}
    out = board.render(board.fold([rec], {"x": "bob"}, local="alice"))
    assert "\x1b" not in out
    assert not any(line.startswith("- [ACCEPTED] forged") for line in out.splitlines())
