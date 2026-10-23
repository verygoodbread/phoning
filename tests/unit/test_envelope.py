"""Unit tests for phoning.envelope."""

import json

import pytest

from phoning import envelope
from phoning.envelope import EnvelopeError, decode, encode, make, validate


def _base(**over: object) -> dict[str, object]:
    env = make("chat", "alice", "s1", "hello")
    env.update(over)  # type: ignore[typeddict-unknown-key]
    return env  # type: ignore[return-value]


def test_make_defaults() -> None:
    env = make("chat", "alice", "s1", "hi")
    assert env["v"] == 1
    assert isinstance(env["eid"], str) and len(env["eid"]) == 32
    int(env["eid"], 16)
    assert isinstance(env["ts"], str) and env["ts"].endswith("Z")
    assert env["thread"] == env["eid"]
    assert env["reply_to"] is None
    assert env["room"] is None
    assert env["refs"] == []
    assert env["data"] == {}
    assert env["from"] == {"principal": "alice", "session": "s1"}


def test_make_explicit_thread_room_refs_data() -> None:
    env = make(
        "ask",
        "bob",
        "s2",
        "need help",
        body="details",
        thread="t1",
        room="general",
        refs=["a", "b"],
        data={"k": 1},
    )
    assert env["thread"] == "t1"
    assert env["room"] == "general"
    assert env["refs"] == ["a", "b"]
    assert env["data"] == {"k": 1}
    assert env["body"] == "details"


def test_encode_prefix_and_roundtrip() -> None:
    env = make("chat", "alice", "s1", "hello world", body="b")
    wire = encode(env)
    assert wire.startswith("@hd1 chat hello world\n")
    _head, _, payload = wire.partition("\n")
    assert json.loads(payload)["eid"] == env["eid"]
    back = decode(wire, fallback_from="nobody")
    assert back == env
    assert back.get("legacy") is None


def test_roundtrip_all_types() -> None:
    cases: list[dict[str, object]] = [
        {"type": "chat", "principal": "a", "session": "s", "subject": "c"},
        {"type": "status", "principal": "a", "session": "s", "subject": "on it",
         "data": {"state": "ACTIVE"}},
        {"type": "ask", "principal": "a", "session": "s", "subject": "question?"},
        {"type": "propose", "principal": "a", "session": "s", "subject": "change x",
         "data": {"scope": "src/x.py"}},
    ]
    ask = make("ask", "a", "s", "q?")
    for kw in cases:
        type_ = str(kw.pop("type"))
        principal = str(kw.pop("principal"))
        session = str(kw.pop("session"))
        subject = str(kw.pop("subject"))
        env = make(type_, principal, session, subject, **kw)  # type: ignore[arg-type]
        assert decode(encode(env)) == env
    acc = make("accept", "a", "s", "yes", reply_to=ask["eid"])  # type: ignore[typeddict-item]
    assert decode(encode(acc)) == acc
    rej = make("reject", "a", "s", "no", reply_to=ask["eid"])  # type: ignore[typeddict-item]
    assert decode(encode(rej)) == rej
    rec = make("receipt", "a", "s", "got it", reply_to=ask["eid"], refs=["x"])  # type: ignore[typeddict-item]
    assert decode(encode(rec)) == rec
    ack = make("ack", "a", "s", "ack it", reply_to=ask["eid"])  # type: ignore[typeddict-item]
    assert decode(encode(ack)) == ack


def test_make_rejects_bad_subject() -> None:
    with pytest.raises(EnvelopeError):
        make("chat", "a", "s", "")
    with pytest.raises(EnvelopeError):
        make("chat", "a", "s", "x" * 81)
    with pytest.raises(EnvelopeError):
        make("chat", "a", "s", "bad\nsubject")


def test_validate_subject_rules() -> None:
    with pytest.raises(EnvelopeError):
        validate(_base(subject=""))
    with pytest.raises(EnvelopeError):
        validate(_base(subject="x" * 81))
    with pytest.raises(EnvelopeError):
        validate(_base(subject="a\nb"))
    with pytest.raises(EnvelopeError):
        validate(_base(subject="a\rb"))
    with pytest.raises(EnvelopeError):
        validate(_base(subject=123))  # type: ignore[typeddict-unknown-key]


def test_validate_subject_control_characters() -> None:
    for bad in ("esc\x1b[2J", "bell\x07", "nul\x00", "c1\x9b", "del\x7f"):
        with pytest.raises(EnvelopeError):
            validate(_base(subject=bad))


def test_validate_type() -> None:
    with pytest.raises(EnvelopeError):
        validate(_base(type="bogus"))


def test_validate_reply_to_required() -> None:
    for t in ("accept", "reject", "receipt", "ack"):
        env = _base(type="chat")
        env["type"] = t
        if t == "receipt":
            env["refs"] = ["r"]
        with pytest.raises(EnvelopeError):
            validate(env)  # type: ignore[arg-type]
        env["reply_to"] = "abc"
        validate(env)  # type: ignore[arg-type]


def test_validate_reports_every_unmet_rule() -> None:
    with pytest.raises(EnvelopeError) as ei:
        make("receipt", "a", "s", "done")
    msg = str(ei.value)
    assert "receipt needs reply_to" in msg
    assert "receipt needs refs" in msg
    assert "; " in msg


def test_validate_status_state() -> None:
    env = make("status", "a", "s", "working", data={"state": "ACTIVE"})
    validate(env)
    bad = _base(type="status", subject="w")
    bad["data"] = {}
    with pytest.raises(EnvelopeError):
        validate(bad)  # type: ignore[arg-type]
    bad["data"] = {"state": "SLEEPING"}
    with pytest.raises(EnvelopeError):
        validate(bad)  # type: ignore[arg-type]


def test_validate_propose_scope() -> None:
    with pytest.raises(EnvelopeError):
        validate(_base(type="propose", subject="p", data={}))
    with pytest.raises(EnvelopeError):
        validate(_base(type="propose", subject="p", data={"scope": ""}))
    with pytest.raises(EnvelopeError):
        validate(_base(type="propose", subject="p", data={"scope": 123}))


def test_validate_receipt_refs() -> None:
    env = _base(type="receipt", subject="r", reply_to="x", refs=[])
    with pytest.raises(EnvelopeError):
        validate(env)  # type: ignore[arg-type]
    env["refs"] = ["one"]
    validate(env)  # type: ignore[arg-type]


def test_validate_envelope_shape() -> None:
    with pytest.raises(EnvelopeError):
        validate(_base(v=2))
    with pytest.raises(EnvelopeError):
        validate(_base(eid=""))
    with pytest.raises(EnvelopeError):
        validate(_base(thread=""))
    with pytest.raises(EnvelopeError):
        validate(_base(**{"from": {"principal": "", "session": "s"}}))
    with pytest.raises(EnvelopeError):
        validate(_base(**{"from": {"principal": "a", "session": ""}}))
    with pytest.raises(EnvelopeError):
        validate(_base(body=123))  # type: ignore[typeddict-unknown-key]
    with pytest.raises(EnvelopeError):
        validate(_base(refs="x"))  # type: ignore[typeddict-unknown-key]
    with pytest.raises(EnvelopeError):
        validate(_base(data=[]))  # type: ignore[typeddict-unknown-key]


def test_validate_malformed_never_crashes() -> None:
    for bad in ({"v": 1}, {"v": 1, "type": "receipt"}):
        with pytest.raises(EnvelopeError):
            validate(bad)


def test_rules_text_mentions_every_type() -> None:
    for word in ("status", "ACTIVE", "propose", "scope", "receipt", "refs", "ack", "reply-to"):
        assert word in envelope.RULES_TEXT
    assert "subject:" in envelope.RULES_TEXT


def test_decode_plain_is_legacy() -> None:
    out = decode("hello world", fallback_from="bob")
    assert out["type"] == "chat"
    assert out["legacy"] is True
    assert out["subject"] == "hello world"
    assert out["body"] == "hello world"
    assert out["from"] == {"principal": "bob", "session": "?"}
    assert out["refs"] == []
    assert out["data"] == {}


def test_decode_empty_is_legacy() -> None:
    out = decode("", fallback_from="x")
    assert out["legacy"] is True
    assert out["subject"] == "(empty)"
    assert out["body"] == ""


def test_decode_subject_truncated_to_first_line_80() -> None:
    body = "a" * 100 + "\nsecond line"
    out = decode(body, fallback_from="z")
    assert out["subject"] == "a" * 80
    assert out["body"] == body


def test_decode_bad_json_is_legacy() -> None:
    out = decode("@hd1 chat hi\n{not json", fallback_from="ann")
    assert out["legacy"] is True
    assert out["type"] == "chat"
    assert out["body"] == "@hd1 chat hi\n{not json"
    assert out["from"]["principal"] == "ann"


def test_decode_invalid_envelope_is_legacy() -> None:
    bad = {"v": 1, "eid": "e", "ts": "t", "from": {"principal": "a", "session": "s"},
           "type": "bogus", "subject": "s", "body": "", "thread": "e",
           "reply_to": None, "room": None, "refs": [], "data": {}}
    wire = "@hd1 bogus s\n" + json.dumps(bad)
    out = decode(wire, fallback_from="a")
    assert out["legacy"] is True
    assert out["type"] == "chat"

    bad2 = {"v": 1, "eid": "e", "ts": "t", "from": {"principal": "a", "session": "s"},
            "type": "accept", "subject": "s", "body": "", "thread": "e",
            "reply_to": None, "room": None, "refs": [], "data": {}}
    out2 = decode("@hd1 accept s\n" + json.dumps(bad2), fallback_from="a")
    assert out2["legacy"] is True


def test_decode_control_char_subject_is_legacy() -> None:
    env = make("chat", "a", "s", "fine")
    env["subject"] = "esc\x1b[2Jclear"
    out = decode("@hd1 chat x\n" + json.dumps(env), fallback_from="a")
    assert out["legacy"] is True
    assert out["type"] == "chat"
    assert out["body"] == "@hd1 chat x\n" + json.dumps(env)


def test_decode_missing_newline_is_legacy() -> None:
    out = decode("@hd1 chat hi", fallback_from="q")
    assert out.get("legacy") is True


def test_decode_proto_prefix_is_legacy() -> None:
    env = make("chat", "a", "s", "hi")
    wire = envelope.MAGIC + "x"  # sanity
    assert wire.startswith("@hd1 ")
    proto_wire = "@at1 chat hi\n" + json.dumps(env)
    out = decode(proto_wire, fallback_from="a")
    assert out.get("legacy") is True


def test_decode_never_raises() -> None:
    for text in ["", "\n\n", "@hd1 ", "@hd1 chat\n", "@hd1 chat s\nnull",
                 "@hd1 chat s\n[]", "@hd1 chat s\n{}", "   "]:
        out = decode(text, fallback_from="f")
        assert out["type"] == "chat"
        assert out["legacy"] is True


def test_thread_defaults_to_eid() -> None:
    env = make("chat", "a", "s", "root")
    assert env["thread"] == env["eid"]
    child = make("chat", "a", "s", "child", thread=env["eid"])
    assert child["thread"] == env["eid"]
