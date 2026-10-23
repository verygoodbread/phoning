"""Acceptance: wave B wires prune, directory and bridge into the `phoning` CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from phoning import bridge, cli, directory, retention
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import ROOT


def phoning(tmp: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(
        os.environ,
        PHONING_HOME=str(tmp / "home"),
        PHONING_DIRECTORY=str(tmp / "directory"),
        PYTHONPATH=str(ROOT),
        COLUMNS="200",
    )
    return subprocess.run([sys.executable, "-m", "phoning.cli", *args], env=env, input="",
                          capture_output=True, text=True, timeout=60)


def help_line(out: str, word: str) -> str:
    """The `--help` line that introduces subcommand `word`."""
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == word:
            return line
    raise AssertionError(f"no help line for {word!r} in:\n{out}")


def test_help_lists_the_three_new_subcommands(tmp_path: Path) -> None:
    out = phoning(tmp_path, "--help").stdout
    for sub in ("prune", "directory", "bridge"):
        assert help_line(out, sub), sub
    for sub in ("up", "hook", "web", "mcp", "doorbell", "send", "own", "board"):
        assert help_line(out, sub), sub  # nothing that existed before went missing


def test_web_and_doorbell_help_text(tmp_path: Path) -> None:
    out = phoning(tmp_path, "--help").stdout
    assert "local viewer (read-only unless --allow-send)" in help_line(out, "web")
    assert "rules" in help_line(out, "doorbell").lower()


def test_module_docstring_usage_lines_mention_the_new_commands() -> None:
    doc = cli.__doc__ or ""
    for sub in ("phoning prune", "phoning directory", "phoning bridge"):
        assert sub in doc, sub
    doorbell_lines = [ln for ln in doc.splitlines() if "phoning doorbell" in ln]
    assert doorbell_lines and any("rules" in ln.lower() for ln in doorbell_lines)


@pytest.mark.parametrize(
    ("sub", "module", "func"),
    [("prune", retention, "main"), ("directory", directory, "main"), ("bridge", bridge, "main")],
)
def test_each_new_subcommand_hands_the_rest_of_argv_to_its_module(
    monkeypatch: pytest.MonkeyPatch, sub: str, module: object, func: str
) -> None:
    seen: list[list[str]] = []

    def fake_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 7

    monkeypatch.setattr(module, func, fake_main)
    assert cli.main([sub, "alice", "--flag", "value", "--other"]) == 7
    assert seen == [["alice", "--flag", "value", "--other"]]


def test_prune_through_the_real_cli(tmp_path: Path) -> None:
    Mailbox(tmp_path / "home" / "alice")
    p = phoning(tmp_path, "prune", "alice", "--dry-run")
    assert p.returncode == 0, p.stderr
    assert "nothing to prune" in p.stdout
    p = phoning(tmp_path, "prune", "alice", "--dry-run", "--json")
    assert p.returncode == 0 and json.loads(p.stdout)["name"] == "alice"
    assert phoning(tmp_path, "prune", "ghost").returncode == 2  # no such mailbox


def test_directory_through_the_real_cli(tmp_path: Path) -> None:
    p = phoning(tmp_path, "directory", "list")
    assert (p.returncode, p.stdout) == (0, "")
    assert phoning(tmp_path, "directory").returncode == 2  # a subcommand is required


def test_bridge_through_the_real_cli_refuses_without_a_mailbox(tmp_path: Path) -> None:
    ident = tmp_path / "legacy" / "identity"
    ident.mkdir(parents=True)
    p = phoning(tmp_path, "bridge", "ghost", "--from-identity", str(ident), "--peer", "bob", "--session", "s")
    assert p.returncode == 2 and "no mailbox for ghost" in p.stderr, p.stderr
    assert phoning(tmp_path, "bridge").returncode == 2  # usage error, not a traceback
    assert not (tmp_path / "legacy" / "bridge.lock").exists()  # a refused start leaves no lock behind
