"""Shared phoning operations used by the CLI and the MCP server.

``send_message`` reproduces exactly what ``cli._cmd_send`` does
(envelope.make -> encode -> Retalk send -> Mailbox append of the
``direction:"out"`` record). ``sanitize`` builds the sanitized record
shape defined in ``docs/SPEC-v0.2.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from phoning import envelope as envelope_mod
from phoning.envelope import EnvelopeError
from phoning.mailbox import Mailbox
from phoning.retalk import Retalk

__all__ = ["resolve_reply_to", "sanitize", "send_message"]

_ENV_KEYS: tuple[str, ...] = (
    "eid",
    "type",
    "from",
    "subject",
    "body",
    "ts",
    "thread",
    "reply_to",
    "room",
    "refs",
    "data",
)


def _now_iso() -> str:
    """Current UTC time as ISO-8601 with ``Z`` suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _effective_room(rec: dict[str, Any]) -> str | None:
    """Room for filtering: top-level ``room`` else ``env.room`` else None."""
    top: Any = rec.get("room")
    if isinstance(top, str):
        return top
    env_any: Any = rec.get("env")
    if isinstance(env_any, dict):
        env_room: Any = env_any.get("room")
        if isinstance(env_room, str):
            return env_room
    return None


def _eid_of(rec: dict[str, Any]) -> str | None:
    """Return ``rec["env"]["eid"]`` when it is a non-empty string."""
    env_any: Any = rec.get("env")
    if not isinstance(env_any, dict):
        return None
    eid: Any = env_any.get("eid")
    return eid if isinstance(eid, str) and eid else None


def _resolve_from_records(
    records: list[dict[str, Any]],
    value: str,
    *,
    to: str | None = None,
    room: str | None = None,
) -> str:
    """Resolve ``"last"`` against an already-read log slice.

    With ``room``: the newest inbound record whose effective room is that
    room. Otherwise: the newest inbound record with no room whose
    ``peer == to``. Any other ``value`` is returned unchanged.
    """
    if value != "last":
        return value
    match: str | None = None
    for rec in records:
        if rec.get("direction") != "in":
            continue
        if room is not None:
            if _effective_room(rec) != room:
                continue
        else:
            if _effective_room(rec) is not None:
                continue
            if rec.get("peer") != to:
                continue
        eid = _eid_of(rec)
        if eid is not None:
            match = eid
    if match is None:
        target = room if room is not None else to
        msg = f"no inbound message from {target} to reply to"
        raise ValueError(msg)
    return match


def resolve_reply_to(
    user_dir: str, value: str, *, to: str | None = None, room: str | None = None
) -> str:
    """Resolve ``"last"`` to the newest matching inbound ``eid``.

    With ``room``: the newest inbound record whose effective room is that
    room. Otherwise: the newest inbound record with no room whose
    ``peer == to``. Any other ``value`` is returned unchanged.
    """
    box = Mailbox(user_dir)
    records = box._read_all()
    return _resolve_from_records(records, value, to=to, room=room)


def _parent_thread(records: list[dict[str, Any]], eid: str) -> str | None:
    """Return the thread of the record whose ``env.eid == eid``.

    Falls back to ``eid`` when the record is present but declares no
    thread, and ``None`` when the parent is not in the log (so the caller
    opens a new thread).
    """
    for rec in records:
        if _eid_of(rec) != eid:
            continue
        env_any: Any = rec.get("env")
        thread_any: Any = env_any.get("thread") if isinstance(env_any, dict) else None
        if isinstance(thread_any, str) and thread_any:
            return thread_any
        return eid
    return None


def send_message(
    user_dir: str,
    name: str,
    session: str,
    *,
    msg_type: str,
    subject: str,
    body: str = "",
    to: str | None = None,
    room: str | None = None,
    thread: str | None = None,
    reply_to: str | None = None,
    data: dict[str, Any] | None = None,
    refs: list[str] | None = None,
) -> str:
    """Send one typed message and return its ``eid``.

    Exactly one of ``to`` or ``room`` must be given. ``reply_to`` may be
    ``"last"`` (resolved against a single read of the mailbox log). When a
    resolved ``reply_to`` is set and ``thread`` is None, the message
    inherits the parent record's thread. Builds the envelope with
    :func:`envelope.make`, encodes it, sends it through :class:`Retalk` and
    appends the ``direction:"out"`` record. ``EnvelopeError``,
    ``ValueError`` and ``RetalkError`` propagate.
    """
    if bool(to) == bool(room):
        msg = "exactly one of 'to' or 'room' must be given"
        raise ValueError(msg)
    box = Mailbox(user_dir)
    records = box._read_all()
    resolved_reply: str | None = reply_to
    if reply_to is not None:
        resolved_reply = _resolve_from_records(records, reply_to, to=to, room=room)
    if resolved_reply is not None and thread is None:
        thread = _parent_thread(records, resolved_reply)
    env = envelope_mod.make(
        msg_type,
        name,
        session,
        subject,
        body,
        thread=thread,
        reply_to=resolved_reply,
        room=room,
        refs=refs,
        data=data,
    )
    text = envelope_mod.encode(env)
    eid_any: Any = env.get("eid")
    if not isinstance(eid_any, str) or not eid_any:
        msg0 = "envelope missing eid"
        raise EnvelopeError(msg0)
    eid = eid_any
    client = Retalk(user_dir)
    if room:
        client.send_group(room, text)
        peer = room
        room_field: str | None = room
    elif to:
        client.send_peer(to, text)
        peer = to
        room_field = None
    else:
        msg2 = "exactly one of 'to' or 'room' must be given"
        raise ValueError(msg2)
    box.append(
        {
            "direction": "out",
            "tid": eid,
            "peer": peer,
            "peer_fp": None,
            "room": room_field,
            "recv_ts": _now_iso(),
            "raw": text,
            "env": env,
        }
    )
    return eid


def sanitize(rec: dict[str, Any], *, local: str) -> dict[str, Any]:
    """Return the sanitized view of mailbox record ``rec``.

    ``sender`` is the transport-bound identity: ``rec["peer"]`` for
    inbound records and ``local`` for outbound records. ``claimed`` is
    ``env.from.principal`` when it is a string different from ``sender``,
    else ``None``. The ``raw`` field and the full ``peer_fp`` are never
    exposed; ``verified_fp8`` is ``peer_fp[:8]`` when ``peer_fp`` is a
    string, else ``None``.
    """
    peer_fp_any: Any = rec.get("peer_fp")
    verified: str | None = peer_fp_any[:8] if isinstance(peer_fp_any, str) else None
    env_any: Any = rec.get("env")
    env_in: dict[str, Any] = env_any if isinstance(env_any, dict) else {}
    env_out: dict[str, Any] = {}
    for key in _ENV_KEYS:
        if key in env_in:
            env_out[key] = env_in[key]
    top_room: Any = rec.get("room")
    if isinstance(top_room, str):
        room_val: str | None = top_room
    else:
        env_room: Any = env_in.get("room")
        room_val = env_room if isinstance(env_room, str) else None
    direction: Any = rec.get("direction")
    peer_any: Any = rec.get("peer")
    sender: Any = local if direction == "out" else peer_any
    claimed: str | None = None
    from_any: Any = env_in.get("from")
    if isinstance(from_any, dict):
        principal_any: Any = from_any.get("principal")
        if isinstance(principal_any, str) and principal_any != sender:
            claimed = principal_any
    return {
        "seq": rec.get("seq"),
        "direction": rec.get("direction"),
        "peer": rec.get("peer"),
        "sender": sender,
        "claimed": claimed,
        "verified_fp8": verified,
        "room": room_val,
        "recv_ts": rec.get("recv_ts"),
        "env": env_out,
    }
