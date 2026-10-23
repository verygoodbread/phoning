"""Unit tests for phoning.ops: sanitize, resolve_reply_to and send_message."""

from __future__ import annotations

from typing import Any

import pytest

from phoning import envelope, ops
from phoning.mailbox import Mailbox

FP = "a3f1c2d4e5b60718293a4b5c6d7e8f90"


def _inbound(
    box: Mailbox,
    peer: str,
    typ: str,
    subject: str,
    *,
    fp: str | None = FP,
    room: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    env = envelope.make(typ, peer, "s1", subject, room=room, **kw)
    rec: dict[str, Any] = {
        "direction": "in",
        "peer": peer,
        "peer_fp": fp,
        "room": room,
        "recv_ts": env["ts"],
        "raw": envelope.encode(env),
        "env": env,
    }
    rec["seq"] = box.append(rec)
    return rec


def _outbound(
    box: Mailbox, me: str, to: str, typ: str, subject: str, *, room: str | None = None, **kw: Any
) -> dict[str, Any]:
    env = envelope.make(typ, me, "me", subject, room=room, **kw)
    rec: dict[str, Any] = {
        "direction": "out",
        "peer": room or to,
        "peer_fp": None,
        "room": room,
        "recv_ts": env["ts"],
        "raw": envelope.encode(env),
        "env": env,
    }
    rec["seq"] = box.append(rec)
    return rec


def test_sanitize_inbound(tmp_path: Any) -> None:
    box = Mailbox(str(tmp_path / "alice"))
    rec = _inbound(box, "bob", "chat", "hi")
    out = ops.sanitize(rec, local="alice")
    assert out["sender"] == "bob"
    assert out["claimed"] is None
    assert out["verified_fp8"] == FP[:8]
    assert out["seq"] == rec["seq"]
    assert out["direction"] == "in"
    assert "raw" not in out
    assert out["env"]["subject"] == "hi"


def test_sanitize_outbound_uses_local(tmp_path: Any) -> None:
    box = Mailbox(str(tmp_path / "alice"))
    rec = _outbound(box, "alice", "bob", "chat", "mine")
    out = ops.sanitize(rec, local="alice")
    assert out["sender"] == "alice"
    assert out["claimed"] is None
    assert out["verified_fp8"] is None


def test_sanitize_claimed_differs(tmp_path: Any) -> None:
    box = Mailbox(str(tmp_path / "alice"))
    env = envelope.make("chat", "impostor", "s1", "hi")
    rec: dict[str, Any] = {
        "direction": "in",
        "peer": "bob",
        "peer_fp": FP,
        "room": None,
        "recv_ts": env["ts"],
        "raw": envelope.encode(env),
        "env": env,
    }
    rec["seq"] = box.append(rec)
    out = ops.sanitize(rec, local="alice")
    assert out["sender"] == "bob"
    assert out["claimed"] == "impostor"
    assert out["verified_fp8"] == FP[:8]


def test_sanitize_non_string_fp_is_none(tmp_path: Any) -> None:
    box = Mailbox(str(tmp_path / "alice"))
    rec = _inbound(box, "bob", "chat", "hi", fp=None)
    out = ops.sanitize(rec, local="alice")
    assert out["verified_fp8"] is None


def test_resolve_reply_to_passthrough(tmp_path: Any) -> None:
    udir = str(tmp_path / "alice")
    Mailbox(udir)
    assert ops.resolve_reply_to(udir, "abc123", to="bob") == "abc123"


def test_resolve_reply_to_peer(tmp_path: Any) -> None:
    udir = str(tmp_path / "alice")
    box = Mailbox(udir)
    first = _inbound(box, "bob", "ask", "q1")
    newer = _inbound(box, "bob", "chat", "q2")
    _inbound(box, "carol", "chat", "other")
    assert ops.resolve_reply_to(udir, "last", to="bob") == newer["env"]["eid"]
    assert first["env"]["eid"] != newer["env"]["eid"]


def test_resolve_reply_to_room(tmp_path: Any) -> None:
    udir = str(tmp_path / "alice")
    box = Mailbox(udir)
    _inbound(box, "bob", "chat", "direct")
    room_rec = _inbound(box, "bob", "chat", "in room", room="ops")
    assert ops.resolve_reply_to(udir, "last", room="ops") == room_rec["env"]["eid"]


def test_resolve_reply_to_no_match(tmp_path: Any) -> None:
    udir = str(tmp_path / "alice")
    Mailbox(udir)
    with pytest.raises(ValueError, match="no inbound message from dave"):
        ops.resolve_reply_to(udir, "last", to="dave")
    with pytest.raises(ValueError, match="no inbound message from nope"):
        ops.resolve_reply_to(udir, "last", room="nope")


def test_send_message_requires_exactly_one_dest(tmp_path: Any) -> None:
    udir = str(tmp_path / "alice")
    Mailbox(udir)
    with pytest.raises(ValueError, match="exactly one"):
        ops.send_message(udir, "alice", "s", msg_type="chat", subject="x")
    with pytest.raises(ValueError, match="exactly one"):
        ops.send_message(udir, "alice", "s", msg_type="chat", subject="x", to="b", room="r")


def _fake_retalk(monkeypatch: pytest.MonkeyPatch, sent: list[str]) -> None:
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


def test_send_message_inherits_thread(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    udir = str(tmp_path / "alice")
    box = Mailbox(udir)
    root = _inbound(box, "bob", "ask", "first q")
    newer = _inbound(box, "bob", "chat", "follow-up", thread=root["env"]["eid"])
    sent: list[str] = []
    _fake_retalk(monkeypatch, sent)
    eid = ops.send_message(
        udir, "alice", "s", msg_type="ack", subject="ok", to="bob", reply_to="last"
    )
    rec = [r for r in box._read_all() if r["env"]["eid"] == eid][0]
    assert rec["env"]["reply_to"] == newer["env"]["eid"]
    assert rec["env"]["thread"] == root["env"]["eid"]
    assert sent and sent[0].startswith("@hd1 ack ok\n")


def test_send_message_unknown_parent_opens_thread(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    udir = str(tmp_path / "alice")
    box = Mailbox(udir)
    sent: list[str] = []
    _fake_retalk(monkeypatch, sent)
    eid = ops.send_message(
        udir, "alice", "s", msg_type="ack", subject="ok", to="bob", reply_to="f" * 32
    )
    rec = [r for r in box._read_all() if r["env"]["eid"] == eid][0]
    assert rec["env"]["reply_to"] == "f" * 32
    assert rec["env"]["thread"] == eid


def test_send_message_room(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    udir = str(tmp_path / "alice")
    box = Mailbox(udir)
    sent: list[str] = []
    _fake_retalk(monkeypatch, sent)
    eid = ops.send_message(
        udir, "alice", "s", msg_type="chat", subject="hi all", room="ops"
    )
    rec = [r for r in box._read_all() if r["env"]["eid"] == eid][0]
    assert rec["direction"] == "out"
    assert rec["room"] == "ops"
    assert rec["env"]["thread"] == eid
