"""Acceptance: `phoning up` (docs/SPEC-v0.2.md §1) — idempotent start of relay + owner daemons."""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from phoning import up
from phoning.mailbox import Mailbox, OwnerBusy
from tests.acceptance.helpers import HAVE_RETALK, ROOT, free_port


def write_cfg(path: Path, cfg: dict[str, Any]) -> Path:
    path.write_text(json.dumps(cfg))
    return path


def test_config_validation(tmp_path: Path) -> None:
    good = write_cfg(tmp_path / "up.json", {"owners": [{"home": "~/h", "name": "a", "session": "s"}]})
    cfg = up.load_config(str(good))
    assert cfg["owners"][0]["home"] == os.path.expanduser("~/h") and cfg["owners"][0]["interval"] == 2
    for bad in ({"owners": [{"home": "/h", "name": "a"}]},          # missing session
                {"owners": [], "extra": 1},                          # unknown key
                {"owners": [{"home": "/h", "name": "a", "session": "s", "x": 1}]},
                {"relay": {"url": "http://127.0.0.1:1"}, "owners": []}):  # relay without db
        with pytest.raises(up.ConfigError):
            up.load_config(str(write_cfg(tmp_path / "bad.json", bad)))


def test_missing_config_exits_2_with_example(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert up.main(["--config", str(tmp_path / "nope.json")]) == 2
    err = capsys.readouterr().err
    assert "nope.json" in err and '"owners"' in err


def test_owner_holder_probe_is_silent(tmp_path: Path) -> None:
    udir = tmp_path / "alice"
    box = Mailbox(udir)
    assert up.owner_holder(str(udir)) is None
    lock = box.acquire_owner("sess-x")
    try:
        holder = up.owner_holder(str(udir))
        assert holder is not None and holder["session"] == "sess-x" and holder["pid"] == os.getpid()
        audit = udir / "mailbox" / "audit.log"
        assert not audit.exists() or audit.read_text() == ""  # probing never writes audit lines
    finally:
        lock.close()
    assert up.owner_holder(str(udir)) is None
    box.acquire_owner("sess-y").close()  # the probe released its lock


def test_dry_run_and_no_identity(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    Mailbox(home / "ghost")  # mailbox but no identity
    (home / "alice" / "identity").mkdir(parents=True)
    Mailbox(home / "alice")
    port = free_port()
    cfg = write_cfg(tmp_path / "up.json", {
        "relay": {"url": f"http://127.0.0.1:{port}", "db": str(tmp_path / "r.db")},
        "owners": [{"home": str(home), "name": "ghost", "session": "g"},
                   {"home": str(home), "name": "alice", "session": "a"}]})
    assert up.main(["--config", str(cfg), "--dry-run", "--json"]) == 0
    items = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(i["kind"], i["state"]) for i in items] == [
        ("relay", "would-start"), ("owner", "no-identity"), ("owner", "would-start")]
    assert not (tmp_path / "r.db").exists() and up.owner_holder(str(home / "alice")) is None


def test_remote_relay_down_is_unmanaged(tmp_path: Path) -> None:
    cfg = up.load_config(str(write_cfg(tmp_path / "up.json", {
        "relay": {"url": "http://10.255.255.1:9", "db": str(tmp_path / "r.db")}, "owners": []})))
    items = up.reconcile(cfg)
    assert items[0]["state"] == "unmanaged"


def test_print_launchagent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    library = Path.home() / "Library" / "LaunchAgents"
    before = sorted(os.listdir(library)) if library.exists() else []
    assert up.main(["--print-launchagent", "--interval", "90", "--config", "/x/up.json"]) == 0
    pl = plistlib.loads(capsys.readouterr().out.encode())
    assert pl["Label"] == "dev.phoning.up"
    assert pl["ProgramArguments"][1:] == ["-m", "phoning.cli", "up", "--config", "/x/up.json"]
    assert pl["RunAtLoad"] is True and pl["StartInterval"] == 90 and pl["AbandonProcessGroup"] is True
    assert pl["StandardOutPath"].endswith("Library/Logs/phoning-up.log")
    assert "/usr/bin" in pl["EnvironmentVariables"]["PATH"]
    assert (sorted(os.listdir(library)) if library.exists() else []) == before  # never installs
    raw = up.launchagent_plist(program=["/p", "up"], interval=60, log_path="/l", path_env="/b",
                               extra_env={"PHONING_UP_CONFIG": "/c"})
    assert plistlib.loads(raw)["EnvironmentVariables"] == {"PATH": "/b", "PHONING_UP_CONFIG": "/c"}


@pytest.fixture
def spawned() -> Iterator[list[int]]:
    pids: list[int] = []
    yield pids
    for pid in pids:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGTERM)
    time.sleep(0.5)
    for pid in pids:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGKILL)


@pytest.mark.skipif(not HAVE_RETALK, reason="retalk and retalk-server must be installed")
def test_up_starts_relay_and_owner_then_is_idempotent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        spawned: list[int]) -> None:
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    home = tmp_path / "home"
    db = tmp_path / "relay" / "relay.db"
    db.parent.mkdir()
    cfg_path = write_cfg(tmp_path / "up.json", {
        "relay": {"url": url, "db": str(db)},
        "owners": [{"home": str(home), "name": "alice", "session": "alice-up", "interval": 1}]})

    # 1) relay down, no identity yet: relay is started, owner reported no-identity
    assert up.main(["--config", str(cfg_path), "--json"]) == 0
    items = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert items[0]["state"] == "started" and isinstance(items[0]["pid"], int)
    spawned.append(items[0]["pid"])
    assert items[1]["state"] == "no-identity"
    assert up.relay_up(url)
    assert stat.S_IMODE((db.parent / "relay.log").stat().st_mode) == 0o600

    env = dict(os.environ, PHONING_HOME=str(home))
    init = subprocess.run([sys.executable, "-m", "phoning.cli", "init", "alice", "--relay", url],
                          env=env, capture_output=True, text=True, timeout=60)
    assert init.returncode == 0, init.stderr

    # 2) identity exists: owner started, detached, holding the lock
    assert up.main(["--config", str(cfg_path), "--json"]) == 0
    items = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert items[0]["state"] == "up"
    owner = items[1]
    assert owner["state"] == "started", owner
    spawned.append(owner["pid"])
    assert os.getpgid(owner["pid"]) == owner["pid"]  # own session/process group: survives the job
    holder = up.owner_holder(str(home / "alice"))
    assert holder is not None and holder["pid"] == owner["pid"] and holder["session"] == "alice-up"
    log = home / "alice" / "owner.log"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    with pytest.raises(OwnerBusy):
        Mailbox(home / "alice").acquire_owner("intruder")

    # 3) idempotent: nothing restarted, same pids
    assert up.main(["--config", str(cfg_path)]) == 0
    out = capsys.readouterr().out
    assert f"relay {url} up" in out and f"running pid {owner['pid']}" in out
    os.kill(owner["pid"], 0)


def test_owner_that_dies_at_once_reports_its_exit_code(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "alice" / "identity").mkdir(parents=True)
    Mailbox(home / "alice")
    cfg = up.load_config(str(write_cfg(tmp_path / "up.json", {
        "owners": [{"home": str(home), "name": "alice", "session": "a"}]})))
    t0 = time.monotonic()
    items = up.reconcile(cfg, python="/usr/bin/false")
    assert items[0]["state"] == "failed" and "rc=1" in items[0]["error"]
    assert time.monotonic() - t0 < 5  # noticed the exit, didn't sit out the 10 s start timeout


def test_quiet_prints_only_events(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    (home / "alice" / "identity").mkdir(parents=True)
    box = Mailbox(home / "alice")
    cfg = write_cfg(tmp_path / "up.json", {"owners": [{"home": str(home), "name": "alice", "session": "a"},
                                                      {"home": str(home), "name": "ghost", "session": "g"}]})
    lock = box.acquire_owner("held")
    try:
        assert up.main(["--config", str(cfg), "--quiet"]) == 0
        assert capsys.readouterr().out == ""  # running + no-identity are not events
    finally:
        lock.close()
    assert up.main(["--print-launchagent", "--quiet"]) == 0
    pl = plistlib.loads(capsys.readouterr().out.encode())
    assert pl["ProgramArguments"][1:] == ["-m", "phoning.cli", "up", "--quiet"]


def test_doorbells_config_dry_run_and_supervision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str], spawned: list[int]) -> None:
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    home = tmp_path / "home"
    Mailbox(home / "alice")
    with pytest.raises(up.ConfigError):
        up.load_config(str(write_cfg(tmp_path / "bad.json", {"owners": [], "doorbells": [
            {"home": str(home), "name": "alice", "bogus": 1}]})))
    cfg = write_cfg(tmp_path / "up.json", {"owners": [], "doorbells": [
        {"home": str(home), "name": "alice", "intro": False, "min_interval": 60, "poll": 1},
        {"home": str(home), "name": "ghost"}]})
    assert up.main(["--config", str(cfg), "--dry-run", "--json"]) == 0
    items = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(i["kind"], i["name"], i["state"]) for i in items] == [
        ("doorbell", "alice", "would-start"), ("doorbell", "ghost", "no-mailbox")]
    assert up.main(["--config", str(cfg), "--json"]) == 0
    items = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    bell = items[0]
    assert bell["state"] == "started", bell
    spawned.append(bell["pid"])
    assert stat.S_IMODE((home / "alice" / "doorbell.log").stat().st_mode) == 0o600
    assert up.main(["--config", str(cfg), "--json"]) == 0
    again = [json.loads(line) for line in capsys.readouterr().out.splitlines()][0]
    assert again["state"] == "running" and again["pid"] == bell["pid"]
