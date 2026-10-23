"""Unit tests for the autolink extensions in phoning.hooks (docs/spec/autolink.md)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from phoning import hooks
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import inbound, outbound

FP = "a3f1c2d4e5b60718293a4b5c6d7e8f90"
THREAD = "019d9f00-1111-7222-8333-444455556666"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k in ("PHONING_NAME", "PHONING_READER", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
              "MUSE_SESSION_ID", "MUSE_THREAD_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PHONING_SESSIONS", str(tmp_path / "sessions.json"))
    Mailbox(tmp_path / "home" / "alice")
    return tmp_path / "home"


def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        argv: list[str], event: dict[str, Any] | None = None) -> tuple[int, str, str]:
    """Run ``hooks.main`` with a patched stdin, returning ``(rc, out, err)``."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event or {})))
    rc = hooks.main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def stop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], extra: list[str] | None = None
) -> tuple[int, str, str]:
    """Run a Stop event for ``alice``/reader ``r``."""
    return run(monkeypatch, capsys, ["stop", "--name", "alice", "--reader", "r", *(extra or [])])


# --------------------------------------------------------------------------
# Harness detection precedence and fail-closed cases
# --------------------------------------------------------------------------


def test_detect_harness_matches_only_the_id_being_bound(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    monkeypatch.setenv("CODEX_THREAD_ID", "other")
    assert hooks._detect_harness("s1") == "claude"
    assert hooks._detect_harness("other") == "codex"
    assert hooks._detect_harness("nope") == "unknown"
    assert hooks._detect_harness(None) == "unknown"
    assert hooks._detect_harness("") == "unknown"


def test_detect_harness_fails_closed_on_ambiguity_and_unsafe_ids(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # two Muse vars holding the same id is still one harness (dedupe by harness)
    monkeypatch.setenv("MUSE_SESSION_ID", "shared")
    monkeypatch.setenv("MUSE_THREAD_ID", "shared")
    assert hooks._detect_harness("shared") == "muse"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "shared")
    assert hooks._detect_harness("shared") == "unknown"
    monkeypatch.delenv("MUSE_SESSION_ID")
    monkeypatch.delenv("MUSE_THREAD_ID")
    # an unsafe id is never guessed from
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "with space")
    assert hooks._detect_harness("with space") == "unknown"


def test_session_id_extended_order_and_legacy_helper(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert hooks._session_id_from_env() is None
    assert hooks._session_id_from_env_extended() is None
    monkeypatch.setenv("MUSE_THREAD_ID", "m-thread")
    assert hooks._session_id_from_env() is None  # legacy helper ignores muse
    assert hooks._session_id_from_env_extended() == "m-thread"
    monkeypatch.setenv("CODEX_THREAD_ID", "cx")
    assert hooks._session_id_from_env_extended() == "cx"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cl")
    assert hooks._session_id_from_env_extended() == "cl"


# --------------------------------------------------------------------------
# Thread regex
# --------------------------------------------------------------------------


def test_thread_regex_boundaries() -> None:
    assert hooks._is_valid_id("a")
    assert hooks._is_valid_id("t" * 128)
    assert hooks._is_valid_id("a_b-c.d")
    assert not hooks._is_valid_id("t" * 129)
    assert not hooks._is_valid_id("")
    assert not hooks._is_valid_id("a b")
    assert not hooks._is_valid_id("a/b")
    assert not hooks._is_valid_id("a\tb")


def test_bind_rejects_bad_thread_and_harness_without_writing(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    for bad in (["--thread", "a b"], ["--thread", ""], ["--thread", "t" * 129],
                ["--harness", "gemini"]):
        rc, out, err = run(monkeypatch, capsys, ["bind", "--name", "alice", *bad])
        assert rc == 2 and out == "" and err != ""
    assert not (tmp_path / "sessions.json").exists()


# --------------------------------------------------------------------------
# Bind stores harness/thread; unsafe env ids fail closed but still bind
# --------------------------------------------------------------------------


def test_bind_stores_unknown_for_unsafe_session_id(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice", "--session-id", "a b"])
    entry = json.loads(out)
    assert rc == 0 and entry["harness"] == "unknown" and entry["thread"] is None
    assert entry["reader"] == "alice"


def test_bind_thread_flag_supplies_session_id(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, _ = run(monkeypatch, capsys,
                     ["bind", "--name", "alice", "--harness", "muse", "--thread", THREAD])
    entry = json.loads(out)
    assert rc == 0 and entry["harness"] == "muse" and entry["thread"] == THREAD


def test_unbind_uses_extended_session_id_order(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MUSE_SESSION_ID", "muse-s1")
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice"])
    assert rc == 0 and json.loads(out)["harness"] == "muse"
    rc, _, _ = run(monkeypatch, capsys, ["unbind"])
    assert rc == 0
    assert "muse-s1" not in json.loads((tmp_path / "sessions.json").read_text())


# --------------------------------------------------------------------------
# targets formatting
# --------------------------------------------------------------------------


def _seed(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
          capsys: pytest.CaptureFixture[str],
          entries: dict[str, Any]) -> None:
    (tmp_path / "sessions.json").write_text(json.dumps(entries))
    capsys.readouterr()


def test_targets_sort_order_and_ready_shapes(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(home, tmp_path, monkeypatch, capsys, {
        "z-b": {"name": "bob", "home": str(home), "reader": "r", "harness": "codex", "thread": "T-z"},
        "a-b": {"name": "bob", "home": str(home), "reader": "r", "harness": "codex", "thread": "T-a"},
        "a-a": {"name": "alice", "home": str(home), "reader": "r", "harness": "claude", "thread": "T-cl"},
    })
    rc, out, _ = run(monkeypatch, capsys, ["targets", "--json"])
    assert rc == 0
    targets = json.loads(out)
    assert [(t["name"], t["session_id"]) for t in targets] == [
        ("alice", "a-a"), ("bob", "a-b"), ("bob", "z-b"),
    ]
    assert list(targets[0].keys()) == [
        "session_id", "name", "home", "harness", "ring", "hook_reader", "claude_session",
    ]
    assert targets[1]["codex_thread"] == "T-a" and targets[2]["codex_thread"] == "T-z"


def test_targets_text_columns_and_skip_placeholder(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(home, tmp_path, monkeypatch, capsys, {
        "ok": {"name": "alice", "home": str(home), "reader": "ah", "harness": "muse", "thread": "MT"},
        "no-thread": {"name": "bob", "home": str(home), "reader": "bh", "harness": "codex",
                      "thread": None},
        "bad": "nope",
    })
    rc, out, _ = run(monkeypatch, capsys, ["targets"])
    assert rc == 0
    lines = out.rstrip("\n").split("\n")
    assert sorted(lines) == sorted([
        f"alice\tmuse\tmuse\tMT\t{home}",
        f"bob\tcodex\t-\t-\t{home}",
        "-\tunknown\t-\t-\t-",
    ])


def test_targets_non_dict_keeps_session_id(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(home, tmp_path, monkeypatch, capsys, {"garbage": ["not", "a", "dict"]})
    rc, out, _ = run(monkeypatch, capsys, ["targets", "--json"])
    assert rc == 0
    (t,) = json.loads(out)
    assert t["session_id"] == "garbage" and t["name"] is None and t["harness"] is None
    assert t["ring"] is None and t["skip"] == "malformed entry"


def test_targets_missing_name_or_home_is_malformed(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(home, tmp_path, monkeypatch, capsys, {
        "n": {"home": str(home), "reader": "r", "harness": "codex", "thread": "T"},
        "h": {"name": "alice", "reader": "r", "harness": "codex", "thread": "T"},
    })
    rc, out, _ = run(monkeypatch, capsys, ["targets", "--json"])
    assert rc == 0
    for t in json.loads(out):
        assert t["ring"] is None and t["skip"] == "malformed entry"


# --------------------------------------------------------------------------
# Mention matching: edge cases and the lazy own-eid scan
# --------------------------------------------------------------------------


def test_mentions_helper_rejects_non_list_and_non_string() -> None:
    assert hooks._mentions_name({"data": {"mentions": ["alice"]}}, "alice")
    assert not hooks._mentions_name({"data": {"mentions": "alice"}}, "alice")
    assert not hooks._mentions_name({"data": {"mentions": [["alice"]]}}, "alice")
    assert not hooks._mentions_name({"data": {"mentions": ["ALICE"]}}, "alice")
    assert not hooks._mentions_name({"data": {"mentions": []}}, "alice")
    assert not hooks._mentions_name({"data": {}}, "alice")
    assert not hooks._mentions_name({}, "alice")
    assert not hooks._mentions_name({"data": {"mentions": ["alice"]}}, None)


def test_own_eid_scan_only_runs_when_needed(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    box = Mailbox(home / "alice")
    calls = {"n": 0}
    original = hooks._own_eids

    def counting(b: Mailbox) -> set[str]:
        calls["n"] += 1
        return original(b)

    monkeypatch.setattr(hooks, "_own_eids", counting)
    # a room record that is not a block type never triggers the scan
    inbound(box, "bob", FP, "chat", "not actionable", room="ops")
    assert stop(monkeypatch, capsys) == (0, "", "")
    assert calls["n"] == 0
    # a mentioned room ask is actionable without the scan
    inbound(box, "bob", FP, "ask", "room ask, mentioned", room="ops",
            data={"mentions": ["alice"]})
    _, out, _ = stop(monkeypatch, capsys)
    assert json.loads(out)["decision"] == "block"
    assert calls["n"] == 0
    # an unmentioned room ask of a block type triggers exactly one scan; with
    # no matching own eid and no reply_to it does not block and commits nothing
    inbound(box, "bob", FP, "ask", "room ask, no mention", room="ops")
    rc, out, _ = stop(monkeypatch, capsys)
    assert rc == 0 and out == "" and calls["n"] == 1
    # a reply_to pointing at one of our own sends blocks, and rescans once
    mine = outbound(box, "alice", "ops", "ask", "q", room="ops")
    inbound(box, "bob", FP, "reject", "reply to us", room="ops",
            reply_to=mine["env"]["eid"])
    rc, out, _ = stop(monkeypatch, capsys)
    assert rc == 0 and json.loads(out)["decision"] == "block" and calls["n"] == 2


def test_room_reply_to_our_own_send_is_actionable(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    box = Mailbox(home / "alice")
    mine = outbound(box, "alice", "ops", "ask", "q", room="ops")
    inbound(box, "bob", FP, "reject", "wrong place", room="ops", reply_to=mine["env"]["eid"])
    rc, out, _ = stop(monkeypatch, capsys)
    assert rc == 0 and json.loads(out)["decision"] == "block"


def test_own_eid_scan_never_runs_for_room_records_that_mention_us(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    box = Mailbox(home / "alice")
    monkeypatch.setattr(hooks, "_own_eids",
                        lambda b: (_ for _ in ()).throw(RuntimeError("should not read")))
    inbound(box, "bob", FP, "ask", "for alice", room="ops", data={"mentions": ["alice"]})
    rc, out, _ = stop(monkeypatch, capsys)
    assert rc == 0 and json.loads(out)["decision"] == "block"


def test_room_record_with_no_own_sends_never_blocks(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    box = Mailbox(home / "alice")
    inbound(box, "bob", FP, "reject", "reply to nothing", room="ops", reply_to="e" * 32)
    assert stop(monkeypatch, capsys) == (0, "", "")
    assert box.cursor("r") == 0


def test_direct_records_block_regardless_of_scan(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    box = Mailbox(home / "alice")
    monkeypatch.setattr(hooks, "_own_eids",
                        lambda b: (_ for _ in ()).throw(RuntimeError("should not read")))
    inbound(box, "bob", FP, "ask", "direct ask, no room")
    rc, out, _ = stop(monkeypatch, capsys)
    assert rc == 0 and json.loads(out)["decision"] == "block"


def test_has_actionable_keeps_the_two_argument_helper_form() -> None:
    recs: list[dict[str, Any]] = [{"env": {"type": "ask"}}, {"env": {"type": "chat"}}]
    assert hooks._has_actionable(recs, {"ask"})
    assert not hooks._has_actionable(recs, {"status"})
    assert not hooks._has_actionable(recs, set())
    assert not hooks._has_actionable([{"env": {"type": "ask"}, "room": "ops"}], {"ask"})


# --------------------------------------------------------------------------
# w-hooks-2 fixes: harness dedupe by harness name + empty --session-id
# --------------------------------------------------------------------------


def test_detect_harness_dedupes_by_harness_name(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both Muse vars holding the same id is still one harness.
    monkeypatch.setenv("MUSE_SESSION_ID", "muse-same")
    monkeypatch.setenv("MUSE_THREAD_ID", "muse-same")
    assert hooks._detect_harness("muse-same") == "muse"
    # Two different harnesses holding the id is still unknown.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "muse-same")
    assert hooks._detect_harness("muse-same") == "unknown"
    monkeypatch.setenv("CODEX_THREAD_ID", "muse-same")
    assert hooks._detect_harness("muse-same") == "unknown"
    # Back to a single harness once the other vars move away.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other")
    monkeypatch.setenv("CODEX_THREAD_ID", "other")
    assert hooks._detect_harness("muse-same") == "muse"


def test_bind_both_muse_vars_holding_the_id_is_still_muse(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MUSE_SESSION_ID", "muse-same")
    monkeypatch.setenv("MUSE_THREAD_ID", "muse-same")
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice"])
    entry = json.loads(out)
    assert rc == 0 and entry["harness"] == "muse" and entry["thread"] == "muse-same"


def test_bind_empty_session_id_falls_back_to_env(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice", "--session-id", ""])
    entry = json.loads(out)
    assert rc == 0 and entry["harness"] == "codex" and entry["thread"] == THREAD
    rc, _, _ = run(monkeypatch, capsys, ["unbind", "--session-id", ""])
    assert rc == 0
    rc, out, _ = run(monkeypatch, capsys, ["targets", "--json"])
    assert rc == 0 and json.loads(out) == []


def test_bind_empty_thread_stays_a_usage_error(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-a")
    rc, out, err = run(monkeypatch, capsys, ["bind", "--name", "alice", "--thread", ""])
    assert rc == 2 and out == "" and err != ""
    assert not (tmp_path / "sessions.json").exists()
