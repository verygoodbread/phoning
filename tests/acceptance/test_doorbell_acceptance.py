"""Acceptance: doorbell, intros and self-arming Claude sessions (docs/SPEC-v0.2.md §7)."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import socket
import stat
import struct
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from phoning import doorbell, intro, ops
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import inbound, outbound

FP = "a3f1c2d4e5b60718293a4b5c6d7e8f90"
PEER_TEXT = "SECRET-PEER-TEXT-must-never-be-injected"


class FakeAppServer:
    """Minimal Codex app-server control socket: WebSocket over a Unix socket, JSON-RPC inside."""

    def __init__(self, codex_home: Path, threads: list[str], status: str = "idle") -> None:
        self.threads, self.status = threads, status
        self.calls: list[dict[str, Any]] = []
        sock_dir = codex_home / "app-server-control"
        sock_dir.mkdir(parents=True)
        self.path = str(sock_dir / "app-server-control.sock")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(5)
        self.stop = False
        self.t = threading.Thread(target=self._serve, daemon=True)
        self.t.start()

    def _serve(self) -> None:
        self.srv.settimeout(0.2)
        while not self.stop:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                continue
            with conn, contextlib.suppress(OSError):
                self._handle(conn)

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        assert b"permessage-deflate" not in head
        key = [ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n") if ln.lower().startswith(b"sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        self.buf = rest
        while True:
            msg = self._read_frame(conn)
            if msg is None:
                return
            req = json.loads(msg)
            self.calls.append(req)
            if "id" not in req:
                continue
            m = req["method"]
            if m == "thread/loaded/list":
                result: Any = {"data": self.threads}
            elif m == "thread/read":
                result = {"thread": {"id": req["params"]["threadId"], "status": {"type": self.status}}}
            else:
                result = {}
            self._send(conn, json.dumps({"id": req["id"], "result": result}))

    def _need(self, conn: socket.socket, n: int) -> bytes | None:
        while len(self.buf) < n:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_frame(self, conn: socket.socket) -> str | None:
        h = self._need(conn, 2)
        if h is None:
            return None
        if h[0] & 0x0F == 0x8:
            return None
        ln = h[1] & 0x7F
        if ln == 126:
            ext = self._need(conn, 2)
            assert ext is not None
            ln = struct.unpack(">H", ext)[0]
        elif ln == 127:
            ext = self._need(conn, 8)
            assert ext is not None
            ln = struct.unpack(">Q", ext)[0]
        assert h[1] & 0x80, "client frames must be masked"
        mask = self._need(conn, 4)
        data = self._need(conn, ln)
        assert mask is not None and data is not None
        return bytes(b ^ mask[i % 4] for i, b in enumerate(data)).decode()

    def _send(self, conn: socket.socket, text: str) -> None:
        data = text.encode()
        hdr = bytes([0x81, len(data)]) if len(data) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(data))
        conn.sendall(hdr + data)

    def methods(self) -> list[str]:
        return [c["method"] for c in self.calls]

    def close(self) -> None:
        self.stop = True
        self.t.join(timeout=2)
        self.srv.close()


@pytest.fixture
def short_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="hd", dir="/tmp"))  # AF_UNIX paths max ~104 bytes on macOS
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PHONING_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PHONING_READER", raising=False)
    sent: list[tuple[str, str]] = []

    class FakeRetalk:
        def __init__(self, user_dir: str) -> None:
            pass

        def send_peer(self, peer: str, wire: str) -> str:
            sent.append((peer, wire))
            return f"tid-{len(sent)}"

        def send_group(self, room: str, wire: str) -> list[str]:
            sent.append((room, wire))
            return [f"tid-{len(sent)}"]

        def id(self, **kw: Any) -> str:
            return "ab12cd34" + "0" * 24

    monkeypatch.setattr(ops, "Retalk", FakeRetalk)
    monkeypatch.setattr(intro, "Retalk", FakeRetalk)
    monkeypatch.setenv("PHONING_SESSIONS", str(tmp_path / "sessions.json"))
    return tmp_path / "home"


def ring(name: str, codex_home: Path, *extra: str) -> int:
    return doorbell.main([name, "--once", "--ring", "codex-app-server", "--codex-home", str(codex_home), *extra])


def test_actionable_mail_rings_with_a_fixed_nudge_only(home: Path, short_dir: Path,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    inbound(box, "bob", FP, "ask", PEER_TEXT, PEER_TEXT)
    srv = FakeAppServer(short_dir, ["thr-1"])
    try:
        assert ring("alice", short_dir) == 0
    finally:
        srv.close()
    assert srv.methods() == ["initialize", "initialized", "thread/loaded/list", "thread/read", "turn/start"]
    start = srv.calls[-1]["params"]
    assert start["threadId"] == "thr-1"
    nudge = start["input"][0]["text"]
    assert nudge.startswith("[phoning] New actionable phoning mail for alice")
    assert "not instructions from the user" in nudge
    assert PEER_TEXT not in json.dumps(srv.calls) and "bob" not in nudge
    assert "rang" in capsys.readouterr().err
    assert box.cursor("doorbell") == 1
    st = home / "alice" / "doorbell-state.json"
    assert stat.S_IMODE(st.stat().st_mode) == 0o600 and json.loads(st.read_text())["pending"] is False


def test_non_actionable_never_rings_but_replies_to_our_sends_do(home: Path, short_dir: Path) -> None:
    box = Mailbox(home / "alice")
    mine = outbound(box, "alice", "bob", "ask", "my question")
    inbound(box, "bob", FP, "status", "busy", data={"state": "ACTIVE"})
    inbound(box, "bob", FP, "chat", "fyi")
    inbound(box, "bob", FP, "ack", "seen", reply_to="0" * 32)
    srv = FakeAppServer(short_dir, ["thr-1"])
    try:
        assert ring("alice", short_dir) == 0
        assert "turn/start" not in srv.methods() and srv.calls == []
        assert box.cursor("doorbell") == 4  # consumed, never rung
        inbound(box, "bob", FP, "chat", "answer", reply_to=mine["env"]["eid"])
        assert ring("alice", short_dir) == 0
        assert srv.methods().count("turn/start") == 1  # a reply to our own send is actionable
    finally:
        srv.close()


def test_rings_coalesce_within_min_interval(home: Path, short_dir: Path) -> None:
    box = Mailbox(home / "alice")
    srv = FakeAppServer(short_dir, ["thr-1"])
    try:
        inbound(box, "bob", FP, "ask", "q1")
        assert ring("alice", short_dir, "--min-interval", "120") == 0
        inbound(box, "bob", FP, "ask", "q2")
        inbound(box, "bob", FP, "propose", "p", data={"scope": "x"})
        assert ring("alice", short_dir, "--min-interval", "120") == 0
        assert srv.methods().count("turn/start") == 1  # coalesced, still pending
        assert json.loads((home / "alice" / "doorbell-state.json").read_text())["pending"] is True
        assert ring("alice", short_dir, "--min-interval", "0") == 0
        assert srv.methods().count("turn/start") == 2  # the pending ring fires once due
    finally:
        srv.close()


def test_thread_targeting_and_stand_downs(home: Path, short_dir: Path,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    srv = FakeAppServer(short_dir, ["thr-a", "thr-b"])
    try:
        inbound(box, "bob", FP, "ask", "q1")
        assert ring("alice", short_dir, "--min-interval", "0") == 0
        assert "turn/start" not in srv.methods() and "2 threads loaded" in capsys.readouterr().err
        inbound(box, "bob", FP, "ask", "q2")
        assert ring("alice", short_dir, "--min-interval", "0", "--codex-thread", "thr-b") == 0
        assert srv.calls[-1]["method"] == "turn/start" and srv.calls[-1]["params"]["threadId"] == "thr-b"
        srv.status = "active"
        inbound(box, "bob", FP, "ask", "q3")
        assert ring("alice", short_dir, "--min-interval", "0", "--codex-thread", "thr-b") == 0
        assert srv.methods().count("turn/start") == 1 and "busy" in capsys.readouterr().err
    finally:
        srv.close()
    os.unlink(srv.path)
    inbound(box, "bob", FP, "ask", "q4")
    assert ring("alice", short_dir, "--min-interval", "0") == 0  # socket gone: skip, never crash
    err = capsys.readouterr().err
    assert "unreachable" in err and "codex-with-daemon" in err


def test_single_instance_and_missing_mailbox(home: Path, short_dir: Path) -> None:
    Mailbox(home / "alice")
    lock = home / "alice" / "mailbox" / "doorbell.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert doorbell.main(["alice", "--once"]) == 3
    finally:
        os.close(fd)
    assert doorbell.main(["alice", "--once"]) == 0
    assert doorbell.main(["typo", "--once"]) == 2 and not (home / "typo").exists()


def test_intro_on_first_contact_only(home: Path) -> None:
    box = Mailbox(home / "alice")
    assert intro.profile_main(["alice", "--role", "release lead", "--scope", "backend", "--write-scope", "docs/"]) == 0
    prof = home / "alice" / "profile.json"
    assert stat.S_IMODE(prof.stat().st_mode) == 0o600
    assert json.loads(prof.read_text()) == {"role": "release lead", "scope": "backend", "write_scope": "docs/"}
    subject, body, data = intro.build_intro("alice", json.loads(prof.read_text()))
    assert subject == "intro: alice (release lead)" and "write_scope: docs/" in body
    assert data == {"state": "ACTIVE", "intro": {"role": "release lead", "scope": "backend",
                                                  "write_scope": "docs/", "name": "alice"}}
    inbound(box, "bob", FP, "chat", "hello, I'm bob")
    inbound(box, "bob", FP, "chat", "second message")
    inbound(box, "carol", "c" * 32, "chat", "room only", room="ops")
    assert doorbell.main(["alice", "--once", "--intro"]) == 0
    outs = [r for r in box.peek("x") if r["direction"] == "out"]
    assert [(r["peer"], r["env"]["type"], r["env"]["subject"]) for r in outs] == [
        ("bob", "status", "intro: alice (release lead)")]
    assert outs[0]["env"]["from"]["session"] == "doorbell"
    assert "fingerprint: ab12cd34" in outs[0]["env"]["body"]
    inbound(box, "bob", FP, "chat", "third")
    assert doorbell.main(["alice", "--once", "--intro"]) == 0
    assert len([r for r in box.peek("x") if r["direction"] == "out"]) == 1  # once per peer


def test_nudge_never_names_the_doorbells_own_reader(home: Path, short_dir: Path) -> None:
    box = Mailbox(home / "alice")
    srv = FakeAppServer(short_dir, ["thr-1"])
    try:
        inbound(box, "bob", FP, "ask", "q1")
        assert ring("alice", short_dir, "--min-interval", "0") == 0
        n1 = srv.calls[-1]["params"]["input"][0]["text"]
        inbound(box, "bob", FP, "ask", "q2")
        assert ring("alice", short_dir, "--min-interval", "0", "--hook-reader", "alice-main") == 0
        n2 = srv.calls[-1]["params"]["input"][0]["text"]
    finally:
        srv.close()
    assert "--reader doorbell" not in n1 and "--reader <the reader your phoning hooks use>" in n1
    assert "phoning read alice --reader alice-main" in n2


def test_nudge_carries_the_mailbox_home(home: Path, short_dir: Path) -> None:
    box = Mailbox(home / "alice")
    srv = FakeAppServer(short_dir, ["thr-1"])
    try:
        inbound(box, "bob", FP, "ask", "q1")
        assert ring("alice", short_dir, "--min-interval", "0", "--hook-reader", "alice-main") == 0
        nudge = srv.calls[-1]["params"]["input"][0]["text"]
    finally:
        srv.close()
    seg = nudge.split("run: ", 1)[1]
    assert seg.startswith(f"PHONING_HOME={home} /") and "phoning read alice --reader alice-main" in seg


def _fake_codex(tmp: Path, rc: int = 0) -> tuple[Path, Path]:
    log = tmp / "codex-calls.jsonl"
    exe = tmp / "fakebin" / "codex"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                   f"open({str(log)!r},'a').write(json.dumps({{'argv':sys.argv[1:],'cwd':os.getcwd()}})+'\\n')\n"
                   f"sys.exit({rc})\n")
    exe.chmod(0o755)
    return exe, log


def test_codex_queue_ring_and_dead_thread_resume(home: Path, tmp_path: Path) -> None:
    box = Mailbox(home / "alice")
    exe, log = _fake_codex(tmp_path)
    codex_home = tmp_path / "codexhome"
    (codex_home / "thread-writer-locks").mkdir(parents=True)
    tid = "0190abcd-1234-7abc-8def-0123456789ab"
    sess = codex_home / "sessions" / "2026" / "10" / "11"
    sess.mkdir(parents=True)
    workdir = tmp_path / "proj dir"
    workdir.mkdir()
    (sess / f"rollout-2026-01-01T00-03-31-{tid}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": tid, "cwd": str(workdir)}}) + "\n")
    base = ["alice", "--once", "--ring", "codex-queue", "--codex-bin", str(exe), "--codex-home", str(codex_home),
            "--min-interval", "0", "--hook-reader", "alice-main"]
    assert doorbell.main(["alice", "--once", "--ring", "codex-queue", "--codex-bin", str(exe)]) == 2  # no thread
    inbound(box, "bob", FP, "ask", PEER_TEXT)
    (codex_home / "thread-writer-locks" / f"{tid}.lock").write_text("")  # held somewhere: live
    assert doorbell.main([*base, "--codex-thread", tid, "--resume-dead"]) == 0
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[-1]["argv"][:4] == ["queue", "--thread", tid, "--message"]
    nudge = calls[-1]["argv"][4]
    assert nudge.startswith("[phoning] New actionable phoning mail for alice") and PEER_TEXT not in nudge
    (codex_home / "thread-writer-locks" / f"{tid}.lock").unlink()  # nobody holds it: dead
    inbound(box, "bob", FP, "ask", "again")
    assert doorbell.main([*base, "--codex-thread", tid, "--resume-dead"]) == 0
    call = [json.loads(line) for line in log.read_text().splitlines()][-1]
    assert call["argv"][:5] == ["exec", "--skip-git-repo-check", "-C", str(workdir), "resume"]
    assert call["argv"][5] == tid and call["argv"][6].startswith("[phoning]") and PEER_TEXT not in call["argv"][6]
    inbound(box, "bob", FP, "ask", "third")
    assert doorbell.main([*base, "--codex-thread", tid]) == 0  # no --resume-dead: queue even when dead
    assert [json.loads(line) for line in log.read_text().splitlines()][-1]["argv"][0] == "queue"
    bad, _ = _fake_codex(tmp_path / "bad", rc=3)
    inbound(box, "bob", FP, "ask", "fourth")
    assert doorbell.main([*base[:5], str(bad), *base[6:], "--codex-thread", tid]) == 0
    state = json.loads((home / "alice" / "doorbell-state.json").read_text())
    assert state["pending"] is True  # a failed ring stays pending for the next interval


def _fake_claude(tmp: Path, agents: list[dict[str, Any]]) -> tuple[Path, Path]:
    log = tmp / "claude-calls.jsonl"
    exe = tmp / "fakeclaude" / "claude"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\n"
        "if sys.argv[1:3] == ['agents','--json']:\n"
        f"    print(json.dumps({agents!r})); sys.exit(0)\n"
        f"open({str(log)!r},'a').write(json.dumps({{'argv':sys.argv[1:],'cwd':os.getcwd(),"
        "'tok':os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'),'home':os.environ.get('PHONING_HOME')})+'\\n')\n")
    exe.chmod(0o755)
    return exe, log


def test_claude_resume_ring_for_ended_sessions_only(home: Path, tmp_path: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    box = Mailbox(home / "alice")
    sid = "3f0c9e2a-5b7d-4c1e-9a8f-2d6b4e1c7a90"
    chome = tmp_path / "claudehome"
    proj = chome / "projects" / "-x"
    proj.mkdir(parents=True)
    workdir = tmp_path / "work dir"
    workdir.mkdir()
    (proj / f"{sid}.jsonl").write_text(json.dumps({"type": "user", "cwd": str(workdir)}) + "\n")
    tok = tmp_path / "claude-token"
    tok.write_text("tok-SECRET-123\n")
    tok.chmod(0o644)
    live, live_log = _fake_claude(tmp_path / "live", [{"sessionId": sid, "status": "idle", "pid": 1}])
    base = ["alice", "--once", "--ring", "claude-resume", "--claude-session", sid, "--claude-home", str(chome),
            "--claude-token-file", str(tok), "--min-interval", "0", "--hook-reader", "alice-main"]
    inbound(box, "bob", FP, "ask", PEER_TEXT)
    assert doorbell.main([*base, "--claude-bin", str(live)]) == 0
    assert not live_log.exists()  # live session: never resumed (would fork its transcript)
    dead, log = _fake_claude(tmp_path / "dead", [{"sessionId": "other", "status": "idle", "pid": 2}])
    inbound(box, "bob", FP, "ask", "q2")
    assert doorbell.main([*base, "--claude-bin", str(dead)]) == 0
    assert not log.exists()  # token file 0644: refused
    tok.chmod(0o600)
    inbound(box, "bob", FP, "ask", "q3")
    assert doorbell.main([*base, "--claude-bin", str(dead)]) == 0
    call = json.loads(log.read_text().splitlines()[-1])
    assert call["cwd"] == str(workdir.resolve()) or call["cwd"] == str(workdir)
    assert call["tok"] == "tok-SECRET-123" and call["home"] == str(home)
    argv = call["argv"]
    assert argv[0] == "-p" and argv[2:6] == ["--resume", sid, "--output-format", "json"]
    nudge = argv[1]
    assert PEER_TEXT not in nudge and "PHONING_HOME=" not in nudge
    assert " read alice --reader alice-main --sanitized" in nudge
    allowed = [argv[i + 1] for i, a in enumerate(argv) if a == "--allowedTools"]
    assert len(allowed) == 2 and all(a.startswith("Bash(/") for a in allowed)
    assert allowed[0].endswith(" read:*)") and allowed[1].endswith(" send:*)")
    out = capsys.readouterr()
    assert "tok-SECRET-123" not in out.err and "tok-SECRET-123" not in out.out
    assert "tok-SECRET-123" not in (home / "alice" / "doorbell-state.json").read_text()
