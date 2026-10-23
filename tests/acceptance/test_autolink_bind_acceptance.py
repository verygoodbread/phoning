"""Acceptance: bind records harness+thread, `hook targets` prints doorbell targets (docs/spec/autolink.md §1-2)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from phoning import hooks
from phoning.mailbox import Mailbox

ENV_KEYS = ("PHONING_NAME", "PHONING_READER", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
            "MUSE_SESSION_ID", "MUSE_THREAD_ID")
THREAD = "019d9f00-1111-7222-8333-444455556666"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PHONING_SESSIONS", str(tmp_path / "sessions.json"))
    Mailbox(tmp_path / "home" / "alice")
    Mailbox(tmp_path / "home" / "bob")
    return tmp_path / "home"


def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        argv: list[str]) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    rc = hooks.main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def registry(tmp_path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((tmp_path / "sessions.json").read_text())
    return data


def test_bind_detects_codex_and_claude_from_env(home: Path, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice", "--reader", "alice-codex"])
    assert rc == 0
    entry = json.loads(out)
    assert entry["harness"] == "codex" and entry["thread"] == THREAD
    assert entry["name"] == "alice" and entry["reader"] == "alice-codex" and entry["home"] == str(home)
    assert registry(tmp_path)[THREAD] == entry
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-sess-1")
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "bob"])
    entry = json.loads(out)
    assert rc == 0 and entry["harness"] == "claude" and entry["thread"] == "claude-sess-1"
    assert entry["reader"] == "claude-sess-1"  # existing default preserved


def test_bind_detects_muse_env_and_muse_without_env_needs_flags(
        home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("MUSE_SESSION_ID", "muse-s1")
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice"])
    entry = json.loads(out)
    assert rc == 0 and entry["harness"] == "muse" and entry["thread"] == "muse-s1"
    monkeypatch.delenv("MUSE_SESSION_ID")
    rc, out, err = run(monkeypatch, capsys, ["bind", "--name", "alice"])
    assert rc == 2 and out == "" and "--thread" in err  # no env at all: nothing to bind under
    rc, out, _ = run(monkeypatch, capsys,
                     ["bind", "--name", "alice", "--harness", "muse", "--thread", "my-muse-session"])
    entry = json.loads(out)
    assert rc == 0 and entry["harness"] == "muse" and entry["thread"] == "my-muse-session"


def test_explicit_flags_override_detection(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-a")
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice", "--harness", "codex",
                                           "--thread", THREAD])
    entry = json.loads(out)
    assert rc == 0 and entry["harness"] == "codex" and entry["thread"] == THREAD
    assert "sess-a" in registry(tmp_path)  # still keyed by the session id as before
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice", "--thread", "other-thread"])
    entry = json.loads(out)
    assert entry["harness"] == "claude" and entry["thread"] == "other-thread"
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice", "--harness", "unknown"])
    entry = json.loads(out)
    assert entry["harness"] == "unknown" and entry["thread"] is None


def test_detection_fails_closed(home: Path, monkeypatch: pytest.MonkeyPatch,
                                capsys: pytest.CaptureFixture[str]) -> None:
    # no env var holds the id being bound: another session's env must not be adopted
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "my-own-session")
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice", "--session-id", "someone-else"])
    entry = json.loads(out)
    assert rc == 0 and entry["harness"] == "unknown" and entry["thread"] is None
    # two harnesses claim the same id: ambiguous, so unknown
    monkeypatch.setenv("CODEX_THREAD_ID", "my-own-session")
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice"])
    entry = json.loads(out)
    assert rc == 0 and entry["harness"] == "unknown" and entry["thread"] is None
    # codex inside claude: ids differ, the one that is being bound wins
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice", "--session-id", THREAD])
    entry = json.loads(out)
    assert rc == 0 and entry["harness"] == "codex" and entry["thread"] == THREAD


def test_bad_flag_values_are_refused_and_write_nothing(
        home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-a")
    for bad in (["--harness", "gemini"], ["--thread", "a b"], ["--thread", "x;rm -rf /"],
                ["--thread", "../../etc"], ["--thread", "t" * 129]):
        rc, out, err = run(monkeypatch, capsys, ["bind", "--name", "alice", *bad])
        assert rc == 2 and out == "" and err != ""
    assert not (tmp_path / "sessions.json").exists()


def test_existing_bind_behaviour_is_kept(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    rc, _, err = run(monkeypatch, capsys, ["bind", "--name", "alice"])
    assert rc == 2 and err != ""  # no session id
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    rc, _, err = run(monkeypatch, capsys, ["bind", "--name", "nobody"])
    assert rc == 2 and "no mailbox" in err
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice", "--session-id", "../../etc"])
    entry = json.loads(out)
    assert rc == 0 and entry["reader"] == "alice"  # unsafe sid -> reader falls back to name
    assert entry["harness"] == "unknown" and entry["thread"] is None  # env holds a different id
    rc, _, _ = run(monkeypatch, capsys, ["unbind", "--session-id", "../../etc"])
    assert rc == 0
    assert run(monkeypatch, capsys, ["bindings"])[0] == 0


def test_targets_json_for_each_harness(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    other = tmp_path / "other home"
    Mailbox(other / "carol")
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    run(monkeypatch, capsys, ["bind", "--name", "alice", "--reader", "alice-hook"])
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-sess")
    run(monkeypatch, capsys, ["bind", "--name", "bob", "--reader", "bob-hook"])
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    run(monkeypatch, capsys, ["bind", "--name", "carol", "--home", str(other), "--reader", "carol-hook",
                              "--session-id", "muse-sess", "--harness", "muse", "--thread", "muse-thread"])
    rc, out, _ = run(monkeypatch, capsys, ["targets", "--json"])
    assert rc == 0
    targets = json.loads(out)
    assert [t["name"] for t in targets] == ["alice", "bob", "carol"]
    by = {t["name"]: t for t in targets}
    assert by["alice"] == {"session_id": THREAD, "name": "alice", "home": str(home), "harness": "codex",
                           "ring": "codex-queue", "hook_reader": "alice-hook", "codex_thread": THREAD}
    assert by["bob"] == {"session_id": "claude-sess", "name": "bob", "home": str(home),
                         "harness": "claude", "ring": "claude-resume", "hook_reader": "bob-hook",
                         "claude_session": "claude-sess"}
    assert by["carol"] == {"session_id": "muse-sess", "name": "carol", "home": str(other),
                           "harness": "muse", "ring": "muse", "hook_reader": "carol-hook",
                           "muse_session": "muse-thread"}


def test_targets_lists_unready_entries_with_null_ring(home: Path, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    run(monkeypatch, capsys, ["bind", "--name", "alice", "--session-id", "legacy-1",
                              "--harness", "unknown"])
    reg = registry(tmp_path)
    reg["old-no-keys"] = {"name": "bob", "home": str(home), "reader": "r", "bound_at": "2026-01-01T00:00:00Z"}
    reg["garbage"] = "not a dict"
    reg["no-thread"] = {"name": "bob", "home": str(home), "reader": "r", "harness": "codex", "thread": None}
    (tmp_path / "sessions.json").write_text(json.dumps(reg))
    rc, out, _ = run(monkeypatch, capsys, ["targets", "--json"])
    assert rc == 0
    targets = {t["session_id"]: t for t in json.loads(out)}
    assert set(targets) == {"legacy-1", "old-no-keys", "garbage", "no-thread"}
    for t in targets.values():
        assert t["ring"] is None and t["skip"]
        assert not {"codex_thread", "claude_session", "muse_session"} & set(t)
    assert targets["legacy-1"]["skip"] == "unknown harness"
    assert targets["old-no-keys"]["skip"] == "unknown harness"
    assert targets["no-thread"]["skip"] == "no thread"
    assert targets["garbage"]["skip"] == "malformed entry"


def test_targets_text_output_and_empty_registry(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, _ = run(monkeypatch, capsys, ["targets", "--json"])
    assert rc == 0 and json.loads(out) == []
    rc, out, _ = run(monkeypatch, capsys, ["targets"])
    assert rc == 0 and out.strip() == ""
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    run(monkeypatch, capsys, ["bind", "--name", "alice", "--reader", "ah"])
    before = (tmp_path / "sessions.json").read_bytes()
    rc, out, _ = run(monkeypatch, capsys, ["targets"])
    assert rc == 0 and out.count("\n") == 1
    assert out.rstrip("\n").split("\t") == ["alice", "codex", "codex-queue", THREAD, str(home)]
    assert (tmp_path / "sessions.json").read_bytes() == before  # read-only


def test_targets_survives_a_corrupt_registry(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "sessions.json").write_text("{not json")
    rc, out, err = run(monkeypatch, capsys, ["targets", "--json"])
    assert rc == 0 and json.loads(out) == [] and "unreadable" in err
    rc, _, err = run(monkeypatch, capsys, ["targets", "--bogus"])
    assert rc == 2 and err != ""


def test_both_muse_vars_holding_the_id_is_still_one_harness(
        home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("MUSE_SESSION_ID", "muse-same")
    monkeypatch.setenv("MUSE_THREAD_ID", "muse-same")
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice"])
    entry = json.loads(out)
    assert rc == 0 and entry["harness"] == "muse" and entry["thread"] == "muse-same"


def test_empty_session_id_flag_falls_back_to_env_as_before(
        home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    rc, out, _ = run(monkeypatch, capsys, ["bind", "--name", "alice", "--session-id", ""])
    entry = json.loads(out)
    assert rc == 0 and entry["harness"] == "codex" and entry["thread"] == THREAD
    rc, _, _ = run(monkeypatch, capsys, ["unbind", "--session-id", ""])
    assert rc == 0
    rc, out, _ = run(monkeypatch, capsys, ["targets", "--json"])
    assert json.loads(out) == []
