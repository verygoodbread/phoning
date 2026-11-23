"""Acceptance: relay `bind` and `public` keys in up.json (docs/spec/remote.md, Part A)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from phoning import up


def _cfg(tmp_path: Path, relay: dict[str, Any]) -> str:
    p = tmp_path / "up.json"
    p.write_text(json.dumps({"relay": relay, "owners": []}))
    return str(p)


BASE = {"url": "http://127.0.0.1:8766", "db": "/tmp/x/relay.db"}


def test_bind_and_public_are_loaded(tmp_path: Path) -> None:
    cfg = up.load_config(_cfg(tmp_path, {**BASE, "bind": "0.0.0.0",
                                         "public": ["http://100.1.2.3:8766/", "http://100.1.2.3:8766",
                                                    "http://127.0.0.1:8766", "http://10.0.0.5:8766"]}))
    assert cfg["relay"]["bind"] == "0.0.0.0"
    assert cfg["relay"]["public"] == ["http://100.1.2.3:8766/", "http://10.0.0.5:8766"]


def test_without_new_keys_nothing_is_added(tmp_path: Path) -> None:
    cfg = up.load_config(_cfg(tmp_path, dict(BASE)))
    assert "bind" not in cfg["relay"]
    assert "public" not in cfg["relay"]


def test_public_that_cleans_to_empty_is_omitted(tmp_path: Path) -> None:
    cfg = up.load_config(_cfg(tmp_path, {**BASE, "public": ["http://127.0.0.1:8766/"]}))
    assert "public" not in cfg["relay"]


@pytest.mark.parametrize("bad", [{"bind": ""}, {"bind": 5}, {"bind": None}])
def test_bad_bind_rejected(tmp_path: Path, bad: dict[str, Any]) -> None:
    with pytest.raises(up.ConfigError, match="relay 'bind' must be a non-empty string"):
        up.load_config(_cfg(tmp_path, {**BASE, **bad}))


@pytest.mark.parametrize("bad", [{"public": "http://x"}, {"public": [""]}, {"public": [3]}, {"public": None}])
def test_bad_public_rejected(tmp_path: Path, bad: dict[str, Any]) -> None:
    with pytest.raises(up.ConfigError, match="relay 'public' must be a list of URLs"):
        up.load_config(_cfg(tmp_path, {**BASE, **bad}))


def _capture_spawn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    seen: list[list[str]] = []

    class _P:
        pid = 4242

        def poll(self) -> None:
            return None

    def fake_spawn(argv: list[str], **_kw: Any) -> _P:
        seen.append(list(argv))
        return _P()

    server = tmp_path / "retalk-server"
    server.write_text("#!/bin/sh\n")
    server.chmod(0o755)
    monkeypatch.setattr(up, "_spawn", fake_spawn)
    monkeypatch.setattr(up, "relay_up", lambda url, timeout=2.0: False)
    monkeypatch.setattr(up, "_poll_relay", lambda url, proc, deadline: True)
    return seen


def test_spawn_uses_bind_and_all_audiences(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_spawn(monkeypatch, tmp_path)
    relay = {**BASE, "db": str(tmp_path / "r" / "relay.db"), "server": str(tmp_path / "retalk-server"),
             "bind": "0.0.0.0", "public": ["http://100.1.2.3:8766"]}
    items = up.reconcile(up.load_config(_cfg(tmp_path, relay)))
    assert items[0]["state"] == "started"
    assert items[0]["public"] == ["http://100.1.2.3:8766"]
    argv = seen[0]
    assert argv[argv.index("--host") + 1] == "0.0.0.0"
    assert argv[argv.index("--audience") + 1] == "http://127.0.0.1:8766,http://100.1.2.3:8766"
    line = up._format_item(items[0])
    assert "http://127.0.0.1:8766" in line and "also http://100.1.2.3:8766" in line


def test_spawn_argv_unchanged_without_new_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_spawn(monkeypatch, tmp_path)
    db = str(tmp_path / "r" / "relay.db")
    server = str(tmp_path / "retalk-server")
    items = up.reconcile(up.load_config(_cfg(tmp_path, {**BASE, "db": db, "server": server})))
    assert seen[0] == [server, "--host", "127.0.0.1", "--port", "8766", "--audience",
                       "http://127.0.0.1:8766", "--db", db]
    assert "public" not in items[0]
    assert "also" not in up._format_item(items[0])
