"""Unit tests for the owner daemon."""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path
from typing import Any

from phoning import daemon
from phoning.daemon import _handle_line, _load_contacts
from phoning.envelope import encode, make
from phoning.mailbox import Mailbox


class FakeChild:
    """Minimal child with no hold: exits immediately (EOF)."""

    def __init__(self, lines: list[str] | None = None) -> None:
        self.stdout: io.StringIO = io.StringIO("".join(lines or []))
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


class FakeRetalk:
    """Mutable contacts plus recorded spawns."""

    def __init__(self, contacts: list[str]) -> None:
        self._contacts: list[str] = list(contacts)
        self.spawns: list[list[str]] = []
        self.lock = threading.Lock()

    def contacts(self) -> list[str]:
        with self.lock:
            return list(self._contacts)

    def set_contacts(self, contacts: list[str]) -> None:
        with self.lock:
            self._contacts = list(contacts)

    def follow_cmd(self, interval: Any, peers: list[str]) -> list[str]:
        with self.lock:
            self.spawns.append(list(peers))
        argv = ["retalk", "receive"]
        for p in peers:
            argv += ["--peer", p]
        return argv

    def spawn_peers(self) -> list[list[str]]:
        with self.lock:
            return [list(s) for s in self.spawns]


def _wait_for(pred: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_handle_line_keeps_fingerprint_and_raw(tmp_path: Path) -> None:
    """Inbound records keep verified fingerprint and raw wire text."""
    box = Mailbox(str(tmp_path / "alice"))
    env = make("chat", "bob", "s1", "hello")
    raw = encode(env)
    line = json.dumps({"id": "t1", "from": "ab" * 16, "name": "bob", "text": raw})
    assert _handle_line(box, line) is True
    recs = box.read("r1")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["direction"] == "in"
    assert rec["peer"] == "bob"
    assert rec["peer_fp"] == "ab" * 16
    assert rec["raw"] == raw
    assert rec["env"]["subject"] == "hello"


def test_handle_line_ignores_non_message(tmp_path: Path) -> None:
    """Malformed or non-message lines are ignored."""
    box = Mailbox(str(tmp_path / "alice"))
    assert _handle_line(box, "") is False
    assert _handle_line(box, "not json") is False
    assert _handle_line(box, json.dumps({"no": "text"})) is False
    assert box.read("r") == []


def test_load_contacts_never_raises() -> None:
    """Transport errors and bad entries yield an empty/deduped list."""

    class _Bad:
        def contacts(self) -> list[str]:
            raise OSError("down")

    assert _load_contacts(_Bad()) == []  # type: ignore[arg-type]

    class _Mixed:
        def contacts(self) -> list[Any]:
            return ["bob", "", "bob", "carol", 123]  # type: ignore[return-value]

    assert _load_contacts(_Mixed()) == ["bob", "carol"]  # type: ignore[arg-type]


def test_run_owner_refreshes_contacts(tmp_path: Path) -> None:
    """Second spawn uses fresh contacts; deterministic, no sleep races."""
    user = tmp_path / "alice"
    user.mkdir(parents=True)
    box = Mailbox(str(user))
    client = FakeRetalk(["bob"])
    stop = threading.Event()

    def popen(argv: list[str], **kw: Any) -> FakeChild:
        return FakeChild()

    th = threading.Thread(
        target=daemon.run_owner,
        kwargs={
            "user_dir": str(user),
            "session": "main",
            "interval": 1,
            "stop_event": stop,
            "popen": popen,
            "retalk": client,
            "mailbox": box,
        },
        daemon=True,
    )
    th.start()
    try:
        assert _wait_for(lambda: len(client.spawn_peers()) >= 1), "first spawn missing"
        # Set the new contacts, then let the first child exit (FakeChild
        # with no hold exits on its own); the next spawn must be fresh.
        client.set_contacts(["bob", "carol"])
        assert _wait_for(lambda: len(client.spawn_peers()) >= 2), "second spawn missing"
        second = client.spawn_peers()[1]
        assert set(second) == {"bob", "carol"}
    finally:
        stop.set()
        th.join(timeout=5.0)
        assert not th.is_alive()


def test_run_owner_appends_inbound(tmp_path: Path) -> None:
    """One NDJSON line from the child lands in the mailbox with evidence."""
    user = tmp_path / "alice"
    user.mkdir(parents=True)
    box = Mailbox(str(user))
    env = make("chat", "bob", "s1", "hi")
    raw = encode(env)
    line = json.dumps({"id": "w1", "from": "cd" * 16, "name": "bob", "text": raw}) + "\n"
    client = FakeRetalk(["bob"])
    stop = threading.Event()

    def popen(argv: list[str], **kw: Any) -> FakeChild:
        return FakeChild([line])

    th = threading.Thread(
        target=daemon.run_owner,
        kwargs={
            "user_dir": str(user),
            "session": "main",
            "interval": 1,
            "stop_event": stop,
            "popen": popen,
            "max_restarts": 1,
            "retalk": client,
            "mailbox": box,
        },
        daemon=True,
    )
    th.start()
    th.join(timeout=5.0)
    assert not th.is_alive()
    recs = box.read("audit")
    assert len(recs) == 1
    assert recs[0]["peer_fp"] == "cd" * 16
    assert recs[0]["raw"] == raw
