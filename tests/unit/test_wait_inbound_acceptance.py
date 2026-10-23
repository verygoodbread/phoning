"""Acceptance: an agent's own sends must not wake its own `wait`."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from phoning.mailbox import Mailbox


def _rec(direction: str, tid: str) -> dict[str, object]:
    return {"direction": direction, "tid": tid, "peer": "bob" if direction == "in" else None,
            "room": None, "recv_ts": "x", "env": {"type": "chat", "subject": tid}}


def test_wait_ignores_own_outbound_by_default(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "alice")
    mb.append(_rec("out", "mine"))
    t0 = time.monotonic()
    assert mb.wait("main", timeout=0.5) == []
    assert time.monotonic() - t0 >= 0.4


def test_wait_wakes_on_inbound_after_own_outbound(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "alice")
    mb.append(_rec("out", "mine"))
    threading.Timer(0.2, lambda: mb.append(_rec("in", "theirs"))).start()
    got = mb.wait("main", timeout=5)
    assert [r["env"]["subject"] for r in got] == ["theirs"]


def test_wait_can_include_own_when_asked(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "alice")
    mb.append(_rec("out", "mine"))
    assert [r["env"]["subject"] for r in mb.wait("main", timeout=1, include_own=True)] == ["mine"]
