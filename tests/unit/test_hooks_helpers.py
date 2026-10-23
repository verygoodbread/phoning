"""Unit tests for :mod:`phoning.hooks`."""

from __future__ import annotations

import io
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from phoning import hooks


def _clear_hook_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove hook-related env vars so each test starts inert."""
    for key in (
        "PHONING_HOME",
        "PHONING_NAME",
        "PHONING_READER",
        "PHONING_SESSIONS",
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_THREAD_ID",
    ):
        monkeypatch.delenv(key, raising=False)


def _isolate_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> str:
    """Point the session registry at a temp file and return its path."""
    path = str(tmp_path / "sessions.json")
    monkeypatch.setenv("PHONING_SESSIONS", path)
    return path


def _make_log(tmp_path: Path, name: str) -> str:
    """Create an empty mailbox log for ``name`` and return the home dir."""
    home = str(tmp_path / "home")
    log = Path(home) / name / "mailbox" / "log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    return home


class _EmptyBox:
    """Minimal Mailbox stub with no unread records."""

    def __init__(self, user_dir: str) -> None:
        self.user_dir = user_dir

    def peek(self, reader: str, limit: int) -> list[dict[str, Any]]:
        _ = (reader, limit)
        return []

    def commit(self, reader: str, seq: int) -> None:
        _ = (reader, seq)


def test_is_valid_reader_accepts_simple_names() -> None:
    """Common reader names match the grammar."""
    assert hooks._is_valid_reader("hook")
    assert hooks._is_valid_reader("sess-abc_123.X")


def test_is_valid_reader_rejects_bad_names() -> None:
    """Empty, long and punctuation-heavy names are invalid."""
    assert not hooks._is_valid_reader("")
    assert not hooks._is_valid_reader("a" * 65)
    assert not hooks._is_valid_reader("has space")
    assert not hooks._is_valid_reader("semi;colon")


def test_resolve_home_prefers_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``--home`` wins over the environment."""
    _clear_hook_env(monkeypatch)
    monkeypatch.setenv("PHONING_HOME", "/tmp/env-home")
    assert hooks._resolve_home("~/x") == str(Path("~/x").expanduser())


def test_resolve_home_falls_back_to_env_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env home wins; otherwise the default under ``~/.phoning`` is used."""
    _clear_hook_env(monkeypatch)
    monkeypatch.setenv("PHONING_HOME", "~/env-home")
    assert hooks._resolve_home(None) == str(Path("~/env-home").expanduser())
    monkeypatch.delenv("PHONING_HOME", raising=False)
    assert hooks._resolve_home(None).endswith(".phoning")


def test_resolve_name_prefers_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit name wins over ``$PHONING_NAME``."""
    _clear_hook_env(monkeypatch)
    monkeypatch.setenv("PHONING_NAME", "env-name")
    assert hooks._resolve_name("flag-name") == "flag-name"
    assert hooks._resolve_name(None) == "env-name"


def test_resolve_name_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No flag and no env yields ``None``."""
    _clear_hook_env(monkeypatch)
    assert hooks._resolve_name(None) is None


def test_resolve_reader_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit beats env beats session id beats ``hook``."""
    _clear_hook_env(monkeypatch)
    assert hooks._resolve_reader("flag", {"session_id": "sess"}) == "flag"
    monkeypatch.setenv("PHONING_READER", "env-r")
    assert hooks._resolve_reader(None, {"session_id": "sess"}) == "env-r"
    monkeypatch.delenv("PHONING_READER", raising=False)
    assert hooks._resolve_reader(None, {"session_id": "sess"}) == "sess"
    assert hooks._resolve_reader(None, {}) == "hook"


def test_has_explicit_or_env_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag or env counts as an explicit reader."""
    _clear_hook_env(monkeypatch)
    assert hooks._has_explicit_or_env_reader("r")
    assert not hooks._has_explicit_or_env_reader(None)
    monkeypatch.setenv("PHONING_READER", "r")
    assert hooks._has_explicit_or_env_reader(None)


def test_parse_block_types_defaults_and_custom() -> None:
    """Default set, blank string and comma lists all parse."""
    assert hooks._parse_block_types(None) == {"ask", "propose", "reject"}
    assert hooks._parse_block_types("") == set()
    assert hooks._parse_block_types("  ") == set()
    assert hooks._parse_block_types("ask, note") == {"ask", "note"}


def test_read_event_empty_and_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty stdin and bad JSON become ``{}``."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert hooks._read_event() == {}
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert hooks._read_event() == {}
    monkeypatch.setattr(sys, "stdin", io.StringIO("[1,2]"))
    assert hooks._read_event() == {}


def test_read_event_valid_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A JSON object on stdin is returned as a dict."""
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id": "s"}'))
    assert hooks._read_event() == {"session_id": "s"}


def test_executable_prefix_prefers_which(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shutil.which`` result wins over the module fallback."""
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/phoning")
    assert hooks._executable_prefix() == ["/usr/local/bin/phoning"]
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert hooks._executable_prefix() == [sys.executable, "-m", "phoning.cli"]


def test_build_command_identity_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None`` fields are omitted for the identity-free install."""
    monkeypatch.setattr(hooks, "_executable_prefix", lambda: ["phoning"])
    cmd = hooks._build_command("session-start", None, None, None)
    assert cmd == "phoning hook session-start"


def test_build_command_with_identity_and_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name/home/reader flags round-trip plus optional ``--arm-waker``."""
    monkeypatch.setattr(hooks, "_executable_prefix", lambda: ["phoning"])
    cmd = hooks._build_command("stop", "amy", "/h", "r1", arm_waker=True)
    assert "--name amy" in cmd
    assert "--reader r1" in cmd
    assert "--arm-waker" in cmd


def test_toml_quote_literal_and_basic() -> None:
    """Plain values use literals; quotes and newlines use basic strings."""
    assert hooks._toml_quote("abc") == "'abc'"
    assert json.loads(hooks._toml_quote("a'b")) == "a'b"
    assert json.loads(hooks._toml_quote("a\nb")) == "a\nb"


def test_error_line_single_line() -> None:
    """Newlines collapse and empty messages fall back to class name."""
    assert hooks._error_line(ValueError("a\nb")) == "phoning hook: a b"
    assert hooks._error_line(ValueError("")) == "phoning hook: ValueError"


def test_sessions_path_env_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env override wins over the XDG-style default."""
    _clear_hook_env(monkeypatch)
    monkeypatch.setenv("PHONING_SESSIONS", "~/s.json")
    assert hooks._sessions_path() == str(Path("~/s.json").expanduser())
    monkeypatch.delenv("PHONING_SESSIONS", raising=False)
    assert hooks._sessions_path().endswith("sessions.json")


def test_load_registry_missing_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing registry file loads as ``{}``."""
    _clear_hook_env(monkeypatch)
    _isolate_registry(monkeypatch, tmp_path)
    assert hooks._load_registry() == {}


def test_load_registry_corrupt_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Corrupt JSON warns on stderr and loads as ``{}``."""
    _clear_hook_env(monkeypatch)
    path = _isolate_registry(monkeypatch, tmp_path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("not json", encoding="utf-8")
    assert hooks._load_registry() == {}
    assert "unreadable" in capsys.readouterr().err


def test_save_and_load_registry_roundtrip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Saved bindings load back unchanged."""
    _clear_hook_env(monkeypatch)
    _isolate_registry(monkeypatch, tmp_path)
    hooks._save_registry({"s1": {"name": "amy"}})
    assert hooks._load_registry() == {"s1": {"name": "amy"}}


def test_session_id_from_env_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude session id wins over the Codex thread id."""
    _clear_hook_env(monkeypatch)
    assert hooks._session_id_from_env() is None
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-1")
    assert hooks._session_id_from_env() == "codex-1"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-1")
    assert hooks._session_id_from_env() == "claude-1"


def test_binding_entry_dict_only() -> None:
    """Only dict entries are returned."""
    assert hooks._binding_entry({"s": {"name": "a"}}, "s") == {"name": "a"}
    assert hooks._binding_entry({"s": "x"}, "s") is None
    assert hooks._binding_entry({}, "s") is None


def test_arm_waker_paragraph_read_uses_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPEC-v0.2 §7.6: the waker read command uses ``--sanitized``."""
    monkeypatch.setattr(hooks, "_executable_prefix", lambda: ["phoning"])
    para = hooks._arm_waker_paragraph("amy", "sess1")
    assert "phoning read amy --reader sess1 --sanitized`" in para
    assert "`PHONING_HOME=" in para


def test_arm_waker_paragraph_wait_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The waker wait command keeps its exact form without ``--sanitized``."""
    monkeypatch.setattr(hooks, "_executable_prefix", lambda: ["phoning"])
    para = hooks._arm_waker_paragraph("amy", "sess1")
    assert "phoning wait amy --reader sess1 --timeout 3600`" in para
    assert para.count("--sanitized") == 1


def test_session_start_arm_waker_payload_uses_sanitized_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end: session-start ``--arm-waker`` text reads with ``--sanitized``."""
    _clear_hook_env(monkeypatch)
    _isolate_registry(monkeypatch, tmp_path)
    home = _make_log(tmp_path, "amy")
    monkeypatch.setattr(hooks, "_executable_prefix", lambda: ["phoning"])
    monkeypatch.setattr(hooks, "_read_event", lambda: {"session_id": "sess1"})
    monkeypatch.setattr(hooks, "Mailbox", _EmptyBox)
    rc = hooks._do_event(
        [
            "session-start",
            "--name",
            "amy",
            "--home",
            home,
            "--reader",
            "sess1",
            "--arm-waker",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    text = payload["hookSpecificOutput"]["additionalContext"]
    assert "phoning read amy --reader sess1 --sanitized" in text
    assert "phoning wait amy --reader sess1 --timeout 3600" in text


def test_has_actionable_matches_block_types() -> None:
    """Blocked types trigger; empty sets and missing env never trigger."""
    inbound: list[dict[str, Any]] = [{"env": {"type": "ask"}}]
    assert hooks._has_actionable(inbound, {"ask"})
    assert not hooks._has_actionable(inbound, {"note"})
    assert not hooks._has_actionable(inbound, set())
    assert not hooks._has_actionable([{}], {"ask"})


def test_resolve_identity_flag_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--name`` short-circuits the registry."""
    _clear_hook_env(monkeypatch)
    _isolate_registry(monkeypatch, tmp_path)
    name, _home, reader, from_reg = hooks._resolve_identity(
        "amy", None, "r1", {"session_id": "s"}
    )
    assert (name, reader, from_reg) == ("amy", "r1", False)


def test_resolve_identity_registry_and_inert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Registry bindings resolve; no binding and no env is inert."""
    _clear_hook_env(monkeypatch)
    _isolate_registry(monkeypatch, tmp_path)
    hooks._save_registry({"s1": {"name": "amy", "home": "/h", "reader": "r1"}})
    name, home, reader, from_reg = hooks._resolve_identity(
        None, None, None, {"session_id": "s1"}
    )
    assert (name, home, reader, from_reg) == ("amy", "/h", "r1", True)
    assert hooks._resolve_identity(None, None, None, {})[0] is None


def test_build_text_empty_and_stop() -> None:
    """Empty non-stop text is blank; empty stop text still guides."""
    assert hooks._build_text(0, [], "amy", "r", 600, 20, False) == ""
    assert "phoning send amy" in hooks._build_text(0, [], "amy", "r", 600, 20, True)


def test_do_event_inert_without_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No name, registry or env means the hook stays silent."""
    _clear_hook_env(monkeypatch)
    _isolate_registry(monkeypatch, tmp_path)
    monkeypatch.setattr(hooks, "_read_event", lambda: {})
    assert hooks._do_event(["session-start"]) == 0
    assert capsys.readouterr().out == ""


def test_do_event_stop_skips_without_actionable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stop stays silent when no unread record needs blocking."""

    class _NoteBox(_EmptyBox):
        def peek(
            self, reader: str, limit: int
        ) -> list[dict[str, Any]]:
            _ = (reader, limit)
            return [{"direction": "in", "seq": 1, "env": {"type": "note"}}]

    _clear_hook_env(monkeypatch)
    _isolate_registry(monkeypatch, tmp_path)
    home = _make_log(tmp_path, "amy")
    monkeypatch.setattr(hooks, "_read_event", lambda: {})
    monkeypatch.setattr(hooks, "Mailbox", _NoteBox)
    assert (
        hooks._do_event(["stop", "--name", "amy", "--home", home, "--reader", "r"])
        == 0
    )
    assert capsys.readouterr().out == ""


def test_do_print_config_claude_arms_waker(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Claude session-start arms the waker; other events do not."""
    _clear_hook_env(monkeypatch)
    monkeypatch.setattr(hooks, "_executable_prefix", lambda: ["phoning"])
    assert hooks._do_print_config(["--for", "claude"]) == 0
    payload = json.loads(capsys.readouterr().out)
    start = payload["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    prompt = payload["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "--arm-waker" in start
    assert "--arm-waker" not in prompt


def test_do_print_config_codex_toml(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Codex output is TOML without any ``--arm-waker`` flag."""
    _clear_hook_env(monkeypatch)
    monkeypatch.setattr(hooks, "_executable_prefix", lambda: ["phoning"])
    assert hooks._do_print_config(["--for", "codex"]) == 0
    out = capsys.readouterr().out
    assert "[[hooks.SessionStart]]" in out
    assert "--arm-waker" not in out
