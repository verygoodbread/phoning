"""Acceptance: phoning v0 against a real local retalk relay (docs/SPEC-v0.md).

Replays common multi-agent failure patterns:
- several readers on one identity must each see every message (no stolen mail);
- two sessions sharing one principal must be told apart;
- a second owner of a mailbox is refused, naming the holder;
- a relayed "CONFIRMED" from a non-owner does not count; the owner's accept does;
- `wait` blocks until news and times out cleanly;
- plain retalk text (legacy peers) still arrives as a chat, never dropped.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("retalk") is None or shutil.which("retalk-server") is None,
    reason="retalk and retalk-server must be installed",
)

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class World:
    def __init__(self, tmp: Path, relay: str) -> None:
        self.tmp, self.relay = tmp, relay
        self.env = dict(os.environ, PHONING_HOME=str(tmp / "home"), PYTHONPATH=str(ROOT))
        self.env.pop("PHONING_SESSION", None)
        self.owners: list[subprocess.Popen[str]] = []

    def run(self, *args: str, check: bool = True, timeout: int = 90) -> subprocess.CompletedProcess[str]:
        p = subprocess.run([sys.executable, "-m", "phoning.cli", *args], env=self.env,
                           capture_output=True, text=True, timeout=timeout)
        if check:
            assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
        return p

    def own(self, name: str, session: str) -> subprocess.Popen[str]:
        p = subprocess.Popen([sys.executable, "-m", "phoning.cli", "own", name, "--session", session,
                              "--interval", "1"], env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        self.owners.append(p)
        return p

    def read_json(self, name: str, reader: str, *extra: str) -> list[dict]:
        out = self.run("read", name, "--reader", reader, "--json", *extra).stdout
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def wait_for(self, name: str, reader: str, n: int, timeout: float = 60) -> list[dict]:
        """Peek (no commit) until `reader` has at least n new records."""
        deadline = time.time() + timeout
        recs: list[dict] = []
        while time.time() < deadline:
            recs = self.read_json(name, reader, "--peek")
            if len(recs) >= n:
                return recs
            self.run("wait", name, "--reader", reader, "--timeout", "5", check=False)
        raise AssertionError(f"{name}/{reader}: expected {n} records, got {len(recs)}: {recs}")


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    tmp = tmp_path_factory.mktemp("phoning")
    port = _free_port()
    relay_url = f"http://127.0.0.1:{port}"
    relay = subprocess.Popen(
        ["retalk-server", "--host", "127.0.0.1", "--port", str(port), "--audience", relay_url],
        env=dict(os.environ, RETALK_SERVER_DB=str(tmp / "relay.db")), cwd=tmp,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    w = World(tmp, relay_url)
    try:
        fps = {}
        for name in ("claude", "codex", "observer"):
            fps[name] = w.run("init", name, "--relay", relay_url).stdout.strip().split()[-1]
            assert len(fps[name]) == 32
        for me in fps:
            for other, fp in fps.items():
                if other != me:
                    w.run("add", me, fp, "--as", other)
        w.run("room", "create", "claude", "ops", "--members", "codex,observer")
        w.own("claude", "A")
        w.own("codex", "vega")
        w.own("observer", "B")
        time.sleep(2)
        yield w
    finally:
        for p in w.owners:
            p.terminate()
        for p in w.owners:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        relay.terminate()
        relay.wait(timeout=10)


def test_every_reader_on_one_identity_sees_every_message(world: World) -> None:
    w = world
    for i, (typ, extra) in enumerate([("chat", []), ("ask", []),
                                      ("status", ["--data", '{"state": "ACTIVE"}'])]):
        w.run("send", "claude", "--room", "ops", "--type", typ, "--subject", f"m{i}", "--session", "A", *extra)
    vega = w.wait_for("codex", "vega", 3)
    lyra = w.wait_for("codex", "lyra", 3)
    obs = w.wait_for("observer", "B", 3)
    for recs in (vega, lyra, obs):
        assert [r["env"]["subject"] for r in recs[:3]] == ["m0", "m1", "m2"]
        assert all(r["room"] == "ops" for r in recs[:3])
    # reading commits only that reader's cursor; the other reader still has them
    assert len(w.read_json("codex", "vega")) >= 3
    assert w.read_json("codex", "vega", "--peek") == []
    assert len(w.read_json("codex", "lyra", "--peek")) >= 3


def test_sessions_sharing_a_principal_are_attributed(world: World) -> None:
    w = world
    w.read_json("claude", "A")  # drain cursor
    w.run("send", "codex", "--to", "claude", "--type", "status", "--subject", "worker update",
          "--session", "vega", "--data", '{"state": "ACTIVE"}')
    w.run("send", "codex", "--to", "claude", "--type", "status", "--subject", "standup",
          "--session", "lyra", "--data", '{"state": "IDLE"}')
    recs = w.wait_for("claude", "A", 2)
    who = {(r["env"]["from"]["principal"], r["env"]["from"]["session"]): r["env"]["subject"] for r in recs}
    assert who[("codex", "vega")] == "worker update"
    assert who[("codex", "lyra")] == "standup"
    assert all(r["env"]["ts"].endswith("Z") for r in recs)


def test_second_owner_is_refused_and_names_the_holder(world: World) -> None:
    p = world.run("own", "codex", "--session", "intruder", check=False, timeout=30)
    assert p.returncode == 3
    assert "vega" in (p.stdout + p.stderr)


def test_only_the_scope_owner_can_accept_a_proposal(world: World) -> None:
    w = world
    owners = w.tmp / "owners.json"
    owners.write_text(json.dumps({"schemas/": "codex"}))
    prop = json.loads(w.run("send", "claude", "--room", "ops", "--type", "propose",
                            "--subject", "add a field to Request", "--session", "A",
                            "--data", '{"scope": "schemas/api.v1.json"}').stdout)["eid"]
    w.wait_for("observer", "B", 1)
    w.run("send", "observer", "--to", "claude", "--type", "accept", "--subject", "CONFIRMED",
          "--reply-to", prop, "--session", "B")
    time.sleep(3)
    board = w.run("board", "claude", "--owners", str(owners)).stdout
    assert "NOT EFFECTIVE" in board and "OPEN" in board
    w.run("send", "codex", "--to", "claude", "--type", "accept", "--subject", "approved by schema owner",
          "--reply-to", prop, "--session", "vega")
    deadline = time.time() + 60
    while time.time() < deadline:
        board = w.run("board", "claude", "--owners", str(owners)).stdout
        if "ACCEPTED" in board:
            break
        time.sleep(1)
    assert "ACCEPTED" in board


def test_wait_times_out_then_wakes(world: World) -> None:
    w = world
    w.read_json("observer", "B")  # drain
    t0 = time.time()
    p = w.run("wait", "observer", "--reader", "B", "--timeout", "1", check=False)
    assert p.returncode == 1 and time.time() - t0 < 10
    w.run("send", "claude", "--to", "observer", "--type", "chat", "--subject", "ping", "--session", "A")
    p = w.run("wait", "observer", "--reader", "B", "--timeout", "60", check=False)
    assert p.returncode == 0 and "ping" in p.stdout


def test_legacy_plain_retalk_text_arrives_as_chat(world: World) -> None:
    w = world
    w.read_json("claude", "A")
    home = Path(w.env["PHONING_HOME"])
    subprocess.run(["retalk", "send", "--dir", str(home / "observer" / "identity"), "-r", w.relay,
                    "-np", "--peer", "claude", "hello from an old peer"],
                   check=True, capture_output=True, text=True, timeout=60)
    recs = w.wait_for("claude", "A", 1)
    legacy = [r for r in recs if r["env"].get("legacy")]
    assert legacy and legacy[0]["env"]["type"] == "chat"
    assert legacy[0]["env"]["body"] == "hello from an old peer"
