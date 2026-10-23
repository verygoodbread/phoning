"""Acceptance: a phoning home is private to its owner."""

from __future__ import annotations

import stat
from pathlib import Path

from phoning.mailbox import Mailbox


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_mailbox_dirs_and_files_are_owner_only(tmp_path: Path) -> None:
    user = tmp_path / "alice"
    mb = Mailbox(user)
    mb.append({"direction": "in", "tid": "t1", "peer": "bob", "room": None, "recv_ts": "x",
               "env": {"type": "chat", "subject": "hi"}})
    mb.commit("main", 1)
    assert _mode(user) == 0o700
    assert _mode(user / "mailbox") == 0o700
    assert _mode(user / "mailbox" / "cursors") == 0o700
    for f in (user / "mailbox" / "log.jsonl", user / "mailbox" / "cursors" / "main.json"):
        assert _mode(f) & 0o077 == 0, f
