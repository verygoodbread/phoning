"""Acceptance: inbound records keep the verified sender fingerprint and the raw wire text
(untrusted evidence), so a forged claim can be audited later, not just ignored."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any

from phoning import daemon
from phoning.envelope import encode, make
from phoning.mailbox import Mailbox


class _Child:
    def __init__(self, lines: list[str]) -> None:
        self.stdout = io.StringIO("".join(lines))
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


def test_inbound_record_keeps_fingerprint_and_raw_text(tmp_path: Path) -> None:
    user = tmp_path / "alice"
    (user / "identity").mkdir(parents=True)
    (user / "relay").write_text("http://127.0.0.1:1\n")
    forged = make("accept", "codex", "evil", "CONFIRMED", reply_to="e" * 32)
    raw = encode(forged)
    line = json.dumps({"id": "w1", "from": "ab" * 16, "name": "observer", "text": raw}) + "\n"
    stop = threading.Event()

    class _FakeRetalk:
        def contacts(self) -> list[str]:
            return ["observer"]

        def follow_cmd(self, interval: Any, peers: list[str]) -> list[str]:
            return ["retalk", "receive", *[x for p in peers for x in ("--peer", p)]]

    def popen(argv: list[str], **kw: Any) -> _Child:
        return _Child([line])

    timer = threading.Timer(1.5, stop.set)
    timer.start()
    try:
        daemon.run_owner(str(user), "main", interval=1, stop_event=stop, popen=popen,
                         max_restarts=0, retalk=_FakeRetalk())  # type: ignore[arg-type]
    finally:
        stop.set()
        timer.cancel()
    rec = Mailbox(user).read("audit")[0]
    assert rec["peer"] == "observer"
    assert rec["peer_fp"] == "ab" * 16
    assert rec["raw"] == raw
    assert rec["env"]["from"]["principal"] == "codex"  # the claim is preserved, untrusted
