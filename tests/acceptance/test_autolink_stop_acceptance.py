"""Acceptance: @mention semantics on Stop (docs/spec/autolink.md §3) and the PHONING_HOME prefix (§4)."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any

import pytest

from phoning import hooks
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import inbound, outbound

FP = "a3f1c2d4e5b60718293a4b5c6d7e8f90"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k in ("PHONING_NAME", "PHONING_READER", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PHONING_SESSIONS", str(tmp_path / "sessions.json"))
    return tmp_path / "home"


def stop(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
         extra: list[str] | None = None, event: dict[str, Any] | None = None) -> tuple[int, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event or {})))
    rc = hooks.main(["stop", "--name", "alice", "--reader", "r", *(extra or [])])
    return rc, capsys.readouterr().out


def test_room_ask_without_mention_does_not_block(home: Path, monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    inbound(box, "bob", FP, "ask", "anyone know the schema owner?", room="ops")
    rc, out = stop(monkeypatch, capsys)
    assert (rc, out) == (0, "") and box.cursor("r") == 0  # nothing committed: surfaces next prompt
    # user-prompt still shows it, and consumes it
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert hooks.main(["user-prompt", "--name", "alice", "--reader", "r"]) == 0
    assert "anyone know the schema owner?" in capsys.readouterr().out and box.cursor("r") == 1


def test_room_ask_mentioning_us_blocks(home: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    inbound(box, "bob", FP, "ask", "alice, who owns it?", room="ops", data={"mentions": ["alice"]})
    rc, out = stop(monkeypatch, capsys)
    assert rc == 0 and json.loads(out)["decision"] == "block"
    assert "alice, who owns it?" in json.loads(out)["reason"] and box.cursor("r") == 1


def test_mention_must_name_us_exactly_and_be_a_list(home: Path, monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    for mentions in (["bob"], ["Alice"], ["alice2"], "alice", {"alice": True}, [["alice"]], [], None):
        inbound(box, "bob", FP, "ask", "not for us", room="ops", data={"mentions": mentions})
    inbound(box, "bob", FP, "ask", "no data at all", room="ops")
    rc, out = stop(monkeypatch, capsys)
    assert (rc, out) == (0, "") and box.cursor("r") == 0


def test_room_reply_to_our_own_send_blocks(home: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    mine = outbound(box, "alice", "ops", "ask", "who owns the schema?", room="ops")
    inbound(box, "bob", FP, "reject", "wrong place", room="ops", reply_to="e" * 32)  # not ours
    rc, out = stop(monkeypatch, capsys)
    assert (rc, out) == (0, "")
    inbound(box, "bob", FP, "reject", "wrong place", room="ops", reply_to=mine["env"]["eid"])
    rc, out = stop(monkeypatch, capsys)
    assert rc == 0 and json.loads(out)["decision"] == "block"
    assert box.cursor("r") == 3


def test_reply_to_an_inbound_eid_is_not_ours(home: Path, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    theirs = inbound(box, "bob", FP, "chat", "hello room", room="ops")
    inbound(box, "carol", FP, "ask", "re bob", room="ops", reply_to=theirs["env"]["eid"])
    rc, out = stop(monkeypatch, capsys)
    assert (rc, out) == (0, "")


def test_addressing_is_necessary_not_sufficient(home: Path, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    mine = outbound(box, "alice", "ops", "ask", "q", room="ops")
    inbound(box, "bob", FP, "chat", "mentions you", room="ops", data={"mentions": ["alice"]})
    inbound(box, "bob", FP, "ack", "ok", room="ops", reply_to=mine["env"]["eid"])
    rc, out = stop(monkeypatch, capsys)
    assert (rc, out) == (0, "") and box.cursor("r") == 0  # chat/ack are not block types
    rc, out = stop(monkeypatch, capsys, ["--block-types", "chat"])
    assert json.loads(out)["decision"] == "block"
    box2 = Mailbox(home / "alice")
    assert box2.cursor("r") == 3


def test_direct_records_keep_todays_rules(home: Path, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    inbound(box, "bob", FP, "chat", "hi", data={"mentions": ["alice"]})
    rc, out = stop(monkeypatch, capsys)
    assert (rc, out) == (0, "")
    inbound(box, "bob", FP, "ask", "direct ask, no mention")
    rc, out = stop(monkeypatch, capsys)
    assert json.loads(out)["decision"] == "block" and box.cursor("r") == 2


def test_blocking_shows_every_unread_record_including_unaddressed_room_traffic(
        home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    inbound(box, "bob", FP, "ask", "room noise", room="ops")
    inbound(box, "bob", FP, "ask", "direct one")
    rc, out = stop(monkeypatch, capsys)
    reason = json.loads(out)["reason"]
    assert "direct one" in reason and "room noise" in reason and box.cursor("r") == 2


def test_empty_block_types_and_stop_hook_active_still_short_circuit(
        home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    inbound(box, "bob", FP, "ask", "hey", room="ops", data={"mentions": ["alice"]})
    assert stop(monkeypatch, capsys, ["--block-types", ""]) == (0, "")
    assert stop(monkeypatch, capsys, event={"stop_hook_active": True}) == (0, "")
    assert box.cursor("r") == 0


def test_room_taken_from_env_room_counts_as_room(home: Path, monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    rec = inbound(box, "bob", FP, "ask", "env-room only", room="ops")
    # simulate a record whose room lives only in the envelope
    log = home / "alice" / "mailbox" / "log.jsonl"
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    lines[0]["room"] = None
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    assert rec["env"]["room"] == "ops"
    assert stop(monkeypatch, capsys) == (0, "")


def test_mention_stop_text_keeps_the_home_prefix(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    other = tmp_path / "Codex work" / "phoning"
    box = Mailbox(other / "orch")
    inbound(box, "bob", FP, "ask", "orch, please", room="ops", data={"mentions": ["orch"]})
    monkeypatch.setenv("CODEX_THREAD_ID", "sess-orch")
    assert hooks.main(["bind", "--name", "orch", "--home", str(other), "--reader", "orch-main"]) == 0
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "sess-orch"})))
    assert hooks.main(["stop"]) == 0
    reason = json.loads(capsys.readouterr().out)["reason"]
    quoted = f"PHONING_HOME='{other}'"
    send_line = next(ln for ln in reason.splitlines() if "phoning send orch" in ln)
    assert quoted in send_line
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "sess-orch"})))
    inbound(box, "bob", FP, "chat", "later")
    assert hooks.main(["session-start", "--arm-waker"]) == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    for cmd in ("wait orch --reader orch-main --timeout 3600", "read orch --reader orch-main --sanitized"):
        assert quoted in next(ln for ln in ctx.splitlines() if cmd in ln)


def test_stop_is_fast_with_a_big_log_and_many_unaddressed_room_asks(
        home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    for i in range(3000):
        outbound(box, "alice", "ops", "status", f"s{i}", room="ops", data={"state": "ACTIVE"})
    for i in range(300):
        inbound(box, "bob", FP, "ask", f"q{i}", room="ops")
    t0 = time.monotonic()
    assert stop(monkeypatch, capsys) == (0, "")
    assert time.monotonic() - t0 < 2.0
