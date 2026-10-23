"""Acceptance: the new `up.json` doorbell keys.

Each key is rendered as its doorbell flag. The rendered argv is then fed to the REAL doorbell (`--once`) with fake
binaries, so a key that `up` renders but the doorbell rejects cannot slip through.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from phoning import doorbell, up
from tests.acceptance.test_muse_ring_acceptance import SID as MUSE_SID
from tests.acceptance.test_muse_ring_acceptance import session as muse_session
from tests.acceptance.test_rules_common import (
    PEER_TEXT,
    approvals,
    calls,
    fake_osascript,
    mail,
    state,
)


def load(tmp: Path, *bells: dict[str, Any]) -> list[dict[str, Any]]:
    path = tmp / "up.json"
    path.write_text(json.dumps({"owners": [], "doorbells": [{"home": str(tmp / "home"), "name": "alice", **b}
                                                            for b in bells]}))
    return list(up.load_config(str(path))["doorbells"])


def argv_of(tmp: Path, **bell: Any) -> list[str]:
    (cfg,) = load(tmp, bell)
    return up._doorbell_argv("/py", cfg)


def value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def bad(tmp: Path, **bell: Any) -> None:
    with pytest.raises(up.ConfigError):
        load(tmp, bell)


# ------------------------------------------------------------ rendering


def test_each_new_key_is_rendered_as_its_flag(tmp_path: Path) -> None:
    argv = argv_of(
        tmp_path,
        ring="claude-resume",
        rules={"resume": "allow", "ring": "approve"},
        rules_file="~/rules.json",
        resume_min_interval=90,
        max_resumes_per_day=3,
        resume_max_sandbox="workspace-write",
        notify_cmd="~/bin/osa",
        codex_bin="~/bin/codex",
        claude_session="sess-1",
        claude_bin="~/bin/claude",
        claude_home="~/.claude-x",
        claude_token_file="~/tok",
    )
    assert argv[:5] == ["/py", "-m", "phoning.cli", "doorbell", "alice"]
    assert json.loads(value(argv, "--rules")) == {"resume": "allow", "ring": "approve"}
    assert value(argv, "--rules-file") == os.path.expanduser("~/rules.json")
    assert float(value(argv, "--resume-min-interval")) == 90.0
    assert value(argv, "--max-resumes-per-day") == "3"
    assert value(argv, "--resume-max-sandbox") == "workspace-write"
    assert value(argv, "--notify-cmd") == os.path.expanduser("~/bin/osa")
    assert value(argv, "--codex-bin") == os.path.expanduser("~/bin/codex")
    assert value(argv, "--ring") == "claude-resume" and value(argv, "--claude-session") == "sess-1"
    assert value(argv, "--claude-bin") == os.path.expanduser("~/bin/claude")
    assert value(argv, "--claude-home") == os.path.expanduser("~/.claude-x")
    assert value(argv, "--claude-token-file") == os.path.expanduser("~/tok")


def test_resume_dead_is_a_bare_flag_only_when_true(tmp_path: Path) -> None:
    assert "--resume-dead" in argv_of(tmp_path, ring="codex-queue", codex_thread="t", resume_dead=True)
    assert "--resume-dead" not in argv_of(tmp_path, ring="codex-queue", codex_thread="t", resume_dead=False)
    assert "--resume-dead" not in argv_of(tmp_path, ring="codex-queue", codex_thread="t")


def test_muse_keys(tmp_path: Path) -> None:
    argv = argv_of(tmp_path, ring="muse", muse_session="quiet-harbor", muse_bin="~/bin/muse",
                   muse_home="~/.muse", muse_args=["--permission-profile", "strict"])
    assert value(argv, "--ring") == "muse" and value(argv, "--muse-session") == "quiet-harbor"
    assert value(argv, "--muse-bin") == os.path.expanduser("~/bin/muse")
    assert value(argv, "--muse-home") == os.path.expanduser("~/.muse")
    assert "--muse-no-resume" not in argv  # muse_resume defaults to true
    # a value that starts with '-' must stay glued to its flag or argparse would read it as an option
    assert "--muse-arg=--permission-profile" in argv and "--muse-arg=strict" in argv
    off = argv_of(tmp_path, ring="muse", muse_session="m", muse_resume=False)
    assert "--muse-no-resume" in off
    on = argv_of(tmp_path, ring="muse", muse_session="m", muse_resume=True)
    assert "--muse-no-resume" not in on and not any(a.startswith("--muse-arg") for a in on)


def test_kimi_and_openclaw_keys(tmp_path: Path) -> None:
    k = argv_of(tmp_path, ring="kimi", kimi_session="session_x", kimi_cwd="~/work", resume_dead=True)
    assert value(k, "--ring") == "kimi" and value(k, "--kimi-session") == "session_x"
    assert value(k, "--kimi-cwd") == os.path.expanduser("~/work") and "--resume-dead" in k
    o = argv_of(tmp_path, ring="openclaw", openclaw_session="ops-1", openclaw_agent="ops")
    assert value(o, "--ring") == "openclaw" and value(o, "--openclaw-session") == "ops-1"
    assert value(o, "--openclaw-agent") == "ops"


def test_existing_keys_still_render_as_before(tmp_path: Path) -> None:
    argv = argv_of(tmp_path, reader="bell", hook_reader="alice-main", ring="codex-app-server", codex_home="/c",
                   codex_thread="thr", types=["ask", "propose"], min_interval=30, intro=True, poll=1)
    assert value(argv, "--reader") == "bell" and value(argv, "--types") == "ask,propose"
    assert value(argv, "--min-interval") == "30.0" and "--intro" in argv


@pytest.mark.parametrize("ring", ["codex-app-server", "codex-queue", "claude-resume", "notify", "muse", "kimi",
                                  "openclaw"])
def test_every_ring_kind_is_accepted(tmp_path: Path, ring: str) -> None:
    (cfg,) = load(tmp_path, {"ring": ring})
    assert cfg["ring"] == ring


def test_an_unknown_ring_kind_is_rejected(tmp_path: Path) -> None:
    bad(tmp_path, ring="telepathy")
    bad(tmp_path, ring="")


@pytest.mark.parametrize(
    "bell",
    [
        {"rules": "resume=allow"},
        {"rules": ["ring"]},
        {"rules": {"teleport": "allow"}},
        {"rules": {"resume": "sometimes"}},
        {"rules": {"resume": True}},
        {"rules_file": ""},
        {"rules_file": 5},
        {"resume_min_interval": -1},
        {"resume_min_interval": True},
        {"resume_min_interval": "soon"},
        {"max_resumes_per_day": -1},
        {"max_resumes_per_day": 1.5},
        {"max_resumes_per_day": True},
        {"resume_max_sandbox": "yolo"},
        {"resume_max_sandbox": 1},
        {"notify_cmd": ""},
        {"resume_dead": "yes"},
        {"resume_dead": 1},
        {"codex_bin": ""},
        {"claude_session": ""},
        {"claude_bin": 3},
        {"claude_home": ""},
        {"claude_token_file": []},
        {"muse_session": ""},
        {"muse_bin": ""},
        {"muse_home": 1},
        {"muse_resume": "no"},
        {"muse_args": "--x"},
        {"muse_args": ["ok", 3]},
        {"muse_args": ["ok", ""]},
        {"kimi_session": ""},
        {"kimi_cwd": ""},
        {"openclaw_session": 7},
        {"openclaw_agent": ""},
        {"muse_typo": "x"},
        {"kimi_bin": "/x"},
    ],
)
def test_bad_values_and_unknown_keys_are_config_errors(tmp_path: Path, bell: dict[str, Any]) -> None:
    bad(tmp_path, **bell)


def test_valid_extremes_are_accepted(tmp_path: Path) -> None:
    (cfg,) = load(tmp_path, {"resume_min_interval": 0, "max_resumes_per_day": 0,
                             "rules": {}, "muse_args": [], "resume_max_sandbox": "read-only"})
    assert cfg["resume_min_interval"] == 0 and cfg["max_resumes_per_day"] == 0 and cfg["rules"] == {}
    argv = up._doorbell_argv("/py", cfg)
    assert value(argv, "--max-resumes-per-day") == "0"


# ----------------------------------------- the real doorbell accepts them


def run_rendered(tmp: Path, argv: list[str], *extra: str) -> int:
    """Run the doorbell exactly as the rendered argv says (one non-blocking pass)."""
    i = argv.index("doorbell")
    return doorbell.main([*argv[i + 1:], "--once", "--home", str(tmp / "home"), "--min-interval", "0", *extra])


def test_rendered_notify_and_rules_run_in_the_real_doorbell(tmp_path: Path, home: Path) -> None:
    exe, log = fake_osascript(tmp_path)
    mail(home, subject=PEER_TEXT)
    argv = argv_of(tmp_path, ring="notify", notify_cmd=str(exe), rules={"notify": "allow"}, hook_reader="alice-main")
    assert run_rendered(tmp_path, argv) == 0
    (call,) = calls(log)
    assert call["argv"][-1] == "phoning: 1 actionable message(s) for alice" and PEER_TEXT not in json.dumps(call)
    mail(home)
    approve = argv_of(tmp_path, ring="notify", notify_cmd=str(exe), rules={"notify": "approve"})
    assert run_rendered(tmp_path, approve) == 0
    assert len(calls(log)) == 1 and len(approvals(home)) == 1  # approval wanted, nothing sent


def test_rendered_muse_keys_run_in_the_real_doorbell(tmp_path: Path, home: Path, world: dict[str, Any]) -> None:
    muse_session(world)
    mail(home)
    argv = argv_of(tmp_path, ring="muse", muse_session="quiet-harbor", muse_bin=world["exe"],
                   muse_home=str(world["store"]), muse_args=["--permission-profile", "strict"],
                   hook_reader="alice-main", resume_min_interval=0)
    assert run_rendered(tmp_path, argv) == 0
    (call,) = [c for c in world["calls"]() if c["kind"] == "exec"]
    a = call["argv"]
    assert a[a.index("--session-id") + 1] == MUSE_SID and a[a.index("--") - 2:a.index("--")] == [
        "--permission-profile", "strict"]
    assert len(state(home)["resumes"]) == 1
    # muse_resume: false renders --muse-no-resume, which the real doorbell honours
    mail(home, subject="q2")
    off = argv_of(tmp_path, ring="muse", muse_session="quiet-harbor", muse_bin=world["exe"],
                  muse_home=str(world["store"]), muse_resume=False, hook_reader="alice-main")
    assert run_rendered(tmp_path, off) == 0
    assert len([c for c in world["calls"]() if c["kind"] == "exec"]) == 1


def test_rendered_resume_keys_are_the_doorbells_own_flags(tmp_path: Path, home: Path) -> None:
    """A rules object and the guardrail numbers survive the trip through argv unchanged."""
    mail(home)
    argv = argv_of(tmp_path, ring="notify", rules={"notify": "prohibit"}, resume_min_interval=5,
                   max_resumes_per_day=2, resume_max_sandbox="read-only", notify_cmd="/nonexistent/osa")
    assert run_rendered(tmp_path, argv) == 0
    assert state(home)["pending"] is False  # `skip: prohibited: notify` cleared it
