"""Acceptance: `"bridges"` in up.json (docs/spec/bridge.md).

Supervision tests use a stand-in "python" that records its argv/env and exits with a chosen code, plus a real process that
holds the shared flock on the bridge lock path. One end-to-end test starts a real bridge against a throwaway retalk relay.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from phoning import bridge, up
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import ROOT
from tests.acceptance.test_bridge_acceptance import Legacy, _wait

HOLDER = r"""
import fcntl, json, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.write(fd, (json.dumps({"pid": os.getpid(), "since": "x"}) + "\n").encode())
print("ready", flush=True)
sys.stdin.read()
"""


def bridge_entry(tmp: Path, **over: Any) -> dict[str, Any]:
    ident = tmp / "legacy" / "users" / "alice" / "identity"
    base: dict[str, Any] = {"home": str(tmp / "home"), "name": "alice", "identity": str(ident),
                            "peers": ["codex-review", "codex-worker"], "session": "up-bridge"}
    base.update(over)
    return base


def world(tmp: Path, *, mailbox: bool = True, identity: bool = True) -> dict[str, Any]:
    if mailbox:
        Mailbox(tmp / "home" / "alice")
    if identity:
        (tmp / "legacy" / "users" / "alice" / "identity").mkdir(parents=True, exist_ok=True)
    return bridge_entry(tmp)


def load(tmp: Path, *entries: dict[str, Any]) -> dict[str, Any]:
    path = tmp / "up.json"
    path.write_text(json.dumps({"owners": [], "bridges": list(entries)}))
    return up.load_config(str(path))


def stand_in(tmp: Path, rc: int = 0, sleep: float = 0.0) -> tuple[str, Path]:
    log = tmp / "bridge-spawned.jsonl"
    exe = tmp / "fakepython"
    exe.write_text(
        f"#!{sys.executable}\nimport json, os, sys, time\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'home': os.environ.get('PHONING_HOME'),"
        " 'cwd': os.getcwd(), 'stdin_tty': sys.stdin.isatty()}) + '\\n')\n"
        f"time.sleep({sleep})\nsys.exit({rc})\n")
    exe.chmod(0o755)
    return str(exe), log


class Holder:
    def __init__(self, lock: Path) -> None:
        self.proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE)
        assert self.proc.stdout is not None and self.proc.stdout.readline().strip() == b"ready"

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)


@pytest.fixture
def held() -> Iterator[list[Holder]]:
    made: list[Holder] = []
    yield made
    for h in made:
        h.kill()


# ------------------------------------------------------------------ config


def test_bridge_config_is_validated_and_normalised(tmp_path: Path) -> None:
    cfg = load(tmp_path, bridge_entry(tmp_path, home="~/h", identity="~/legacy/identity", interval=5))
    (b,) = cfg["bridges"]
    assert b["home"] == os.path.expanduser("~/h") and b["identity"] == os.path.expanduser("~/legacy/identity")
    assert b["peers"] == ["codex-review", "codex-worker"] and b["session"] == "up-bridge" and b["interval"] == 5
    (plain,) = load(tmp_path, bridge_entry(tmp_path))["bridges"]
    assert "interval" not in plain  # absent means the bridge's own default


@pytest.mark.parametrize(
    "over",
    [
        {"home": ""},
        {"name": ""},
        {"identity": ""},
        {"identity": 3},
        {"session": ""},
        {"peers": []},
        {"peers": "codex-review"},
        {"peers": ["ok", ""]},
        {"peers": ["ok", 7]},
        {"interval": 0},
        {"interval": -2},
        {"interval": True},
        {"interval": "fast"},
        {"nope": 1},
    ],
)
def test_bad_bridge_entries_are_config_errors(tmp_path: Path, over: dict[str, Any]) -> None:
    with pytest.raises(up.ConfigError):
        load(tmp_path, bridge_entry(tmp_path, **over))


@pytest.mark.parametrize("drop", ["home", "name", "identity", "peers", "session"])
def test_required_bridge_keys(tmp_path: Path, drop: str) -> None:
    entry = bridge_entry(tmp_path)
    del entry[drop]
    with pytest.raises(up.ConfigError):
        load(tmp_path, entry)


def test_bridges_must_be_a_list(tmp_path: Path) -> None:
    path = tmp_path / "up.json"
    path.write_text(json.dumps({"owners": [], "bridges": {"home": "/h"}}))
    with pytest.raises(up.ConfigError):
        up.load_config(str(path))


# ------------------------------------------------------------ dry run states


def test_states_without_starting_anything(tmp_path: Path, held: list[Holder]) -> None:
    cfg = load(tmp_path, bridge_entry(tmp_path))
    (item,) = up.reconcile(cfg, dry_run=True)
    assert (item["kind"], item["name"], item["state"]) == ("bridge", "alice", "no-mailbox")
    Mailbox(tmp_path / "home" / "alice")
    (item,) = up.reconcile(cfg, dry_run=True)
    assert item["state"] == "no-identity"
    entry = world(tmp_path)
    lock = Path(entry["identity"]).parent / "bridge.lock"
    (item,) = up.reconcile(cfg, dry_run=True)
    assert item["state"] == "would-start" and not lock.exists()  # probing never creates the lock file
    held.append(Holder(lock))
    (item,) = up.reconcile(cfg, dry_run=True)
    assert item["state"] == "running" and item["pid"] == held[0].proc.pid
    held[0].kill()
    (item,) = up.reconcile(cfg, dry_run=True)
    assert item["state"] == "would-start"  # a lock file left by a dead bridge holds no flock


def test_the_probe_is_the_shared_flock_on_the_path_bridge_py_uses(tmp_path: Path) -> None:
    entry = world(tmp_path)
    lock = bridge._lock_path(os.path.dirname(os.path.abspath(entry["identity"])))
    assert lock == str(Path(entry["identity"]).parent / "bridge.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, json.dumps({"pid": os.getpid()}).encode())
        (item,) = up.reconcile(load(tmp_path, entry), dry_run=True)
        assert item["state"] == "running" and item["pid"] == os.getpid()
    finally:
        os.close(fd)


# ------------------------------------------------------------------ spawning


def test_a_bridge_is_spawned_like_an_owner(tmp_path: Path) -> None:
    entry = world(tmp_path)
    entry["interval"] = 3
    exe, log = stand_in(tmp_path, rc=1)
    (item,) = up.reconcile(load(tmp_path, entry), dry_run=False, python=exe)
    (call,) = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert call["argv"] == ["-m", "phoning.cli", "bridge", "alice", "--from-identity", entry["identity"],
                            "--peer", "codex-review", "--peer", "codex-worker", "--session", "up-bridge",
                            "--home", str(tmp_path / "home"), "--interval", "3"]
    assert call["home"] == str(tmp_path / "home") and call["cwd"] == str(tmp_path / "home" / "alice")
    assert item["state"] == "failed" and "bridge exited rc=1" in item["error"] and "bridge.log" in item["error"]
    logf = tmp_path / "home" / "alice" / "bridge.log"
    assert stat.S_IMODE(logf.stat().st_mode) == 0o600


def test_no_interval_means_no_interval_flag(tmp_path: Path) -> None:
    entry = world(tmp_path)
    exe, log = stand_in(tmp_path, rc=1)
    up.reconcile(load(tmp_path, entry), dry_run=False, python=exe)
    assert "--interval" not in json.loads(log.read_text().splitlines()[0])["argv"]


def test_exit_3_means_somebody_else_already_runs_it(tmp_path: Path) -> None:
    entry = world(tmp_path)
    exe, _log = stand_in(tmp_path, rc=3)
    (item,) = up.reconcile(load(tmp_path, entry), dry_run=False, python=exe)
    assert item["state"] == "running" and "error" not in item


def test_exit_3_is_not_a_failure_for_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    entry = world(tmp_path)
    exe, _log = stand_in(tmp_path, rc=3)
    load(tmp_path, entry)
    monkeypatch.setattr(sys, "executable", exe)
    assert up.main(["--config", str(tmp_path / "up.json"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["state"] == "running"


def test_a_bridge_that_never_takes_its_lock_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = world(tmp_path)
    exe, _log = stand_in(tmp_path, rc=0, sleep=30)
    monkeypatch.setattr(up, "BRIDGE_START_TIMEOUT", 0.4)
    monkeypatch.setattr(up, "_POLL", 0.05)
    procs: list[subprocess.Popen[Any]] = []
    real_spawn = up._spawn

    def spy(argv: Any, **kw: Any) -> subprocess.Popen[Any]:
        p = real_spawn(argv, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(up, "_spawn", spy)
    try:
        (item,) = up.reconcile(load(tmp_path, entry), dry_run=False, python=exe)
    finally:
        for p in procs:
            with contextlib.suppress(Exception):
                p.kill()
                p.wait(timeout=10)
    assert item["state"] == "failed" and "did not take the lock" in item["error"]


def test_bridge_start_timeout_is_longer_than_the_owner_one() -> None:
    assert up.BRIDGE_START_TIMEOUT >= 30.0 > up.START_TIMEOUT


def test_a_running_bridge_is_left_alone(tmp_path: Path, held: list[Holder]) -> None:
    entry = world(tmp_path)
    exe, log = stand_in(tmp_path, rc=1)
    held.append(Holder(Path(entry["identity"]).parent / "bridge.lock"))
    (item,) = up.reconcile(load(tmp_path, entry), dry_run=False, python=exe)
    assert item["state"] == "running" and item["pid"] == held[0].proc.pid
    assert not log.exists()  # nothing was spawned


def test_bridge_items_come_after_owners_and_doorbells(tmp_path: Path) -> None:
    entry = world(tmp_path)
    (tmp_path / "home" / "alice" / "identity").mkdir(parents=True)
    path = tmp_path / "up.json"
    path.write_text(json.dumps({
        "owners": [{"home": str(tmp_path / "home"), "name": "alice", "session": "a"}],
        "doorbells": [{"home": str(tmp_path / "home"), "name": "alice"}],
        "bridges": [entry]}))
    items = up.reconcile(up.load_config(str(path)), dry_run=True)
    assert [i["kind"] for i in items] == ["owner", "doorbell", "bridge"]


# ------------------------------------------------------------ end to end


def test_up_starts_a_real_bridge_once_and_reports_it_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            lg: Legacy) -> None:
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("PHONING_HOME", str(lg.home))
    entry = {"home": str(lg.home), "name": "claude-lead", "identity": str(lg.identity("claude-lead")),
             "peers": ["codex-review"], "session": "up-bridge", "interval": 1}
    cfg = load(tmp_path, entry)
    pid = 0
    try:
        (item,) = up.reconcile(cfg, dry_run=False)
        assert item["state"] == "started", item
        pid = item["pid"]
        lg.send("codex-review", "hello through up")
        rec = _wait(lambda: next(iter(lg.records()), None), what="the bridged record")
        assert rec["peer"] == "codex-review" and rec["via"] == "bridge:claude-lead"
        (again,) = up.reconcile(cfg, dry_run=False)
        assert again["state"] == "running" and again["pid"] == pid
        body = json.loads(lg.lock.read_text())
        assert body["session"] == "up-bridge" and body["peers"] == ["codex-review"]
    finally:
        if pid:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.2)
