"""Unit tests for phoning.mailbox (stdlib only, no network)."""

import json
import multiprocessing
import time
from pathlib import Path
from typing import Any

import pytest

from phoning.mailbox import Mailbox, OwnerBusy


def _make_record(
    tid: str | None,
    msg_type: str = "chat",
    room: str | None = None,
    subject: str = "hello",
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "direction": "in",
        "tid": tid,
        "peer": "peer",
        "recv_ts": "2026-01-01T00:00:00Z",
        "room": room,
        "env": {"type": msg_type, "subject": subject},
    }
    return rec


def _append_worker(user_dir: str, prefix: str, count: int) -> None:
    from phoning.mailbox import Mailbox as _Box

    box = _Box(user_dir)
    for i in range(count):
        rec: dict[str, Any] = {
            "direction": "out",
            "tid": f"{prefix}-{i}",
            "peer": "p",
            "recv_ts": "2026-01-01T00:00:00Z",
            "room": None,
            "env": {"type": "chat", "subject": f"{prefix}-{i}"},
        }
        box.append(rec)


def test_append_assigns_monotonic_seq(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    s1 = box.append(_make_record("t1"))
    s2 = box.append(_make_record("t2"))
    s3 = box.append(_make_record("t3"))
    assert (s1, s2, s3) == (1, 2, 3)
    recs = box.peek("alice")
    assert [r["seq"] for r in recs] == [1, 2, 3]


def test_monotonic_across_instances_multiprocess(tmp_path: Path) -> None:
    user_dir = str(tmp_path)
    p1 = multiprocessing.Process(target=_append_worker, args=(user_dir, "a", 25))
    p2 = multiprocessing.Process(target=_append_worker, args=(user_dir, "b", 25))
    p1.start()
    p2.start()
    p1.join(30)
    p2.join(30)
    assert p1.exitcode == 0
    assert p2.exitcode == 0
    box = Mailbox(user_dir)
    recs = box.peek("fresh")
    assert len(recs) == 50
    seqs = sorted(r["seq"] for r in recs)
    assert seqs == list(range(1, 51))


def test_dedupe_on_direction_and_tid(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    first = box.append(_make_record("dup"))
    again = box.append(_make_record("dup"))
    assert first == 1
    assert again == 1
    assert len(box.peek("r")) == 1
    other_dir: dict[str, Any] = _make_record("dup")
    other_dir["direction"] = "out"
    third = box.append(other_dir)
    assert third == 2
    assert len(box.peek("r")) == 2


def test_dedupe_requires_tid(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    a = box.append(_make_record(None))
    b = box.append(_make_record(None))
    assert (a, b) == (1, 2)
    missing: dict[str, Any] = {
        "direction": "in",
        "peer": "p",
        "recv_ts": "2026-01-01T00:00:00Z",
        "room": None,
        "env": {"type": "chat"},
    }
    c = box.append(missing)
    d = box.append(dict(missing))
    assert (c, d) == (3, 4)


def test_independent_cursors(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    box.append(_make_record("t1"))
    box.append(_make_record("t2"))
    box.append(_make_record("t3"))
    got_a = box.read("anna")
    assert len(got_a) == 3
    assert box.cursor("anna") == 3
    assert box.cursor("bob") == 0
    got_b = box.peek("bob")
    assert len(got_b) == 3
    box.commit("bob", 1)
    assert box.cursor("bob") == 1
    assert box.cursor("anna") == 3
    rest_b = box.peek("bob")
    assert [r["seq"] for r in rest_b] == [2, 3]


def test_peek_does_not_commit(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    box.append(_make_record("t1"))
    assert box.cursor("r") == 0
    first = box.peek("r")
    assert len(first) == 1
    assert box.cursor("r") == 0
    second = box.peek("r")
    assert len(second) == 1
    assert second[0]["seq"] == first[0]["seq"]
    assert box.cursor("r") == 0


def test_read_commits(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    box.append(_make_record("t1"))
    box.append(_make_record("t2"))
    recs = box.read("r")
    assert [r["seq"] for r in recs] == [1, 2]
    assert box.cursor("r") == 2
    assert box.peek("r") == []
    box.append(_make_record("t3"))
    assert [r["seq"] for r in box.peek("r")] == [3]


def test_type_filter(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    box.append(_make_record("t1", msg_type="chat", subject="hi"))
    box.append(_make_record("t2", msg_type="status", subject="st"))
    box.append(_make_record("t3", msg_type="ask", subject="q"))
    chats = box.peek("r", types=["chat"])
    assert [r["env"]["type"] for r in chats] == ["chat"]
    multi = box.peek("r", types=["chat", "ask"])
    assert sorted(r["env"]["type"] for r in multi) == ["ask", "chat"]
    limited = box.read("r2", types=["status"])
    assert len(limited) == 1
    assert limited[0]["env"]["type"] == "status"


def test_room_filter(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    box.append(_make_record("t1", room="red"))
    box.append(_make_record("t2", room="blue"))
    box.append(_make_record("t3", room=None))
    reds = box.peek("r", room="red")
    assert len(reds) == 1
    assert reds[0]["room"] == "red"
    blues = box.peek("r", room="blue")
    assert len(blues) == 1
    assert blues[0]["room"] == "blue"
    everything = box.peek("r")
    assert len(everything) == 3


def test_limit(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    for i in range(5):
        box.append(_make_record(f"t{i}"))
    two = box.peek("r", limit=2)
    assert [r["seq"] for r in two] == [1, 2]
    assert box.cursor("r") == 0


def test_wait_returns_immediately_with_news(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    box.append(_make_record("t1"))
    start = time.monotonic()
    recs = box.wait("r", timeout=5.0, poll=0.05)
    elapsed = time.monotonic() - start
    assert len(recs) == 1
    assert recs[0]["seq"] == 1
    assert elapsed < 2.0
    assert box.cursor("r") == 0


def test_wait_timeout_returns_empty(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    start = time.monotonic()
    recs = box.wait("nobody", timeout=0.3, poll=0.05)
    elapsed = time.monotonic() - start
    assert recs == []
    assert elapsed >= 0.25
    assert elapsed < 5.0


def test_acquire_owner_second_raises_and_audit(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    lock = box.acquire_owner("first")
    try:
        box2 = Mailbox(tmp_path)
        with pytest.raises(OwnerBusy) as excinfo:
            box2.acquire_owner("second")
        assert excinfo.value.holder.get("session") == "first"
        assert "first" in str(excinfo.value)
        assert str(excinfo.value.holder.get("pid")) in str(excinfo.value)
    finally:
        lock.close()
    audit_file = tmp_path / "mailbox" / "audit.log"
    assert audit_file.exists()
    lines = audit_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1
    last = json.loads(lines[-1])
    assert last.get("session") == "second"
    holder = last.get("holder")
    assert isinstance(holder, dict)
    assert holder.get("session") == "first"


def test_lock_released_after_close(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    lock = box.acquire_owner("s1")
    lock.close()
    assert lock.closed
    lock2 = box.acquire_owner("s2")
    try:
        assert lock2.session == "s2"
    finally:
        lock2.close()
    with box.acquire_owner("s3"):
        pass
    lock4 = box.acquire_owner("s4")
    lock4.close()


def test_reader_name_validation(tmp_path: Path) -> None:
    box = Mailbox(tmp_path)
    box.append(_make_record("t1"))
    bad_names = ["", "a/b", "has space", "bang!", "x" * 65, "../x", "a,b"]
    for name in bad_names:
        with pytest.raises(ValueError, match="reader"):
            box.cursor(name)
        with pytest.raises(ValueError, match="reader"):
            box.peek(name)
        with pytest.raises(ValueError, match="reader"):
            box.commit(name, 0)
        with pytest.raises(ValueError, match="reader"):
            box.read(name)
        with pytest.raises(ValueError, match="reader"):
            box.wait(name, timeout=0.01)
    good_names = ["a", "A-1_.x", "x" * 64, "reader.1-2_3"]
    for name in good_names:
        assert box.cursor(name) == 0
        assert len(box.peek(name)) == 1
    anything: Any = 123
    with pytest.raises(ValueError, match="reader"):
        box.cursor(anything)
