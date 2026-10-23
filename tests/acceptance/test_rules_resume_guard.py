"""Acceptance: resume guardrails and the shared ring/resume limit (docs/spec/rules.md §2)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from phoning import doorbell
from tests.acceptance.test_rules_common import (
    SID,
    TID,
    bell,
    calls,
    claude_home_with_session,
    codex_home_with_thread,
    fake_claude,
    fake_codex,
    home_fixture,  # noqa: F401  (registers the `home` fixture)
    mail,
    state,
    token_file,
    write_state,
)


def claude_args(tmp: Path) -> tuple[list[str], Path]:
    chome, _ = claude_home_with_session(tmp)
    dead, log = fake_claude(tmp / "dead", [{"sessionId": "other", "status": "idle", "pid": 2}])
    tok = token_file(tmp)
    return (["--ring", "claude-resume", "--claude-session", SID, "--claude-bin", str(dead), "--claude-home", str(chome),
             "--claude-token-file", str(tok), "--min-interval", "0", "--hook-reader", "alice-main"], log)


def test_one_resume_per_resume_min_interval(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args, log = claude_args(tmp_path)
    mail(home)
    assert bell(*args) == 0
    assert len(calls(log)) == 1 and len(state(home)["resumes"]) == 1
    capsys.readouterr()
    mail(home, subject="q2")
    assert bell(*args) == 0
    assert len(calls(log)) == 1  # blocked: default 600 s between resumes
    err = capsys.readouterr().err
    assert "skip: resume rate-limited" in err and "min interval 600" in err
    assert state(home)["pending"] is True  # kept: it retries when allowed
    assert bell(*args, "--resume-min-interval", "0") == 0  # no new mail needed, the pending ring fires
    assert len(calls(log)) == 2 and len(state(home)["resumes"]) == 2


def test_resume_min_interval_counts_from_the_recorded_spawn_time(home: Path, tmp_path: Path) -> None:
    args, log = claude_args(tmp_path)
    now = time.time()
    write_state(home, {"last_ring": 0.0, "pending": False, "introduced": [], "resumes": [now - 601]})
    mail(home)
    assert bell(*args) == 0
    assert len(calls(log)) == 1  # 601 s ago is outside the 600 s window
    write_state(home, {"last_ring": 0.0, "pending": False, "introduced": [], "resumes": [now - 30]})
    mail(home, subject="q2")
    assert bell(*args) == 0
    assert len(calls(log)) == 1  # 30 s ago is inside it


def test_daily_cap_and_old_entries_do_not_count(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args, log = claude_args(tmp_path)
    loose = [*args, "--resume-min-interval", "0", "--max-resumes-per-day", "2"]
    for i in range(3):
        mail(home, subject=f"q{i}")
        assert bell(*loose) == 0
    assert len(calls(log)) == 2
    assert "daily cap 2" in capsys.readouterr().err
    assert len(state(home)["resumes"]) == 2
    old = time.time() - 90000
    write_state(home, {"last_ring": 0.0, "pending": False, "introduced": [], "resumes": [old] * 5})
    mail(home, subject="after a day")
    assert bell(*loose) == 0
    assert len(calls(log)) == 3  # more than 24 h old: pruned, not counted
    assert len(state(home)["resumes"]) == 1


def test_zero_per_day_blocks_every_resume(home: Path, tmp_path: Path) -> None:
    args, log = claude_args(tmp_path)
    mail(home)
    assert bell(*args, "--max-resumes-per-day", "0", "--resume-min-interval", "0") == 0
    assert calls(log) == []


def test_skips_and_failures_before_spawn_do_not_count(home: Path, tmp_path: Path) -> None:
    chome, _ = claude_home_with_session(tmp_path)
    live, llog = fake_claude(tmp_path / "live", [{"sessionId": SID, "status": "idle", "pid": 1}])
    tok = token_file(tmp_path)
    tok.chmod(0o644)  # bad token mode
    dead, dlog = fake_claude(tmp_path / "dead", [{"sessionId": "other", "status": "idle", "pid": 2}])
    common = ["--ring", "claude-resume", "--claude-session", SID, "--claude-home", str(chome),
              "--claude-token-file", str(tok), "--min-interval", "0", "--resume-min-interval", "0"]
    mail(home)
    assert bell(*common, "--claude-bin", str(live)) == 0
    mail(home, subject="q2")
    assert bell(*common, "--claude-bin", str(dead)) == 0
    assert calls(llog) == [] and calls(dlog) == []
    assert state(home).get("resumes", []) == []


def test_ring_and_resume_share_one_interval_per_target(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_codex(tmp_path)
    chome, _ = codex_home_with_thread(tmp_path, locked=False)
    args = ["--ring", "codex-queue", "--codex-thread", TID, "--codex-bin", str(exe), "--codex-home", str(chome),
            "--resume-dead"]
    mail(home)
    assert bell(*args, "--min-interval", "120") == 0
    assert [c["argv"][0] for c in calls(log)] == ["exec"]  # dead thread: resumed
    mail(home, subject="q2")
    assert bell(*args, "--min-interval", "120") == 0
    assert len(calls(log)) == 1  # the shared last_ring blocks both kinds
    capsys.readouterr()
    assert bell(*args, "--min-interval", "0") == 0  # due again, but the resume window (600 s) is still shut
    assert [c["argv"][0] for c in calls(log)] == ["exec", "queue"]  # degraded to the durable queue ring
    assert "skip: resume rate-limited" in capsys.readouterr().err
    assert len(state(home)["resumes"]) == 1


def test_a_live_ring_is_not_a_resume(home: Path, tmp_path: Path) -> None:
    exe, log = fake_codex(tmp_path)
    chome, _ = codex_home_with_thread(tmp_path, locked=True)
    args = ["--ring", "codex-queue", "--codex-thread", TID, "--codex-bin", str(exe), "--codex-home", str(chome),
            "--resume-dead", "--min-interval", "0"]
    for i in range(3):
        mail(home, subject=f"q{i}")
        assert bell(*args) == 0
    assert [c["argv"][0] for c in calls(log)] == ["queue"] * 3
    assert state(home).get("resumes", []) == []


@pytest.mark.parametrize("flag", [["--resume-min-interval", "-1"], ["--max-resumes-per-day", "-1"]])
def test_negative_guardrail_values_are_usage_errors(home: Path, flag: list[str]) -> None:
    mail(home)
    assert doorbell.main(["alice", "--once", *flag]) == 2
