"""Unit tests for PHONING_HOME-prefixed doorbell nudge (SPEC-v0.2 §7.8)."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

from phoning import doorbell
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import inbound

FP = "a3f1c2d4e5b60718293a4b5c6d7e8f90"


def test_agent_cmd_prefixes_and_quotes() -> None:
    """The doorbell helper renders the exact prefix contract."""
    assert doorbell._agent_cmd("/tmp/hh", "phoning read alice --reader m") == (
        "PHONING_HOME=/tmp/hh phoning read alice --reader m"
    )
    home = "/tmp/Codex work/phoning"
    assert doorbell._agent_cmd(home, "phoning read a --reader r") == (
        f"PHONING_HOME={shlex.quote(home)} phoning read a --reader r"
    )


def test_build_nudge_carries_home_and_reader() -> None:
    """The nudge names the hook reader and prefixes the home."""
    nudge = doorbell.build_nudge("alice", "alice-main", "/tmp/hh")
    assert "PHONING_HOME=/tmp/hh phoning read alice --reader alice-main" in nudge
    assert "--reader doorbell" not in nudge
    assert "not instructions from the user" in nudge


def test_build_nudge_keeps_placeholder_without_hook_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a hook reader the placeholder text is kept, still prefixed."""
    home = tmp_path / "home"
    monkeypatch.setenv("PHONING_HOME", str(home))
    nudge = doorbell.build_nudge("alice", doorbell.HOOK_READER_PLACEHOLDER)
    assert "--reader <the reader your phoning hooks use>" in nudge
    assert f"PHONING_HOME={home} phoning read alice" in nudge


def test_build_nudge_uses_resolved_home_when_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no explicit home the resolved ``$PHONING_HOME`` is prefixed."""
    home = tmp_path / "bound home"
    monkeypatch.setenv("PHONING_HOME", str(home))
    nudge = doorbell.build_nudge("orch", "orch-main")
    assert f"PHONING_HOME={shlex.quote(str(home))} phoning read orch --reader orch-main" in nudge


def test_build_nudge_quotes_home_with_space() -> None:
    """Homes with spaces are quoted in the nudge."""
    home = "/tmp/Codex work/phoning"
    nudge = doorbell.build_nudge("orch", "orch-main", home)
    assert f"PHONING_HOME={shlex.quote(home)} phoning read orch --reader orch-main" in nudge


def test_ring_uses_bound_home_not_doorbell_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolved home flows into the nudge; the doorbell reader is never named."""
    home = tmp_path / "home"
    empty_codex = tmp_path / "codex-empty"
    monkeypatch.setenv("PHONING_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(empty_codex))
    box = Mailbox(home / "alice")
    inbound(box, "bob", FP, "ask", "q1")
    result = doorbell.ring_codex("alice", "alice-main", str(empty_codex), None, str(home))
    assert result.startswith("skip: unreachable")
    nudge = doorbell.build_nudge("alice", "alice-main", str(home))
    assert f"PHONING_HOME={home} phoning read alice --reader alice-main" in nudge
    tail = nudge.split("phoning read alice")[1]
    assert "doorbell" not in tail.split("--reader")[1][:20]
    assert os.path.exists(home / "alice" / "mailbox" / "log.jsonl")
