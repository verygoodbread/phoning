"""Unit tests for the phone-book command names (``phoning ring`` = ``phoning doorbell`` and so on)."""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from phoning import cli


def test_every_alias_names_a_real_command() -> None:
    commands = set(cli.build_parser()._subparsers._group_actions[0].choices)  # type: ignore[union-attr]
    for alias, target in cli.ALIASES.items():
        assert target in commands, alias
        assert alias not in commands, f"{alias} shadows a real command"


def test_help_lists_the_phone_names() -> None:
    text = cli.build_parser().format_help()
    for alias in ("group-call", "phonebook", "ring", "switchboard"):
        assert alias in text


@pytest.mark.parametrize(("alias", "target"), [("group-call", "board"), ("listen", "wait"), ("line", "room")])
def test_parser_aliases_reach_the_same_parser(alias: str, target: str, capsys: Any) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([alias, "--help"])
    assert exc.value.code == 0
    assert f"usage: phoning {target}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("alias", "module", "func"),
    [("phonebook", "phoning.directory", "main"), ("ring", "phoning.doorbell", "main"),
     ("dial-tone", "phoning.up", "main"), ("caller-id", "phoning.intro", "profile_main")],
)
def test_delegated_aliases_forward_the_rest(alias: str, module: str, func: str, monkeypatch: Any) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(importlib.import_module(module), func, lambda argv=None: seen.append(list(argv)) or 0)
    assert cli.main([alias, "a", "--b"]) == 0
    assert seen == [["a", "--b"]]


def test_alias_only_applies_to_the_command_word(monkeypatch: Any) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(importlib.import_module("phoning.directory"), "main",
                        lambda argv=None: seen.append(list(argv)) or 0)
    assert cli.main(["phonebook", "ring", "listen"]) == 0
    assert seen == [["ring", "listen"]]
