"""Unit: call cards and alias picking in `phoning/remote.py` (docs/spec/remote.md, Part B).

Pure tests: no relay, no retalk, no files. Only the names the spec gives are used
(`make_card`, `parse_card`, `CardError`, `pick_alias`).
"""

from __future__ import annotations

import base64
import copy
import json
from typing import Any

import pytest

from phoning.remote import CardError, make_card, parse_card, pick_alias

FP1 = "0123456789abcdef0123456789abcdef"
FP2 = "fedcba9876543210fedcba9876543210"


def _valid() -> dict[str, Any]:
    return {
        "v": 1,
        "relay": "http://100.101.102.103:8766",
        "who": "sam-mbp",
        "room": "team",
        "lines": [
            {"name": "claude-lead", "fingerprint": FP1, "harness": "claude", "role": "lead",
             "code": "D6Be9b2DsiNwqVnJjSH3Mw"},
            {"name": "codex.worker_2", "fingerprint": FP2, "harness": None, "role": None, "code": None},
        ],
    }


def _raw(obj: Any) -> str:
    """Encode ANY JSON value the way the spec says a card is encoded (no validation)."""
    data = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return "call-card:1:" + base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _raw_bytes(data: bytes) -> str:
    return "call-card:1:" + base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# -- make_card / parse_card round trip -----------------------------------------


def test_make_card_is_the_exact_wire_format() -> None:
    c = _valid()
    card = make_card(c)
    assert card == _raw(c)
    assert card.startswith("call-card:1:")
    assert "=" not in card and "\n" not in card and " " not in card


@pytest.mark.parametrize("mutate", [
    lambda c: c,
    lambda c: c.update(room=None),
    lambda c: c.update(relay="https://relay.example.com/"),
    lambda c: c.update(who="a" * 24),
    lambda c: c.update(who="0"),
    lambda c: c.update(room="a" * 64),
    lambda c: c.update(lines=c["lines"][:1]),
    lambda c: c.update(lines=[{"name": f"n{i}", "fingerprint": f"{i:032x}", "harness": None,
                               "role": None, "code": f"code-{i:04d}"} for i in range(64)]),
    lambda c: c["lines"][0].update(code="a" * 8),
    lambda c: c["lines"][0].update(code="Z" * 64),
    lambda c: c["lines"][0].update(code="aB3_-xY9"),
    lambda c: c["lines"][1].update(code="D6Be9b2DsiNwqVnJjSH3Mw"),
])
def test_round_trip_for_valid_cards(mutate: Any) -> None:
    c = _valid()
    mutate(c)
    original = copy.deepcopy(c)
    assert parse_card(make_card(c)) == original


def test_parse_accepts_a_hand_encoded_card() -> None:
    c = _valid()
    assert parse_card(_raw(c)) == c


# -- tolerance: whitespace, line wraps, surrounding text ---------------------------


def test_parse_tolerates_line_wraps_and_whitespace_anywhere() -> None:
    card = make_card(_valid())
    # a chat app wrapping at 20 columns, with odd indentation and a CRLF
    wrapped = "\n".join(card[i:i + 20] for i in range(0, len(card), 20))
    assert parse_card(wrapped) == _valid()
    spaced = "  " + " \t".join(card[i:i + 7] for i in range(0, len(card), 7)) + "\r\n"
    assert parse_card(spaced) == _valid()
    # even inside the prefix
    assert parse_card("call-\ncard:1:\n" + card[len("call-card:1:"):]) == _valid()


def test_parse_finds_a_card_embedded_in_other_text() -> None:
    card = make_card(_valid())
    msg = f"hey! join my call:\n\n    they run: phoning remote join '{card}'\n\nsee you there."
    assert parse_card(msg) == _valid()
    assert parse_card(f"```\n{card}\n```") == _valid()
    assert parse_card(f"<{card}>") == _valid()


def test_parse_takes_the_first_card_in_the_text() -> None:
    first = _valid()
    second = _valid()
    second["who"] = "other-box"
    text = f"first: '{make_card(first)}' second: '{make_card(second)}'"
    assert parse_card(text) == first


def test_unknown_extra_keys_are_ignored() -> None:
    c = _valid()
    c["future"] = {"anything": [1, 2, 3]}
    c["lines"][0]["avatar"] = "x.png"
    got = parse_card(_raw(c))  # no CardError
    assert got["v"] == 1 and got["relay"] == c["relay"] and got["who"] == c["who"]
    assert got["room"] == c["room"]
    assert [ln["name"] for ln in got["lines"]] == ["claude-lead", "codex.worker_2"]
    assert [ln["fingerprint"] for ln in got["lines"]] == [FP1, FP2]
    assert got["lines"][0]["harness"] == "claude" and got["lines"][0]["role"] == "lead"
    assert got["lines"][0]["code"] == "D6Be9b2DsiNwqVnJjSH3Mw" and got["lines"][1]["code"] is None


def test_a_line_without_code_parses_as_code_null() -> None:
    c = _valid()
    for ln in c["lines"]:
        del ln["code"]
    got = parse_card(_raw(c))
    assert [ln["code"] for ln in got["lines"]] == [None, None]
    # and that parsed form round-trips exactly
    assert parse_card(make_card(got)) == got


# -- validation: every rule raises CardError --------------------------------------


@pytest.mark.parametrize("text", [
    "",
    "no card here",
    "call-card:2:" + _raw(_valid())[len("call-card:1:"):],
    "call-card:1:",
    "callcard:1:" + _raw(_valid())[len("call-card:1:"):],
])
def test_missing_card_is_a_card_error(text: str) -> None:
    with pytest.raises(CardError):
        parse_card(text)


@pytest.mark.parametrize("payload", ["A", "AAAAA", "Zm9vY"])
def test_bad_base64_is_a_card_error(payload: str) -> None:
    with pytest.raises(CardError):
        parse_card("call-card:1:" + payload)


@pytest.mark.parametrize("data", [b"not json", b"{\"v\":1,", b"\xff\xfe\x00garbage"])
def test_bad_json_is_a_card_error(data: bytes) -> None:
    with pytest.raises(CardError):
        parse_card(_raw_bytes(data))


@pytest.mark.parametrize("value", [[1, 2], "a string", 5, None])
def test_json_that_is_not_an_object_is_a_card_error(value: Any) -> None:
    with pytest.raises(CardError):
        parse_card(_raw(value))


def _broken(**changes: Any) -> dict[str, Any]:
    c = _valid()
    for k, v in changes.items():
        if v is _DROP:
            c.pop(k)
        else:
            c[k] = v
    return c


_DROP = object()


@pytest.mark.parametrize("v", [2, 0, "1", 1.5, None, _DROP])
def test_version_must_be_1(v: Any) -> None:
    with pytest.raises(CardError):
        parse_card(_raw(_broken(v=v)))


@pytest.mark.parametrize("relay", ["ftp://host:8766", "127.0.0.1:8766", "ws://h:1", "", "http:/x",
                                   5, None, ["http://h"], _DROP])
def test_relay_must_be_http_or_https(relay: Any) -> None:
    with pytest.raises(CardError):
        parse_card(_raw(_broken(relay=relay)))


@pytest.mark.parametrize("who", ["", "-box", "Sam-mbp", "sam_mbp", "sam.mbp", "a" * 25, "a b",
                                 5, None, _DROP])
def test_who_must_be_a_short_lowercase_label(who: Any) -> None:
    with pytest.raises(CardError):
        parse_card(_raw(_broken(who=who)))


@pytest.mark.parametrize("room", ["", "-team", ".team", "../x", "a/b", "a" * 65, "te am", 5, [], _DROP])
def test_room_must_be_a_legal_name_or_null(room: Any) -> None:
    with pytest.raises(CardError):
        parse_card(_raw(_broken(room=room)))


def _lines(n: int) -> list[dict[str, Any]]:
    return [{"name": f"n{i}", "fingerprint": f"{i:032x}", "harness": None, "role": None, "code": None}
            for i in range(n)]


@pytest.mark.parametrize("lines", [[], _lines(65), "claude-lead", None, {"name": "x"}, _DROP])
def test_lines_must_be_a_list_of_1_to_64(lines: Any) -> None:
    with pytest.raises(CardError):
        parse_card(_raw(_broken(lines=lines)))


def _with_line(**changes: Any) -> dict[str, Any]:
    c = _valid()
    for k, v in changes.items():
        if v is _DROP:
            c["lines"][0].pop(k)
        else:
            c["lines"][0][k] = v
    return c


@pytest.mark.parametrize("name", ["", "-x", ".x", "_x", "a/b", "../etc", "a b", "a" * 65, 5, None, _DROP])
def test_line_name_must_be_legal(name: Any) -> None:
    with pytest.raises(CardError):
        parse_card(_raw(_with_line(name=name)))


@pytest.mark.parametrize("fp", ["A" * 32, FP1.upper(), "0" * 31, "0" * 33, "g" * 32, "", " " + "0" * 31,
                                5, None, _DROP])
def test_line_fingerprint_must_be_32_lowercase_hex(fp: Any) -> None:
    with pytest.raises(CardError):
        parse_card(_raw(_with_line(fingerprint=fp)))


@pytest.mark.parametrize("field", ["harness", "role"])
@pytest.mark.parametrize("value", [5, ["claude"], {"x": 1}, True])
def test_line_harness_and_role_must_be_string_or_null(field: str, value: Any) -> None:
    with pytest.raises(CardError):
        parse_card(_raw(_with_line(**{field: value})))


@pytest.mark.parametrize("code", ["", "short7c", "a" * 65, "has space", "abc+def/ghi", "abcdefgh=",
                                  "abcdefgh!", "\u00e9" * 8, 5, True, ["abcdefgh"], {"c": "abcdefgh"}])
def test_line_code_must_match_the_invite_pattern_or_be_null(code: Any) -> None:
    with pytest.raises(CardError):
        parse_card(_raw(_with_line(code=code)))


@pytest.mark.parametrize("line", ["claude-lead", None, 5, ["claude-lead", FP1]])
def test_each_line_must_be_an_object(line: Any) -> None:
    c = _valid()
    c["lines"][1] = line
    with pytest.raises(CardError):
        parse_card(_raw(c))


def test_line_names_must_be_unique() -> None:
    c = _valid()
    c["lines"][1]["name"] = c["lines"][0]["name"]
    with pytest.raises(CardError):
        parse_card(_raw(c))


def test_card_error_is_an_exception_with_a_reason() -> None:
    assert issubclass(CardError, Exception)
    with pytest.raises(CardError) as info:
        parse_card(_raw(_broken(v=2)))
    assert str(info.value)


# -- pick_alias --------------------------------------------------------------------


def test_pick_alias_keeps_a_free_name() -> None:
    assert pick_alias("claude-lead", "sam-mbp", set()) == "claude-lead"
    assert pick_alias("claude-lead", "sam-mbp", {"other", "sam-mbp-claude-lead"}) == "claude-lead"


def test_pick_alias_prefixes_who_on_collision() -> None:
    assert pick_alias("claude", "sam-mbp", {"claude"}) == "sam-mbp-claude"


def test_pick_alias_counts_up_when_the_prefixed_name_is_taken() -> None:
    taken = {"claude", "sam-mbp-claude"}
    assert pick_alias("claude", "sam-mbp", taken) == "sam-mbp-claude-2"
    taken |= {"sam-mbp-claude-2"}
    assert pick_alias("claude", "sam-mbp", taken) == "sam-mbp-claude-3"
    taken |= {"sam-mbp-claude-3", "sam-mbp-claude-4"}
    assert pick_alias("claude", "sam-mbp", taken) == "sam-mbp-claude-5"


def test_pick_alias_cuts_the_prefixed_name_to_64() -> None:
    name = "n" * 60
    who = "computer-24-chars-long-x"
    assert len(who) == 24
    got = pick_alias(name, who, {name})
    assert got == f"{who}-{name}"[:64]
    assert len(got) == 64


def test_pick_alias_counter_keeps_the_whole_alias_within_64() -> None:
    name = "n" * 60
    who = "computer-24-chars-long-x"
    first = f"{who}-{name}"[:64]
    got = pick_alias(name, who, {name, first})
    assert got.endswith("-2") and len(got) <= 64
    assert got == first[:62] + "-2"
    got3 = pick_alias(name, who, {name, first, got})
    assert got3.endswith("-3") and len(got3) <= 64
    assert got3 == first[:62] + "-3"


def test_pick_alias_with_guest_prefix_as_the_watcher_uses_it() -> None:
    assert pick_alias("bea", "guest", {"guest-1a2b3c4d"}) == "bea"
    assert pick_alias("bea", "guest", {"bea"}) == "guest-bea"
    assert pick_alias("bea", "guest", {"bea", "guest-bea"}) == "guest-bea-2"


def test_pick_alias_does_not_mutate_taken() -> None:
    taken = {"claude"}
    pick_alias("claude", "box", taken)
    assert taken == {"claude"}


def test_words_after_a_link_on_the_next_line_are_not_glued_on() -> None:
    # review #17: stripping whitespace glues "See you there" onto the payload; the parser cuts it back off
    c = _valid()
    link = make_card(c)
    assert parse_card(link + "\nSee you there") == parse_card(link)
    assert parse_card(link[:25] + "\n" + link[25:] + "\nbye") == parse_card(link)


def test_a_malformed_relay_url_is_a_card_error_not_a_crash() -> None:
    c = _valid()
    c["relay"] = "http://[::1"
    payload = base64.urlsafe_b64encode(json.dumps(c).encode()).rstrip(b"=").decode()
    with pytest.raises(CardError):
        parse_card("call-card:1:" + payload)
