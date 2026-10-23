"""Acceptance: `phoning doorbell --ring muse` (docs/spec/muse.md §4).

A fake `muse` executable on a temporary store (see test_muse_ring_acceptance.py); liveness is the real flock held by a
separate process. Nothing talks to a real Muse, and every call passes an explicit `--home`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from phoning import doorbell
from tests.acceptance.test_muse_ring_acceptance import SID, Holder, session
from tests.acceptance.test_rules_common import PEER_TEXT, approvals, mail, state, write_state

NAME = "quiet-harbor"


def muse_bell(home: Path, w: dict[str, Any], *extra: str, target: str | None = NAME) -> int:
    argv = ["alice", "--once", "--home", str(home), "--ring", "muse", "--muse-bin", w["exe"],
            "--muse-home", str(w["store"]), "--hook-reader", "alice-main", "--min-interval", "0"]
    if target is not None:
        argv += ["--muse-session", target]
    return doorbell.main([*argv, *extra])


def nudge(home: Path) -> str:
    return doorbell.build_nudge("alice", "alice-main", str(home))


def exec_calls(w: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in w["calls"]() if c["kind"] == "exec"]


def err(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().err


def test_help_lists_the_muse_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        doorbell.main(["alice", "--help"])
    out = capsys.readouterr().out
    for flag in ("muse", "--muse-session", "--muse-bin", "--muse-home", "--muse-no-resume", "--muse-arg"):
        assert flag in out, flag


def test_a_muse_ring_without_a_session_exits_2(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mail(home)
    assert doorbell.main(["alice", "--once", "--home", str(home), "--ring", "muse"]) == 2
    assert "--muse-session" in err(capsys)
    assert not (home / "alice" / "doorbell-state.json").exists()


def test_a_dead_session_is_revived_with_the_fixed_nudge(home: Path, world: dict[str, Any],
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    session(world, turns=2)
    mail(home, subject=PEER_TEXT)
    assert muse_bell(home, world) == 0
    assert "resumed: rc=0" in err(capsys)
    (call,) = exec_calls(world)
    argv = call["argv"]
    assert argv[argv.index("--session-id") + 1] == SID and argv[-2:] == ["--", nudge(home)]
    assert call["prompt"] == nudge(home) and call["history"] == 2 and call["stdin"] == ""
    assert call["env"]["PHONING_HOME"] == str(home)
    assert PEER_TEXT not in json.dumps(world["calls"]()) and "bob" not in call["prompt"]
    st = state(home)
    assert st["pending"] is False and st["pending_count"] == 0 and len(st["resumes"]) == 1 and st["last_ring"] > 0


def test_no_resume_flag_keeps_an_ended_session_asleep(home: Path, world: dict[str, Any],
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    session(world)
    mail(home)
    assert muse_bell(home, world, "--muse-no-resume") == 0
    assert "skip: not running (resume not enabled)" in err(capsys)
    assert world["calls"]() == []
    st = state(home)
    assert st["pending"] is False and st["resumes"] == []  # a skip never counts as a resume


def test_a_live_session_that_refuses_the_sender_is_a_quiet_skip(home: Path, world: dict[str, Any],
                                                                holders: list[Holder],
                                                                capsys: pytest.CaptureFixture[str]) -> None:
    holders.append(Holder(session(world)))
    world["configure"](send_reply={"schema_version": 1, "status": "unavailable", "error_code": "sender_unverified"},
                       send_rc=1)
    mail(home)
    assert muse_bell(home, world) == 0
    assert "skip: sender_unverified" in err(capsys)
    assert [c["kind"] for c in world["calls"]()] == ["send"]  # woken, never resumed
    st = state(home)
    assert st["pending"] is False and st["resumes"] == []  # the hook path delivers; nothing retries
    assert muse_bell(home, world) == 0  # a second pass with no new mail makes no call at all
    assert [c["kind"] for c in world["calls"]()] == ["send"]


def test_a_live_session_is_woken_and_a_wake_is_not_a_resume(home: Path, world: dict[str, Any],
                                                            holders: list[Holder],
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    holders.append(Holder(session(world)))
    mail(home, subject=PEER_TEXT)
    assert muse_bell(home, world, "--rules", '{"resume":"prohibit"}') == 0  # resume is not what a wake does
    assert "rang: delivered" in err(capsys)
    (call,) = world["calls"]()
    assert call["body"] == nudge(home) and PEER_TEXT not in call["body"]
    assert state(home)["resumes"] == []


def test_the_ring_rule_gates_a_wake(home: Path, world: dict[str, Any], holders: list[Holder],
                                    capsys: pytest.CaptureFixture[str]) -> None:
    holders.append(Holder(session(world)))
    mail(home)
    assert muse_bell(home, world, "--rules", '{"ring":"prohibit"}') == 0
    assert "skip: prohibited: ring" in err(capsys)
    assert world["calls"]() == [] and state(home)["pending"] is False


def test_a_prohibited_resume_never_spawns(home: Path, world: dict[str, Any],
                                          capsys: pytest.CaptureFixture[str]) -> None:
    session(world)
    mail(home)
    assert muse_bell(home, world, "--rules", '{"resume":"prohibit"}') == 0
    assert "skip: prohibited: resume" in err(capsys)
    assert world["calls"]() == []
    st = state(home)
    assert st["pending"] is False and st["resumes"] == []


def test_a_resume_that_needs_approval_runs_once_when_approved(home: Path, world: dict[str, Any],
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    session(world)
    mail(home, subject=PEER_TEXT)
    assert muse_bell(home, world, "--rules", '{"resume":"approve"}') == 0
    out = err(capsys)
    assert "needs approval: resume (id " in out and world["calls"]() == []
    (rec,) = approvals(home)
    assert rec["status"] == "pending" and rec["action"] == "resume"
    assert rec["target"]["ring"] == "muse" and rec["target"]["muse_session"] == NAME
    assert PEER_TEXT not in json.dumps(rec) and "bob" not in json.dumps(rec)
    aid = rec["id"]
    flags = ["--home", str(home), "--muse-bin", world["exe"], "--muse-home", str(world["store"])]
    assert doorbell.main(["alice", "--approve", aid, *flags]) == 0
    assert "resumed: rc=0" in err(capsys)
    (call,) = exec_calls(world)
    assert call["prompt"] == nudge(home)
    assert len(state(home)["resumes"]) == 1
    assert doorbell.main(["alice", "--approve", aid, *flags]) == 2  # consumed
    assert len(exec_calls(world)) == 1


def test_a_denied_resume_never_runs(home: Path, world: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    session(world)
    mail(home)
    assert muse_bell(home, world, "--rules", '{"resume":"approve"}') == 0
    aid = approvals(home)[0]["id"]
    assert doorbell.main(["alice", "--deny", aid, "--home", str(home)]) == 0
    assert doorbell.main(["alice", "--approve", aid, "--home", str(home), "--muse-bin", world["exe"],
                          "--muse-home", str(world["store"])]) == 2
    assert world["calls"]() == []


def test_the_min_interval_guardrail_blocks_a_resume(home: Path, world: dict[str, Any],
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    session(world)
    mail(home)
    write_state(home, {"resumes": [time.time() - 5]})
    assert muse_bell(home, world, "--resume-min-interval", "600") == 0
    assert "skip: resume rate-limited: min interval 600s" in err(capsys)
    assert exec_calls(world) == []
    st = state(home)
    assert st["pending"] is True and st["last_ring"] > 0  # retried when due


def test_the_daily_cap_guardrail_blocks_a_resume(home: Path, world: dict[str, Any],
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    session(world)
    mail(home)
    write_state(home, {"resumes": [time.time() - 3600]})
    assert muse_bell(home, world, "--resume-min-interval", "0", "--max-resumes-per-day", "1") == 0
    assert "skip: resume rate-limited: daily cap 1" in err(capsys)
    assert exec_calls(world) == []
    assert muse_bell(home, world, "--resume-min-interval", "0", "--max-resumes-per-day", "2") == 0
    assert len(exec_calls(world)) == 1 and len(state(home)["resumes"]) == 2


def test_the_shared_min_interval_covers_muse_too(home: Path, world: dict[str, Any],
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    session(world)
    mail(home)
    assert muse_bell(home, world) == 0
    assert len(exec_calls(world)) == 1
    mail(home, subject="q2")
    assert muse_bell(home, world, "--min-interval", "3600", "--resume-min-interval", "0") == 0
    assert len(exec_calls(world)) == 1 and state(home)["pending"] is True  # coalesced, not lost


def test_extra_args_reach_muse_before_the_separator(home: Path, world: dict[str, Any]) -> None:
    session(world)
    mail(home)
    assert muse_bell(home, world, "--muse-arg=--permission-profile", "--muse-arg", "strict") == 0
    (call,) = exec_calls(world)
    argv = call["argv"]
    sep = argv.index("--")
    assert argv[sep - 2:sep] == ["--permission-profile", "strict"]


def test_forbidden_extra_args_fail_the_ring_and_keep_pending(home: Path, world: dict[str, Any],
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    session(world)
    mail(home)
    assert muse_bell(home, world, "--muse-arg=--workspace", "--muse-arg", "/elsewhere") == 0
    assert "ring failed: invalid extra args" in err(capsys)
    assert exec_calls(world) == [] and state(home)["pending"] is True


def test_a_failing_resume_is_retried_and_still_counts(home: Path, world: dict[str, Any],
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    session(world)
    world["configure"](exec_rc=3)
    mail(home)
    assert muse_bell(home, world) == 0
    assert "resumed: rc=3" in err(capsys)
    st = state(home)
    assert st["pending"] is True and len(st["resumes"]) == 1


def test_a_typo_session_is_never_created(home: Path, world: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    session(world)
    mail(home)
    before = sorted(p.name for p in (world["store"] / "sessions").rglob("*"))
    assert muse_bell(home, world, target="no-such-session") == 0
    assert "skip: unreachable" in err(capsys)
    assert world["calls"]() == []
    assert sorted(p.name for p in (world["store"] / "sessions").rglob("*")) == before
