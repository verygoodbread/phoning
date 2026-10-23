"""Acceptance: @mention-aware actionability for room records (docs/spec/rules.md §4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from phoning import doorbell
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import inbound, outbound
from tests.acceptance.test_rules_common import (
    FP,
    PEER_TEXT,
    TID,
    bell,
    calls,
    codex_home_with_thread,
    fake_codex,
    home_fixture,  # noqa: F401  (registers the `home` fixture)
    state,
)


def run(home: Path, tmp: Path, *extra: str) -> list[dict[str, Any]]:
    exe, log = fake_codex(tmp)
    chome, _ = codex_home_with_thread(tmp, locked=True)
    assert bell("--ring", "codex-queue", "--codex-thread", TID, "--codex-bin", str(exe), "--codex-home", str(chome),
                "--min-interval", "0", *extra) == 0
    return calls(log)


@pytest.mark.parametrize("data", [
    None, {}, {"mentions": []}, {"mentions": ["bob"]}, {"mentions": "alice"}, {"mentions": {"alice": True}},
    {"mentions": [1, None]}, {"mentions": ["Alice"]}, {"mentions": ["alice2"]},
])
def test_room_records_without_our_mention_never_ring(home: Path, tmp_path: Path, data: Any) -> None:
    box = Mailbox(home / "alice")
    inbound(box, "carol", FP, "ask", PEER_TEXT, room="ops", data=data)
    inbound(box, "carol", FP, "propose", "p", room="ops", data={"scope": "x", **(data or {})})
    assert run(home, tmp_path) == []
    assert box.cursor("doorbell") == 2 and state(home)["pending"] is False


@pytest.mark.parametrize("typ", ["chat", "ask", "status"])
def test_room_record_mentioning_us_rings_whatever_its_type(home: Path, tmp_path: Path, typ: str) -> None:
    box = Mailbox(home / "alice")
    data: dict[str, Any] = {"mentions": ["mallory", "alice"]}
    if typ == "status":
        data["state"] = "ACTIVE"
    inbound(box, "carol", FP, typ, PEER_TEXT, PEER_TEXT, room="ops", data=data)
    got = run(home, tmp_path)
    assert [c["argv"][0] for c in got] == ["queue"]
    nudge = got[0]["argv"][4]
    assert PEER_TEXT not in nudge and "mallory" not in nudge and "ops" not in nudge.replace("operations", "")


def test_reply_to_our_own_room_send_rings(home: Path, tmp_path: Path) -> None:
    box = Mailbox(home / "alice")
    mine = outbound(box, "alice", "ops", "ask", "who owns the schema?", room="ops")
    inbound(box, "carol", FP, "chat", "i do", room="ops", reply_to=mine["env"]["eid"])
    inbound(box, "carol", FP, "chat", "unrelated", room="ops", reply_to="0" * 32)
    assert len(run(home, tmp_path)) == 1


def test_direct_records_keep_todays_rules(home: Path, tmp_path: Path) -> None:
    box = Mailbox(home / "alice")
    inbound(box, "bob", FP, "chat", "fyi", data={"mentions": ["alice"]})  # direct chat: still not actionable by type
    assert run(home, tmp_path / "a") == []
    inbound(box, "bob", FP, "ask", "q")  # direct ask, no mentions: actionable
    assert len(run(home, tmp_path / "b")) == 1


def test_is_actionable_without_a_name_ignores_mentions() -> None:
    rec: dict[str, Any] = {"env": {"type": "chat", "room": "ops", "reply_to": None, "data": {"mentions": ["alice"]}}, "room": "ops"}
    assert doorbell.is_actionable(rec, {"ask"}, set()) is False
    assert doorbell.is_actionable(rec, {"ask"}, set(), "alice") is True
    assert doorbell.is_actionable(rec, {"ask"}, set(), "bob") is False
