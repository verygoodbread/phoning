"""Text hygiene for untrusted display strings (docs/SPEC-v0.2.md §6.1).

Every text surface that shows strings authored by other agents passes them
through these helpers first, so control characters can never forge structure
(newlines in a header, escape sequences, tabs that fake indentation).

This module imports nothing from :mod:`phoning`.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["clean_block", "clean_line", "has_control"]

# C0 controls U+0000-U+001F, DEL U+007F and C1 controls U+0080-U+009F.
CONTROL_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_LINE_BREAK_RE: Final[re.Pattern[str]] = re.compile(r"\r\n|\r|\n")
_TAB_RE: Final[re.Pattern[str]] = re.compile(r"\t")


def has_control(s: str) -> bool:
    """Return True when ``s`` contains any C0, DEL or C1 control character."""
    return CONTROL_RE.search(s) is not None


def clean_line(s: str) -> str:
    """Return ``s`` as single-line display text.

    ``\\r\\n``, ``\\r``, ``\\n`` and ``\\t`` each become one space. Every
    other C0 control (U+0000-U+001F), DEL (U+007F) and C1 control
    (U+0080-U+009F) is removed.
    """
    out = _LINE_BREAK_RE.sub(" ", s)
    out = _TAB_RE.sub(" ", out)
    return CONTROL_RE.sub("", out)


def clean_block(s: str) -> str:
    """Return ``s`` as multi-line display text.

    ``\\r\\n`` and ``\\r`` become ``\\n``, and ``\\t`` becomes four spaces.
    Every other C0, DEL and C1 control character is removed, except ``\\n``.
    """
    out = _LINE_BREAK_RE.sub("\n", s)
    out = _TAB_RE.sub("    ", out)

    def _strip(m: re.Match[str]) -> str:
        return "\n" if m.group() == "\n" else ""

    return CONTROL_RE.sub(_strip, out)
