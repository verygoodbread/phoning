"""Unit tests for ``phoning.up`` (docs/SPEC-v0.2.md §1, §7.5)."""

from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path
from typing import Any

import pytest

from phoning import up
from phoning.mailbox import Mailbox


def write_cfg(path: Path, cfg: dict[str, Any]) -> Path:
    """Write ``cfg`` as JSON to ``path`` and return it."""
    path.write_text(json.dumps(cfg))
    return path


def test_relay_defaults_and_expansion(tmp_path: Path) -> None:
    cfg = up.load_config(
        str(
            write_cfg(
                tmp_path / "up.json",
                {"relay": {"url": "http://127.0.0.1:1", "db": "~/r/relay.db"}, "owners": []},
            )
        )
    )
    relay = cfg["relay"]
    assert relay["db"] == os.path.expanduser("~/r/relay.db")
    assert relay["log"] == os.path.join(os.path.dirname(relay["db"]), "relay.log")
    assert "server" not in relay


def test_owner_validation_errors(tmp_path: Path) -> None:
    bad: list[dict[str, Any]] = [
        {"owners": "nope"},
        {"owners": [{"home": "/h", "name": "a"}]},
        {"owners": [{"home": "/h", "name": "a", "session": "s", "interval": True}]},
        {"owners": [{"home": "/h", "name": "a", "session": "s", "interval": -1}]},
        {"doorbells": "nope", "owners": []},
        {"doorbells": [{"home": "/h"}]},
        {"doorbells": [{"home": "/h", "name": "a", "nope": 1}]},
        {"doorbells": [{"home": "/h", "name": "a", "types": "ask"}]},
        {"doorbells": [{"home": "/h", "name": "a", "intro": "yes"}]},
        {"doorbells": [{"home": "/h", "name": "a", "poll": "fast"}]},
        {"doorbells": [{"home": "/h", "name": "a", "reader": ""}]},
        {"nope": 1},
    ]
    for i, cfg in enumerate(bad):
        with pytest.raises(up.ConfigError):
            up.load_config(str(write_cfg(tmp_path / f"bad{i}.json", cfg)))


def test_doorbell_config_normalised(tmp_path: Path) -> None:
    cfg = up.load_config(
        str(
            write_cfg(
                tmp_path / "up.json",
                {
                    "owners": [],
                    "doorbells": [
                        {
                            "home": "~/h",
                            "name": "alice",
                            "reader": "bell",
                            "hook_reader": "alice-main",
                            "ring": "codex-app-server",
                            "codex_home": "~/c",
                            "codex_thread": "thr",
                            "types": ["ask"],
                            "min_interval": 30,
                            "intro": True,
                            "poll": 1,
                        }
                    ],
                },
            )
        )
    )
    bell = cfg["doorbells"][0]
    assert bell["home"] == os.path.expanduser("~/h")
    assert bell["codex_home"] == os.path.expanduser("~/c")
    assert bell["min_interval"] == 30.0 and bell["poll"] == 1.0
    assert bell["intro"] is True and bell["types"] == ["ask"]


def test_doorbell_argv_flag_mapping(tmp_path: Path) -> None:
    cfg = up.load_config(
        str(
            write_cfg(
                tmp_path / "up.json",
                {
                    "owners": [],
                    "doorbells": [
                        {
                            "home": "/h",
                            "name": "alice",
                            "reader": "bell",
                            "hook_reader": "alice-main",
                            "ring": "codex-app-server",
                            "codex_home": "/c",
                            "codex_thread": "thr",
                            "types": ["ask", "propose"],
                            "min_interval": 30,
                            "intro": True,
                            "poll": 1,
                        }
                    ],
                },
            )
        )
    )
    argv = up._doorbell_argv("/py", cfg["doorbells"][0])
    assert argv[:5] == ["/py", "-m", "phoning.cli", "doorbell", "alice"]
    assert "--reader" in argv and "--hook-reader" in argv
    assert argv[argv.index("--reader") + 1] == "bell"
    assert argv[argv.index("--hook-reader") + 1] == "alice-main"
    assert argv[argv.index("--types") + 1] == "ask,propose"
    assert argv[argv.index("--min-interval") + 1] == "30.0"
    assert "--intro" in argv


def test_lock_holder_probe_is_silent(tmp_path: Path) -> None:
    # (acceptance covers the owner lock end to end; keep the unit probe local
    # to the doorbell lock so nothing races the acceptance fixture.)
    udir = tmp_path / "alice"
    Mailbox(udir)
    assert up.lock_holder(str(udir), "doorbell.lock") is None
    assert up.owner_holder(str(udir)) is None


def test_reconcile_doorbell_no_mailbox_and_dry_run(tmp_path: Path) -> None:
    home = tmp_path / "home"
    Mailbox(home / "alice")
    cfg = up.load_config(
        str(
            write_cfg(
                tmp_path / "up.json",
                {"owners": [], "doorbells": [{"home": str(home), "name": "alice"}]},
            )
        )
    )
    items = up.reconcile(cfg, dry_run=True)
    assert [(i["kind"], i["state"]) for i in items] == [("doorbell", "would-start")]
    cfg2 = up.load_config(
        str(
            write_cfg(
                tmp_path / "up2.json",
                {"owners": [], "doorbells": [{"home": str(home), "name": "ghost"}]},
            )
        )
    )
    items2 = up.reconcile(cfg2, dry_run=True)
    assert items2[0]["state"] == "no-mailbox"


def test_launchagent_plist_roundtrip() -> None:
    raw = up.launchagent_plist(
        program=["/p", "-m", "phoning.cli", "up"],
        interval=90,
        log_path="/l",
        path_env="/b",
    )
    pl = plistlib.loads(raw)
    assert pl["Label"] == up.DEFAULT_LABEL
    assert pl["StartInterval"] == 90 and pl["RunAtLoad"] is True
    assert pl["EnvironmentVariables"] == {"PATH": "/b"}


def test_missing_config_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert up.main(["--config", str(tmp_path / "nope.json")]) == 2
    err = capsys.readouterr().err
    assert "nope.json" in err and "owners" in err


def test_quiet_plist_passes_quiet(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert up.main(["--print-launchagent", "--quiet"]) == 0
    pl = plistlib.loads(capsys.readouterr().out.encode())
    assert pl["ProgramArguments"][1:] == ["-m", "phoning.cli", "up", "--quiet"]
    assert "/usr/bin" in pl["EnvironmentVariables"]["PATH"]
