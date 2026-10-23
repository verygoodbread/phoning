"""Acceptance: `phoning doorbell --ring kimi|openclaw` (docs/spec/providers.md §7).

Fake `kimi`, `ps`, `lsof` and `openclaw` executables plus a loopback fake `kimi web`; nothing talks to a real harness.
The fake `ps`/`lsof` are put first on PATH because the doorbell has no flag for them (the waker falls back to `which`).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from phoning import doorbell
from tests.acceptance.test_harness_support import (
    SID,
    FakeKimiWeb,
    agent_calls,
    fake_kimi,
    fake_lsof,
    fake_openclaw,
    fake_ps,
    make_kimi_home,
    oc_session,
    read_log,
)
from tests.acceptance.test_rules_common import PEER_TEXT, approvals, mail, state, write_state

OC_SID = "ops-incident-42"


def nudge(home: Path) -> str:
    return doorbell.build_nudge("alice", "alice-main", str(home))


def err(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().err


@pytest.fixture
def kimi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    work = tmp_path / "work"
    khome, sdir = make_kimi_home(tmp_path, work)
    exe, log = fake_kimi(tmp_path)
    ps = fake_ps(tmp_path, [(1, "/sbin/launchd"), (200, "/usr/bin/zsh")])
    lsof = fake_lsof(tmp_path, {})
    monkeypatch.setenv("PATH", os.pathsep.join([str(ps.parent), str(lsof.parent), os.environ.get("PATH", "")]))
    return {"work": work, "khome": khome, "sdir": sdir, "exe": exe, "log": log, "tmp": tmp_path}


@pytest.fixture
def web() -> Iterator[FakeKimiWeb]:
    srv = FakeKimiWeb(subscribed=[SID])
    yield srv
    srv.close()


def kimi_bell(home: Path, k: dict[str, Any], *extra: str, session: str | None = SID) -> int:
    argv = ["alice", "--once", "--home", str(home), "--ring", "kimi", "--kimi-bin", str(k["exe"]),
            "--kimi-home", str(k["khome"]), "--hook-reader", "alice-main", "--min-interval", "0"]
    if session is not None:
        argv += ["--kimi-session", session]
    return doorbell.main([*argv, *extra])


def kimi_calls(k: dict[str, Any]) -> list[dict[str, Any]]:
    return read_log(k["log"])


# ------------------------------------------------------------------ kimi


def test_help_lists_the_harness_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        doorbell.main(["alice", "--help"])
    out = capsys.readouterr().out
    for flag in ("kimi", "openclaw", "--kimi-session", "--kimi-bin", "--kimi-home", "--kimi-cwd",
                 "--openclaw-session", "--openclaw-agent", "--openclaw-bin"):
        assert flag in out, flag


def test_a_harness_ring_without_its_session_exits_2(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mail(home)
    assert doorbell.main(["alice", "--once", "--home", str(home), "--ring", "kimi"]) == 2
    assert "--kimi-session" in err(capsys)
    assert doorbell.main(["alice", "--once", "--home", str(home), "--ring", "openclaw"]) == 2
    assert "--openclaw-session" in err(capsys)
    assert not (home / "alice" / "doorbell-state.json").exists()


def test_a_dead_kimi_session_is_left_alone_without_resume_dead(home: Path, kimi: dict[str, Any],
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    mail(home)
    assert kimi_bell(home, kimi) == 0
    assert "skip: dead (resume_dead not set)" in err(capsys)
    assert kimi_calls(kimi) == []
    st = state(home)
    assert st["pending"] is False and st["resumes"] == []


def test_a_dead_kimi_session_resumes_with_the_fixed_nudge(home: Path, kimi: dict[str, Any],
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    mail(home, subject=PEER_TEXT)
    assert kimi_bell(home, kimi, "--resume-dead") == 0
    assert "resumed: rc=0" in err(capsys)
    (call,) = kimi_calls(kimi)
    assert call["argv"][1:] == ["-S", SID, "-p", nudge(home)]
    assert os.path.realpath(call["cwd"]) == os.path.realpath(kimi["work"])
    assert call["stdin"] == "" and call["kimi_home"] == str(kimi["khome"])
    assert PEER_TEXT not in json.dumps(call) and "bob" not in call["argv"][-1]
    st = state(home)
    assert st["pending"] is False and len(st["resumes"]) == 1


def test_kimi_cwd_is_checked_against_the_session(home: Path, kimi: dict[str, Any],
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    other = kimi["tmp"] / "other"
    other.mkdir()
    mail(home)
    assert kimi_bell(home, kimi, "--resume-dead", "--kimi-cwd", str(other)) == 0
    assert "skip:" in err(capsys) and kimi_calls(kimi) == []
    assert state(home)["resumes"] == []  # a skip never counts as a resume
    mail(home, subject="q2")
    assert kimi_bell(home, kimi, "--resume-dead", "--kimi-cwd", str(kimi["work"])) == 0
    assert len(kimi_calls(kimi)) == 1


def test_a_kimi_resume_obeys_the_resume_rule(home: Path, kimi: dict[str, Any],
                                            capsys: pytest.CaptureFixture[str]) -> None:
    mail(home)
    assert kimi_bell(home, kimi, "--resume-dead", "--rules", '{"resume":"prohibit"}') == 0
    assert "skip: prohibited: resume" in err(capsys)
    assert kimi_calls(kimi) == [] and state(home)["pending"] is False


def test_a_kimi_resume_that_needs_approval_runs_once_when_approved(home: Path, kimi: dict[str, Any],
                                                                   capsys: pytest.CaptureFixture[str]) -> None:
    mail(home, subject=PEER_TEXT)
    assert kimi_bell(home, kimi, "--resume-dead", "--rules", '{"resume":"approve"}') == 0
    assert "needs approval: resume (id " in err(capsys) and kimi_calls(kimi) == []
    (rec,) = approvals(home)
    assert rec["action"] == "resume" and rec["target"]["ring"] == "kimi" and rec["target"]["kimi_session"] == SID
    assert PEER_TEXT not in json.dumps(rec)
    flags = ["--home", str(home), "--kimi-bin", str(kimi["exe"]), "--kimi-home", str(kimi["khome"])]
    assert doorbell.main(["alice", "--approve", rec["id"], *flags]) == 0
    assert "resumed: rc=0" in err(capsys)
    (call,) = kimi_calls(kimi)
    assert call["argv"][1:] == ["-S", SID, "-p", nudge(home)]
    assert doorbell.main(["alice", "--approve", rec["id"], *flags]) == 2
    assert len(kimi_calls(kimi)) == 1


def test_the_resume_guardrails_apply_to_kimi(home: Path, kimi: dict[str, Any],
                                             capsys: pytest.CaptureFixture[str]) -> None:
    mail(home)
    write_state(home, {"resumes": [time.time() - 5]})
    assert kimi_bell(home, kimi, "--resume-dead", "--resume-min-interval", "600") == 0
    assert "skip: resume rate-limited: min interval 600s" in err(capsys)
    assert kimi_calls(kimi) == [] and state(home)["pending"] is True
    assert kimi_bell(home, kimi, "--resume-dead", "--resume-min-interval", "0", "--max-resumes-per-day", "1") == 0
    assert "skip: resume rate-limited: daily cap 1" in err(capsys)
    assert kimi_calls(kimi) == []


def test_a_failing_kimi_resume_keeps_pending(home: Path, kimi: dict[str, Any],
                                             capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_kimi(kimi["tmp"] / "bad", rc=3)
    kimi["exe"], kimi["log"] = exe, log
    mail(home)
    assert kimi_bell(home, kimi, "--resume-dead") == 0
    assert "resumed: rc=3" in err(capsys)
    st = state(home)
    assert st["pending"] is True and len(st["resumes"]) == 1


def test_a_kimi_web_session_is_woken_through_the_server_and_that_is_not_a_resume(
    home: Path, kimi: dict[str, Any], web: FakeKimiWeb, capsys: pytest.CaptureFixture[str]
) -> None:
    web.register(kimi["khome"])
    mail(home)
    # resume is prohibited; a web wake is a `ring`, so it still goes out
    assert kimi_bell(home, kimi, "--resume-dead", "--rules", '{"resume":"prohibit"}') == 0
    assert "rang: web" in err(capsys)
    posts = [r for r in web.requests if r["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["path"].endswith("/prompts") and nudge(home) in json.dumps(posts[0]["body"])
    assert kimi_calls(kimi) == [] and state(home)["resumes"] == []


def test_the_ring_rule_gates_a_kimi_web_wake(home: Path, kimi: dict[str, Any], web: FakeKimiWeb,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    web.register(kimi["khome"])
    mail(home)
    assert kimi_bell(home, kimi, "--rules", '{"ring":"prohibit"}') == 0
    assert "skip: prohibited: ring" in err(capsys)
    assert [r for r in web.requests if r["method"] == "POST"] == []


def test_an_unknown_kimi_session_is_never_created(home: Path, kimi: dict[str, Any],
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    mail(home)
    assert kimi_bell(home, kimi, "--resume-dead", session="session_does-not-exist") == 0
    assert "skip: absent" in err(capsys) and kimi_calls(kimi) == []


# -------------------------------------------------------------- openclaw


def oc_bell(home: Path, exe: Path, *extra: str, session: str = OC_SID) -> int:
    return doorbell.main(["alice", "--once", "--home", str(home), "--ring", "openclaw", "--openclaw-session", session,
                          "--openclaw-bin", str(exe), "--hook-reader", "alice-main", "--min-interval", "0", *extra])


def test_an_openclaw_session_is_woken_through_the_gateway(home: Path, tmp_path: Path,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(OC_SID, agent="ops")])
    mail(home, subject=PEER_TEXT)
    assert oc_bell(home, exe, "--openclaw-agent", "ops") == 0
    assert "rang: gateway" in err(capsys)
    (call,) = agent_calls(log)
    argv = call["argv"]
    assert argv[argv.index("--agent") + 1] == "ops" and argv[argv.index("--session-id") + 1] == OC_SID
    assert argv[argv.index("--message") + 1] == nudge(home) and "--local" not in argv
    assert PEER_TEXT not in json.dumps(call)
    st = state(home)
    assert st["pending"] is False and st["resumes"] == []  # a gateway wake is a ring, not a resume


def test_the_ring_rule_gates_an_openclaw_wake(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(OC_SID)])
    mail(home)
    assert oc_bell(home, exe, "--rules", '{"ring":"prohibit"}') == 0
    assert "skip: prohibited: ring" in err(capsys) and agent_calls(log) == []


def test_a_failed_gateway_wake_keeps_pending(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(OC_SID)], agent_rc=3)
    mail(home)
    assert oc_bell(home, exe) == 0
    assert "ring failed: rc=3" in err(capsys)
    assert state(home)["pending"] is True


def test_an_unknown_openclaw_session_is_never_created(home: Path, tmp_path: Path,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session("someone-else")])
    mail(home)
    assert oc_bell(home, exe, "--resume-dead") == 0
    assert "skip: absent" in err(capsys) and agent_calls(log) == []


def test_openclaw_with_the_gateway_down_needs_resume_dead(home: Path, tmp_path: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(OC_SID)], health="down")
    mail(home)
    assert oc_bell(home, exe) == 0
    assert "skip: dead (resume_dead not set)" in err(capsys) and agent_calls(log) == []
    assert state(home)["resumes"] == []


def test_openclaw_headless_resume_counts_as_a_resume(home: Path, tmp_path: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(OC_SID)], health="down")
    mail(home)
    assert oc_bell(home, exe, "--resume-dead") == 0
    assert "resumed: rc=0" in err(capsys)
    (call,) = agent_calls(log)
    argv = call["argv"]
    assert "--local" in argv and argv[argv.index("--message") + 1] == nudge(home)
    st = state(home)
    assert st["pending"] is False and len(st["resumes"]) == 1


def test_openclaw_resume_obeys_the_rule_and_the_guardrails(home: Path, tmp_path: Path,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(OC_SID)], health="down")
    mail(home)
    assert oc_bell(home, exe, "--resume-dead", "--rules", '{"resume":"prohibit"}') == 0
    assert "skip: prohibited: resume" in err(capsys) and agent_calls(log) == []
    mail(home, subject="q2")
    write_state(home, {"resumes": [time.time() - 5]})
    assert oc_bell(home, exe, "--resume-dead", "--resume-min-interval", "600") == 0
    assert "skip: resume rate-limited: min interval 600s" in err(capsys) and agent_calls(log) == []
    assert state(home)["pending"] is True


def test_an_openclaw_resume_that_needs_approval_runs_once_when_approved(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exe, log = fake_openclaw(tmp_path, sessions=[oc_session(OC_SID, agent="ops")], health="down")
    mail(home)
    assert oc_bell(home, exe, "--resume-dead", "--openclaw-agent", "ops", "--rules", '{"resume":"approve"}') == 0
    assert "needs approval: resume (id " in err(capsys) and agent_calls(log) == []
    (rec,) = approvals(home)
    assert rec["target"]["ring"] == "openclaw" and rec["target"]["openclaw_session"] == OC_SID
    assert rec["target"]["openclaw_agent"] == "ops"
    flags = ["--home", str(home), "--openclaw-bin", str(exe)]
    assert doorbell.main(["alice", "--approve", rec["id"], *flags]) == 0
    (call,) = agent_calls(log)
    assert "--local" in call["argv"] and call["argv"][call["argv"].index("--agent") + 1] == "ops"
    assert doorbell.main(["alice", "--approve", rec["id"], *flags]) == 2
