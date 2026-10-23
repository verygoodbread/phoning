"""Acceptance: `phoning prune` retention (docs/spec/retention.md).

Every test builds a real mailbox with ``Mailbox.append`` under a temporary home and drives
``phoning.retention.main``. The concurrency test runs a separate appender process.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from phoning import board, envelope, retention
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import ROOT

NAME = "alice"


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Box:
    """A throwaway mailbox plus helpers."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.user_dir = home / NAME
        self.mb = Mailbox(self.user_dir)
        self.log = self.user_dir / "mailbox" / "log.jsonl"
        self.archive_dir = self.user_dir / "mailbox" / "archive"

    def add(self, days_ago: float, typ: str = "chat", subject: str = "s", *, peer: str = "bob",
            direction: str = "in", data: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        env = envelope.make(typ, peer, "s1", subject, "decrypted-body", data=data, **kw)
        env["ts"] = _iso(days_ago)
        rec = {"direction": direction, "tid": env["eid"], "peer": peer, "peer_fp": None, "room": None,
               "recv_ts": _iso(days_ago), "raw": envelope.encode(env), "env": env}
        rec["seq"] = self.mb.append(rec)
        return rec

    def raw_records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
        return out

    def seqs(self) -> list[int]:
        return [r["seq"] for r in self.raw_records() if "seq" in r]

    def archived(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.archive_dir.exists():
            return out
        for f in sorted(self.archive_dir.glob("log-*.jsonl.gz")):
            for line in gzip.decompress(f.read_bytes()).decode("utf-8").splitlines():
                out.append(json.loads(line))
        return out


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path / "home")


def prune(box: Box, *args: str) -> tuple[int, str, str]:
    """Run ``retention.main`` and return (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = retention.main([NAME, "--home", str(box.home), *args])
    return rc, out.getvalue(), err.getvalue()


def prune_json(box: Box, *args: str) -> dict[str, Any]:
    rc, out, err = prune(box, "--json", *args)
    assert rc == 0, err
    return json.loads(out)  # type: ignore[no-any-return]


def tree(user_dir: Path) -> dict[str, tuple[bytes | None, int]]:
    """Every path under the user dir -> (bytes or None for dirs, mode)."""
    snap: dict[str, tuple[bytes | None, int]] = {}
    for root, dirs, files in os.walk(user_dir):
        for d in dirs:
            p = Path(root) / d
            snap[str(p.relative_to(user_dir))] = (None, stat.S_IMODE(p.stat().st_mode))
        for f in files:
            p = Path(root) / f
            snap[str(p.relative_to(user_dir))] = (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
    return snap


# ---------------------------------------------------------------------------
# basic semantics
# ---------------------------------------------------------------------------


def test_prune_by_age_and_tail_preserves_seq_and_bytes(box: Box) -> None:
    for _ in range(20):
        box.add(100)  # seq 1..20, old
    for _ in range(5):
        box.add(1)  # seq 21..25, recent
    before = box.log.read_bytes().splitlines(keepends=True)
    rep = prune_json(box, "--keep-days", "30", "--keep-last", "8")
    # recent = 21..25, tail = 18..25 -> kept 18..25
    assert box.seqs() == list(range(18, 26))
    assert rep["total"] == 25 and rep["kept"] == 8 and rep["pruned"] == 17
    assert sum(rep["kept_by"].values()) == rep["kept"]
    after = box.log.read_bytes().splitlines(keepends=True)
    assert after == before[17:], "kept lines must be byte-for-byte, in order, never renumbered"
    assert stat.S_IMODE(box.log.stat().st_mode) == 0o600
    # append continues after the highest seq, never reusing a pruned one
    assert box.mb.append({"direction": "in", "tid": "zz", "peer": "bob", "env": {}}) == 26


def test_highest_seq_survives_keep_last_zero(box: Box) -> None:
    for _ in range(5):
        box.add(100)
    prune_json(box, "--keep-days", "0", "--keep-last", "0")
    assert box.seqs() == [5]
    assert box.mb.append({"direction": "in", "tid": "n", "peer": "bob", "env": {}}) == 6


def test_dry_run_writes_nothing(box: Box) -> None:
    for _ in range(10):
        box.add(100)
    box.mb.commit("doorbell", 10)
    snap = tree(box.user_dir)
    rep = prune_json(box, "--dry-run", "--keep-days", "30", "--keep-last", "3")
    assert rep["dry_run"] is True and rep["pruned"] == 7 and rep["kept"] == 3
    assert rep["archive"] is None
    assert tree(box.user_dir) == snap, "dry-run must not create, modify or chmod anything"
    assert not box.archive_dir.exists()
    rc, out, _ = prune(box, "--dry-run", "--keep-last", "3")
    assert rc == 0 and "dry-run" in out


def test_unread_records_are_never_dropped(box: Box) -> None:
    for _ in range(30):
        box.add(100)  # seq 1..30, all old
    box.mb.commit("alpha", 25)
    box.mb.commit("slow", 12)  # lowest committed cursor
    rep = prune_json(box, "--keep-days", "30", "--keep-last", "2")
    # kept: seq > 12 (unread by 'slow') -> 13..30
    assert box.seqs() == list(range(13, 31))
    assert rep["min_cursor"] == 12 and rep["min_cursor_reader"] == "slow"
    assert rep["kept_by"]["unread"] >= 1


def test_corrupt_cursor_protects_everything(box: Box) -> None:
    for _ in range(10):
        box.add(100)
    (box.user_dir / "mailbox" / "cursors" / "broken.json").write_text("{nope", encoding="utf-8")
    box.mb.commit("ok", 10)
    prune_json(box, "--keep-days", "0", "--keep-last", "0")
    assert box.seqs() == list(range(1, 11))


def test_no_cursor_files_means_no_unread_protection(box: Box) -> None:
    for _ in range(10):
        box.add(100)
    rep = prune_json(box, "--keep-days", "0", "--keep-last", "4")
    assert box.seqs() == [7, 8, 9, 10]
    assert rep["min_cursor"] is None and rep["min_cursor_reader"] is None


def test_thread_chain_kept_and_board_fold_unchanged(box: Box) -> None:
    root_ask = box.add(100, "ask", "old question")  # thread root, unanswered by seq order
    box.add(100, "chat", "noise 1")
    box.add(100, "chat", "noise 2")
    answer = box.add(100, "chat", "old answer", reply_to=root_ask["env"]["eid"], thread=root_ask["env"]["eid"])
    prop = box.add(100, "propose", "old proposal", data={"scope": "x/y"})
    box.add(100, "chat", "noise 3")
    accept = box.add(100, "accept", "ok", reply_to=prop["env"]["eid"], thread=prop["env"]["eid"])
    box.add(100, "chat", "noise 4")
    # a recent record deep in both threads pulls the whole chain in
    box.add(1, "chat", "follow-up", thread=root_ask["env"]["eid"], reply_to=answer["env"]["eid"])
    box.add(1, "chat", "follow-up 2", thread=prop["env"]["eid"], reply_to=accept["env"]["eid"])
    full = box.raw_records()
    fold_before = board.fold(full, {}, local="me")
    rep = prune_json(box, "--keep-days", "30", "--keep-last", "0", "--no-archive")
    kept_subjects = {r["env"]["subject"] for r in box.raw_records()}
    assert {"old question", "old answer", "old proposal", "ok", "follow-up", "follow-up 2"} <= kept_subjects
    assert not kept_subjects & {"noise 1", "noise 2", "noise 3", "noise 4"}
    assert rep["kept_by"]["thread"] >= 4
    fold_after = board.fold(box.raw_records(), {}, local="me")
    assert [p["state"] for p in fold_after["proposals"]] == [p["state"] for p in fold_before["proposals"]]
    assert [p["state"] for p in fold_after["proposals"]] == ["ACCEPTED"]
    assert fold_after["open_asks"] == fold_before["open_asks"] == []


def test_replies_to_a_kept_ask_survive_so_it_does_not_fold_as_open(box: Box) -> None:
    box.add(100, "chat", "old noise")
    ask = box.add(100, "ask", "old question")
    reply = box.add(100, "chat", "old reply", reply_to=ask["env"]["eid"], thread=ask["env"]["eid"])
    assert reply["seq"] == 3
    # a recent message in the ask's thread keeps the ask (thread root) but does not reply to the reply
    box.add(1, "chat", "recent in thread", thread=ask["env"]["eid"])
    assert board.fold(box.raw_records(), {}, local="me")["open_asks"] == []
    prune_json(box, "--keep-days", "30", "--keep-last", "0", "--no-archive")
    assert {r["env"]["subject"] for r in box.raw_records()} == {"old question", "old reply", "recent in thread"}
    assert board.fold(box.raw_records(), {}, local="me")["open_asks"] == []


def test_reader_api_works_across_seq_gaps(box: Box) -> None:
    for i in range(1, 21):
        box.add(100 if i <= 14 else 1, direction="in" if i % 2 else "out")
    box.mb.commit("r", 6)
    prune_json(box, "--keep-days", "30", "--keep-last", "3")
    # reader cursor 6 -> records 7..20 kept; 1..6 gone; seqs are untouched
    assert box.seqs() == list(range(7, 21))
    mb = Mailbox(box.user_dir)
    peeked = mb.peek("r")
    assert [r["seq"] for r in peeked] == list(range(7, 21))
    assert mb.cursor("r") == 6, "peek must not commit"
    fresh = mb.peek("fresh-reader")
    assert [r["seq"] for r in fresh] == list(range(7, 21)), "a new reader sees the retained history"
    got = mb.wait("r", timeout=1.0)
    assert [r["seq"] for r in got] == [s for s in range(7, 21) if s % 2 == 1]
    read = mb.read("r", limit=4)
    assert [r["seq"] for r in read] == [7, 8, 9, 10] and mb.cursor("r") == 10
    # prune harder: everything the reader has already read goes
    mb.commit("r", 10)
    prune_json(box, "--keep-days", "0", "--keep-last", "2")
    assert box.seqs() == list(range(11, 21))
    assert [r["seq"] for r in mb.read("r")] == list(range(11, 21))
    assert mb.cursor("r") == 20


def test_gap_in_the_middle_does_not_confuse_readers(box: Box) -> None:
    """Old head kept via a thread link, middle pruned, recent tail: seqs 1-2, 9-11 survive."""
    head = [box.add(100) for _ in range(2)]
    for _ in range(6):
        box.add(100)  # seq 3..8, pruned
    box.add(1)  # 9
    box.add(1)  # 10
    box.add(1, "chat", "x", thread=head[0]["env"]["eid"], reply_to=head[1]["env"]["eid"])  # 11
    box.mb.commit("a", 8)
    prune_json(box, "--keep-days", "30", "--keep-last", "0")
    assert box.seqs() == [1, 2, 9, 10, 11]
    mb = Mailbox(box.user_dir)
    assert [r["seq"] for r in mb.peek("a")] == [9, 10, 11]
    assert [r["seq"] for r in mb.read("a", limit=2)] == [9, 10]
    assert mb.cursor("a") == 10
    assert [r["seq"] for r in mb.wait("a", timeout=1.0, include_own=True)] == [11]
    assert [r["seq"] for r in mb.peek("b")] == [1, 2, 9, 10, 11], "a reader at 0 sees the retained records"
    assert mb.append({"direction": "in", "tid": "n", "peer": "bob", "env": {}}) == 12


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------


def test_archive_holds_exactly_the_pruned_records(box: Box) -> None:
    for _ in range(12):
        box.add(100)
    original = box.raw_records()
    rep = prune_json(box, "--keep-days", "30", "--keep-last", "4")
    arch = rep["archive"]
    assert arch is not None
    p = Path(arch)
    assert p.parent == box.archive_dir
    assert p.name.startswith("log-") and p.name.endswith(".jsonl.gz")
    stamp = p.name[len("log-"):-len(".jsonl.gz")]
    datetime.strptime(stamp, "%Y%m%dT%H%M%S")  # the documented pattern
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(box.archive_dir.stat().st_mode) == 0o700
    assert box.archived() == original[:8]
    assert box.raw_records() == original[8:]
    # no staging leftovers
    names = {x.name for x in (box.user_dir / "mailbox").iterdir()}
    assert not {n for n in names if "prune" in n}, names


def test_archive_dir_tightened_if_it_already_exists(box: Box) -> None:
    box.archive_dir.mkdir(mode=0o755)
    os.chmod(box.archive_dir, 0o755)
    for _ in range(4):
        box.add(100)
    prune_json(box, "--keep-days", "0", "--keep-last", "1")
    assert stat.S_IMODE(box.archive_dir.stat().st_mode) == 0o700


def test_no_archive_creates_nothing(box: Box) -> None:
    for _ in range(6):
        box.add(100)
    rep = prune_json(box, "--no-archive", "--keep-days", "0", "--keep-last", "1")
    assert rep["pruned"] == 5 and rep["archive"] is None
    assert not box.archive_dir.exists()


def test_archive_name_collision_never_overwrites(box: Box) -> None:
    for _ in range(6):
        box.add(100)
    box.archive_dir.mkdir(mode=0o700)
    now = datetime.now(UTC)
    planted: dict[Path, bytes] = {}
    for delta in range(-2, 5):  # every plausible name for "this second"
        f = box.archive_dir / f"log-{(now + timedelta(seconds=delta)).strftime('%Y%m%dT%H%M%S')}.jsonl.gz"
        f.write_bytes(gzip.compress(b'{"planted": true}\n'))
        planted[f] = f.read_bytes()
    rep = prune_json(box, "--keep-days", "0", "--keep-last", "1")
    assert {f: f.read_bytes() for f in planted} == planted, "an existing archive was overwritten"
    new = Path(rep["archive"])
    assert new not in planted and new.exists()
    assert new.name.startswith("log-") and new.name.endswith(".jsonl.gz")
    assert [r["seq"] for r in [json.loads(x) for x in gzip.decompress(new.read_bytes()).decode().splitlines()]] \
        == [1, 2, 3, 4, 5]


def test_nothing_to_prune_is_a_noop(box: Box) -> None:
    for _ in range(5):
        box.add(1)
    snap = tree(box.user_dir)
    rep = prune_json(box)
    assert rep["pruned"] == 0 and rep["kept"] == 5 and rep["archive"] is None
    assert tree(box.user_dir) == snap
    rc, out, _ = prune(box)
    assert rc == 0 and "nothing to prune" in out


def test_second_prune_is_idempotent(box: Box) -> None:
    for _ in range(10):
        box.add(100)
    prune_json(box, "--keep-days", "30", "--keep-last", "3")
    snap = tree(box.user_dir)
    rep = prune_json(box, "--keep-days", "30", "--keep-last", "3")
    assert rep["pruned"] == 0
    assert tree(box.user_dir) == snap


# ---------------------------------------------------------------------------
# output and arguments
# ---------------------------------------------------------------------------


def test_json_report_shape(box: Box) -> None:
    for _ in range(6):
        box.add(100)
    box.mb.commit("d", 2)
    rep = prune_json(box, "--keep-days", "30", "--keep-last", "1")
    for key in ("name", "dry_run", "total", "kept", "pruned", "kept_by", "min_cursor", "min_cursor_reader",
                "bytes_before", "bytes_after", "archive", "recovered", "recovery_pending", "warnings"):
        assert key in rep, key
    assert rep["name"] == NAME and rep["dry_run"] is False
    assert rep["total"] == 6 and rep["kept"] == 4 and rep["pruned"] == 2  # seq 3..6 kept (unread)
    assert set(rep["kept_by"]) == {"recent", "tail", "unread", "thread", "decisions", "opaque"}
    assert rep["bytes_after"] == box.log.stat().st_size < rep["bytes_before"]


def test_human_output_mentions_counts_and_archive(box: Box) -> None:
    for _ in range(6):
        box.add(100)
    rc, out, err = prune(box, "--keep-days", "0", "--keep-last", "1")
    assert rc == 0 and err == ""
    assert "5" in out and "6" in out and "archive" in out


def test_defaults_are_30_days_and_500_records(box: Box) -> None:
    for _ in range(3):
        box.add(31)  # older than 30d, but only 3 records (< 500 tail)
    rep = prune_json(box)
    assert rep["pruned"] == 0, "default keep-last 500 covers a small log"


@pytest.mark.parametrize("args", [
    ["--keep-days", "-1"], ["--keep-last", "-5"], ["--keep-days", "abc"], ["--keep-last", "1.5"],
    ["--bogus"],
])
def test_bad_arguments_exit_2(box: Box, args: list[str]) -> None:
    box.add(1)
    snap = tree(box.user_dir)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), pytest.raises(SystemExit) as ei:
        retention.main([NAME, "--home", str(box.home), *args])
    assert ei.value.code == 2
    assert tree(box.user_dir) == snap


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", ".", "", "x" * 65, "sp ace"])
def test_unsafe_names_rejected(tmp_path: Path, bad: str) -> None:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = retention.main([bad, "--home", str(tmp_path)])
        except SystemExit as exc:
            rc = int(exc.code) if isinstance(exc.code, int) else 2
    assert rc == 2
    assert list(tmp_path.iterdir()) == [], "nothing may be created"


def test_missing_mailbox_exit_2_and_creates_nothing(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = retention.main(["ghost", "--home", str(home)])
    assert rc == 2 and "no mailbox" in err.getvalue()
    assert list(home.iterdir()) == []


def test_home_env_var_is_used(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    for _ in range(4):
        box.add(100)
    monkeypatch.setenv("PHONING_HOME", str(box.home))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = retention.main([NAME, "--keep-days", "0", "--keep-last", "1", "--json"])
    assert rc == 0 and json.loads(out.getvalue())["pruned"] == 3


# ---------------------------------------------------------------------------
# fail closed
# ---------------------------------------------------------------------------


def test_unparseable_timestamps_and_junk_lines_are_kept(box: Box) -> None:
    for _ in range(3):
        box.add(100)
    # record with garbage timestamps, a junk line, a record with no seq, and a non-object line
    with open(box.log, "ab") as fh:
        fh.write(json.dumps({"seq": 4, "direction": "in", "tid": "t4", "recv_ts": "not-a-time",
                             "env": {"ts": "also-bad"}}).encode() + b"\n")
        fh.write(b"this is not json\n")
        fh.write(json.dumps({"direction": "in", "tid": "noseq"}).encode() + b"\n")
        fh.write(b"[1, 2, 3]\n")
    box.add(100)  # seq 5 (after the junk), the highest seq
    rep = prune_json(box, "--keep-days", "30", "--keep-last", "0", "--no-archive")
    text = box.log.read_text(encoding="utf-8")
    assert '"t4"' in text and "this is not json" in text and '"noseq"' in text and "[1, 2, 3]" in text
    assert box.seqs() == [4, 5]
    assert rep["kept_by"]["opaque"] == 3
    assert rep["pruned"] == 3


def test_trailing_partial_line_is_preserved(box: Box) -> None:
    for _ in range(4):
        box.add(100)
    with open(box.log, "ab") as fh:
        fh.write(b'{"seq": 99, "partial')  # torn write, no newline
    prune_json(box, "--keep-days", "0", "--keep-last", "1", "--no-archive")
    assert box.log.read_bytes().endswith(b'{"seq": 99, "partial')


# ---------------------------------------------------------------------------
# crash recovery of the staged rewrite
# ---------------------------------------------------------------------------


def test_recovers_a_staged_rewrite_after_a_crash(box: Box) -> None:
    for _ in range(10):
        box.add(100)
    lines = box.log.read_bytes().splitlines(keepends=True)
    ready = box.user_dir / "mailbox" / "log.jsonl.prune.ready"
    ready.write_bytes(b"".join(lines[8:]))  # the plan: keep seq 9, 10
    os.chmod(ready, 0o600)
    # the crash left only a partial write in log.jsonl, then a later append landed after it
    box.log.write_bytes(lines[8] + json.dumps({"seq": 11, "direction": "in", "tid": "late", "env": {},
                                                "recv_ts": _iso(0)}).encode() + b"\n")
    rep = prune_json(box, "--keep-days", "0", "--keep-last", "100", "--no-archive")
    assert rep["recovered"] is True
    assert box.seqs() == [9, 10, 11]
    assert not ready.exists()
    assert stat.S_IMODE(box.log.stat().st_mode) == 0o600


def test_dry_run_reports_pending_recovery_without_acting(box: Box) -> None:
    for _ in range(4):
        box.add(100)
    ready = box.user_dir / "mailbox" / "log.jsonl.prune.ready"
    ready.write_bytes(b"")
    snap = tree(box.user_dir)
    rep = prune_json(box, "--dry-run")
    assert rep["recovery_pending"] is True
    assert tree(box.user_dir) == snap


# ---------------------------------------------------------------------------
# concurrency: a live appender while prune runs
# ---------------------------------------------------------------------------

APPENDER = """
import sys, json
from datetime import UTC, datetime, timedelta
sys.path.insert(0, {root!r})
from phoning.mailbox import Mailbox
mb = Mailbox({user_dir!r})
old = (datetime.now(UTC) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
for i in range(int(sys.argv[1])):
    fresh = i % 2
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if fresh else old
    mb.append({{"direction": "in", "tid": "a%d" % i, "peer": "bob", "recv_ts": ts, "env": {{"fresh": fresh}}}})
"""


@pytest.mark.parametrize(("keep_days", "keep_last"), [("30", "0"), ("0", "0"), ("0", "3")])
def test_prune_never_loses_a_concurrent_append(box: Box, keep_days: str, keep_last: str) -> None:
    n_seed, n_app = 200, 2000
    for _ in range(n_seed):
        box.add(100)
    code = APPENDER.format(root=str(ROOT), user_dir=str(box.user_dir))
    app = subprocess.Popen([sys.executable, "-c", code, str(n_app)], stdin=subprocess.DEVNULL)
    passes = 0
    try:
        while app.poll() is None:
            rc, _, err = prune(box, "--keep-days", keep_days, "--keep-last", keep_last)
            assert rc == 0, err
            passes += 1
            time.sleep(0.003)
    finally:
        if app.poll() is None:
            app.kill()
    assert app.wait(timeout=60) == 0
    assert passes >= 3, "the appender finished before prune really overlapped; raise n_app"
    rc, _, err = prune(box, "--keep-days", keep_days, "--keep-last", keep_last)
    assert rc == 0, err

    live = box.raw_records()
    arch = box.archived()
    everything = live + arch
    tids = [r["tid"] for r in everything if re.fullmatch(r"a\d+", str(r.get("tid", "")))]
    expected = {f"a{i}" for i in range(n_app)}
    assert len(tids) == len(set(tids)), "a record appears twice across log + archives"
    assert set(tids) == expected, f"lost {len(expected - set(tids))} appended records"
    # seq space is intact: unique and contiguous 1..N across log + archives, nothing reused
    seqs = sorted(r["seq"] for r in everything)
    assert seqs == list(range(1, n_seed + n_app + 1))
    # records still inside the retention window are in the live log itself
    if keep_days == "30":
        fresh_live = {r["tid"] for r in live if r.get("env", {}).get("fresh") == 1}
        assert fresh_live == {f"a{i}" for i in range(n_app) if i % 2}
    live_seqs = [r["seq"] for r in live]
    assert live_seqs == sorted(live_seqs) and len(live_seqs) == len(set(live_seqs))
    assert stat.S_IMODE(box.log.stat().st_mode) == 0o600


def test_readers_never_see_a_torn_log_while_prune_runs(box: Box) -> None:
    """A reader polling while prunes run only ever sees whole, parseable records."""
    for _ in range(300):
        box.add(100)
    mb = Mailbox(box.user_dir)
    code = APPENDER.format(root=str(ROOT), user_dir=str(box.user_dir))
    app = subprocess.Popen([sys.executable, "-c", code, "600"], stdin=subprocess.DEVNULL)
    bad = 0
    try:
        while app.poll() is None:
            prune(box, "--keep-days", "0", "--keep-last", "5", "--no-archive")
            recs = mb.peek("scanner", limit=100000)
            seqs = [r["seq"] for r in recs]
            if not seqs or seqs != sorted(seqs) or len(seqs) != len(set(seqs)):
                bad += 1
    finally:
        if app.poll() is None:
            app.kill()
    assert bad == 0


# ---------------------------------------------------------------------------
# scale: prune holds the append lock, so it must be near-linear
# ---------------------------------------------------------------------------


def test_prune_is_fast_on_a_large_log(box: Box) -> None:
    """20k records, 4k asks, long reply chains: must finish in seconds (lock is held meanwhile)."""
    old, new = _iso(100), _iso(0)
    n = 20000
    eids: list[str] = []
    lines: list[str] = []
    for i in range(1, n + 1):
        eid = f"e{i:06d}"
        eids.append(eid)
        is_ask = i % 5 == 0
        env = {"eid": eid, "type": "ask" if is_ask else "chat", "ts": old,
               "thread": eid if is_ask or i < 10 else eids[i - 3],
               "reply_to": None if is_ask else eids[i - 2]}
        lines.append(json.dumps({"seq": i, "direction": "in", "tid": eid, "peer": "bob",
                                 "recv_ts": new if i > n - 100 else old, "env": env}))
    box.log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    t0 = time.monotonic()
    rep = prune_json(box, "--dry-run", "--keep-days", "30", "--keep-last", "0")
    dry = time.monotonic() - t0
    assert dry < 10.0, f"dry-run took {dry:.1f}s on 20000 records"
    t0 = time.monotonic()
    real = prune_json(box, "--keep-days", "30", "--keep-last", "0", "--no-archive")
    took = time.monotonic() - t0
    assert took < 10.0, f"prune took {took:.1f}s on 20000 records"
    assert real["kept"] == rep["kept"] and real["total"] == n
    assert real["kept"] >= 100
