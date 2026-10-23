"""Unit tests for the pure helpers in :mod:`phoning.retention`."""

from __future__ import annotations

import gzip
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from phoning import retention


def _line(seq: int, **env: object) -> bytes:
    payload = {"seq": seq, "direction": "in", "tid": f"t{seq}", "env": dict(env)}
    return (json.dumps(payload) + "\n").encode("utf-8")


def _rec(seq: int, ts: str = "2000-01-01T00:00:00Z", **env: object) -> tuple[Any, Any, bytes]:
    obj: dict[str, Any] = {"seq": seq, "recv_ts": ts, "env": dict(env)}
    return (seq, obj, (json.dumps(obj) + "\n").encode())


def _now() -> float:
    return datetime.now(UTC).timestamp()


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute(
    recs: list[tuple[Any, Any, bytes]],
    cursors: dict[str, int] | None = None,
    keep_days: float = 30.0,
    keep_last: int = 0,
) -> tuple[set[int], dict[str, int]]:
    idx, kept_by = retention._compute_keep_set(recs, cursors or {}, keep_days, keep_last, _now())
    return (set(idx), kept_by)


def test_parse_z_suffix() -> None:
    assert retention._parse_ts("2020-01-02T03:04:05Z") == datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_parse_naive_is_utc() -> None:
    dt = retention._parse_ts("2020-01-02T03:04:05")
    assert dt is not None and dt.tzinfo == UTC and (dt.hour, dt.minute) == (3, 4)


def test_parse_offset_to_utc() -> None:
    assert retention._parse_ts("2020-01-02T03:04:05+02:00") == datetime(2020, 1, 2, 1, 4, 5, tzinfo=UTC)


def test_parse_bad() -> None:
    for bad in ("", "not-a-time", "also-bad", None, 12, [], {}):
        assert retention._parse_ts(bad) is None


def test_parse_space_form() -> None:
    dt = retention._parse_ts("2020-01-02 03:04:05")
    assert dt is not None and dt.year == 2020


def test_parse_line_opaque() -> None:
    assert retention._parse_line(b"nope\n") is None
    assert retention._parse_line(b"[1,2]\n") is None
    assert retention._parse_line(b'{"no":"seq"}\n') is None
    assert retention._parse_line(b'{"seq":true}\n') is None
    assert retention._parse_line(b'{"seq":1.5}\n') is None


def test_parse_line_ok() -> None:
    obj = retention._parse_line(_line(4))
    assert obj is not None and obj["seq"] == 4


def test_cursors_corrupt_is_zero_and_tmp_ignored(tmp_path: Path) -> None:
    d = tmp_path / "c"
    d.mkdir()
    (d / "a.json").write_text('{"seq": 5}', encoding="utf-8")
    (d / "broken.json").write_text("{nope", encoding="utf-8")
    (d / "x.json.tmp").write_text('{"seq": 1}', encoding="utf-8")
    cursors, warnings = retention._read_cursors(str(d))
    assert cursors == {"a": 5, "broken": 0}
    assert warnings


def test_cursors_bad_shapes_are_zero(tmp_path: Path) -> None:
    d = tmp_path / "c"
    d.mkdir()
    (d / "n.json").write_text('{"seq": -1}', encoding="utf-8")
    (d / "b.json").write_text('{"seq": true}', encoding="utf-8")
    (d / "l.json").write_text('[1]', encoding="utf-8")
    cursors, _w = retention._read_cursors(str(d))
    assert cursors == {"n": 0, "b": 0, "l": 0}
    assert retention._min_cursor(cursors) == (0, "b")


def test_min_cursor_lowest_and_empty() -> None:
    assert retention._min_cursor({"a": 3, "b": 7}) == (3, "a")
    assert retention._min_cursor({}) == (None, None)


def test_missing_cursors_dir(tmp_path: Path) -> None:
    cursors, _w = retention._read_cursors(str(tmp_path / "missing"))
    assert cursors == {}


def test_tail_zero_keeps_highest() -> None:
    recs = [_rec(i) for i in range(1, 6)]
    kept, by = _compute(recs, keep_days=0.0, keep_last=0)
    assert kept == {4}
    assert by["tail"] == 1


def test_tail_last_n() -> None:
    recs = [_rec(i, ts="2000-01-01T00:00:00Z") for i in range(1, 11)]
    kept, by = _compute(recs, keep_days=0.0, keep_last=3)
    assert kept == {7, 8, 9}
    assert by["tail"] == 3


def test_recent_bad_ts_kept() -> None:
    bad: tuple[Any, Any, bytes] = (7, {"seq": 7, "recv_ts": "garbage", "env": {}}, _line(7))
    decoy = _rec(8, ts="2000-01-01T00:00:00Z")
    kept, by = _compute([bad, decoy], keep_days=0.0, keep_last=0)
    assert 0 in kept and by["recent"] == 1 and by["tail"] == 1


def test_recent_missing_ts_kept() -> None:
    obj: dict[str, Any] = {"seq": 1, "env": {}}
    rec: tuple[Any, Any, bytes] = (1, obj, (json.dumps(obj) + "\n").encode())
    decoy = _rec(2, ts="2000-01-01T00:00:00Z")
    kept, _by = _compute([rec, decoy], keep_days=0.0, keep_last=0)
    assert 0 in kept


def test_recent_falls_back_to_env_ts() -> None:
    obj: dict[str, Any] = {"seq": 1, "env": {"ts": _now_iso()}}
    rec: tuple[Any, Any, bytes] = (1, obj, (json.dumps(obj) + "\n").encode())
    decoy = _rec(2, ts="2000-01-01T00:00:00Z")
    kept, by = _compute([rec, decoy], keep_days=30.0, keep_last=0)
    assert 0 in kept and by["recent"] == 1


def test_unread_lowest_cursor() -> None:
    recs = [_rec(i, ts="2000-01-01T00:00:00Z") for i in range(1, 6)]
    kept, by = _compute(recs, {"a": 4, "b": 2}, keep_days=0.0, keep_last=0)
    assert kept == {2, 3, 4}
    assert by["unread"] == 2 and by["tail"] == 1


def test_opaque_kept() -> None:
    recs: list[tuple[Any, Any, bytes]] = [
        (None, None, b"junk\n"),
        _rec(1, ts="2000-01-01T00:00:00Z"),
        (None, None, b"[1]\n"),
    ]
    kept, by = _compute(recs, keep_days=0.0, keep_last=0)
    assert 0 in kept and 2 in kept and by["opaque"] == 2


def test_attribution_tail_beats_recent() -> None:
    obj: dict[str, Any] = {"seq": 1, "recv_ts": _now_iso(), "env": {}}
    rec: tuple[Any, Any, bytes] = (1, obj, (json.dumps(obj) + "\n").encode())
    kept, by = _compute([rec], keep_days=30.0, keep_last=5)
    assert kept == {0} and by["tail"] == 1 and by["recent"] == 0


def test_thread_chain_to_root() -> None:
    root = (1, {"seq": 1, "recv_ts": "2000-01-01T00:00:00Z", "env": {"eid": "r"}}, _line(1, eid="r"))
    mid = (
        2,
        {"seq": 2, "recv_ts": "2000-01-01T00:00:00Z", "env": {"eid": "m", "thread": "r"}},
        _line(2, eid="m", thread="r"),
    )
    leaf = (
        3,
        {"seq": 3, "recv_ts": _now_iso(), "env": {"eid": "l", "thread": "r", "reply_to": "m"}},
        _line(3, eid="l", thread="r", reply_to="m"),
    )
    kept, by = _compute([root, mid, leaf], keep_days=30.0, keep_last=0)
    assert kept == {0, 1, 2}
    assert by["thread"] == 2


def test_thread_missing_target_ignored() -> None:
    rec = (
        1,
        {"seq": 1, "recv_ts": _now_iso(), "env": {"eid": "x", "thread": "nope"}},
        _line(1, eid="x", thread="nope"),
    )
    kept, _by = _compute([rec], keep_days=30.0, keep_last=0)
    assert kept == {0}


def test_decisions_reply_to_kept_ask() -> None:
    ask = (
        1,
        {"seq": 1, "recv_ts": "2000-01-01T00:00:00Z", "env": {"eid": "q", "type": "ask", "thread": "r"}},
        _line(1, eid="q", type="ask", thread="r"),
    )
    root = (2, {"seq": 2, "recv_ts": "2000-01-01T00:00:00Z", "env": {"eid": "r"}}, _line(2, eid="r"))
    leaf = (
        3,
        {"seq": 3, "recv_ts": _now_iso(), "env": {"eid": "x", "thread": "r", "reply_to": "q"}},
        _line(3, eid="x", thread="r", reply_to="q"),
    )
    kept, by = _compute([ask, root, leaf], keep_days=30.0, keep_last=0)
    assert kept == {0, 1, 2}
    assert by["thread"] == 2


def test_decisions_propose_reply() -> None:
    prop = (
        1,
        {"seq": 1, "recv_ts": "2000-01-01T00:00:00Z", "env": {"eid": "p", "type": "propose"}},
        _line(1, eid="p", type="propose"),
    )
    reply = (
        2,
        {"seq": 2, "recv_ts": _now_iso(), "env": {"eid": "a", "reply_to": "p"}},
        _line(2, eid="a", reply_to="p"),
    )
    kept, by = _compute([prop, reply], keep_days=30.0, keep_last=0)
    assert kept == {0, 1} and by["thread"] == 1


def test_decisions_chat_does_not_pull_replies() -> None:
    old = (
        1,
        {"seq": 1, "recv_ts": "2000-01-01T00:00:00Z", "env": {"eid": "c", "type": "chat"}},
        _line(1, eid="c", type="chat"),
    )
    other = (
        2,
        {"seq": 2, "recv_ts": "2000-01-01T00:00:00Z", "env": {"eid": "r", "reply_to": "c"}},
        _line(2, eid="r", reply_to="c"),
    )
    fresh = _rec(3, ts=_now_iso())
    kept, _by = _compute([old, other, fresh], keep_days=30.0, keep_last=0)
    assert 0 not in kept and 1 not in kept


def test_merge_appends_later() -> None:
    merged = retention._merge_recovery(_line(9) + _line(10), _line(10) + _line(11))
    assert [json.loads(x)["seq"] for x in merged.decode().splitlines()] == [9, 10, 11]


def test_merge_dedupes() -> None:
    merged = retention._merge_recovery(_line(1) + _line(2), _line(2) + _line(2))
    assert [json.loads(x)["seq"] for x in merged.decode().splitlines()] == [1, 2]


def test_merge_ignores_opaque_and_old() -> None:
    merged = retention._merge_recovery(_line(4), b"junk\n" + _line(3) + _line(5))
    assert [json.loads(x)["seq"] for x in merged.decode().splitlines()] == [4, 5]


def test_archive_collision_suffix(tmp_path: Path) -> None:
    d = tmp_path / "a"
    d.mkdir()
    (d / "log-20200101T000000.jsonl.gz").write_bytes(b"x")
    nxt = Path(retention._archive_path(str(d), "20200101T000000"))
    assert nxt.name == "log-20200101T000000-1.jsonl.gz"


def test_archive_first_free(tmp_path: Path) -> None:
    d = tmp_path / "a"
    d.mkdir()
    assert Path(retention._archive_path(str(d), "20200101T000000")).name == "log-20200101T000000.jsonl.gz"


def test_write_archive_modes_and_no_overwrite(tmp_path: Path) -> None:
    d = tmp_path / "a"
    first = retention._write_archive(str(d), "20200101T000000", b"a\n")
    second = retention._write_archive(str(d), "20200101T000000", b"b\n")
    assert first != second
    assert gzip.decompress(Path(first).read_bytes()) == b"a\n"
    assert gzip.decompress(Path(second).read_bytes()) == b"b\n"
    assert os.stat(d).st_mode & 0o777 == 0o700
    assert os.stat(first).st_mode & 0o777 == 0o600


def test_valid_names() -> None:
    assert retention._valid_name("alice")
    assert retention._valid_name("a.b-c_1")
    assert not retention._valid_name("..")
    assert not retention._valid_name(".")
    assert not retention._valid_name("a/b")
    assert not retention._valid_name("")
    assert not retention._valid_name("x" * 65)
    assert not retention._valid_name("sp ace")
