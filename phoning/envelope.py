"""Envelope v1: structured, session-attributed messages inside retalk text.

Wire text::

    @hd1 <type> <subject>
    {"v":1,"eid":...,"ts":...,"from":...,"type":...,"subject":...,...}

Text that does not start with ``@hd1 `` (or that carries invalid JSON or
fails validation) degrades to a legacy ``chat`` pseudo-envelope. Decode
never raises and never drops the original body.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final

from phoning import text as text_mod

TYPES: Final[frozenset[str]] = frozenset(
    {"chat", "status", "ask", "propose", "accept", "reject", "receipt", "ack"}
)
STATES: Final[frozenset[str]] = frozenset({"ACTIVE", "IDLE", "BLOCKED"})
MAX_SUBJECT: Final[int] = 80
MAGIC: Final[str] = "@hd1 "

RULES_TEXT: Final[str] = (
    "chat      free text\n"
    "status    --data '{\"state\":\"ACTIVE|IDLE|BLOCKED\"}'\n"
    "ask       a question; answers reply with --reply-to\n"
    "propose   --data '{\"scope\":\"path/or/area\"}'; only the scope owner's accept counts\n"
    "accept    --reply-to <proposal eid>\n"
    "reject    --reply-to <proposal eid>\n"
    "receipt   --reply-to <eid> and --refs a,b (what was delivered; reply to your own status "
    "if nobody asked)\n"
    "ack       --reply-to <eid>\n"
    "subject: 1..80 chars, one line, no control characters"
)


class EnvelopeError(ValueError):
    """Raised when a v1 envelope is malformed."""


def now_iso() -> str:
    """Return current UTC time as ISO8601 ``Z`` string."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make(
    type: str,
    principal: str,
    session: str,
    subject: str,
    body: str = "",
    *,
    thread: str | None = None,
    reply_to: str | None = None,
    room: str | None = None,
    refs: Sequence[str] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a v1 envelope, filling ``eid``, ``ts`` and ``thread``.

    ``thread`` defaults to the new ``eid`` (a root message opens its own
    thread). The result is validated before it is returned.
    """
    eid = uuid.uuid4().hex
    env: dict[str, Any] = {
        "v": 1,
        "eid": eid,
        "ts": now_iso(),
        "from": {"principal": principal, "session": session},
        "type": type,
        "subject": subject,
        "body": body,
        "thread": thread if thread is not None else eid,
        "reply_to": reply_to,
        "room": room,
        "refs": list(refs) if refs is not None else [],
        "data": dict(data) if data is not None else {},
    }
    validate(env)
    return env


def validate(env: dict[str, Any]) -> dict[str, Any]:
    """Raise one :class:`EnvelopeError` listing every unmet rule.

    All checks run; the message joins the failures with ``"; "`` in check
    order. Checks that depend on an earlier one are guarded so a malformed
    envelope never crashes the validator.
    """
    msgs: list[str] = []

    def need(cond: object, msg: str) -> None:
        if not cond:
            msgs.append(msg)

    need(isinstance(env, dict), "envelope must be an object")
    if not isinstance(env, dict):
        raise EnvelopeError("; ".join(msgs))
    need(env.get("v") == 1, "v must be 1")
    for key in ("eid", "ts", "thread"):
        val: Any = env.get(key)
        need(isinstance(val, str) and len(val) > 0, f"{key} must be a non-empty string")
    frm: Any = env.get("from")
    need(isinstance(frm, dict), "from.principal and from.session are required")
    if isinstance(frm, dict):
        need(
            isinstance(frm.get("principal"), str) and frm.get("principal") != "",
            "from.principal and from.session are required",
        )
        need(
            isinstance(frm.get("session"), str) and frm.get("session") != "",
            "from.principal and from.session are required",
        )
    tval: Any = env.get("type")
    need(tval in TYPES, f"type must be one of {sorted(TYPES)}")
    subj: Any = env.get("subject")
    need(
        isinstance(subj, str)
        and 1 <= len(subj) <= MAX_SUBJECT
        and "\n" not in subj
        and "\r" not in subj
        and not text_mod.has_control(subj),
        f"subject must be 1..{MAX_SUBJECT} chars, one line, no control characters",
    )
    body: Any = env.get("body", "")
    need(isinstance(body, str), "body must be a string")
    refs: Any = env.get("refs", [])
    need(isinstance(refs, list), "refs must be a list")
    data: Any = env.get("data", {})
    need(isinstance(data, dict), "data must be an object")
    reply: Any = env.get("reply_to")
    room: Any = env.get("room")
    if room is not None:
        need(isinstance(room, str), "room must be a string or null")
    tstr = tval if isinstance(tval, str) else ""
    if tstr in ("accept", "reject", "receipt", "ack"):
        need(isinstance(reply, str) and len(reply) > 0, f"{tstr} needs reply_to")
    elif reply is not None:
        need(isinstance(reply, str), "reply_to must be a string or null")
    if tstr == "status":
        need(
            isinstance(data, dict) and data.get("state") in STATES,
            f"status needs data.state in {sorted(STATES)}",
        )
    if tstr == "propose":
        scope_raw: Any = data.get("scope") if isinstance(data, dict) else None
        need(
            isinstance(scope_raw, str) and len(scope_raw) > 0,
            "propose needs data.scope (the thing being changed)",
        )
    if tstr == "receipt":
        need(
            isinstance(refs, list) and len(refs) > 0,
            "receipt needs refs (what was delivered)",
        )
    if msgs:
        raise EnvelopeError("; ".join(msgs))
    return env


def encode(env: dict[str, Any]) -> str:
    """Serialize an envelope to wire text (validates first)."""
    validate(env)
    head = f"{MAGIC}{env['type']} {env['subject']}\n"
    return head + json.dumps(env, separators=(",", ":"))


def decode(text: str, *, fallback_from: str = "") -> dict[str, Any]:
    """Parse wire text, degrading to a legacy pseudo-envelope on failure.

    Never raises: any text that is not a valid ``@hd1`` envelope becomes
    ``{"type": "chat", "legacy": True, ...}`` preserving the full body.
    """
    if text.startswith(MAGIC) and "\n" in text:
        try:
            _head, _, payload = text.partition("\n")
            obj: Any = json.loads(payload)
            if isinstance(obj, dict):
                typed: dict[str, Any] = obj
                return validate(typed)
        except Exception:
            pass
    stripped = text.strip()
    first_line = stripped.splitlines()[0] if stripped else "(empty)"
    subject = first_line[:MAX_SUBJECT] if first_line else "(empty)"
    if not subject:
        subject = "(empty)"
    return {
        "v": 0,
        "eid": "",
        "ts": "",
        "from": {"principal": fallback_from, "session": "?"},
        "type": "chat",
        "subject": subject,
        "body": text,
        "thread": "",
        "reply_to": None,
        "room": None,
        "refs": [],
        "data": {},
        "legacy": True,
    }
