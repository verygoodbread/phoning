"""Acceptance: `codex exec resume` inherits the thread's sandbox, never looser (docs/spec/rules.md §3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.test_rules_common import (
    PEER_TEXT,
    TID,
    bell,
    calls,
    cfg_pairs,
    codex_home_with_thread,
    effective_sandbox,
    fake_codex,
    home_fixture,  # noqa: F401  (registers the `home` fixture)
    mail,
)


def ctx(sandbox: Any, approval: Any = "never") -> dict[str, Any]:
    out: dict[str, Any] = {"cwd": "/x", "model": "m"}
    if sandbox is not None:
        out["sandbox_policy"] = sandbox
    if approval is not None:
        out["approval_policy"] = approval
    return out


def resume(home: Path, tmp: Path, contexts: list[dict[str, Any]] | None, *extra: str, raw_tail: str = "",
           ) -> tuple[list[str], Path]:
    """Resume a dead thread whose rollout holds ``contexts``; return the fake codex's argv and the workdir."""
    exe, log = fake_codex(tmp)
    chome, workdir = codex_home_with_thread(tmp, locked=False, contexts=contexts, raw_tail=raw_tail)
    mail(home, subject=PEER_TEXT)
    assert bell("--ring", "codex-queue", "--codex-thread", TID, "--codex-bin", str(exe), "--codex-home", str(chome),
                "--resume-dead", "--min-interval", "0", *extra) == 0
    got = calls(log)
    assert len(got) == 1 and got[0]["argv"][0] == "exec", got
    argv: list[str] = got[0]["argv"]
    # the pinned positions of the existing contract
    assert argv[:7] == ["exec", "--skip-git-repo-check", "-C", str(workdir), "resume", TID, argv[6]]
    assert argv[6].startswith("[phoning]") and PEER_TEXT not in " ".join(argv)
    for banned in ("--dangerously-bypass-approvals-and-sandbox", "--full-auto", "--yolo"):
        assert banned not in argv
    return argv, workdir


def test_inherits_the_recorded_sandbox_and_approval(home: Path, tmp_path: Path) -> None:
    argv, _ = resume(home, tmp_path, [ctx({"type": "workspace-write", "writable_roots": ["/x"]}, "on-request")])
    assert effective_sandbox(argv) == "workspace-write"
    assert cfg_pairs(argv)["approval_policy"] == "on-request"


def test_the_last_turn_context_wins(home: Path, tmp_path: Path) -> None:
    argv, _ = resume(home, tmp_path, [ctx({"type": "danger-full-access"}, "never"), ctx({"type": "read-only"}, "untrusted")])
    assert effective_sandbox(argv) == "read-only" and cfg_pairs(argv)["approval_policy"] == "untrusted"


def test_a_bare_string_policy_is_accepted(home: Path, tmp_path: Path) -> None:
    argv, _ = resume(home, tmp_path, [ctx("workspace-write", "on-failure")])
    assert effective_sandbox(argv) == "workspace-write" and cfg_pairs(argv)["approval_policy"] == "on-failure"


def test_no_turn_context_means_read_only_and_never(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv, _ = resume(home, tmp_path, None)
    assert effective_sandbox(argv) == "read-only" and cfg_pairs(argv)["approval_policy"] == "never"
    assert "resume: sandbox unknown, using read-only" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [
    ctx({"type": "external-sandbox"}, "never"),
    ctx({"type": "danger-full-access"}, "sometimes"),
    ctx({"type": "danger-full-access"}, None),
    ctx(None, "on-request"),
    ctx({"mode": "workspace-write"}, "never"),
    ctx(42, "never"),
])
def test_unrecognised_or_partial_policy_is_unknown(home: Path, tmp_path: Path, bad: dict[str, Any],
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    argv, _ = resume(home, tmp_path, [bad])
    assert effective_sandbox(argv) == "read-only" and cfg_pairs(argv)["approval_policy"] == "never"
    assert "resume: sandbox unknown, using read-only" in capsys.readouterr().err


def test_malformed_rollout_lines_are_unknown_not_a_crash(home: Path, tmp_path: Path) -> None:
    argv, _ = resume(home, tmp_path, None, raw_tail='{"type": "turn_context", "payload": \n not json at all\n')
    assert effective_sandbox(argv) == "read-only"


def test_max_sandbox_clamps_but_never_loosens(home: Path, tmp_path: Path) -> None:
    argv, _ = resume(home, tmp_path / "a", [ctx({"type": "danger-full-access"}, "never")], "--resume-max-sandbox", "workspace-write")
    assert effective_sandbox(argv) == "workspace-write"
    argv, _ = resume(home, tmp_path / "b", [ctx({"type": "read-only"}, "never")], "--resume-max-sandbox", "workspace-write",
                     "--resume-min-interval", "0")
    assert effective_sandbox(argv) == "read-only"  # recorded is stricter than the cap: kept


def test_a_turn_context_beyond_the_scan_limit_is_unknown(home: Path, tmp_path: Path) -> None:
    filler = ('{"type": "event_msg", "payload": {"type": "noise", "pad": "' + "x" * 1000 + '"}}\n') * 9000  # ~9 MB
    argv, _ = resume(home, tmp_path, [ctx({"type": "danger-full-access"}, "never")], raw_tail=filler)
    assert effective_sandbox(argv) == "read-only"  # never reads the whole rollout; unknown fails closed


def test_a_turn_context_near_the_end_of_a_big_rollout_is_found(home: Path, tmp_path: Path) -> None:
    filler = ('{"type": "event_msg", "payload": {"type": "noise", "pad": "' + "x" * 1000 + '"}}\n') * 3000  # ~3 MB
    last = '{"type": "turn_context", "payload": {"sandbox_policy": {"type": "workspace-write"}, "approval_policy": "never"}}\n'
    argv, _ = resume(home, tmp_path, None, raw_tail=filler + last)
    assert effective_sandbox(argv) == "workspace-write"
