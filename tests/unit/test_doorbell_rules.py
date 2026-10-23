"""Unit tests for the doorbell rule helpers (docs/spec/rules.md §7)."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from phoning import doorbell

TID = "0190abcd-1234-7abc-8def-0123456789ab"


def test_public_names_are_exported() -> None:
    for name in ("parse_rules", "resume_blocked", "resume_policy_args", "build_notify_text", "ring_notify",
                 "approvals_path", "request_approval", "load_approvals", "decide_approval", "RULE_ACTIONS",
                 "RULE_LEVELS", "DEFAULT_RULES"):
        assert name in doorbell.__all__ and hasattr(doorbell, name)


def test_defaults_and_constants() -> None:
    assert doorbell.RULE_ACTIONS == ("ring", "resume", "notify")
    assert doorbell.RULE_LEVELS == ("allow", "approve", "prohibit")
    assert doorbell.DEFAULT_RULES == {"ring": "allow", "resume": "prohibit", "notify": "allow"}
    assert doorbell.parse_rules(None) == doorbell.DEFAULT_RULES


def test_parse_rules_json_file_and_precedence(tmp_path: Path) -> None:
    assert doorbell.parse_rules('{"ring": "approve"}') == {"ring": "approve", "resume": "prohibit", "notify": "allow"}
    f = tmp_path / "r.json"
    f.write_text('{"ring": "prohibit", "notify": "approve"}')
    got = doorbell.parse_rules('{"ring": "allow"}', str(f))
    assert got == {"ring": "allow", "resume": "prohibit", "notify": "approve"}
    assert doorbell.parse_rules(None, str(f))["ring"] == "prohibit"


def test_parse_rules_resume_opt_in_only_when_resume_is_unnamed(tmp_path: Path) -> None:
    assert doorbell.parse_rules(None, resume_opt_in=True)["resume"] == "allow"
    assert doorbell.parse_rules('{"resume": "approve"}', resume_opt_in=True)["resume"] == "approve"
    assert doorbell.parse_rules('{"resume": "prohibit"}', resume_opt_in=True)["resume"] == "prohibit"
    f = tmp_path / "r.json"
    f.write_text('{"resume": "prohibit"}')
    assert doorbell.parse_rules(None, str(f), resume_opt_in=True)["resume"] == "prohibit"


@pytest.mark.parametrize("bad", ['{"ring": "yes"}', '{"fly": "allow"}', "[]", '"allow"', "{", '{"ring": 1}', '{"ring": null}', ""])
def test_parse_rules_rejects_bad_input(bad: str) -> None:
    if bad == "":
        assert doorbell.parse_rules(bad) == doorbell.DEFAULT_RULES  # an empty flag is "not given"
        return
    with pytest.raises(ValueError):
        doorbell.parse_rules(bad)


def test_parse_rules_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        doorbell.parse_rules(None, str(tmp_path / "nope.json"))


def _room(mentions: Any, reply_to: str | None = None, typ: str = "chat") -> dict[str, Any]:
    env: dict[str, Any] = {"type": typ, "room": "ops", "reply_to": reply_to, "data": {"mentions": mentions}}
    return {"room": "ops", "env": env}


def test_is_actionable_room_rules() -> None:
    types = {"ask", "propose", "reject"}
    assert doorbell.is_actionable(_room(["alice"]), types, set(), "alice")
    assert not doorbell.is_actionable(_room(["bob"]), types, set(), "alice")
    assert not doorbell.is_actionable(_room(["alice"]), types, set(), None)
    assert not doorbell.is_actionable(_room("alice"), types, set(), "alice")
    assert not doorbell.is_actionable(_room([None, 3]), types, set(), "alice")
    assert not doorbell.is_actionable(_room([], typ="ask"), types, set(), "alice")  # types don't apply in rooms
    assert doorbell.is_actionable(_room([], reply_to="mine"), types, {"mine"}, "alice")
    assert doorbell.is_actionable(_room([], reply_to="mine"), types, {"mine"})  # replies need no name


def test_is_actionable_direct_rules_unchanged() -> None:
    types = {"ask"}
    direct_ask = {"env": {"type": "ask", "reply_to": None, "room": None}}
    direct_chat = {"env": {"type": "chat", "reply_to": None, "room": None, "data": {"mentions": ["alice"]}}}
    assert doorbell.is_actionable(direct_ask, types, set(), "alice")
    assert not doorbell.is_actionable(direct_chat, types, set(), "alice")
    assert doorbell.is_actionable(direct_chat | {"env": {**direct_chat["env"], "reply_to": "m"}}, types, {"m"}, "alice")


def test_resume_blocked() -> None:
    now = 1_000_000.0
    assert doorbell.resume_blocked([], now, 600, 6) is None
    assert doorbell.resume_blocked([now - 601], now, 600, 6) is None
    reason = doorbell.resume_blocked([now - 30], now, 600, 6)
    assert reason is not None and "min interval 600" in reason
    spaced = [now - 700 * (i + 1) for i in range(6)]
    cap = doorbell.resume_blocked(spaced, now, 600, 6)
    assert cap is not None and "daily cap 6" in cap
    assert doorbell.resume_blocked(spaced[:5], now, 600, 6) is None
    assert doorbell.resume_blocked([now - 90000] * 10, now, 600, 6) is None  # older than 24 h
    zero = doorbell.resume_blocked([], now, 0, 0)
    assert zero is not None and "daily cap 0" in zero
    assert doorbell.resume_blocked([now - 1], now, 0, 6) is None


def _rollout(tmp: Path, lines: list[Any], tid: str = TID) -> Path:
    d = tmp / "sessions" / "2026" / "10" / "11"
    d.mkdir(parents=True)
    (d / f"rollout-2026-10-11T00-00-00-{tid}.jsonl").write_text(
        "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n")
    return tmp


def _tc(sandbox: Any, approval: Any) -> dict[str, Any]:
    return {"type": "turn_context", "payload": {"sandbox_policy": sandbox, "approval_policy": approval}}


def test_resume_policy_args_exact_shape(tmp_path: Path) -> None:
    home = _rollout(tmp_path, [_tc({"type": "workspace-write"}, "on-request")])
    assert doorbell.resume_policy_args(str(home), TID) == (
        ["-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="on-request"'], True)


def test_resume_policy_args_unknown_is_read_only_never(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert doorbell.resume_policy_args(str(empty), TID) == (
        ["-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"'], False)
    home = _rollout(tmp_path / "x", [{"type": "session_meta", "payload": {}}, "garbage{"])
    assert doorbell.resume_policy_args(str(home), TID)[1] is False


def test_resume_policy_args_last_wins_and_cap_only_tightens(tmp_path: Path) -> None:
    home = _rollout(tmp_path, [_tc({"type": "read-only"}, "never"), _tc({"type": "danger-full-access"}, "never")])
    assert doorbell.resume_policy_args(str(home), TID)[0][1] == 'sandbox_mode="danger-full-access"'
    assert doorbell.resume_policy_args(str(home), TID, "workspace-write")[0][1] == 'sandbox_mode="workspace-write"'
    assert doorbell.resume_policy_args(str(home), TID, "read-only")[0][1] == 'sandbox_mode="read-only"'
    ro = _rollout(tmp_path / "ro", [_tc({"type": "read-only"}, "never")])
    assert doorbell.resume_policy_args(str(ro), TID, "danger-full-access")[0][1] == 'sandbox_mode="read-only"'


@pytest.mark.parametrize("sandbox,approval", [
    ({"type": "external-sandbox"}, "never"), ({"type": "read-only"}, "bogus"), ({"type": "read-only"}, None),
    (None, "never"), ({}, "never"), (["workspace-write"], "never"), (7, "never"),
])
def test_resume_policy_args_partial_or_unrecognised_is_unknown(tmp_path: Path, sandbox: Any, approval: Any) -> None:
    home = _rollout(tmp_path, [_tc(sandbox, approval)])
    args, known = doorbell.resume_policy_args(str(home), TID)
    assert known is False and args == ["-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"']


def test_build_notify_text() -> None:
    assert doorbell.build_notify_text("dana", 3) == "phoning: 3 actionable message(s) for dana"
    assert doorbell.build_notify_text("dana", 1) == "phoning: 1 actionable message(s) for dana"


def _fake_osa(tmp: Path, rc: int = 0) -> tuple[Path, Path]:
    log = tmp / "osa.jsonl"
    exe = tmp / "osascript"
    exe.write_text(f"#!/usr/bin/env python3\nimport json,sys\nopen({str(log)!r},'a').write(json.dumps(sys.argv[1:])+'\\n')\nsys.exit({rc})\n")
    exe.chmod(0o755)
    return exe, log


def test_ring_notify_passes_the_text_as_one_argv_item(tmp_path: Path) -> None:
    exe, log = _fake_osa(tmp_path)
    assert doorbell.ring_notify('x"; do shell script "id', 2, str(exe)) == "rang: notified"
    argv = json.loads(log.read_text().splitlines()[0])
    assert argv[-2:] == ["--", 'phoning: 2 actionable message(s) for x"; do shell script "id']
    assert all('shell script' not in part for part in argv[:-1])
    assert argv[0] == "-e"


def test_ring_notify_failures(tmp_path: Path) -> None:
    exe, _ = _fake_osa(tmp_path, rc=3)
    assert doorbell.ring_notify("a", 1, str(exe)) == "ring failed: rc=3"
    assert doorbell.ring_notify("a", 1, str(tmp_path / "missing")).startswith("ring failed:")


def test_approvals_roundtrip_dedupe_and_modes(tmp_path: Path) -> None:
    user = tmp_path / "alice"
    user.mkdir()
    target = {"ring": "claude-resume", "claude_session": "sid", "count": 1}
    aid = doorbell.request_approval(str(user), "resume", target)
    assert len(aid) == 8 and all(c in "0123456789abcdef" for c in aid)
    path = doorbell.approvals_path(str(user))
    assert path == str(user / "doorbell-approvals.jsonl")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert doorbell.request_approval(str(user), "resume", {**target, "count": 5}) == aid  # dedupe ignores count
    other = doorbell.request_approval(str(user), "ring", target)
    assert other != aid
    got = doorbell.load_approvals(str(user))
    assert set(got) == {aid, other}
    assert got[aid]["status"] == "pending" and got[aid]["action"] == "resume" and got[aid]["target"] == target
    doorbell.decide_approval(str(user), aid, "approved")
    assert doorbell.load_approvals(str(user))[aid]["status"] == "approved"
    assert doorbell.load_approvals(str(user))[aid]["action"] == "resume"  # a decision keeps the request's fields
    assert doorbell.load_approvals(str(user))[aid]["target"] == target
    # a decided record no longer dedupes: the same request gets a fresh id
    assert doorbell.request_approval(str(user), "resume", target) not in (aid, other)
    doorbell.decide_approval(str(user), other, "denied")
    assert doorbell.load_approvals(str(user))[other]["status"] == "denied"


def test_load_approvals_skips_garbage_and_missing_file(tmp_path: Path) -> None:
    user = tmp_path / "bob"
    user.mkdir()
    assert doorbell.load_approvals(str(user)) == {}
    aid = doorbell.request_approval(str(user), "notify", {"ring": "notify", "count": 1})
    with open(doorbell.approvals_path(str(user)), "a", encoding="utf-8") as fh:
        fh.write("not json\n[1,2]\n{\"id\": 5}\n\n")
    assert list(doorbell.load_approvals(str(user))) == [aid]


def test_state_roundtrip_keeps_new_keys(tmp_path: Path) -> None:
    path = str(tmp_path / "doorbell-state.json")
    now = time.time()
    doorbell._save_state(path, {"last_ring": 1.0, "pending": True, "introduced": ["x"], "resumes": [now], "pending_count": 4})
    loaded = doorbell._load_state(path)
    assert loaded["resumes"] == [now] and loaded["pending_count"] == 4 and loaded["pending"] is True
    Path(path).write_text(json.dumps({"resumes": "x", "pending_count": "y", "last_ring": 2.0}))
    loaded = doorbell._load_state(path)
    assert loaded["resumes"] == [] and loaded["pending_count"] == 0 and loaded["last_ring"] == 2.0
