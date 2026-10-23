"""Unit tests for the phoning CLI."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from phoning import cli
from phoning import ops as ops_mod
from phoning.cli import (
    effective_room,
    format_human,
    parse_comma_list,
    resolve_session,
)
from phoning.envelope import EnvelopeError
from phoning.retalk import RetalkError


def _make_retalk_error(msg: str) -> RetalkError:
    """Build a ``RetalkError`` carrying ``msg`` without coupling to its init."""
    exc = RetalkError.__new__(RetalkError)
    Exception.__init__(exc, msg)
    return exc


def test_parse_comma_list() -> None:
    """Comma splitting drops empties and whitespace."""
    assert parse_comma_list(None) is None
    assert parse_comma_list("a,b , ,c") == ["a", "b", "c"]
    assert parse_comma_list("") == []


def test_resolve_session_precedence(monkeypatch: Any) -> None:
    """Explicit arg wins, then env vars, then cli."""
    assert resolve_session("s1") == "s1"
    monkeypatch.delenv("PHONING_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    assert resolve_session() == "cli"
    monkeypatch.setenv("PHONING_SESSION", "hs")
    assert resolve_session() == "hs"


def test_effective_room_prefers_top() -> None:
    """Top-level room wins over env.room."""
    assert effective_room({"room": "r1", "env": {"room": "r2"}}) == "r1"
    assert effective_room({"room": None, "env": {"room": "r2"}}) == "r2"
    assert effective_room({"env": {}}) is None


def test_format_human_with_room() -> None:
    """Human line includes room bracket when present."""
    rec = {
        "seq": 3,
        "recv_ts": "2026-01-01T00:00:00Z",
        "room": "team",
        "env": {
            "from": {"principal": "bob", "session": "s1"},
            "type": "note",
            "subject": "hi",
            "ts": "2026-01-01T00:00:00Z",
        },
    }
    line = format_human(rec)
    assert line == "#3 2026-01-01T00:00:00Z bob/s1 [team] note: hi"


def test_send_rejects_body_and_body_file(monkeypatch: Any, tmp_path: Path) -> None:
    """--body and --body-file are mutually exclusive (usage error)."""
    monkeypatch.setenv("PHONING_HOME", str(tmp_path))
    rc = cli.main(
        ["send", "alice", "--to", "bob", "--type", "chat", "--subject", "s",
         "--body", "a", "--body-file", "b"]
    )
    assert rc == cli.EXIT_USAGE


def test_send_invalid_data_json(monkeypatch: Any, tmp_path: Path) -> None:
    """Invalid --data JSON is a usage error."""
    monkeypatch.setenv("PHONING_HOME", str(tmp_path))
    rc = cli.main(
        ["send", "alice", "--to", "bob", "--type", "chat", "--subject", "s",
         "--data", "{bad"]
    )
    assert rc == cli.EXIT_USAGE


def test_send_data_must_be_object(monkeypatch: Any, tmp_path: Path) -> None:
    """Non-object --data JSON is a usage error."""
    monkeypatch.setenv("PHONING_HOME", str(tmp_path))
    rc = cli.main(
        ["send", "alice", "--to", "bob", "--type", "chat", "--subject", "s",
         "--data", "[1,2]"]
    )
    assert rc == cli.EXIT_USAGE


def test_send_delegates_to_ops(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    """``send`` forwards parsed args to ``ops.send_message`` and prints the eid."""
    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    calls: list[dict[str, Any]] = []

    def fake_send(
        user_dir: str, name: str, session: str, **kw: Any
    ) -> str:
        calls.append({"user_dir": user_dir, "name": name, "session": session, **kw})
        return "e" * 32

    monkeypatch.setattr(ops_mod, "send_message", fake_send)
    rc = cli.main(
        ["send", "alice", "--to", "bob", "--type", "chat", "--subject", "hi",
         "--session", "s", "--body", "hello"]
    )
    assert rc == cli.EXIT_OK
    assert len(calls) == 1
    call = calls[0]
    assert call["name"] == "alice"
    assert call["to"] == "bob"
    assert call["room"] is None
    assert call["msg_type"] == "chat"
    assert call["subject"] == "hi"
    assert call["body"] == "hello"
    assert call["session"] == "s"
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == {"eid": "e" * 32}
    assert out == json.dumps({"eid": "e" * 32})


def test_send_room_delegates_to_ops(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    """Room sends forward ``room`` and parse refs."""
    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    calls: list[dict[str, Any]] = []

    def fake_send(
        user_dir: str, name: str, session: str, **kw: Any
    ) -> str:
        calls.append({"user_dir": user_dir, "name": name, "session": session, **kw})
        return "f" * 32

    monkeypatch.setattr(ops_mod, "send_message", fake_send)
    rc = cli.main(
        ["send", "alice", "--room", "team", "--type", "chat", "--subject", "hey",
         "--refs", "a,b ", "--data", '{"k": 1}']
    )
    assert rc == cli.EXIT_OK
    assert calls[0]["room"] == "team"
    assert calls[0]["to"] is None
    assert calls[0]["refs"] == ["a", "b"]
    assert calls[0]["data"] == {"k": 1}
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == {"eid": "f" * 32}


def test_send_envelope_error_is_usage(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    """EnvelopeError and ValueError from ops map to EXIT_USAGE."""

    def boom(*a: Any, **kw: Any) -> str:
        raise EnvelopeError("bad envelope")

    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ops_mod, "send_message", boom)
    rc = cli.main(["send", "alice", "--to", "bob", "--type", "chat", "--subject", "hi"])
    assert rc == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "error: bad envelope" in err

    def boom_value(*a: Any, **kw: Any) -> str:
        raise ValueError("bad value")

    monkeypatch.setattr(ops_mod, "send_message", boom_value)
    rc = cli.main(["send", "alice", "--to", "bob", "--type", "chat", "--subject", "hi"])
    assert rc == cli.EXIT_USAGE


def test_send_retalk_error_is_nothing(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    """RetalkError from ops maps to EXIT_NOTHING."""

    def boom(*a: Any, **kw: Any) -> str:
        raise _make_retalk_error("relay down")

    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ops_mod, "send_message", boom)
    rc = cli.main(["send", "alice", "--to", "bob", "--type", "chat", "--subject", "hi"])
    assert rc == cli.EXIT_NOTHING
    err = capsys.readouterr().err
    assert "error: relay down" in err


def test_send_body_file(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    """--body-file contents reach ops as body."""
    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    body_file = tmp_path / "body.txt"
    body_file.write_text("from-file", encoding="utf-8")
    seen: dict[str, Any] = {}

    def fake_send(user_dir: str, name: str, session: str, **kw: Any) -> str:
        seen.update(kw)
        return "e" * 32

    monkeypatch.setattr(ops_mod, "send_message", fake_send)
    rc = cli.main(
        ["send", "alice", "--to", "bob", "--type", "chat", "--subject", "s",
         "--body-file", str(body_file)]
    )
    assert rc == cli.EXIT_OK
    assert seen["body"] == "from-file"
    capsys.readouterr()


def test_home_dir_override(monkeypatch: Any) -> None:
    """PHONING_HOME overrides the default home."""
    monkeypatch.setenv("PHONING_HOME", "/tmp/custom")
    assert cli.home_dir() == "/tmp/custom"
    monkeypatch.delenv("PHONING_HOME", raising=False)
    assert cli.home_dir().endswith(os.path.join(".phoning"))


def test_help_lists_new_subcommands() -> None:
    """``phoning --help`` lists up/hook/web/mcp alongside the v0 commands."""
    text = cli.build_parser().format_help()
    for sub in ("up", "hook", "web", "mcp", "send", "own"):
        assert sub in text


def test_up_delegates(monkeypatch: Any) -> None:
    """``phoning up`` forwards the remainder to ``phoning.up.main``."""
    seen: dict[str, Any] = {}

    def fake_main(argv: Any = None) -> int:
        seen["argv"] = list(argv) if argv is not None else None
        return 7

    monkeypatch.setattr("phoning.up.main", fake_main)
    assert cli.main(["up", "--dry-run", "--json"]) == 7
    assert seen["argv"] == ["--dry-run", "--json"]


def test_hook_delegates(monkeypatch: Any) -> None:
    """``phoning hook`` forwards the remainder to ``phoning.hooks.main``."""
    seen: dict[str, Any] = {}

    def fake_main(argv: Any = None) -> int:
        seen["argv"] = list(argv) if argv is not None else None
        return 3

    monkeypatch.setattr("phoning.hooks.main", fake_main)
    assert cli.main(["hook", "stop", "--name", "alice"]) == 3
    assert seen["argv"] == ["stop", "--name", "alice"]


def test_web_delegates(monkeypatch: Any) -> None:
    """``phoning web`` forwards the remainder to ``phoning.web.main``."""
    seen: dict[str, Any] = {}

    def fake_main(argv: Any = None) -> int:
        seen["argv"] = list(argv) if argv is not None else None
        return 5

    monkeypatch.setattr("phoning.web.main", fake_main)
    assert cli.main(["web", "alice", "--port", "0"]) == 5
    assert seen["argv"] == ["alice", "--port", "0"]


def test_mcp_delegates(monkeypatch: Any) -> None:
    """``phoning mcp`` forwards the remainder to ``phoning.mcp.main``."""
    seen: dict[str, Any] = {}

    def fake_main(argv: Any = None) -> int:
        seen["argv"] = list(argv) if argv is not None else None
        return 9

    monkeypatch.setattr("phoning.mcp.main", fake_main)
    assert cli.main(["mcp", "alice"]) == 9
    assert seen["argv"] == ["alice"]


def test_send_still_works_after_wiring(monkeypatch: Any, tmp_path: Path) -> None:
    """Wiring does not change plain ``send`` dispatch (no lazy-import cost)."""
    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))

    def fake_send(user_dir: str, name: str, session: str, **kw: Any) -> str:
        return "e" * 32

    lazy_modules = ("http.server", "phoning.up", "phoning.hooks", "phoning.web", "phoning.mcp")
    saved: dict[str, Any] = {}
    for mod in lazy_modules:
        if mod in sys.modules:
            saved[mod] = sys.modules.pop(mod)
    try:
        with mock.patch.object(ops_mod, "send_message", fake_send):
            rc = cli.main(
                ["send", "alice", "--to", "bob", "--type", "chat", "--subject", "hi"]
            )
            assert rc == cli.EXIT_OK
            for mod in lazy_modules:
                assert mod not in sys.modules
    finally:
        sys.modules.update(saved)


def test_cli_send_uses_ops_module_attribute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Send goes through the ``ops`` module attribute so it can be monkeypatched."""
    calls: list[dict[str, object]] = []

    def fake_send(user_dir: str, name: str, session: str, **kw: object) -> str:
        calls.append({"user_dir": user_dir, "name": name, "session": session, **kw})
        return "e" * 32

    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ops_mod, "send_message", fake_send)
    rc = cli.main(
        ["send", "alice", "--to", "bob", "--type", "chat", "--subject", "hi",
         "--session", "s"]
    )
    assert rc == 0 and calls and calls[0]["name"] == "alice"
    assert calls[0]["to"] == "bob"
    assert calls[0]["msg_type"] == "chat"
    assert calls[0]["subject"] == "hi"
    assert calls[0]["session"] == "s"
