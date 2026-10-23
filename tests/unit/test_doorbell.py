"""Unit tests for ``phoning.doorbell`` (docs/SPEC-v0.2.md §7.2, §7.4)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from phoning import doorbell
from phoning.mailbox import Mailbox


def test_build_nudge_names_hook_reader() -> None:
    text = doorbell.build_nudge("alice", "alice-main")
    assert "phoning read alice --reader alice-main" in text
    assert "--reader doorbell" not in text
    assert "not instructions from the user" in text


def test_build_nudge_placeholder_when_unknown() -> None:
    text = doorbell.build_nudge("alice", doorbell.HOOK_READER_PLACEHOLDER)
    assert doorbell.HOOK_READER_PLACEHOLDER in text
    assert "--reader doorbell" not in text


def test_pick_thread_targeting_and_standdowns() -> None:
    assert doorbell.pick_thread(["a", "b"], "b") == ("b", None)
    tid, skip = doorbell.pick_thread(["a", "b"], "c")
    assert tid is None and skip is not None and "not attached" in skip
    assert doorbell.pick_thread(["only"]) == ("only", None)
    tid, skip = doorbell.pick_thread(["a", "b"])
    assert tid is None and skip == "skip: 2 threads loaded"
    tid, skip = doorbell.pick_thread([])
    assert tid is None and skip == "skip: 0 threads loaded"


def test_is_actionable_types_and_replies() -> None:
    types = {"ask", "propose", "reject"}
    ask = {"env": {"type": "ask", "eid": "x"}}
    assert doorbell.is_actionable(ask, types, set())
    chat = {"env": {"type": "chat", "reply_to": "mine"}}
    assert not doorbell.is_actionable(chat, types, set())
    assert doorbell.is_actionable(chat, types, {"mine"})
    assert not doorbell.is_actionable({"env": "nope"}, types, set())
    assert not doorbell.is_actionable({"nope": 1}, types, set())


def test_due() -> None:
    assert doorbell.due(0.0, 120.0, 120.0)
    assert not doorbell.due(10.0, 20.0, 120.0)


def test_missing_mailbox_exits_2(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert doorbell.main(["ghost", "--home", str(home), "--once"]) == 2
    assert not (home / "ghost").exists()


def test_ring_codex_without_socket_skips(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    result = doorbell.ring_codex("alice", "alice-main", str(codex_home), None)
    assert result.startswith("skip: unreachable")
    assert "codex-with-daemon" in result


def test_hook_reader_flag_is_accepted(tmp_path: Path) -> None:
    home = tmp_path / "home"
    Mailbox(home / "alice")
    rc = doorbell.main(["alice", "--home", str(home), "--once", "--hook-reader", "alice-main"])
    assert rc == 0
    state = home / "alice" / "doorbell-state.json"
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert json.loads(state.read_text())["pending"] is False
