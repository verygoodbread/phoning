"""Unit tests for phoning.directory (no retalk binary, no network)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

import phoning.directory as dmod
from phoning.retalk import RetalkError

FP1 = "11111111111111111111111111111111"
FP2 = "22222222222222222222222222222222"
FP3 = "33333333333333333333333333333333"
RELAY = "http://127.0.0.1:8766"


class FakeRetalk:
    """In-memory Retalk double reading relay files from disk."""

    fingerprints: dict[str, str] = {}
    relay_overrides: dict[str, str | None] = {}
    add_calls: list[tuple[str, str, str]] = []
    fail_add_for: set[str] = set()
    fail_id_for: set[str] = set()
    fail_rc: int = 9

    def __init__(self, user_dir: str | os.PathLike[str], *args: Any, **kwargs: Any) -> None:
        self.user_dir: str = os.fspath(user_dir)
        if self.user_dir in FakeRetalk.relay_overrides:
            self.relay: str | None = FakeRetalk.relay_overrides[self.user_dir]
        else:
            relay_path = os.path.join(self.user_dir, "relay")
            try:
                with open(relay_path, encoding="utf-8") as fh:  # noqa: PTH123
                    val = fh.readline().strip()
                    self.relay = val or None
            except OSError:
                self.relay = None

    def id(self, *, timeout: float = 60.0) -> str:
        if self.user_dir in FakeRetalk.fail_id_for:
            raise RetalkError(FakeRetalk.fail_rc, ["retalk", "id"])
        if self.user_dir in FakeRetalk.fingerprints:
            return FakeRetalk.fingerprints[self.user_dir]
        raise RetalkError(1, ["retalk", "id"])

    def add(self, fp: str, alias: str, *, timeout: float = 60.0) -> None:
        FakeRetalk.add_calls.append((self.user_dir, fp, alias))
        if self.user_dir in FakeRetalk.fail_add_for:
            raise RetalkError(FakeRetalk.fail_rc, ["retalk", "add", fp])


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    cards = tmp_path / "cards"
    home.mkdir()
    monkeypatch.setenv("PHONING_HOME", str(home))
    monkeypatch.setenv("PHONING_DIRECTORY", str(cards))
    monkeypatch.setattr(dmod, "Retalk", FakeRetalk)
    FakeRetalk.fingerprints.clear()
    FakeRetalk.relay_overrides.clear()
    FakeRetalk.add_calls.clear()
    FakeRetalk.fail_add_for.clear()
    FakeRetalk.fail_id_for.clear()
    FakeRetalk.fail_rc = 9
    return home, cards


def _principal(home: Path, name: str, *, fp: str | None, relay: str | None = RELAY) -> Path:
    udir = home / name
    (udir / "identity").mkdir(parents=True, exist_ok=True)
    if relay is not None:
        (udir / "relay").write_text(relay + "\n", encoding="utf-8")
    if fp is not None:
        FakeRetalk.fingerprints[str(udir)] = fp
    return udir


def _write_card(cards: Path, name: str, fp: str, relay: str = RELAY) -> Path:
    cards.mkdir(parents=True, exist_ok=True)
    path = cards / f"{name}.json"
    path.write_text(json.dumps({
        "name": name, "fingerprint": fp, "relay": relay,
        "harness": None, "role": None, "published_at": "2026-10-11T18:00:00Z",
    }), encoding="utf-8")
    return path


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# -- validation ---------------------------------------------------------

@pytest.mark.parametrize("good", ["a", "A0", "claude-lead", "x_y.z-1", "a" * 64])
def test_legal_names_accepted(good: str) -> None:
    assert dmod.is_legal_name(good) is True


@pytest.mark.parametrize("bad", ["", "../evil", "a/b", ".hidden", "-x", ".", "a" * 65, "a\n", "a ", " x"])
def test_legal_names_rejected(bad: str) -> None:
    assert dmod.is_legal_name(bad) is False


@pytest.mark.parametrize("good", ["0" * 32, "0123456789abcdef0123456789abcdef"])
def test_legal_fingerprints_accepted(good: str) -> None:
    assert dmod.is_legal_fingerprint(good) is True


@pytest.mark.parametrize("bad", ["", "0" * 31, "0" * 33, "g" * 32, "A" * 32, "0" * 32 + "\n", "../" + "a" * 29])
def test_legal_fingerprints_rejected(bad: str) -> None:
    assert dmod.is_legal_fingerprint(bad) is False


def test_normalize_relay_strips_ws_and_slashes() -> None:
    assert dmod.normalize_relay("http://x/") == "http://x"
    assert dmod.normalize_relay("  http://x///  ") == "http://x"
    assert dmod.normalize_relay(RELAY) == RELAY


def test_well_formed_vs_valid() -> None:
    good: dict[str, Any] = {"name": "bob", "fingerprint": FP1, "relay": RELAY,
                            "harness": None, "role": "r", "published_at": "2026-10-11T00:00:00Z"}
    assert dmod.is_well_formed_card(good) is True
    assert dmod.is_valid_card(good) is True
    malformed = dict(good, fingerprint="ZZZ")
    assert dmod.is_well_formed_card(malformed) is True
    assert dmod.is_valid_card(malformed) is False
    for broken in [
        dict(good, name="../x"), dict(good, fingerprint=123), dict(good, relay=""),
        dict(good, harness=4), dict(good, role=[]), dict(good, published_at=None),
        {"name": "bob"}, "string", [], None,
    ]:
        assert dmod.is_well_formed_card(broken) is False
        assert dmod.is_valid_card(broken) is False


# -- publish ------------------------------------------------------------

def test_publish_success_modes_and_output(world: tuple[Path, Path], capsys: Any) -> None:
    home, cards = world
    _principal(home, "alice", fp=FP1)
    rc = dmod.main(["publish", "alice", "--harness", "codex", "--role", "worker"])
    assert rc == 0
    card_path = cards / "alice.json"
    assert card_path.exists()
    assert _mode(cards) == 0o700
    assert _mode(card_path) == 0o600
    assert [p.name for p in cards.iterdir() if ".tmp" in p.name] == []
    out = capsys.readouterr().out
    card = json.loads(out.strip().splitlines()[-1])
    assert card["name"] == "alice" and card["fingerprint"] == FP1
    assert card["relay"] == RELAY
    assert set(card) == {"name", "fingerprint", "relay", "harness", "role", "published_at"}


def test_publish_tightens_wide_dir(world: tuple[Path, Path]) -> None:
    home, cards = world
    _principal(home, "alice", fp=FP1)
    cards.mkdir(exist_ok=True)
    cards.chmod(0o755)
    assert dmod.main(["publish", "alice"]) == 0
    assert _mode(cards) == 0o700


def test_publish_illegal_name_writes_nothing(world: tuple[Path, Path], capsys: Any) -> None:
    _home, cards = world
    assert dmod.main(["publish", "../evil"]) == 2
    assert capsys.readouterr().err.startswith("error:")
    assert not cards.exists()


def test_publish_missing_identity(world: tuple[Path, Path], capsys: Any) -> None:
    assert dmod.main(["publish", "ghost"]) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_publish_no_relay(world: tuple[Path, Path], capsys: Any) -> None:
    home, _cards = world
    _principal(home, "nore", fp=FP1, relay=None)
    assert dmod.main(["publish", "nore"]) == 2
    assert "no relay" in capsys.readouterr().err


def test_publish_bad_fingerprint_from_retalk(world: tuple[Path, Path], capsys: Any) -> None:
    home, cards = world
    _principal(home, "bad", fp="NOTHEX", relay=RELAY)
    assert dmod.main(["publish", "bad"]) == 1
    assert capsys.readouterr().err.startswith("error:")
    assert not (cards / "bad.json").exists()


def test_publish_retalk_failure(world: tuple[Path, Path], capsys: Any) -> None:
    home, _cards = world
    udir = _principal(home, "flaky", fp=FP1)
    FakeRetalk.fail_id_for.add(str(udir))
    assert dmod.main(["publish", "flaky"]) == 1
    assert capsys.readouterr().err.startswith("error:")


# -- list ---------------------------------------------------------------

def test_list_missing_dir_is_empty(world: tuple[Path, Path], capsys: Any) -> None:
    assert dmod.main(["list"]) == 0
    assert capsys.readouterr().out == ""


def test_list_sorted_and_skips_junk(world: tuple[Path, Path], capsys: Any) -> None:
    _home, cards = world
    _write_card(cards, "bobb", FP2)
    _write_card(cards, "aa", FP1)
    (cards / "junk.json").write_text("{not json", encoding="utf-8")
    (cards / "liar.json").write_text(json.dumps({
        "name": "someone-else", "fingerprint": FP1, "relay": RELAY,
        "harness": None, "role": None, "published_at": "2026-10-11T00:00:00Z"}), encoding="utf-8")
    assert dmod.main(["list"]) == 0
    captured = capsys.readouterr()
    assert "aa" in captured.out and "bobb" in captured.out
    assert "junk.json" in captured.err and "liar.json" in captured.err
    assert dmod.main(["list", "--json"]) == 0
    out2 = capsys.readouterr().out
    names = [json.loads(ln)["name"] for ln in out2.splitlines() if ln.strip()]
    assert names == sorted(names) and "aa" in names


def test_list_skipped_message_and_dash(world: tuple[Path, Path], capsys: Any) -> None:
    _home, cards = world
    _write_card(cards, "solo", FP1)
    (cards / "junk.json").write_text("oops", encoding="utf-8")
    assert dmod.main(["list"]) == 0
    captured = capsys.readouterr()
    assert "junk.json" in captured.err
    assert FP1[:8] in captured.out
    assert "-" in captured.out


# -- add ----------------------------------------------------------------

def test_add_success(world: tuple[Path, Path], capsys: Any) -> None:
    home, cards = world
    _principal(home, "anna", fp=FP1)
    _principal(home, "bob", fp=FP2)
    _write_card(cards, "bob", FP2)
    assert dmod.main(["add", "anna", "bob"]) == 0
    assert FakeRetalk.add_calls == [(str(home / "anna"), FP2, "bob")]
    assert "verified" in capsys.readouterr().out


def test_add_relay_mismatch(world: tuple[Path, Path], capsys: Any) -> None:
    home, cards = world
    _principal(home, "anna", fp=FP1)
    _principal(home, "bob", fp=FP2)
    _write_card(cards, "bob", FP2, relay="http://127.0.0.1:9")
    assert dmod.main(["add", "anna", "bob"]) == 3
    assert "relay" in capsys.readouterr().err
    assert FakeRetalk.add_calls == []


def test_add_trailing_slash_ok(world: tuple[Path, Path]) -> None:
    home, cards = world
    _principal(home, "anna", fp=FP1)
    _principal(home, "bob", fp=FP2)
    _write_card(cards, "bob", FP2, relay=RELAY + "/")
    assert dmod.main(["add", "anna", "bob"]) == 0


def test_add_malformed_fingerprint(world: tuple[Path, Path]) -> None:
    home, cards = world
    _principal(home, "anna", fp=FP1)
    _write_card(cards, "bob", "ZZZ")
    assert dmod.main(["add", "anna", "bob"]) == 3
    assert FakeRetalk.add_calls == []


def test_add_name_mismatch(world: tuple[Path, Path]) -> None:
    home, cards = world
    _principal(home, "anna", fp=FP1)
    _write_card(cards, "mallory", FP2)
    # rewrite inner name to differ
    p = cards / "mallory.json"
    obj = json.loads(p.read_text(encoding="utf-8"))
    obj["name"] = "bob"
    p.write_text(json.dumps(obj), encoding="utf-8")
    assert dmod.main(["add", "anna", "mallory"]) == 3
    assert FakeRetalk.add_calls == []


def test_add_own_fingerprint(world: tuple[Path, Path], capsys: Any) -> None:
    home, cards = world
    _principal(home, "anna", fp=FP1)
    _write_card(cards, "clone", FP1)
    assert dmod.main(["add", "anna", "clone"]) == 3
    assert "own" in capsys.readouterr().err


def test_add_missing_card_and_self(world: tuple[Path, Path]) -> None:
    home, _cards = world
    _principal(home, "anna", fp=FP1)
    assert dmod.main(["add", "anna", "ghost"]) == 2
    assert dmod.main(["add", "anna", "anna"]) == 2
    assert dmod.main(["add", "anna", "bob", "--as", "../x"]) == 2


def test_add_retalk_failure_hides_body(world: tuple[Path, Path], capsys: Any) -> None:
    home, cards = world
    _principal(home, "anna", fp=FP1)
    _principal(home, "ghost", fp=FP2)
    _write_card(cards, "ghost", FP2)
    FakeRetalk.fail_add_for.add(str(home / "anna"))
    FakeRetalk.fail_rc = 7
    assert dmod.main(["add", "anna", "ghost"]) == 1
    err = capsys.readouterr().err
    assert "rc=" in err and "unverified" in err
    assert "get_keys" not in err


# -- mutual -------------------------------------------------------------

def test_mutual_needs_peer_home(world: tuple[Path, Path], capsys: Any) -> None:
    home, cards = world
    _principal(home, "anna", fp=FP1)
    _principal(home, "bob", fp=FP2)
    _write_card(cards, "bob", FP2)
    assert dmod.main(["add", "anna", "bob", "--mutual"]) == 2
    assert "--peer-home" in capsys.readouterr().err
    assert FakeRetalk.add_calls == []


def test_mutual_success_two_homes(world: tuple[Path, Path], tmp_path: Path, capsys: Any) -> None:
    home, cards = world
    other = tmp_path / "home2"
    other.mkdir()
    _principal(home, "anna", fp=FP1)
    _principal(other, "bob", fp=FP2)
    _write_card(cards, "bob", FP2)
    rc = dmod.main(["add", "anna", "bob", "--mutual", "--peer-home", str(other),
                    "--mutual-alias", "boss"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "file ownership" in out
    assert (str(home / "anna"), FP2, "bob") in FakeRetalk.add_calls
    assert (str(other / "bob"), FP1, "boss") in FakeRetalk.add_calls


def test_mutual_symlink_refused(world: tuple[Path, Path], tmp_path: Path) -> None:
    home, cards = world
    _principal(home, "anna", fp=FP1)
    _principal(home, "bob", fp=FP2)
    _write_card(cards, "bob", FP2)
    link_home = tmp_path / "linkhome"
    link_home.mkdir()
    (link_home / "bob").symlink_to(home / "bob")
    assert dmod.main(["add", "anna", "bob", "--mutual", "--peer-home", str(link_home)]) == 3
    assert FakeRetalk.add_calls == []


def test_mutual_card_mismatch_writes_nothing(world: tuple[Path, Path]) -> None:
    home, cards = world
    _principal(home, "anna", fp=FP1)
    _principal(home, "bob", fp=FP2)
    _write_card(cards, "bob", FP3)
    assert dmod.main(["add", "anna", "bob", "--mutual", "--peer-home", str(home)]) == 3
    assert FakeRetalk.add_calls == []


def test_mutual_unwritable_refused(world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    home, cards = world
    _principal(home, "anna", fp=FP1)
    _principal(home, "bob", fp=FP2)
    _write_card(cards, "bob", FP2)
    real_access = os.access

    def fake_access(path: str | os.PathLike[str], mode: int) -> bool:
        if os.fspath(path) == str(home / "bob"):
            return False
        return real_access(path, mode)

    monkeypatch.setattr(os, "access", fake_access)
    assert dmod.main(["add", "anna", "bob", "--mutual", "--peer-home", str(home)]) == 3
    assert FakeRetalk.add_calls == []


def test_mutual_second_add_failure_reports(world: tuple[Path, Path], capsys: Any) -> None:
    home, cards = world
    _principal(home, "anna", fp=FP1)
    _principal(home, "bob", fp=FP2)
    _write_card(cards, "bob", FP2)
    FakeRetalk.fail_add_for.add(str(home / "bob"))
    rc = dmod.main(["add", "anna", "bob", "--mutual", "--peer-home", str(home)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "was NOT added" in out
