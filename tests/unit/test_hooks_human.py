"""Unit tests: hook text cues agents to greet a human who joined the call (``data.human``)."""

from __future__ import annotations

from typing import Any

from phoning import hooks

FP = "0123456789abcdef0123456789abcdef"


def _rec(seq: int, peer: str, subject: str, data: Any = None, principal: str | None = None) -> dict[str, Any]:
    env: dict[str, Any] = {"v": 1, "eid": f"e{seq}", "ts": "2026-10-22T10:00:00Z", "type": "chat",
                           "from": {"principal": principal or peer, "session": "s"}, "subject": subject,
                           "body": "", "room": "team"}
    if data is not None:
        env["data"] = data
    return {"seq": seq, "direction": "in", "peer": peer, "peer_fp": FP, "room": "team", "env": env}


def _text(recs: list[dict[str, Any]]) -> str:
    return hooks._build_text(len(recs), recs, "claude-main", "r", 400, 10, False, home="/tmp/h")


def test_human_message_adds_a_greeting_cue_by_verified_name() -> None:
    text = _text([_rec(1, "sam", "sam joined the call", {"human": True, "presence": "joined"}),
                  _rec(2, "sam", "hi all", {"human": True})])
    assert 'sam is a human on this call. When you reply, open with a quick, warm hello by name (like "oh hey sam!")' in text
    assert text.count("is a human on this call") == 1


def test_no_cue_without_the_flag_or_with_a_non_true_flag() -> None:
    text = _text([_rec(1, "codex-main", "status"), _rec(2, "bob", "x", {"human": "yes"}), _rec(3, "eve", "y", [1])])
    assert "human on this call" not in text


def test_cue_names_the_verified_sender_not_the_claim() -> None:
    text = _text([_rec(1, "eve", "hello", {"human": True}, principal="sam\x1b[31m")])
    assert "eve is a human on this call" in text
    assert "oh hey sam" not in text and "\x1b" not in text


def test_two_humans_are_named_together() -> None:
    text = _text([_rec(1, "sam", "a", {"human": True}), _rec(2, "dana", "b", {"human": True})])
    assert 'sam, dana are humans on this call' in text
