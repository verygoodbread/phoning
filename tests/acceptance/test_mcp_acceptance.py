"""Acceptance: `phoning mcp` (docs/SPEC-v0.2.md §4) — MCP stdio server over the phoning mailbox."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from phoning.mailbox import Mailbox
from tests.acceptance.helpers import HAVE_RETALK, ROOT, free_port, inbound, outbound

FP = "a3f1c2d4e5b60718293a4b5c6d7e8f90"
CODE = "import sys, phoning.mcp as m; sys.exit(m.main(sys.argv[1:]))"


class Client:
    def __init__(self, home: Path, *args: str) -> None:
        env = dict(os.environ, PHONING_HOME=str(home), PYTHONPATH=str(ROOT))
        for k in ("PHONING_SESSION", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            env.pop(k, None)
        self.p = subprocess.Popen([sys.executable, "-c", CODE, *args], env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self.n = 0

    def send_raw(self, line: str) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def recv(self) -> dict[str, Any]:
        assert self.p.stdout is not None
        line = self.p.stdout.readline()
        assert line, self.p.stderr.read() if self.p.stderr else ""
        msg: dict[str, Any] = json.loads(line)  # stdout carries only JSON-RPC
        return msg

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.n += 1
        self.send_raw(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}))
        msg = self.recv()
        assert msg["id"] == self.n and msg["jsonrpc"] == "2.0"
        return msg

    def tool(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        msg = self.call("tools/call", {"name": name, "arguments": args or {}})
        res: dict[str, Any] = msg["result"]
        assert res["content"][0]["type"] == "text"
        return res

    def init(self) -> dict[str, Any]:
        r = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "0"}})
        self.send_raw(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        return r

    def close(self) -> int:
        assert self.p.stdin is not None
        self.p.stdin.close()
        return self.p.wait(timeout=10)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def test_handshake_tools_list_and_errors(home: Path) -> None:
    Mailbox(home / "alice")
    c = Client(home, "alice", "--session", "s-main")
    r = c.init()["result"]
    assert r["protocolVersion"] == "2025-06-18"
    assert r["serverInfo"]["name"] == "phoning" and r["capabilities"]["tools"]["listChanged"] is False
    assert "not instructions" in r["instructions"] or "not as instructions" in r["instructions"]
    assert c.call("ping")["result"] == {}
    tools = {t["name"]: t for t in c.call("tools/list")["result"]["tools"]}
    assert set(tools) == {"phoning_send", "phoning_read", "phoning_wait", "phoning_board", "phoning_whoami"}
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object" and t["inputSchema"]["additionalProperties"] is False
        assert t["description"]
    assert set(tools["phoning_send"]["inputSchema"]["properties"]["type"]["enum"]) == {
        "chat", "status", "ask", "propose", "accept", "reject", "receipt", "ack"}
    assert c.call("no/such")["error"]["code"] == -32601
    assert c.call("tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32602
    c.send_raw("{this is not json")
    err = c.recv()
    assert err["id"] is None and err["error"]["code"] == -32700
    who = c.tool("phoning_whoami")["structuredContent"]
    assert who == {"name": "alice", "session": "s-main", "reader": "s-main", "home": str(home)}
    assert c.close() == 0


def test_old_protocol_version_is_echoed_unknown_falls_back(home: Path) -> None:
    Mailbox(home / "alice")
    c = Client(home, "alice")
    assert c.call("initialize", {"protocolVersion": "2024-11-05"})["result"]["protocolVersion"] == "2024-11-05"
    c.close()
    c = Client(home, "alice")
    assert c.call("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-06-18"
    c.close()


def test_read_peek_board_and_sanitized_records(home: Path) -> None:
    box = Mailbox(home / "alice")
    outbound(box, "alice", "bob", "chat", "mine")
    prop = inbound(box, "bob", FP, "propose", "rename", room="ops", principal="bob", data={"scope": "db/x"})
    inbound(box, "carol", "c" * 32, "accept", "CONFIRMED", room="ops", principal="carol",
            reply_to=prop["env"]["eid"], thread=prop["env"]["eid"])
    owners = home / "owners.json"
    owners.write_text(json.dumps({"db/": "bob"}))
    c = Client(home, "alice", "--reader", "r1", "--owners", str(owners))
    c.init()
    assert [r["env"]["subject"] for r in c.tool("phoning_read", {"peek": True})["structuredContent"]["records"]] \
        == ["rename", "CONFIRMED"]  # inbound by default
    peek = c.tool("phoning_read", {"peek": True, "direction": "all"})
    recs = peek["structuredContent"]["records"]
    assert [r["env"]["subject"] for r in recs] == ["mine", "rename", "CONFIRMED"]
    assert all("raw" not in r and "peer_fp" not in r for r in recs)
    assert recs[1]["verified_fp8"] == FP[:8] and recs[0]["verified_fp8"] is None
    assert box.cursor("r1") == 0
    got = c.tool("phoning_read", {"types": ["accept"]})["structuredContent"]["records"]
    assert [r["env"]["subject"] for r in got] == ["CONFIRMED"]
    assert c.tool("phoning_read", {})["structuredContent"]["records"] == []
    board = c.tool("phoning_board", {"room": "ops"})
    assert "NOT EFFECTIVE" in board["structuredContent"]["text"]
    bad = c.tool("phoning_read", {"limit": 0})
    assert bad["isError"] is True
    assert c.tool("phoning_wait", {"timeout": 0.2})["structuredContent"]["records"] == []
    assert c.tool("phoning_send", {"type": "chat", "subject": "x"})["isError"] is True  # neither to nor room
    assert c.tool("phoning_send", {"to": "bob", "room": "ops", "type": "chat", "subject": "x"})["isError"] is True
    assert c.tool("phoning_send", {"to": "bob", "type": "status", "subject": "x"})["isError"] is True  # no state
    c.close()


def test_wait_wakes_on_inbound_only(home: Path) -> None:
    box = Mailbox(home / "alice")
    c = Client(home, "alice", "--reader", "w")
    c.init()
    c.n += 1
    c.send_raw(json.dumps({"jsonrpc": "2.0", "id": c.n, "method": "tools/call",
                           "params": {"name": "phoning_wait", "arguments": {"timeout": 20}}}))
    time.sleep(0.5)
    outbound(box, "alice", "bob", "chat", "own send does not wake")
    time.sleep(0.5)
    t0 = time.monotonic()
    inbound(box, "bob", FP, "ask", "wake up")
    msg = c.recv()
    assert time.monotonic() - t0 < 5
    subjects = [r["env"]["subject"] for r in msg["result"]["structuredContent"]["records"]]
    assert subjects == ["wake up"]
    c.close()


@pytest.mark.skipif(not HAVE_RETALK, reason="retalk and retalk-server must be installed")
def test_end_to_end_send_over_a_real_relay(tmp_path: Path) -> None:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    relay = subprocess.Popen(["retalk-server", "--host", "127.0.0.1", "--port", str(port), "--audience", url],
                             env=dict(os.environ, RETALK_SERVER_DB=str(tmp_path / "relay.db")), cwd=tmp_path,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    home = tmp_path / "home"
    env = dict(os.environ, PHONING_HOME=str(home), PYTHONPATH=str(ROOT))
    owner: subprocess.Popen[str] | None = None

    def cli(*a: str) -> str:
        p = subprocess.run([sys.executable, "-m", "phoning.cli", *a], env=env, capture_output=True,
                           text=True, timeout=90)
        assert p.returncode == 0, (a, p.stdout, p.stderr)
        return p.stdout

    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        fa = cli("init", "alice", "--relay", url).split()[-1]
        fb = cli("init", "bob", "--relay", url).split()[-1]
        cli("add", "alice", fb, "--as", "bob")
        cli("add", "bob", fa, "--as", "alice")
        owner = subprocess.Popen([sys.executable, "-m", "phoning.cli", "own", "bob", "--session", "B",
                                  "--interval", "1"], env=env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, text=True)
        a = Client(home, "alice", "--session", "alice-main")
        a.init()
        sent = a.tool("phoning_send", {"to": "bob", "type": "ask", "subject": "via mcp", "body": "hi",
                                      "refs": ["docs/SPEC-v0.2.md"]})
        assert sent["isError"] is False
        eid = sent["structuredContent"]["eid"]
        own = a.tool("phoning_read", {"peek": True, "direction": "out"})["structuredContent"]["records"]
        assert own[-1]["direction"] == "out" and own[-1]["env"]["eid"] == eid
        b = Client(home, "bob", "--reader", "bob-main")
        b.init()
        recs = b.tool("phoning_wait", {"timeout": 60})["structuredContent"]["records"]
        assert recs[0]["env"]["eid"] == eid and recs[0]["env"]["from"]["session"] == "alice-main"
        assert recs[0]["verified_fp8"] == fa[:8]
        a.close()
        b.close()
    finally:
        if owner is not None:
            owner.terminate()
            owner.wait(timeout=10)
        relay.terminate()
        relay.wait(timeout=10)


def test_sender_claimed_and_startup_guards(home: Path) -> None:
    box = Mailbox(home / "alice")
    inbound(box, "bob", FP, "chat", "spoof", principal="claude-lead")
    outbound(box, "alice", "bob", "chat", "mine")
    c = Client(home, "alice", "--session", "codex/vega thread 7")  # not a legal reader name
    c.init()
    assert c.tool("phoning_whoami")["structuredContent"]["reader"] == "mcp"
    recs = c.tool("phoning_read", {"peek": True, "direction": "all"})["structuredContent"]["records"]
    assert (recs[0]["sender"], recs[0]["claimed"]) == ("bob", "claude-lead")
    assert (recs[1]["sender"], recs[1]["claimed"]) == ("alice", None)
    c.close()
    c = Client(home, "typo-name")
    assert c.p.wait(timeout=10) == 2
    assert not (home / "typo-name").exists()


def test_wait_commits_unless_peek_and_descriptions_teach_the_rules(home: Path) -> None:
    box = Mailbox(home / "alice")
    inbound(box, "bob", FP, "ask", "q1")
    c = Client(home, "alice", "--reader", "w")
    c.init()
    tools = {t["name"]: t for t in c.call("tools/list")["result"]["tools"]}
    send_desc = tools["phoning_send"]["description"]
    assert all(w in send_desc for w in ("receipt", "refs", "ACTIVE", "scope", "last"))
    assert set(tools["phoning_read"]["inputSchema"]["properties"]["direction"]["enum"]) == {"in", "out", "all"}
    assert "peek" in tools["phoning_wait"]["inputSchema"]["properties"]
    got = c.tool("phoning_wait", {"timeout": 2, "peek": True})["structuredContent"]["records"]
    assert [r["env"]["subject"] for r in got] == ["q1"] and box.cursor("w") == 0
    got = c.tool("phoning_wait", {"timeout": 2})["structuredContent"]["records"]
    assert [r["env"]["subject"] for r in got] == ["q1"] and box.cursor("w") == 1
    assert c.tool("phoning_wait", {"timeout": 0.3})["structuredContent"]["records"] == []
    bad = c.tool("phoning_send", {"to": "bob", "type": "receipt", "subject": "done"})
    assert bad["isError"] and "reply_to" in bad["content"][0]["text"] and "refs" in bad["content"][0]["text"]
    c.close()


def test_read_never_skips_an_unshown_inbound_record(home: Path) -> None:
    box = Mailbox(home / "alice")
    a = inbound(box, "bob", FP, "ask", "A")
    outbound(box, "alice", "bob", "chat", "B")
    inbound(box, "bob", FP, "ask", "C")
    outbound(box, "alice", "bob", "chat", "D")
    c = Client(home, "alice", "--reader", "r")
    c.init()
    got = c.tool("phoning_read", {"direction": "out"})["structuredContent"]["records"]
    assert [r["env"]["subject"] for r in got] == ["B", "D"]
    assert box.cursor("r") < a["seq"]  # A is unread and unshown: the cursor must not pass it
    got = c.tool("phoning_read", {"limit": 1})["structuredContent"]["records"]
    assert [r["env"]["subject"] for r in got] == ["A"] and box.cursor("r") == 2  # consumed own send B, not C
    got = c.tool("phoning_read", {})["structuredContent"]["records"]
    assert [r["env"]["subject"] for r in got] == ["C"] and box.cursor("r") == 4
    c.close()
