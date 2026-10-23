"""Acceptance: `--ring notify` for a human principal's mailbox (docs/spec/rules.md §5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from phoning import doorbell
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import inbound
from tests.acceptance.test_rules_common import (
    FP,
    PEER_TEXT,
    approvals,
    bell,
    calls,
    fake_osascript,
    home_fixture,  # noqa: F401  (registers the `home` fixture)
    mail,
    state,
)


def notify(*extra: str, exe: Path, interval: str = "0") -> int:
    return bell("--ring", "notify", "--notify-cmd", str(exe), "--min-interval", interval, *extra)


def text_of(call: dict[str, list[str]]) -> str:
    return call["argv"][-1]


def test_notification_text_is_fixed_and_the_argv_is_safe(home: Path, tmp_path: Path) -> None:
    exe, log = fake_osascript(tmp_path)
    mail(home, subject=PEER_TEXT)
    mail(home, "propose", "p2", data={"scope": "x"})
    mail(home, "chat", "not actionable")
    assert notify(exe=exe) == 0
    got = calls(log)
    assert len(got) == 1
    argv = got[0]["argv"]
    assert text_of(got[0]) == "phoning: 2 actionable message(s) for alice"
    assert argv[0] == "-e" and argv[-2] == "--"
    assert "on run argv" in argv and "end run" in argv
    for part in argv[:-1]:  # nothing is interpolated into the script source
        assert "alice" not in part and "actionable" not in part
    joined = " ".join(argv)
    assert PEER_TEXT not in joined and "bob" not in joined and "propose" not in joined
    assert state(home)["pending"] is False


def test_counts_accumulate_across_coalesced_batches_and_reset(home: Path, tmp_path: Path) -> None:
    exe, log = fake_osascript(tmp_path)
    mail(home)
    assert notify(exe=exe) == 0
    assert text_of(calls(log)[-1]) == "phoning: 1 actionable message(s) for alice"
    mail(home, subject="q2")
    mail(home, subject="q3")
    assert notify(exe=exe, interval="120") == 0
    assert len(calls(log)) == 1 and state(home)["pending"] is True  # coalesced inside the min-interval
    assert notify(exe=exe) == 0  # due now; no new mail needed
    assert text_of(calls(log)[-1]) == "phoning: 2 actionable message(s) for alice"
    mail(home, subject="q4")
    assert notify(exe=exe) == 0
    assert text_of(calls(log)[-1]) == "phoning: 1 actionable message(s) for alice"


def test_room_mentions_notify_a_human(home: Path, tmp_path: Path) -> None:
    exe, log = fake_osascript(tmp_path)
    box = Mailbox(home / "alice")
    inbound(box, "carol", FP, "chat", "quiet", room="ops")
    inbound(box, "carol", FP, "chat", PEER_TEXT, room="ops", data={"mentions": ["alice"]})
    assert notify(exe=exe) == 0
    assert [text_of(c) for c in calls(log)] == ["phoning: 1 actionable message(s) for alice"]


def test_notify_prohibit_and_approve(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_osascript(tmp_path)
    mail(home)
    assert notify("--rules", '{"notify":"prohibit"}', exe=exe) == 0
    assert calls(log) == [] and "skip: prohibited: notify" in capsys.readouterr().err
    for i in range(3):
        mail(home, subject=f"q{i}")
        assert notify("--rules", '{"notify":"approve"}', exe=exe) == 0
    assert calls(log) == [] and len(approvals(home)) == 1 and approvals(home)[0]["action"] == "notify"
    assert "needs approval: notify" in capsys.readouterr().err
    assert doorbell.main(["alice", "--approve", approvals(home)[0]["id"], "--notify-cmd", str(exe)]) == 0
    assert [text_of(c) for c in calls(log)] == ["phoning: 3 actionable message(s) for alice"]
    assert state(home)["pending_count"] == 0


def test_a_missing_or_failing_command_keeps_the_ring_pending(home: Path, tmp_path: Path,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    mail(home)
    assert notify(exe=tmp_path / "nope" / "osascript") == 0
    assert "ring failed" in capsys.readouterr().err
    assert state(home)["pending"] is True and state(home)["pending_count"] == 1
    bad, log = fake_osascript(tmp_path, rc=1)
    assert notify(exe=bad) == 0
    assert len(calls(log)) == 1 and "ring failed: rc=1" in capsys.readouterr().err
    assert state(home)["pending"] is True
    good, glog = fake_osascript(tmp_path / "ok")
    assert notify(exe=good) == 0
    assert [text_of(c) for c in calls(glog)] == ["phoning: 1 actionable message(s) for alice"]
