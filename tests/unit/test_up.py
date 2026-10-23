"""Unit tests for `phoning up` (docs/SPEC-v0.2.md §1) that the acceptance tests don't reach."""

from __future__ import annotations

import contextlib
import fcntl
import inspect
import json
import os
import plistlib
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from phoning import up

PY = sys.executable


# -- config -------------------------------------------------------------------


def _write(tmp_path: Path, name: str, obj: object) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return p


def _exec_script(tmp_path: Path, name: str, code: str) -> str:
    """Write an executable python script (shebang) and return its path."""
    p = tmp_path / name
    p.write_text(f"#!{PY}\n{code}\n")
    p.chmod(0o755)
    return str(p)


def test_load_config_expands_paths_and_defaults(tmp_path: Path) -> None:
    cfg = up.load_config(str(_write(tmp_path, "up.json", {
        "relay": {"url": "http://127.0.0.1:8766", "db": "~/relay/relay.db"},
        "owners": [{"home": "~/h", "name": "a", "session": "s"}]})))
    relay = cfg["relay"]
    assert relay["db"] == os.path.expanduser("~/relay/relay.db")
    assert relay["log"] == os.path.join(os.path.dirname(relay["db"]), "relay.log")
    assert "server" not in relay
    assert cfg["owners"][0]["interval"] == 2


def test_load_config_keeps_explicit_server_and_log(tmp_path: Path) -> None:
    cfg = up.load_config(str(_write(tmp_path, "up.json", {
        "relay": {"url": "http://127.0.0.1:1", "db": "~/r.db", "log": "~/r.log",
                  "server": "~/srv"}, "owners": []})))
    assert cfg["relay"]["log"] == os.path.expanduser("~/r.log")
    assert cfg["relay"]["server"] == os.path.expanduser("~/srv")


def test_load_config_rejects_bad_shapes(tmp_path: Path) -> None:
    for i, bad in enumerate((
        {"owners": {}},
        {"owners": [{"home": "/h", "name": "a", "session": "s", "interval": "x"}]},
        {"owners": [{"home": "", "name": "a", "session": "s"}]},
        {"relay": {"url": "", "db": "/db"}},
        {"relay": {"url": "u", "db": "/db", "extra": 1}},
        "not an object",
        42,
    )):
        p = _write(tmp_path, f"bad{i}.json", bad)
        with pytest.raises(up.ConfigError):
            up.load_config(str(p))


def test_load_config_missing_and_invalid_json(tmp_path: Path) -> None:
    with pytest.raises(up.ConfigError):
        up.load_config(str(tmp_path / "nope.json"))
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(up.ConfigError):
        up.load_config(str(p))


def test_config_path_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = tmp_path / "env.json"
    monkeypatch.setenv("PHONING_UP_CONFIG", str(env_path))
    # explicit --config wins over $PHONING_UP_CONFIG
    assert up._config_path("/x.json") == "/x.json"
    # $PHONING_UP_CONFIG wins over the default
    assert up._config_path(None) == str(env_path)
    # a missing env config is genuinely missing -> exit 2, not a crash
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("PHONING_UP_CONFIG", str(missing))
    assert up.main(["--dry-run", "--json"]) == 2


def test_main_uses_env_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                              capsys: pytest.CaptureFixture[str]) -> None:
    p = _write(tmp_path, "up.json", {"owners": []})
    monkeypatch.setenv("PHONING_UP_CONFIG", str(p))
    assert up.main(["--json"]) == 0
    assert capsys.readouterr().out == ""


def test_explicit_config_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    env_path = tmp_path / "env.json"
    env_path.write_text(json.dumps({"owners": [{"home": "/h", "name": "n",
                                                "session": "s"}]}))
    explicit = _write(tmp_path, "explicit.json", {"owners": []})
    monkeypatch.setenv("PHONING_UP_CONFIG", str(env_path))
    assert up.main(["--config", str(explicit), "--json"]) == 0
    assert capsys.readouterr().out == ""


def test_missing_config_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert up.main(["--config", str(tmp_path / "gone.json")]) == 2
    err = capsys.readouterr().err
    assert "error:" in err and "gone.json" in err and '"owners"' in err


def test_unreadable_config_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "cfg.json"
    p.write_text("{broken")
    assert up.main(["--config", str(p)]) == 2
    assert "invalid JSON" in capsys.readouterr().err


# -- owner_holder --------------------------------------------------------------


def test_owner_holder_missing_file(tmp_path: Path) -> None:
    assert up.owner_holder(str(tmp_path / "nobody")) is None


def test_owner_holder_reads_held_lock(tmp_path: Path) -> None:
    udir = tmp_path / "u"
    (udir / "mailbox").mkdir(parents=True)
    lock = udir / "mailbox" / "owner.lock"
    holder = {"session": "s", "pid": 123, "since": "t"}
    with lock.open("w") as fh:
        fh.write(json.dumps(holder))
        fh.flush()
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        got = up.owner_holder(str(udir))
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    assert got == holder


def test_owner_holder_unparseable_body(tmp_path: Path) -> None:
    udir = tmp_path / "u"
    (udir / "mailbox").mkdir(parents=True)
    lock = udir / "mailbox" / "owner.lock"
    lock.write_text("not json")
    with lock.open("r+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        assert up.owner_holder(str(udir)) == {}
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    assert up.owner_holder(str(udir)) is None


def test_owner_holder_probe_is_read_only(tmp_path: Path) -> None:
    udir = tmp_path / "u"
    (udir / "mailbox").mkdir(parents=True)
    lock = udir / "mailbox" / "owner.lock"
    lock.write_text("{}")
    before = lock.read_bytes()
    assert up.owner_holder(str(udir)) is None
    assert lock.read_bytes() == before
    assert not (udir / "mailbox" / "audit.log").exists()


# -- relay ---------------------------------------------------------------------


def test_relay_up_true_for_listening_socket() -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = int(srv.getsockname()[1])
        assert up.relay_up(f"http://127.0.0.1:{port}") is True


def test_relay_up_false_for_closed_port() -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        port = int(srv.getsockname()[1])
    assert up.relay_up(f"http://127.0.0.1:{port}", timeout=0.2) is False


def test_relay_address_defaults_and_errors() -> None:
    assert up._relay_address("http://localhost") == ("localhost", 80)
    assert up._relay_address("https://example.test") == ("example.test", 443)
    assert up._relay_address("http://[::1]:9/x") == ("::1", 9)
    with pytest.raises(up.ConfigError):
        up._relay_address("http:///nohost")


# -- binaries -------------------------------------------------------------------


def test_find_binary_uses_path_and_local_fallback(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    assert up._find_binary("definitely-not-a-real-binary-xyz") is None
    fake = tmp_path / ".local" / "bin"
    fake.mkdir(parents=True)
    tool = fake / "mytool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/nonexistent")
    assert up._find_binary("mytool") == str(tool)


def test_owner_env_prefixes_retalk_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binp = tmp_path / "bin"
    binp.mkdir()
    retalk = binp / "retalk"
    retalk.write_text("#!/bin/sh\n")
    retalk.chmod(0o755)
    monkeypatch.setenv("PATH", str(binp))
    monkeypatch.setenv("HOME", str(tmp_path))
    env = up._owner_env("/some/home")
    assert env["PHONING_HOME"] == "/some/home"
    assert env["PATH"].startswith(str(binp))


def test_owner_env_without_retalk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PHONING_HOME", raising=False)
    env = up._owner_env("/h")
    assert env["PHONING_HOME"] == "/h"
    assert env["PATH"] == "/nonexistent"


def test_open_private_log_creates_0600(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "x.log"
    fd = up._open_private_log(str(path))
    os.close(fd)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_open_private_log_tightens_existing(tmp_path: Path) -> None:
    path = tmp_path / "loose.log"
    path.write_text("x")
    path.chmod(0o644)
    fd = up._open_private_log(str(path))
    os.close(fd)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# -- launchagent ----------------------------------------------------------------


def test_launchagent_plist_shape() -> None:
    raw = up.launchagent_plist(
        program=[PY, "-m", "phoning.cli", "up"],
        interval=30,
        log_path="/x/log",
        path_env="/bin",
    )
    pl = plistlib.loads(raw)
    assert pl["Label"] == up.DEFAULT_LABEL
    assert pl["RunAtLoad"] is True and pl["StartInterval"] == 30
    assert pl["AbandonProcessGroup"] is True and pl["ProcessType"] == "Background"
    assert pl["StandardOutPath"] == pl["StandardErrorPath"] == "/x/log"
    assert pl["EnvironmentVariables"] == {"PATH": "/bin"}


def test_launchagent_plist_merges_extra_env() -> None:
    raw = up.launchagent_plist(program=["/p"], log_path="/l", path_env="/b",
                               extra_env={"PHONING_UP_CONFIG": "/c"})
    assert plistlib.loads(raw)["EnvironmentVariables"] == {"PATH": "/b",
                                                           "PHONING_UP_CONFIG": "/c"}


def test_launchagent_path_env_contains_system_dirs() -> None:
    path = up._launchagent_path_env()
    for d in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        assert d in path.split(os.pathsep)
    # no duplicates
    parts = path.split(os.pathsep)
    assert len(parts) == len(set(parts))


def test_print_launchagent_never_installs(capsys: pytest.CaptureFixture[str]) -> None:
    assert up.main(["--print-launchagent", "--interval", "7"]) == 0
    pl = plistlib.loads(capsys.readouterr().out.encode())
    assert pl["StartInterval"] == 7
    assert "--config" not in pl["ProgramArguments"]


def test_print_launchagent_with_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert up.main(["--print-launchagent", "--config", "/x/up.json"]) == 0
    pl = plistlib.loads(capsys.readouterr().out.encode())
    assert pl["ProgramArguments"][1:] == ["-m", "phoning.cli", "up", "--config", "/x/up.json"]


# -- reconcile -------------------------------------------------------------------


def test_reconcile_relay_up_no_spawn(tmp_path: Path) -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = int(srv.getsockname()[1])
        cfg = {"relay": {"url": f"http://127.0.0.1:{port}", "db": str(tmp_path / "r.db"),
                         "log": str(tmp_path / "r.log")}, "owners": []}
        items = up.reconcile(cfg)
    assert items == [{"kind": "relay", "url": f"http://127.0.0.1:{port}", "state": "up"}]
    assert not (tmp_path / "r.db").exists()


def test_reconcile_relay_dry_run(tmp_path: Path) -> None:
    cfg = {"relay": {"url": "http://127.0.0.1:1", "db": str(tmp_path / "r.db"),
                     "log": str(tmp_path / "r.log")}, "owners": []}
    assert up.reconcile(cfg, dry_run=True)[0]["state"] == "would-start"


def test_reconcile_relay_unmanaged_remote(tmp_path: Path) -> None:
    cfg = {"relay": {"url": "http://10.255.255.1:9", "db": str(tmp_path / "r.db"),
                     "log": str(tmp_path / "r.log")}, "owners": []}
    item = up.reconcile(cfg)[0]
    assert item["state"] == "unmanaged"
    assert "pid" not in item and "error" not in item


def test_reconcile_relay_missing_server_is_failed(tmp_path: Path) -> None:
    cfg = {"relay": {"url": "http://127.0.0.1:1", "db": str(tmp_path / "r.db"),
                     "log": str(tmp_path / "r.log"),
                     "server": str(tmp_path / "no-such-server")}, "owners": []}
    item = up.reconcile(cfg)[0]
    assert item["state"] == "failed"
    assert "retalk-server" in item["error"]


def test_reconcile_relay_unwritable_cwd_is_failed(tmp_path: Path) -> None:
    # server exists but db dir cannot be created (a file is in the way)
    server = tmp_path / "srv"
    server.write_text("#!/bin/sh\n")
    server.chmod(0o755)
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    cfg = {"relay": {"url": "http://127.0.0.1:1", "db": str(blocker / "r.db"),
                     "log": str(tmp_path / "r.log"), "server": str(server)}, "owners": []}
    item = up.reconcile(cfg)[0]
    assert item["state"] == "failed"
    assert "see " in item["error"]


def test_reconcile_relay_has_no_python_param() -> None:
    assert "python" not in inspect.signature(up._reconcile_relay).parameters


def test_removed_child_helpers_are_gone() -> None:
    assert not hasattr(up, "_child_status")
    assert not hasattr(up, "_child_alive")
    assert not hasattr(up, "_cached_status")
    assert not hasattr(up, "_EXIT_STATUS")


def test_reconcile_owner_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    ghost = home / "ghost"
    ghost.mkdir(parents=True)
    alice = home / "alice" / "identity"
    alice.mkdir(parents=True)
    cfg = {"owners": [
        {"home": str(home), "name": "ghost", "session": "g", "interval": 2},
        {"home": str(home), "name": "alice", "session": "a", "interval": 2},
    ]}
    items = up.reconcile(cfg, dry_run=True)
    assert [(i["kind"], i["state"]) for i in items] == [
        ("owner", "no-identity"), ("owner", "would-start")]
    assert items[0]["home"] == str(home) and items[0]["name"] == "ghost"


def test_reconcile_owner_running_reports_pid(tmp_path: Path) -> None:
    home = tmp_path / "home"
    udir = home / "alice"
    (udir / "identity").mkdir(parents=True)
    (udir / "mailbox").mkdir(parents=True)
    lock = udir / "mailbox" / "owner.lock"
    holder = {"session": "a", "pid": 4242, "since": "x"}
    cfg = {"owners": [{"home": str(home), "name": "alice", "session": "a", "interval": 2}]}
    with lock.open("w") as fh:
        fh.write(json.dumps(holder))
        fh.flush()
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        items = up.reconcile(cfg)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    assert items[0]["state"] == "running" and items[0]["pid"] == 4242


def test_reconcile_owner_failed_reports_rc(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "alice" / "identity").mkdir(parents=True)
    cfg = {"owners": [{"home": str(home), "name": "alice", "session": "a", "interval": 2}]}
    item = up.reconcile(cfg, python="/nonexistent/python-xyz")[0]
    assert item["state"] == "failed"
    assert "cannot start owner" in item["error"]
    assert "see " in item["error"]
    assert (home / "alice" / "owner.log").exists()


def test_reconcile_owner_exit_rc_reported(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "alice" / "identity").mkdir(parents=True)
    # an executable interpreter stand-in that exits immediately with rc=3
    launcher = _exec_script(tmp_path, "boom.py", "import sys; sys.exit(3)")
    t0 = time.monotonic()
    item = up._reconcile_owner(
        {"home": str(home), "name": "alice", "session": "a", "interval": 2},
        dry_run=False, python=launcher)
    assert item["state"] == "failed"
    assert "owner exited rc=3" in item["error"] and "see " in item["error"]
    assert time.monotonic() - t0 < 5


def test_reconcile_owner_timeout_mentions_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / "alice" / "identity").mkdir(parents=True)
    # an executable interpreter stand-in that lingers without taking the lock
    launcher = _exec_script(tmp_path, "linger.py", "import time; time.sleep(60)")
    monkeypatch.setattr(up, "START_TIMEOUT", 0.3)
    monkeypatch.setattr(up, "_POLL", 0.05)
    procs: list[subprocess.Popen[Any]] = []
    orig_spawn = up._spawn

    def _capturing(
        argv: Sequence[str], *, env: dict[str, str], cwd: str, log_path: str
    ) -> subprocess.Popen[Any]:
        proc = orig_spawn(argv, env=env, cwd=cwd, log_path=log_path)
        procs.append(proc)
        return proc

    monkeypatch.setattr(up, "_spawn", _capturing)
    try:
        item = up._reconcile_owner(
            {"home": str(home), "name": "alice", "session": "a", "interval": 2},
            dry_run=False, python=launcher)
    finally:
        for proc in procs:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)
    assert item["state"] == "failed"
    assert "did not take the lock" in item["error"] and "see " in item["error"]


# -- spawning / polling ----------------------------------------------------------


def test_spawn_returns_popen(tmp_path: Path) -> None:
    log = tmp_path / "spawn.log"
    proc = up._spawn(
        [PY, "-c", "import time; time.sleep(30)"],
        env=dict(os.environ),
        cwd=str(tmp_path),
        log_path=str(log),
    )
    try:
        assert isinstance(proc, subprocess.Popen)
        assert isinstance(proc.pid, int)
        assert proc.poll() is None
        assert stat.S_IMODE(log.stat().st_mode) == 0o600
    finally:
        with contextlib.suppress(Exception):
            proc.kill()
        proc.wait(timeout=10)
    assert proc.returncode is not None


def test_poll_relay_notices_dead_child_at_once() -> None:
    proc = subprocess.Popen([PY, "-c", "pass"])  # noqa: S603
    assert proc.wait(timeout=10) == 0
    t0 = time.monotonic()
    assert up._poll_relay("http://127.0.0.1:1", proc, time.monotonic() + 5.0) is False
    assert time.monotonic() - t0 < 2.0


def test_poll_owner_notices_dead_child_at_once(tmp_path: Path) -> None:
    proc = subprocess.Popen([PY, "-c", "pass"])  # noqa: S603
    assert proc.wait(timeout=10) == 0
    t0 = time.monotonic()
    assert up._poll_owner(str(tmp_path), proc, time.monotonic() + 5.0) is False
    assert time.monotonic() - t0 < 2.0


def test_poll_owner_matches_holder_pid(tmp_path: Path) -> None:
    udir = tmp_path / "u"
    (udir / "mailbox").mkdir(parents=True)
    lock = udir / "mailbox" / "owner.lock"
    proc = subprocess.Popen(  # noqa: S603
        [PY, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        with lock.open("w") as fh:
            fh.write(json.dumps({"session": "s", "pid": proc.pid}))
            fh.flush()
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            assert up._poll_owner(str(udir), proc, time.monotonic() + 5.0) is True
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(Exception):
            proc.kill()
        proc.wait(timeout=10)


# -- helpers ---------------------------------------------------------------------


def test_pid_of() -> None:
    assert up._pid_of({}) is None
    assert up._pid_of({"pid": True}) is None
    assert up._pid_of({"pid": "3"}) is None
    assert up._pid_of({"pid": 3}) == 3


def test_format_item_lines() -> None:
    assert up._format_item({"kind": "relay", "url": "u", "state": "up"}) == "relay u up"
    assert up._format_item(
        {"kind": "relay", "url": "u", "state": "started", "pid": 5}) == "relay u started pid 5"
    assert up._format_item(
        {"kind": "relay", "url": "u", "state": "failed", "error": "boom"}
    ) == "relay u failed (boom)"
    line = up._format_item({"kind": "owner", "name": "n", "home": "/h", "state": "failed",
                            "error": "boom"})
    assert line == "owner n@/h failed (boom)"


def test_main_json_and_human_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = _write(tmp_path, "up.json", {"owners": []})
    assert up.main(["--config", str(p), "--json"]) == 0
    assert capsys.readouterr().out == ""
    # a failing item yields exit 1 in both modes
    bad = {"relay": {"url": "http://127.0.0.1:1", "db": str(tmp_path / "r.db"),
                     "log": str(tmp_path / "r.log"),
                     "server": str(tmp_path / "nope")}, "owners": []}
    q = _write(tmp_path, "bad.json", {"owners": []})
    q.write_text(json.dumps(bad))
    assert up.main(["--config", str(q)]) == 1
    out = capsys.readouterr().out
    assert "relay http://127.0.0.1:1 failed" in out


# -- quiet -----------------------------------------------------------------------


def _mixed_items() -> list[dict[str, Any]]:
    return [
        {"kind": "relay", "url": "http://127.0.0.1:1", "state": "up"},
        {"kind": "relay", "url": "http://127.0.0.1:2", "state": "unmanaged"},
        {"kind": "owner", "name": "run", "home": "/h", "state": "running", "pid": 11},
        {"kind": "owner", "name": "ghost", "home": "/h", "state": "no-identity"},
        {"kind": "owner", "name": "dry", "home": "/h", "state": "would-start"},
        {"kind": "owner", "name": "new", "home": "/h", "state": "started", "pid": 22},
        {"kind": "relay", "url": "http://127.0.0.1:3", "state": "failed",
         "error": "boom"},
    ]


def _stub_reconcile(
    monkeypatch: pytest.MonkeyPatch, items: list[dict[str, Any]]
) -> None:
    def _fake(
        cfg: dict[str, Any], *, dry_run: bool = False, python: str = PY
    ) -> list[dict[str, Any]]:
        return list(items)

    monkeypatch.setattr(up, "reconcile", _fake)


def test_quiet_prints_only_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _write(tmp_path, "up.json", {"owners": []})
    _stub_reconcile(monkeypatch, _mixed_items())
    assert up.main(["--config", str(p), "--quiet"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "relay http://127.0.0.1:2 unmanaged",
        "owner new@/h started pid 22",
        "relay http://127.0.0.1:3 failed (boom)",
    ]


def test_quiet_json_prints_only_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _write(tmp_path, "up.json", {"owners": []})
    _stub_reconcile(monkeypatch, _mixed_items())
    assert up.main(["--config", str(p), "--quiet", "--json"]) == 1
    items = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(i["kind"], i["state"]) for i in items] == [
        ("relay", "unmanaged"), ("owner", "started"), ("relay", "failed")]


def test_quiet_no_events_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _write(tmp_path, "up.json", {"owners": []})
    _stub_reconcile(monkeypatch, [
        {"kind": "relay", "url": "http://127.0.0.1:1", "state": "up"},
        {"kind": "owner", "name": "a", "home": "/h", "state": "running", "pid": 1},
        {"kind": "owner", "name": "b", "home": "/h", "state": "no-identity"},
        {"kind": "owner", "name": "c", "home": "/h", "state": "would-start"},
    ])
    assert up.main(["--config", str(p), "--quiet"]) == 0
    assert capsys.readouterr().out == ""
    assert up.main(["--config", str(p), "--quiet", "--json"]) == 0
    assert capsys.readouterr().out == ""


def test_quiet_exit_codes_match_full_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _write(tmp_path, "up.json", {"owners": []})
    # unmanaged alone still exits 0
    _stub_reconcile(monkeypatch, [
        {"kind": "relay", "url": "http://10.0.0.1:9", "state": "unmanaged"}])
    assert up.main(["--config", str(p), "--quiet"]) == 0
    assert "unmanaged" in capsys.readouterr().out
    # a failed item exits 1 even though quiet only filters display
    _stub_reconcile(monkeypatch, [
        {"kind": "owner", "name": "a", "home": "/h", "state": "failed",
         "error": "boom"}])
    assert up.main(["--config", str(p), "--quiet"]) == 1
    assert "failed" in capsys.readouterr().out


def test_print_launchagent_quiet_passthrough(capsys: pytest.CaptureFixture[str]) -> None:
    assert up.main(["--print-launchagent", "--quiet"]) == 0
    pl = plistlib.loads(capsys.readouterr().out.encode())
    assert pl["ProgramArguments"][1:] == ["-m", "phoning.cli", "up", "--quiet"]


def test_print_launchagent_quiet_after_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert up.main(["--print-launchagent", "--config", "/x/up.json", "--quiet"]) == 0
    pl = plistlib.loads(capsys.readouterr().out.encode())
    assert pl["ProgramArguments"][1:] == [
        "-m", "phoning.cli", "up", "--config", "/x/up.json", "--quiet"]
