"""Unit tests for the legacy bridge (``phoning.bridge``).

No retalk, no network and no real sleeps: the child is a fake with an
``io.StringIO`` stdout and the backoff wait is kept short by using tiny
intervals and bounded ``max_restarts``.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import queue
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from phoning import bridge
from phoning.mailbox import Mailbox


class FakeChild:
    """Minimal ``_Child`` stand-in: fixed stdout, no real process."""

    def __init__(self, lines: list[str] | None = None) -> None:
        self.stdout = io.StringIO("".join(f"{ln}\n" for ln in (lines or [])))
        self.terminated = False
        self.killed = False
        self.waited = 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited += 1
        return 0


class FakeClient:
    """Fake ``Retalk``: fixed contacts and a recorded ``follow_cmd``."""

    def __init__(self, contacts: list[str] | None = None) -> None:
        self._contacts = contacts if contacts is not None else []
        self.last_peers: list[str] = []
        self.last_interval: float | int = 0
        self.identity_dir: str | None = None

    def contacts(self) -> list[str]:
        return list(self._contacts)

    def follow_cmd(self, interval: float | int = 2, peers: Any = ()) -> list[str]:
        self.last_interval = interval
        self.last_peers = list(peers)
        return ["retalk", "receive", *[f"--peer {p}" for p in peers]]


def _make_user_dir(tmp_path: Path, name: str = "claude-lead") -> Path:
    user_dir = tmp_path / name
    (user_dir / "mailbox").mkdir(parents=True)
    (user_dir / "mailbox" / "log.jsonl").write_text("")
    return user_dir


def _make_identity(tmp_path: Path, name: str = "claude-lead") -> Path:
    legacy_user = tmp_path / "legacy-users" / name
    identity = legacy_user / "identity"
    identity.mkdir(parents=True)
    (legacy_user / "relay").write_text("http://relay.example\n")
    return identity


def _records(user_dir: Path) -> list[dict[str, Any]]:
    log = user_dir / "mailbox" / "log.jsonl"
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]


# -- record shape ---------------------------------------------------------


def test_appended_record_shape_and_via(tmp_path: Path) -> None:
    user_dir = _make_user_dir(tmp_path)
    identity = _make_identity(tmp_path)
    box = Mailbox(str(user_dir))
    line = json.dumps({"id": "tid-1", "name": "codex-review", "from": "f" * 32, "text": "hi there"})
    assert bridge._append_record(box, bridge._legacy_name(str(identity)), line) is not None
    rec = _records(user_dir)[0]
    assert rec["direction"] == "in"
    assert rec["tid"] == "tid-1"
    assert rec["peer"] == "codex-review"
    assert rec["peer_fp"] == "f" * 32
    assert rec["room"] is None
    assert rec["raw"] == "hi there"
    assert rec["via"] == "bridge:claude-lead"
    assert rec["recv_ts"].endswith("Z")
    assert rec["env"]["type"] == "chat"


def test_via_falls_back_to_identity_dir_basename(tmp_path: Path) -> None:
    # dir whose basename is not "identity" -> basename(DIR)
    identity = tmp_path / "weird-name"
    identity.mkdir()
    assert bridge._legacy_name(str(identity)) == "weird-name"
    # usual layout -> basename(dirname(DIR))
    usual = tmp_path / "users" / "claude-lead" / "identity"
    usual.mkdir(parents=True)
    assert bridge._legacy_name(str(usual)) == "claude-lead"


def test_peer_falls_back_to_fingerprint_when_name_missing(tmp_path: Path) -> None:
    user_dir = _make_user_dir(tmp_path)
    box = Mailbox(str(user_dir))
    line = json.dumps({"id": "t", "from": "a" * 32, "text": "x"})
    assert bridge._append_record(box, "claude-lead", line) is not None
    rec = _records(user_dir)[0]
    assert rec["peer"] == "a" * 32
    assert rec["peer_fp"] == "a" * 32


def test_malformed_and_contact_lines_are_ignored(tmp_path: Path) -> None:
    user_dir = _make_user_dir(tmp_path)
    box = Mailbox(str(user_dir))
    assert bridge._append_record(box, "claude-lead", "") is None
    assert bridge._append_record(box, "claude-lead", "not json") is None
    assert bridge._append_record(box, "claude-lead", "[1,2,3]") is None
    assert bridge._append_record(box, "claude-lead", json.dumps({"name": "x"})) is None
    assert bridge._append_record(
        box, "claude-lead", json.dumps({"kind": "contact", "text": "ignore me"})
    ) is None
    assert _records(user_dir) == []


def test_append_dedupes_on_direction_and_tid(tmp_path: Path) -> None:
    user_dir = _make_user_dir(tmp_path)
    box = Mailbox(str(user_dir))
    line = json.dumps({"id": "same", "name": "p", "from": "b" * 32, "text": "one"})
    assert bridge._append_record(box, "claude-lead", line) is not None
    again = json.dumps({"id": "same", "name": "p", "from": "b" * 32, "text": "two"})
    bridge._append_record(box, "claude-lead", again)
    assert len(_records(user_dir)) == 1


# -- peer validation ------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["codex-review", "a", "A1._-", "x" * 64],
)
def test_peer_regex_accepts(value: str) -> None:
    assert bridge._valid_peer(value) is True


@pytest.mark.parametrize(
    "value",
    ["-x", "../etc/passwd", "", ".hidden", "a" * 65, "with space"],
)
def test_peer_regex_rejects(value: str) -> None:
    assert bridge._valid_peer(value) is False


def test_main_rejects_option_like_peer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    identity = _make_identity(tmp_path)
    rc = bridge.main(
        [
            "claude-lead",
            "--from-identity",
            str(identity),
            "--peer=--all",
            "--session",
            "s",
        ]
    )
    assert rc == 2
    assert "invalid --peer" in capsys.readouterr().err


# -- checks order / exit codes -------------------------------------------


def test_missing_mailbox_exits_2_and_keeps_exit_code_2(tmp_path: Path) -> None:
    identity = _make_identity(tmp_path)
    home = tmp_path / "phoning-home"
    rc = bridge.main(
        [
            "ghost",
            "--from-identity",
            str(identity),
            "--peer",
            "codex-review",
            "--session",
            "s",
            "--home",
            str(home),
        ]
    )
    assert rc == 2
    assert not (home / "ghost").exists()


def test_missing_identity_dir_exits_2(tmp_path: Path) -> None:
    _make_user_dir(tmp_path)
    home = tmp_path
    rc = bridge.main(
        [
            "claude-lead",
            "--from-identity",
            str(tmp_path / "nope" / "identity"),
            "--peer",
            "codex-review",
            "--session",
            "s",
            "--home",
            str(home),
        ]
    )
    assert rc == 2


def test_unknown_peer_exits_2(tmp_path: Path) -> None:
    user_dir = _make_user_dir(tmp_path)
    identity = _make_identity(tmp_path)
    rc = bridge.main(
        [
            "claude-lead",
            "--from-identity",
            str(identity),
            "--peer",
            "codex-nobody",
            "--session",
            "s",
            "--home",
            str(tmp_path),
        ]
    )
    assert rc == 2
    assert (user_dir / "mailbox" / "log.jsonl").exists()


def test_check_peers_fails_closed_on_empty_contacts() -> None:
    with pytest.raises(bridge.BridgeError) as exc:
        bridge._check_peers(FakeClient([]), ["codex-review"])
    assert exc.value.code == 2


def test_check_peers_accepts_fingerprint_without_contact() -> None:
    peers = bridge._check_peers(FakeClient(["other"]), ["a" * 32])
    assert peers == ["a" * 32]


# -- follower pid logic ---------------------------------------------------


def test_live_follower_refuses(tmp_path: Path) -> None:
    legacy_user = tmp_path / "legacy-users" / "claude-lead"
    legacy_user.mkdir(parents=True)
    (legacy_user / "follow.codex-review.pid").write_text(str(os.getpid()))
    with pytest.raises(bridge.BridgeError) as exc:
        bridge._check_followers(str(legacy_user))
    assert exc.value.code == 3


def test_stale_and_garbage_followers_are_ignored(tmp_path: Path) -> None:
    # a pid that is not an integer, or does not exist, is stale and ignored
    legacy_user = tmp_path / "legacy-users" / "claude-lead"
    legacy_user.mkdir(parents=True)
    (legacy_user / "follow.a.pid").write_text("not-a-pid\n")
    (legacy_user / "follow.b.pid").write_text("\n")
    bridge._check_followers(str(legacy_user))  # no raise


def test_garbage_then_live_follower_refuses(tmp_path: Path) -> None:
    legacy_user = tmp_path / "legacy-users" / "claude-lead"
    legacy_user.mkdir(parents=True)
    (legacy_user / "follow.a.pid").write_text("not-a-pid\n")
    (legacy_user / "follow.b.pid").write_text(str(os.getpid()))
    with pytest.raises(bridge.BridgeError) as exc:
        bridge._check_followers(str(legacy_user))
    assert exc.value.code == 3
    assert "only reader" in str(exc.value)


def test_unreadable_pid_file_is_stale(tmp_path: Path) -> None:
    legacy_user = tmp_path / "legacy-users" / "claude-lead"
    legacy_user.mkdir(parents=True)
    (legacy_user / "follow.a.pid").mkdir()
    bridge._check_followers(str(legacy_user))  # no raise


# -- lock -----------------------------------------------------------------


def test_lock_refuses_when_another_process_holds_flock(tmp_path: Path) -> None:
    identity = _make_identity(tmp_path)
    path = str(tmp_path / "legacy-users" / "claude-lead" / "bridge.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(bridge.BridgeError) as exc:
            bridge._acquire_lock(str(identity))
        assert exc.value.code == 3
        assert "already running" in str(exc.value)
    finally:
        os.close(fd)


def test_lock_body_has_pid_and_mode(tmp_path: Path) -> None:
    identity = _make_identity(tmp_path)
    lock = bridge._acquire_lock(str(identity))
    try:
        lock.write_body(
            {"pid": os.getpid(), "since": "x", "name": "n", "session": "s", "peers": []}
        )
        path = tmp_path / "legacy-users" / "claude-lead" / "bridge.lock"
        assert (path.stat().st_mode & 0o777) == 0o600
        body = json.loads(path.read_text())
        assert body["pid"] == os.getpid()
    finally:
        lock.close()


def test_refused_start_leaves_no_lock_body(tmp_path: Path) -> None:
    # a live follower refuses the start before the lock is ever taken
    legacy_user = tmp_path / "legacy-users" / "claude-lead"
    legacy_user.mkdir(parents=True)
    (legacy_user / "follow.x.pid").write_text(str(os.getpid()))
    identity = tmp_path / "legacy-users" / "claude-lead" / "identity"
    identity.mkdir()
    with contextlib.suppress(bridge.BridgeError):
        bridge._check_followers(str(legacy_user))
    assert not (legacy_user / "bridge.lock").exists()


# -- run loop -------------------------------------------------------------


def _fingerprint_line(tid: str, text: str) -> str:
    return json.dumps({"id": tid, "name": "codex-review", "from": "c" * 32, "text": text})


def test_run_bridge_appends_then_stops(tmp_path: Path) -> None:
    user_dir = _make_user_dir(tmp_path)
    child = FakeChild([_fingerprint_line("t1", "hello")])
    stop = threading.Event()
    bridge.run_bridge(
        str(user_dir),
        "x",
        ["codex-review"],
        "s",
        interval=0.01,
        stop_event=stop,
        popen=lambda argv, **kw: child,
        max_restarts=1,
        client=FakeClient(["codex-review"]),
    )
    assert _records(user_dir)[0]["raw"] == "hello"


def test_run_bridge_restarts_after_child_exit(tmp_path: Path) -> None:
    user_dir = _make_user_dir(tmp_path)
    children = [
        FakeChild([_fingerprint_line("t1", "first")]),
        FakeChild([_fingerprint_line("t2", "second")]),
    ]
    counter = {"n": 0}

    def popen(argv: list[str], **kw: Any) -> FakeChild:
        child = children[min(counter["n"], len(children) - 1)]
        counter["n"] += 1
        return child

    bridge.run_bridge(
        str(user_dir),
        "x",
        ["codex-review"],
        "s",
        interval=0.01,
        stop_event=threading.Event(),
        popen=popen,
        max_restarts=2,
        client=FakeClient(["codex-review"]),
    )
    assert [r["raw"] for r in _records(user_dir)] == ["first", "second"]


def test_run_bridge_child_is_stopped_on_sigterm(tmp_path: Path) -> None:
    user_dir = _make_user_dir(tmp_path)
    holder: dict[str, FakeChild] = {}

    def popen(argv: list[str], **kw: Any) -> FakeChild:
        child = FakeChild([])  # EOF immediately -> child exits
        holder["child"] = child
        return child

    bridge.run_bridge(
        str(user_dir),
        "x",
        ["codex-review"],
        "s",
        interval=0.01,
        stop_event=threading.Event(),
        popen=popen,
        max_restarts=1,
        client=FakeClient(["codex-review"]),
    )
    assert holder["child"].terminated is True


def test_run_bridge_backoff_is_bounded(tmp_path: Path) -> None:
    # several empty children: no crash, no unbounded loop
    user_dir = _make_user_dir(tmp_path)
    calls = {"n": 0}

    def popen(argv: list[str], **kw: Any) -> FakeChild:
        calls["n"] += 1
        return FakeChild([])

    bridge.run_bridge(
        str(user_dir),
        "x",
        ["codex-review"],
        "s",
        interval=0.01,
        stop_event=threading.Event(),
        popen=popen,
        max_restarts=3,
        client=FakeClient(["codex-review"]),
    )
    assert calls["n"] == 3


def test_backoff_constants() -> None:
    assert bridge._BACKOFF_START == 1.0
    assert bridge._BACKOFF_MAX == 30.0
    assert min(bridge._BACKOFF_START * 2, bridge._BACKOFF_MAX) == 2.0


def test_follow_cmd_receives_interval_and_peers(tmp_path: Path) -> None:
    user_dir = _make_user_dir(tmp_path)
    client = FakeClient(["codex-review"])
    bridge.run_bridge(
        str(user_dir),
        "x",
        ["codex-review", "codex-worker"],
        "s",
        interval=7,
        stop_event=threading.Event(),
        popen=lambda argv, **kw: FakeChild([]),
        max_restarts=1,
        client=client,
    )
    assert client.last_peers == ["codex-review", "codex-worker"]
    assert client.last_interval == 7


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turn long backoff sleeps into no-ops inside run_bridge."""
    real_event = threading.Event
    orig_wait = real_event.wait

    def fast_wait(self: threading.Event, timeout: float | None = None) -> bool:
        if timeout is None:
            # Thread.start() waits on an untimed Event; leave that one alone.
            return orig_wait(self)
        return self.is_set()

    monkeypatch.setattr(real_event, "wait", fast_wait, raising=False)
    yield


# -- review fixes: queued lines, crashes, client build, logging -----------


def test_queued_lines_are_appended_after_stop_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every line already queued is appended before returning, even after stop."""
    user_dir = _make_user_dir(tmp_path)
    box = Mailbox(str(user_dir))
    stop = threading.Event()
    queued: list[str | None] = [
        _fingerprint_line("t1", "one"),
        _fingerprint_line("t2", "two"),
        None,
    ]

    class BlockingChild:
        stdout = io.StringIO("")

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

    client = FakeClient(["codex-review"])

    def fake_reader(stdout: Any, out_queue: queue.Queue[str | None]) -> None:
        # push the queued lines, then leave the reader thread
        for item in queued:
            out_queue.put(item)

    monkeypatch.setattr(bridge, "_reader_loop", fake_reader)
    # stop is already set, so the loop body never runs and only the drain path
    # can append anything.
    stop.set()
    rc_set, delivered, _rc = bridge._drain(
        box,
        "claude-lead",
        client,
        0.01,
        stop,
        lambda argv, **kw: BlockingChild(),
        ["codex-review"],
    )
    raws = [r["raw"] for r in _records(user_dir)]
    assert raws == ["one", "two"]
    assert delivered is True
    assert rc_set is True


def test_fake_mailbox_that_raises_makes_run_bridge_log_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user_dir = _make_user_dir(tmp_path)
    child = FakeChild([_fingerprint_line("t1", "boom")])

    class BadMailbox:
        def append(self, record: dict[str, Any]) -> int | None:
            raise OSError("disk full")

    with pytest.raises(OSError):
        bridge.run_bridge(
            str(user_dir),
            "x",
            ["codex-review"],
            "s",
            interval=0.01,
            stop_event=threading.Event(),
            popen=lambda argv, **kw: child,
            max_restarts=1,
            mailbox=BadMailbox(),  # type: ignore[arg-type]
            client=FakeClient(["codex-review"]),
        )


def test_main_returns_1_and_logs_fatal_on_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_user_dir(tmp_path)
    identity = _make_identity(tmp_path)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(bridge, "run_bridge", boom)
    monkeypatch.setattr(
        bridge, "_make_client", lambda identity_dir, relay: FakeClient(["codex-review"])
    )
    rc = bridge.main(
        [
            "claude-lead",
            "--from-identity",
            str(identity),
            "--peer",
            "codex-review",
            "--session",
            "s",
            "--home",
            str(tmp_path),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "fatal: OSError" in err
    assert "started" in err
    assert "stopped" in err


def test_run_bridge_without_client_builds_it_and_spawns_follow_argv(tmp_path: Path) -> None:
    user_dir = _make_user_dir(tmp_path)
    identity = _make_identity(tmp_path)
    captured: dict[str, list[str]] = {}
    identity_arg = str(identity)

    def popen(argv: list[str], **kw: Any) -> FakeChild:
        captured["argv"] = argv
        return FakeChild([])

    bridge.run_bridge(
        str(user_dir),
        identity_arg,
        ["codex-review"],
        "s",
        interval=0.01,
        stop_event=threading.Event(),
        popen=popen,
        max_restarts=1,
    )
    argv = captured["argv"]
    assert "--dir" in argv
    assert argv[argv.index("--dir") + 1] == identity_arg
    assert "-r" in argv
    assert argv[argv.index("-r") + 1] == "http://relay.example"
    assert "--peer" in argv and "codex-review" in argv
    assert "--follow" in argv and "--quiet" in argv and "--save" in argv


def test_appended_seq_log_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user_dir = _make_user_dir(tmp_path)
    child = FakeChild([_fingerprint_line("t1", "logme")])
    bridge.run_bridge(
        str(user_dir),
        "x",
        ["codex-review"],
        "s",
        interval=0.01,
        stop_event=threading.Event(),
        popen=lambda argv, **kw: child,
        max_restarts=1,
        client=FakeClient(["codex-review"]),
    )
    err = capsys.readouterr().err
    assert "appended seq=" in err
    assert "peer=codex-review" in err
    assert "logme" not in err


def test_restart_line_shows_real_rc(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user_dir = _make_user_dir(tmp_path)
    children = [
        FakeChild([_fingerprint_line("t1", "first")]),
        FakeChild([_fingerprint_line("t2", "second")]),
    ]
    counter = {"n": 0}

    def popen(argv: list[str], **kw: Any) -> FakeChild:
        child = children[min(counter["n"], len(children) - 1)]
        counter["n"] += 1
        return child

    bridge.run_bridge(
        str(user_dir),
        "x",
        ["codex-review"],
        "s",
        interval=0.01,
        stop_event=threading.Event(),
        popen=popen,
        max_restarts=2,
        client=FakeClient(["codex-review"]),
    )
    err = capsys.readouterr().err
    assert "child exited rc=0; restart in" in err


def test_no_stopped_line_after_refused_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy_user = tmp_path / "legacy-users" / "claude-lead"
    legacy_user.mkdir(parents=True)
    (legacy_user / "follow.x.pid").write_text(str(os.getpid()))
    identity = legacy_user / "identity"
    identity.mkdir()
    _make_user_dir(tmp_path)
    monkeypatch.setattr(
        bridge, "_make_client", lambda identity_dir, relay: FakeClient(["codex-review"])
    )
    rc = bridge.main(
        [
            "claude-lead",
            "--from-identity",
            str(identity),
            "--peer",
            "codex-review",
            "--session",
            "s",
            "--home",
            str(tmp_path),
        ]
    )
    assert rc == 3
    err = capsys.readouterr().err
    # assert on the *log lines*, not on bare substrings: the pytest tmp path
    # itself contains the word "stopped", which would collide with a plain
    # ``"stopped" in err`` check.
    lines = [ln for ln in err.splitlines() if ln.startswith("phoning bridge: ")]
    assert "phoning bridge: stopped" not in lines
    assert not any(ln.startswith("phoning bridge: started") for ln in lines)
    assert any("only reader" in ln for ln in lines)
