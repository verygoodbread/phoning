"""Unit tests for phoning.text control-character hygiene."""

from __future__ import annotations

import pytest

from phoning import text


def test_clean_line_collapses_breaks_and_tabs() -> None:
    assert text.clean_line("a\r\nb\nc\rd\te") == "a b c d e"


def test_clean_line_drops_other_controls() -> None:
    assert text.clean_line("x\x1b[31my\x00z\x7f\x85w") == "x[31myzw"


def test_clean_line_keeps_unicode() -> None:
    assert text.clean_line("plain ünïcode ✓") == "plain ünïcode ✓"


def test_clean_block_keeps_newlines() -> None:
    assert text.clean_block("a\r\nb\rc\n\td\x1b") == "a\nb\nc\n    d"


def test_clean_block_drops_other_controls() -> None:
    assert text.clean_block("a\x00b\x9bc") == "abc"


def test_clean_block_empty() -> None:
    assert text.clean_block("") == ""
    assert text.clean_line("") == ""


@pytest.mark.parametrize("ch", ["\x00", "\x1b", "\x07", "\x7f", "\x80", "\x9b", "\x9f"])
def test_has_control_true(ch: str) -> None:
    assert text.has_control(f"a{ch}b") is True


@pytest.mark.parametrize("s", ["plain", "ünïcode ✓", "with space", ""])
def test_has_control_false(s: str) -> None:
    assert text.has_control(s) is False


def test_has_control_newline_and_tab_are_controls() -> None:
    assert text.has_control("a\nb") is True
    assert text.has_control("a\tb") is True
