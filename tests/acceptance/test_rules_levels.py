"""Acceptance: per-target allow / approve / prohibit rules and the approval queue (docs/spec/rules.md §1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from phoning import doorbell
from tests.acceptance.test_rules_common import (
    PEER_TEXT,
    SID,
    TID,
    approvals,
    assert_mode_0600,
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
)


def queue_args(tmp: Path, exe: Path, *extra: str) -> list[str]:
    chome, _ = codex_home_with_thread(tmp, locked=True)
    return ["--ring", "codex-queue", "--codex-thread", TID, "--codex-bin", str(exe),
            "--codex-home", str(chome), "--min-interval", "0", "--hook-reader", "alice-main", *extra]


def test_default_rules_allow_ring(home: Path, tmp_path: Path) -> None:
    exe, log = fake_codex(tmp_path)
    mail(home, subject=PEER_TEXT)
    assert bell(*queue_args(tmp_path, exe)) == 0
    assert [c["argv"][0] for c in calls(log)] == ["queue"]
    assert approvals(home) == []


def test_ring_prohibit_never_acts_and_clears_pending(home: Path, tmp_path: Path,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_codex(tmp_path)
    mail(home)
    assert bell(*queue_args(tmp_path, exe), "--rules", '{"ring":"prohibit"}') == 0
    assert calls(log) == []
    assert "skip: prohibited: ring" in capsys.readouterr().err
    assert state(home)["pending"] is False
    assert approvals(home) == []


def test_ring_prohibit_covers_the_app_server_path(home: Path, tmp_path: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    mail(home)
    # no socket exists: if the doorbell tried to ring it would print "unreachable", not "prohibited"
    assert bell("--ring", "codex-app-server", "--codex-home", str(tmp_path / "nohome"), "--min-interval", "0",
                "--rules", '{"ring":"prohibit"}') == 0
    err = capsys.readouterr().err
    assert "skip: prohibited: ring" in err and "unreachable" not in err


def test_resume_prohibited_by_rule_even_with_the_flags(home: Path, tmp_path: Path,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    # claude-resume: skip, never spawns the resume child
    chome, _ = claude_home_with_session(tmp_path)
    dead, clog = fake_claude(tmp_path / "dead", [{"sessionId": "other", "status": "idle", "pid": 2}])
    tok = token_file(tmp_path)
    mail(home)
    assert bell("--ring", "claude-resume", "--claude-session", SID, "--claude-bin", str(dead),
                "--claude-home", str(chome), "--claude-token-file", str(tok), "--min-interval", "0",
                "--rules", '{"resume":"prohibit"}') == 0
    assert calls(clog) == []
    assert "skip: prohibited: resume" in capsys.readouterr().err
    # codex dead thread: no exec resume; degrades to the queue ring
    exe, log = fake_codex(tmp_path / "cx")
    cchome, _ = codex_home_with_thread(tmp_path / "cx", locked=False)
    mail(home, subject="again")
    assert bell("--ring", "codex-queue", "--codex-thread", TID, "--codex-bin", str(exe), "--codex-home", str(cchome),
                "--resume-dead", "--min-interval", "0", "--rules", '{"resume":"prohibit"}') == 0
    assert [c["argv"][0] for c in calls(log)] == ["queue"]
    assert "skip: prohibited: resume" in capsys.readouterr().err


def test_resume_is_not_allowed_without_its_opt_in_flags(home: Path, tmp_path: Path) -> None:
    # a dead thread but no --resume-dead: queue only (nothing ever resumes by default)
    exe, log = fake_codex(tmp_path)
    cchome, _ = codex_home_with_thread(tmp_path, locked=False)
    mail(home)
    assert bell("--ring", "codex-queue", "--codex-thread", TID, "--codex-bin", str(exe), "--codex-home", str(cchome),
                "--min-interval", "0") == 0
    assert [c["argv"][0] for c in calls(log)] == ["queue"]


def test_approve_level_writes_a_record_and_acts_only_on_approval(home: Path, tmp_path: Path,
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_codex(tmp_path)
    args = queue_args(tmp_path, exe, "--rules", '{"ring":"approve"}')
    mail(home, subject=PEER_TEXT)
    assert bell(*args) == 0
    err = capsys.readouterr().err
    assert calls(log) == [] and "needs approval: ring" in err
    recs = approvals(home)
    assert len(recs) == 1 and recs[0]["status"] == "pending" and recs[0]["action"] == "ring"
    assert len(recs[0]["id"]) == 8
    assert_mode_0600(home / "alice" / "doorbell-approvals.jsonl")
    assert recs[0]["id"] in err
    assert PEER_TEXT not in (home / "alice" / "doorbell-approvals.jsonl").read_text()
    assert state(home)["pending"] is False
    # more mail while undecided: no duplicate record
    mail(home, subject="q2")
    assert bell(*args) == 0
    assert len(approvals(home)) == 1
    # approve: performs once
    aid = recs[0]["id"]
    assert doorbell.main(["alice", "--approve", aid, "--codex-bin", str(exe)]) == 0
    assert [c["argv"][:3] for c in calls(log)] == [["queue", "--thread", TID]]
    nudge = calls(log)[0]["argv"][4]
    assert nudge.startswith("[phoning] New actionable phoning mail for alice") and PEER_TEXT not in nudge
    assert approvals(home)[-1] == {**approvals(home)[-1], "id": aid, "status": "approved"}
    # a second approval is refused and runs nothing
    assert doorbell.main(["alice", "--approve", aid, "--codex-bin", str(exe)]) == 2
    assert len(calls(log)) == 1
    # unknown id
    assert doorbell.main(["alice", "--approve", "deadbeef", "--codex-bin", str(exe)]) == 2


def test_deny_discards_and_cannot_be_approved(home: Path, tmp_path: Path) -> None:
    exe, log = fake_codex(tmp_path)
    mail(home)
    assert bell(*queue_args(tmp_path, exe, "--rules", '{"ring":"approve"}')) == 0
    aid = approvals(home)[0]["id"]
    assert doorbell.main(["alice", "--deny", aid]) == 0
    assert approvals(home)[-1]["status"] == "denied"
    assert doorbell.main(["alice", "--approve", aid, "--codex-bin", str(exe)]) == 2
    assert doorbell.main(["alice", "--deny", aid]) == 2
    assert calls(log) == []


def test_approve_works_while_the_doorbell_lock_is_held(home: Path, tmp_path: Path) -> None:
    import fcntl
    import os

    exe, log = fake_codex(tmp_path)
    mail(home)
    assert bell(*queue_args(tmp_path, exe, "--rules", '{"ring":"approve"}')) == 0
    aid = approvals(home)[0]["id"]
    fd = os.open(home / "alice" / "mailbox" / "doorbell.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a running doorbell
        assert doorbell.main(["alice", "--approve", aid, "--codex-bin", str(exe)]) == 0
    finally:
        os.close(fd)
    assert len(calls(log)) == 1


def test_approved_resume_runs_the_claude_resume_once(home: Path, tmp_path: Path) -> None:
    chome, workdir = claude_home_with_session(tmp_path)
    dead, clog = fake_claude(tmp_path / "dead", [{"sessionId": "other", "status": "idle", "pid": 2}])
    tok = token_file(tmp_path)
    args = ["--ring", "claude-resume", "--claude-session", SID, "--claude-bin", str(dead), "--claude-home", str(chome),
            "--claude-token-file", str(tok), "--min-interval", "0", "--hook-reader", "alice-main",
            "--rules", '{"resume":"approve"}']
    mail(home, subject=PEER_TEXT)
    assert bell(*args) == 0
    assert calls(clog) == []
    recs = approvals(home)
    assert len(recs) == 1 and recs[0]["action"] == "resume"
    assert "tok-SECRET-123" not in (home / "alice" / "doorbell-approvals.jsonl").read_text()
    assert doorbell.main(["alice", "--approve", recs[0]["id"], "--claude-bin", str(dead)]) == 0
    got = calls(clog)
    assert len(got) == 1 and got[0]["argv"][2:4] == ["--resume", SID] and got[0]["tok"] == "tok-SECRET-123"
    assert PEER_TEXT not in got[0]["argv"][1]
    assert len(state(home)["resumes"]) == 1  # an approved resume counts against the guardrails


def test_approval_that_skips_stays_pending(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    chome, _ = claude_home_with_session(tmp_path)
    live, clog = fake_claude(tmp_path / "live", [{"sessionId": SID, "status": "idle", "pid": 1}])
    dead, dlog = fake_claude(tmp_path / "dead", [{"sessionId": "other", "status": "idle", "pid": 2}])
    tok = token_file(tmp_path)
    args = ["--ring", "claude-resume", "--claude-session", SID, "--claude-bin", str(dead), "--claude-home", str(chome),
            "--claude-token-file", str(tok), "--min-interval", "0", "--rules", '{"resume":"approve"}']
    mail(home)
    assert bell(*args) == 0
    aid = approvals(home)[0]["id"]
    capsys.readouterr()
    assert doorbell.main(["alice", "--approve", aid, "--claude-bin", str(live)]) == 1  # session is live: skip
    assert "skip: live" in capsys.readouterr().err
    assert calls(clog) == [] and approvals(home)[-1]["status"] == "pending"
    assert doorbell.main(["alice", "--approve", aid, "--claude-bin", str(dead)]) == 0
    assert len(calls(dlog)) == 1


def test_rules_file_and_flag_precedence(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_codex(tmp_path)
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps({"ring": "prohibit", "notify": "approve"}))
    mail(home)
    assert bell(*queue_args(tmp_path, exe), "--rules-file", str(rules_file)) == 0
    assert calls(log) == [] and "skip: prohibited: ring" in capsys.readouterr().err
    mail(home, subject="q2")  # the flag overrides the file key by key
    assert bell(*queue_args(tmp_path, exe), "--rules-file", str(rules_file), "--rules", '{"ring":"allow"}') == 0
    assert [c["argv"][0] for c in calls(log)] == ["queue"]


@pytest.mark.parametrize("bad", ['{"ring":"yes"}', '{"fly":"allow"}', "[1,2]", "not json", '{"ring":3}'])
def test_bad_rules_exit_2_before_touching_anything(home: Path, tmp_path: Path, bad: str,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_codex(tmp_path)
    mail(home)
    assert bell(*queue_args(tmp_path, exe), "--rules", bad) == 2
    assert "error" in capsys.readouterr().err
    assert calls(log) == []
    assert not (home / "alice" / "doorbell-state.json").exists()
    assert doorbell.main(["alice", "--once", "--rules-file", str(tmp_path / "missing.json")]) == 2


def test_approve_and_deny_are_mutually_exclusive_and_need_a_mailbox(home: Path) -> None:
    mail(home)
    assert doorbell.main(["alice", "--approve", "aaaaaaaa", "--deny", "aaaaaaaa"]) == 2
    assert doorbell.main(["nobody", "--approve", "aaaaaaaa"]) == 2


def test_approved_codex_resume_needs_the_thread_to_still_be_dead(home: Path, tmp_path: Path,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_codex(tmp_path)
    chome, _ = codex_home_with_thread(tmp_path, locked=False)
    args = ["--ring", "codex-queue", "--codex-thread", TID, "--codex-bin", str(exe), "--codex-home", str(chome),
            "--resume-dead", "--min-interval", "0", "--rules", '{"resume":"approve"}']
    mail(home)
    assert bell(*args) == 0
    aid = approvals(home)[0]["id"]
    assert approvals(home)[0]["action"] == "resume" and calls(log) == []
    (chome / "thread-writer-locks" / f"{TID}.lock").write_text("")  # somebody picked the thread up meanwhile
    capsys.readouterr()
    assert doorbell.main(["alice", "--approve", aid, "--codex-bin", str(exe)]) == 1
    assert "skip: thread no longer dead" in capsys.readouterr().err
    assert calls(log) == [] and approvals(home)[-1]["status"] == "pending"
    (chome / "thread-writer-locks" / f"{TID}.lock").unlink()
    assert doorbell.main(["alice", "--approve", aid, "--codex-bin", str(exe)]) == 0
    assert [c["argv"][0] for c in calls(log)] == ["exec"]


def test_approve_and_deny_never_create_a_missing_principal_dir(home: Path) -> None:
    assert doorbell.main(["ghost", "--approve", "aaaaaaaa"]) == 2
    assert doorbell.main(["ghost", "--deny", "aaaaaaaa"]) == 2
    assert doorbell.main(["ghost", "--once", "--rules", '{"ring":"approve"}']) == 2
    assert not (home / "ghost").exists()


@pytest.mark.parametrize("field,value", [
    ("hook_reader", "x; touch /tmp/pwned"),
    ("hook_reader", "a b"),
    ("codex_thread", "t$(id)"),
    ("home", 5),
    ("codex_home", ["x"]),
    ("resume_dead", "yes"),
])
def test_a_tampered_approval_target_is_refused(home: Path, tmp_path: Path, field: str, value: object) -> None:
    exe, log = fake_codex(tmp_path)
    mail(home)
    assert bell(*queue_args(tmp_path, exe, "--rules", '{"ring":"approve"}')) == 0
    path = home / "alice" / "doorbell-approvals.jsonl"
    rec = json.loads(path.read_text().splitlines()[0])
    rec["target"][field] = value
    path.write_text(json.dumps(rec) + "\n")
    assert doorbell.main(["alice", "--approve", rec["id"], "--codex-bin", str(exe)]) == 2
    assert calls(log) == []


def test_decided_records_keep_their_action_and_target(home: Path, tmp_path: Path) -> None:
    exe, _ = fake_codex(tmp_path)
    mail(home)
    assert bell(*queue_args(tmp_path, exe, "--rules", '{"ring":"approve"}')) == 0
    aid = approvals(home)[0]["id"]
    assert doorbell.main(["alice", "--deny", aid]) == 0
    from phoning.doorbell import load_approvals

    got = load_approvals(str(home / "alice"))[aid]
    assert got["status"] == "denied" and got["action"] == "ring" and got["target"]["codex_thread"] == TID
