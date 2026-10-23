"""Acceptance: same-machine onboarding, `phoning directory` (docs/spec/directory.md).

Uses a real retalk-server on a throwaway port and real retalk identities. Nothing here touches
~/.phoning, ~/.config/phoning or the live relay: PHONING_HOME and PHONING_DIRECTORY are temporary.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from phoning.retalk import Retalk
from tests.acceptance.helpers import free_port

pytestmark = pytest.mark.skipif(
    shutil.which("retalk") is None or shutil.which("retalk-server") is None,
    reason="retalk and retalk-server must be installed",
)

ROOT = Path(__file__).resolve().parents[2]
HEX32 = re.compile(r"[0-9a-f]{32}")


class Dir:
    """A throwaway world: relay, phoning home, card directory."""

    def __init__(self, tmp: Path, relay: str) -> None:
        self.tmp, self.relay = tmp, relay
        self.home = tmp / "home"
        self.cards = tmp / "cards"
        self.env = dict(os.environ, PHONING_HOME=str(self.home), PHONING_DIRECTORY=str(self.cards),
                        PYTHONPATH=str(ROOT))
        self.env.pop("PHONING_SESSION", None)
        self.n = 0

    def phoning(self, *args: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "phoning.cli", *args], env=self.env,
                              capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def directory(self, *args: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "phoning.directory", *args], env=self.env,
                              capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def principal(self, prefix: str = "p", home: Path | None = None) -> tuple[str, str]:
        """Create a fresh principal with a real identity; return (name, fingerprint)."""
        self.n += 1
        name = f"{prefix}{self.n}"
        env = dict(self.env, PHONING_HOME=str(home or self.home))
        p = subprocess.run([sys.executable, "-m", "phoning.cli", "init", name, "--relay", self.relay],
                           env=env, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)
        assert p.returncode == 0, (p.stdout, p.stderr)
        fp = p.stdout.strip().split()[-1]
        assert HEX32.fullmatch(fp)
        return name, fp

    def contacts(self, name: str, home: Path | None = None) -> dict[str, dict[str, Any]]:
        """Raw `retalk contacts --json`, keyed by alias (so tests can see `verified`)."""
        udir = (home or self.home) / name
        p = subprocess.run(["retalk", "contacts", "--json", "--dir", str(udir / "identity"),
                            "-r", self.relay, "-np"], capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        out: dict[str, dict[str, Any]] = {}
        for line in p.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                rec = json.loads(line)
                out[rec["name"]] = rec
        return out


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Dir]:
    tmp = tmp_path_factory.mktemp("dirworld")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    relay = subprocess.Popen(
        ["retalk-server", "--host", "127.0.0.1", "--port", str(port), "--audience", url],
        env=dict(os.environ, RETALK_SERVER_DB=str(tmp / "relay.db")), cwd=tmp,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    try:
        yield Dir(tmp, url)
    finally:
        relay.terminate()
        relay.wait(timeout=10)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _card(w: Dir, name: str) -> dict[str, Any]:
    return json.loads((w.cards / f"{name}.json").read_text())


def test_publish_writes_a_private_card(world: Dir) -> None:
    name, fp = world.principal()
    p = world.directory("publish", name, "--harness", "claude-code", "--role", "architect")
    assert p.returncode == 0, (p.stdout, p.stderr)
    card = _card(world, name)
    assert card["name"] == name and card["fingerprint"] == fp
    assert card["relay"] == world.relay
    assert card["harness"] == "claude-code" and card["role"] == "architect"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", card["published_at"])
    assert _mode(world.cards) == 0o700
    assert _mode(world.cards / f"{name}.json") == 0o600
    assert json.loads(p.stdout.strip().splitlines()[-1])["fingerprint"] == fp
    # no temp files left behind
    assert [f.name for f in world.cards.iterdir() if ".tmp" in f.name] == []


def test_publish_tightens_a_wide_directory_and_replaces_the_card(world: Dir) -> None:
    name, _fp = world.principal()
    world.cards.mkdir(exist_ok=True)
    world.cards.chmod(0o755)
    assert world.directory("publish", name).returncode == 0
    assert _mode(world.cards) == 0o700
    first = _card(world, name)
    assert first["harness"] is None and first["role"] is None
    time.sleep(1.1)
    assert world.directory("publish", name, "--role", "builder").returncode == 0
    second = _card(world, name)
    assert second["role"] == "builder" and second["published_at"] > first["published_at"]


def test_publish_refuses_illegal_names_and_missing_identities(world: Dir) -> None:
    for bad in ("../evil", "a/b", ".hidden", "-x", ""):
        p = world.directory("publish", bad)
        assert p.returncode == 2, (bad, p.stdout, p.stderr)
    assert not (world.tmp / "evil.json").exists()
    p = world.directory("publish", "nobody-home")
    assert p.returncode == 2 and p.stderr.startswith("error:")
    assert not (world.cards / "nobody-home.json").exists()


def test_list_prints_cards_and_skips_junk(world: Dir) -> None:
    a, fa = world.principal("la")
    b, _fb = world.principal("lb")
    world.directory("publish", a, "--harness", "codex", "--role", "worker")
    world.directory("publish", b)
    (world.cards / "junk.json").write_text("{not json")
    (world.cards / "liar.json").write_text(json.dumps(
        {"name": "someone-else", "fingerprint": fa, "relay": world.relay, "harness": None,
         "role": None, "published_at": "2026-10-11T00:00:00Z"}))
    p = world.directory("list")
    assert p.returncode == 0, p.stderr
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith(("la", "lb"))]
    assert any(ln.startswith(a) and fa[:8] in ln and "codex" in ln and "worker" in ln for ln in lines)
    assert any(ln.startswith(b) and "-" in ln for ln in lines)
    assert "junk.json" in p.stderr and "liar.json" in p.stderr
    assert "someone-else" not in p.stdout
    js = [json.loads(ln) for ln in world.directory("list", "--json").stdout.splitlines() if ln.strip()]
    names = [c["name"] for c in js]
    assert a in names and b in names and names == sorted(names)
    assert "someone-else" not in names


def test_list_with_no_directory_is_empty_success(world: Dir) -> None:
    env = dict(world.env, PHONING_DIRECTORY=str(world.tmp / "does-not-exist"))
    p = subprocess.run([sys.executable, "-m", "phoning.directory", "list"], env=env,
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert p.returncode == 0 and p.stdout.strip() == ""


def test_add_from_a_card_verifies_the_peer(world: Dir) -> None:
    a, _ = world.principal("aa")
    b, fb = world.principal("ab")
    world.directory("publish", b)
    p = world.directory("add", a, b)
    assert p.returncode == 0, (p.stdout, p.stderr)
    c = world.contacts(a)
    assert c[b]["fingerprint"] == fb and c[b]["verified"] is True
    # idempotent
    assert world.directory("add", a, b).returncode == 0
    # --as picks the alias
    assert world.directory("add", a, b, "--as", "friend").returncode == 0
    assert world.contacts(a)["friend"]["fingerprint"] == fb


def test_add_refuses_a_relay_mismatch(world: Dir) -> None:
    a, _ = world.principal("ra")
    b, _ = world.principal("rb")
    world.directory("publish", b)
    card = _card(world, b)
    card["relay"] = "http://127.0.0.1:9"
    (world.cards / f"{b}.json").write_text(json.dumps(card))
    p = world.directory("add", a, b)
    assert p.returncode == 3 and "relay" in p.stderr
    assert b not in world.contacts(a)
    # a trailing slash is not a mismatch
    card["relay"] = world.relay + "/"
    (world.cards / f"{b}.json").write_text(json.dumps(card))
    assert world.directory("add", a, b).returncode == 0


@pytest.mark.parametrize("bad_fp", ["0" * 31, "0" * 33, "g" * 32, "A" * 32, "", "../" + "a" * 29])
def test_add_refuses_a_malformed_fingerprint(world: Dir, bad_fp: str) -> None:
    a, _ = world.principal("ma")
    b, _ = world.principal("mb")
    world.directory("publish", b)
    card = _card(world, b)
    card["fingerprint"] = bad_fp
    (world.cards / f"{b}.json").write_text(json.dumps(card))
    p = world.directory("add", a, b)
    assert p.returncode == 3, (bad_fp, p.stdout, p.stderr)
    assert b not in world.contacts(a)


def test_add_refuses_a_card_whose_name_does_not_match_its_file(world: Dir) -> None:
    a, _ = world.principal("na")
    b, fb = world.principal("nb")
    (world.cards).mkdir(exist_ok=True)
    (world.cards / "mallory.json").write_text(json.dumps(
        {"name": b, "fingerprint": fb, "relay": world.relay, "harness": None, "role": None,
         "published_at": "2026-10-11T00:00:00Z"}))
    p = world.directory("add", a, "mallory")
    assert p.returncode == 3
    assert "mallory" not in world.contacts(a) and b not in world.contacts(a)


def test_add_refuses_missing_cards_self_and_missing_identities(world: Dir) -> None:
    a, _ = world.principal("sa")
    b, _ = world.principal("sb")
    assert world.directory("add", a, "ghost-card").returncode == 2
    world.directory("publish", a)
    assert world.directory("add", a, a).returncode == 2           # NAME == PEER
    assert world.directory("add", "no-such-principal", a).returncode == 2
    # our own fingerprint published under another name
    card = _card(world, a)
    card["name"] = "alias-of-me"
    (world.cards / "alias-of-me.json").write_text(json.dumps(card))
    p = world.directory("add", a, "alias-of-me")
    assert p.returncode == 3 and "own" in p.stderr
    del b


def test_a_failed_verify_exits_1_without_echoing_retalk_output(world: Dir) -> None:
    a, _ = world.principal("va")
    (world.cards).mkdir(exist_ok=True)
    (world.cards / "ghost.json").write_text(json.dumps(
        {"name": "ghost", "fingerprint": "0123456789abcdef0123456789abcdef", "relay": world.relay,
         "harness": None, "role": None, "published_at": "2026-10-11T00:00:00Z"}))
    p = world.directory("add", a, "ghost")
    assert p.returncode == 1
    assert "rc=" in p.stderr and "unverified" in p.stderr
    assert "get_keys" not in p.stderr and "get_keys" not in p.stdout


def test_mutual_needs_peer_home_and_writes_nothing_without_it(world: Dir) -> None:
    a, _ = world.principal("ua")
    b, _ = world.principal("ub")
    world.directory("publish", b)
    p = world.directory("add", a, b, "--mutual")
    assert p.returncode == 2 and "--peer-home" in p.stderr
    assert b not in world.contacts(a) and a not in world.contacts(b)


def test_mutual_adds_both_directions_and_mail_flows(world: Dir) -> None:
    a, fa = world.principal("xa")
    b, fb = world.principal("xb")
    world.directory("publish", b)
    p = world.directory("add", a, b, "--mutual", "--peer-home", str(world.home))
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert "file ownership" in p.stdout
    ca, cb = world.contacts(a), world.contacts(b)
    assert ca[b]["fingerprint"] == fb and ca[b]["verified"] is True
    assert cb[a]["fingerprint"] == fa and cb[a]["verified"] is True
    Retalk(world.home / a).send_peer(b, "hello over the directory")
    got = subprocess.run(["retalk", "receive", "--peer", a, "--dir", str(world.home / b / "identity"),
                          "-r", world.relay, "-np", "--quiet"], capture_output=True, text=True,
                         timeout=60, stdin=subprocess.DEVNULL)
    texts = [json.loads(ln)["text"] for ln in got.stdout.splitlines() if ln.strip().startswith("{")]
    assert texts == ["hello over the directory"]


def test_mutual_works_across_two_homes_and_honours_mutual_alias(world: Dir) -> None:
    other_home = world.tmp / "home2"
    a, _ = world.principal("ya")
    b, _ = world.principal("yb", home=other_home)
    world.directory("publish", b, "--home", str(other_home))
    p = world.directory("add", a, b, "--mutual", "--peer-home", str(other_home), "--mutual-alias", "boss")
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert "boss" in world.contacts(b, home=other_home) and b in world.contacts(a)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_mutual_is_refused_when_the_peer_home_is_not_writable(world: Dir) -> None:
    a, _ = world.principal("wa")
    b, _ = world.principal("wb")
    world.directory("publish", b)
    peer_dir = world.home / b
    peer_dir.chmod(0o500)
    try:
        p = world.directory("add", a, b, "--mutual", "--peer-home", str(world.home))
    finally:
        peer_dir.chmod(0o700)
    assert p.returncode == 3 and "ownership" in p.stderr
    assert b not in world.contacts(a) and a not in world.contacts(b)


def test_mutual_is_refused_when_the_peer_is_a_symlink(world: Dir) -> None:
    a, _ = world.principal("la2")
    b, _ = world.principal("lb2")
    world.directory("publish", b)
    link_home = world.tmp / "linkhome"
    link_home.mkdir()
    (link_home / b).symlink_to(world.home / b)
    p = world.directory("add", a, b, "--mutual", "--peer-home", str(link_home))
    assert p.returncode == 3
    assert b not in world.contacts(a) and a not in world.contacts(b)


def test_mutual_is_refused_when_the_card_does_not_match_the_peers_real_identity(world: Dir) -> None:
    a, _ = world.principal("ga")
    b, _ = world.principal("gb")
    c, fc = world.principal("gc")
    world.directory("publish", b)
    card = _card(world, b)
    card["fingerprint"] = fc          # a real, registered fingerprint, but not b's
    (world.cards / f"{b}.json").write_text(json.dumps(card))
    p = world.directory("add", a, b, "--mutual", "--peer-home", str(world.home))
    assert p.returncode == 3
    assert b not in world.contacts(a) and a not in world.contacts(b) and a not in world.contacts(c)


def test_directory_module_never_uses_shell_true() -> None:
    src = (ROOT / "phoning" / "directory.py").read_text()
    assert "shell=True" not in src and "os.system" not in src
