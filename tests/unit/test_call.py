"""Unit tests for ``phoning eavesdrop`` / ``phoning join`` (phoning/call.py)."""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import pytest

from phoning import call, cli
from phoning.mailbox import Mailbox
from phoning.retalk import RetalkError

FP = "0123456789abcdef0123456789abcdef"


def _rec(
    direction: str,
    peer: str,
    subject: str,
    *,
    principal: str | None = None,
    session: str = "s1",
    typ: str = "chat",
    room: str | None = "team",
    body: str = "",
    eid: str | None = None,
) -> dict[str, Any]:
    eid = eid or f"e{abs(hash((direction, peer, subject))) % 10**12}"
    return {
        "direction": direction,
        "tid": eid,
        "peer": peer,
        "peer_fp": FP if direction == "in" else None,
        "room": room,
        "recv_ts": "2026-10-22T10:00:00Z",
        "raw": "@hd1 secret raw text",
        "env": {
            "v": 1,
            "eid": eid,
            "ts": "2026-10-22T10:00:01Z",
            "from": {"principal": principal or peer, "session": session},
            "type": typ,
            "subject": subject,
            "body": body,
            "thread": eid,
            "room": room,
        },
    }


@pytest.fixture
def home(tmp_path: Path) -> Path:
    box = Mailbox(tmp_path / "dana")
    box.append(_rec("in", "bob", "who owns the schema?", typ="ask"))
    box.append(_rec("in", "carol", "side chat", room=None))
    box.append(_rec("in", "bob", "other room", room="ops"))
    box.append(_rec("in", "eve", "forged\x1b[31m", principal="bob", body="line1\nline2"))
    return tmp_path


def _eavesdrop(home: Path, *extra: str) -> str:
    out = io.StringIO()
    assert call.run_call("dana", home=str(home), room="team", peer=None, session="eavesdrop",
                         talk=False, stdout=out, once=True, **dict(e.split("=", 1) for e in extra)) == 0
    return out.getvalue()


def test_eavesdrop_shows_board_and_only_this_room(home: Path) -> None:
    text = _eavesdrop(home)
    assert "# Board" in text and "## Open asks" in text
    assert "listening in on party line team as dana" in text
    assert "who owns the schema?" in text
    assert "side chat" not in text and "other room" not in text


def test_eavesdrop_shows_verified_sender_and_strips_control_chars(home: Path) -> None:
    text = _eavesdrop(home)
    assert "eve/s1 (claims bob) chat: forged" in text
    assert "\x1b" not in text
    assert "  | line1" in text and "  | line2" in text
    assert "secret raw text" not in text and FP not in text


def test_eavesdrop_never_moves_a_cursor(home: Path) -> None:
    _eavesdrop(home)
    assert not os.path.exists(Mailbox(home / "dana").cursors_dir) or not os.listdir(
        Mailbox(home / "dana").cursors_dir
    )


def test_eavesdrop_direct_line(home: Path) -> None:
    out = io.StringIO()
    assert call.run_call("dana", home=str(home), room=None, peer="carol", session="x",
                         talk=False, stdout=out, once=True) == 0
    text = out.getvalue()
    assert "direct line with carol" in text and "side chat" in text
    assert "who owns the schema?" not in text


def test_missing_mailbox_is_usage_error_and_creates_nothing(tmp_path: Path) -> None:
    assert call.run_call("nobody", home=str(tmp_path), room="team", peer=None, session="x",
                         talk=False, stdout=io.StringIO(), once=True) == call.EXIT_USAGE
    assert not (tmp_path / "nobody").exists()


class _Spy:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def __call__(self, udir: str, name: str, session: str, **kw: Any) -> str:
        if self.fail:
            raise RetalkError.__new__(RetalkError)
        self.calls.append({"udir": udir, "name": name, "session": session, **kw})
        return f"sent{len(self.calls)}"


def _join(home: Path, typed: str, spy: _Spy, announce: bool = True) -> str:
    out = io.StringIO()
    rc = call.run_call("dana", home=str(home), room="team", peer=None, session="human", talk=True,
                       stdin=io.StringIO(typed), stdout=out, poll=0.01, send=spy, announce=announce)
    assert rc == 0
    return out.getvalue()


def _talk(spy: _Spy) -> list[dict[str, Any]]:
    return [c for c in spy.calls if c["msg_type"] != "status"]


def test_join_sends_lines_as_chat_and_asks(home: Path) -> None:
    spy = _Spy()
    text = _join(home, "hello all\n\n/ask who is on the migration?\n/leave\nnot sent\n", spy)
    assert [(c["msg_type"], c["subject"], c["room"], c["to"]) for c in spy.calls] == [
        ("status", "human joined the call", "team", None),
        ("chat", "hello all", "team", None),
        ("ask", "who is on the migration?", "team", None),
        ("status", "human left the call", "team", None),
    ]
    assert all(c["session"] == "human" and c["name"] == "dana" for c in spy.calls)
    assert "joined party line team as human" in text and "hung up on party line team" in text


def test_join_long_line_keeps_full_text_in_body(home: Path) -> None:
    spy = _Spy()
    long = "x" * 150
    _join(home, long + "\n", spy)
    (c,) = _talk(spy)
    assert len(c["subject"]) == call.SUBJECT_MAX and c["subject"].endswith("...")
    assert c["body"] == long


def test_join_commands_do_not_send(home: Path) -> None:
    spy = _Spy()
    text = _join(home, "/board\n/what\n/ask\n", spy)
    assert _talk(spy) == []
    assert text.count("# Board") == 2
    assert "usage: /ask SUBJECT" in text and call.HELP_TEXT in text


def test_join_send_failure_keeps_the_call_open(home: Path) -> None:
    spy = _Spy(fail=True)
    text = _join(home, "first\nsecond\n", spy)
    assert text.count("error: not sent") == 4  # joined, two lines, left


def test_join_does_not_echo_its_own_sends(home: Path) -> None:
    box = Mailbox(home / "dana")

    def send(udir: str, name: str, session: str, **kw: Any) -> str:
        rec = _rec("out", "team", kw["subject"], principal="dana", session=session, eid="mine1")
        box.append(rec)
        return "mine1"

    out = io.StringIO()
    stdin = io.StringIO("only once\n")
    assert call.run_call("dana", home=str(home), room="team", peer=None, session="human", talk=True,
                         stdin=stdin, stdout=out, poll=0.01, send=send) == 0
    assert out.getvalue().count("only once") == 0


def test_cli_parsers_validate(home: Path, capsys: Any) -> None:
    assert call.eavesdrop_main(["dana", "--room", "team", "--home", str(home), "--once"]) == 0
    assert "party line team" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        call.join_main(["dana", "--home", str(home)])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        call.eavesdrop_main(["dana", "--room", "a", "--to", "b"])
    assert call.eavesdrop_main(["dana", "--room", "team", "--home", str(home), "--poll", "0"]) == 2


def test_top_level_cli_routes_join_and_eavesdrop(monkeypatch: Any) -> None:
    seen: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(call, "join_main", lambda argv: seen.append(("join", list(argv))) or 0)
    monkeypatch.setattr(call, "eavesdrop_main", lambda argv: seen.append(("eavesdrop", list(argv))) or 0)
    assert cli.main(["join", "dana", "--room", "team"]) == 0
    assert cli.main(["eavesdrop", "dana", "--room", "team"]) == 0
    assert seen == [("join", ["dana", "--room", "team"]), ("eavesdrop", ["dana", "--room", "team"])]
    text = cli.build_parser().format_help()
    assert "eavesdrop" in text and "join" in text


def test_join_announces_itself_and_marks_every_message_human(home: Path) -> None:
    spy = _Spy()
    _join(home, "hi\n", spy)
    joined, chat, left = spy.calls
    assert joined["msg_type"] == "status"
    assert joined["data"] == {"human": True, "state": "ACTIVE", "presence": "joined"}
    assert chat["data"] == {"human": True}
    assert left["msg_type"] == "status"
    assert left["data"] == {"human": True, "state": "IDLE", "presence": "left"}


def test_join_quiet_skips_the_announcements(home: Path) -> None:
    spy = _Spy()
    _join(home, "hi\n", spy, announce=False)
    assert [c["msg_type"] for c in spy.calls] == ["chat"]


def test_eavesdrop_never_sends(home: Path) -> None:
    spy = _Spy()
    assert call.run_call("dana", home=str(home), room="team", peer=None, session="eavesdrop", talk=False,
                         stdout=io.StringIO(), once=True, send=spy) == 0
    assert spy.calls == []


@pytest.mark.parametrize(("line", "shown"), [("human-sam", "sam"), ("sam", "sam"), ("human-", "human-")])
def test_display_name(line: str, shown: str) -> None:
    assert call.display_name(line) == shown


def test_join_main_shows_the_human_by_name(monkeypatch: Any) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(call, "run_call", lambda name, **kw: seen.append({"name": name, **kw}) or 0)
    assert call.join_main(["human-sam", "--room", "team"]) == 0
    assert call.join_main(["human-sam", "--room", "team", "--as", "Pat Example", "--quiet"]) == 0
    assert call.eavesdrop_main(["human-sam", "--room", "team"]) == 0
    assert [(c["session"], c["announce"], c["talk"]) for c in seen] == [
        ("sam", True, True), ("Pat Example", False, True), ("eavesdrop", True, False)]
