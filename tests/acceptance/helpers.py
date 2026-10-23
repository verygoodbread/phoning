"""Shared helpers for v0.2 acceptance tests: build real mailbox logs without a relay."""

from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path
from typing import Any

from phoning import envelope
from phoning.mailbox import Mailbox

ROOT = Path(__file__).resolve().parents[2]
HAVE_RETALK = shutil.which("retalk") is not None and shutil.which("retalk-server") is not None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def inbound(box: Mailbox, peer: str, fp: str | None, typ: str, subject: str, body: str = "",
            *, principal: str | None = None, session: str = "s1", room: str | None = None,
            **kw: Any) -> dict[str, Any]:
    """Append one inbound record (as the owner daemon would) and return it with its seq."""
    env = envelope.make(typ, principal or peer, session, subject, body, room=room, **kw)
    rec = {"direction": "in", "tid": env["eid"], "peer": peer, "peer_fp": fp, "room": room,
           "recv_ts": env["ts"], "raw": envelope.encode(env), "env": env}
    rec["seq"] = box.append(rec)
    return rec


def outbound(box: Mailbox, me: str, to: str, typ: str, subject: str, body: str = "",
             *, session: str = "me", room: str | None = None, **kw: Any) -> dict[str, Any]:
    env = envelope.make(typ, me, session, subject, body, room=room, **kw)
    rec = {"direction": "out", "tid": env["eid"], "peer": room or to, "peer_fp": None, "room": room,
           "recv_ts": env["ts"], "raw": envelope.encode(env), "env": env}
    rec["seq"] = box.append(rec)
    return rec


def snapshot(user_dir: Path) -> dict[str, bytes]:
    """Bytes of every file under the mailbox dir (to prove read-only surfaces wrote nothing)."""
    out: dict[str, bytes] = {}
    for root, _dirs, files in os.walk(user_dir / "mailbox"):
        for f in files:
            p = Path(root) / f
            out[str(p.relative_to(user_dir))] = p.read_bytes()
    return out
