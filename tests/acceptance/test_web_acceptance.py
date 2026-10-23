"""Acceptance: `phoning web` (docs/SPEC-v0.2.md §3) — read-only, loopback-only, token-gated viewer."""

from __future__ import annotations

import http.client
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from phoning import web
from phoning.mailbox import Mailbox
from tests.acceptance.helpers import ROOT, inbound, outbound, snapshot

FP = "a3f1c2d4e5b60718293a4b5c6d7e8f90"
TOKEN = "t0ken-for-tests-only-abcdefghijklmnop"


class Srv:
    def __init__(self, user_dir: Path, owners: dict[str, Any] | None = None) -> None:
        self.httpd = web.make_server(str(user_dir), host="127.0.0.1", port=0, token=TOKEN, owners=owners or {})
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def req(self, method: str, path: str, *, token: str | None = TOKEN, host: str | None = None,
            body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, data

    def api(self, path: str) -> Any:
        status, hdrs, data = self.req("GET", path)
        assert status == 200, (path, status, data)
        assert hdrs["content-type"].startswith("application/json")
        return json.loads(data)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[Srv, Path, dict[str, Any]]]:
    udir = tmp_path / "home" / "alice"
    box = Mailbox(udir)
    (udir / "identity").mkdir()
    (udir / "identity" / "store.db").write_text("PRIVATE-KEY-MATERIAL-SENTINEL")
    recs: dict[str, Any] = {}
    recs["prop"] = inbound(box, "bob", FP, "propose", "rename table", "plan <script>alert(1)</script>",
                           principal="bob", session="vega", room="ops", data={"scope": "db/schema"})
    recs["acc"] = inbound(box, "carol", "c" * 32, "accept", "CONFIRMED", room="ops",
                          principal="carol", reply_to=recs["prop"]["env"]["eid"],
                          thread=recs["prop"]["env"]["eid"])
    recs["dm"] = inbound(box, "bob", FP, "chat", "direct hi", "key sk-abcdefghijklmnopqrstuvwx and "
                         "ghp_abcdefghijklmnopqrstuvwxyz0123 and AKIAABCDEFGHIJKLMNOP done")
    recs["out"] = outbound(box, "alice", "bob", "chat", "my reply")
    box.commit("sess-1", 2)
    srv = Srv(udir, owners={"db/": "bob"})
    yield srv, udir, recs
    srv.httpd.shutdown()
    srv.httpd.server_close()


def test_security_headers_and_static_shell(world: tuple[Srv, Path, dict[str, Any]]) -> None:
    srv, _, _ = world
    for path in ("/", "/app.js", "/app.css"):
        status, hdrs, body = srv.req("GET", path, token=None)
        assert status == 200, path
        csp = hdrs["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert hdrs["x-content-type-options"] == "nosniff"
        assert hdrs["referrer-policy"] == "no-referrer"
        assert hdrs["cache-control"] == "no-store"
        assert hdrs["x-frame-options"] == "DENY"
        assert b"rename table" not in body  # static assets carry no data
    _, _, html = srv.req("GET", "/", token=None)
    assert b"/app.js" in html and b"/app.css" in html
    assert b"<script>" not in html and b"<style" not in html
    _, _, js = srv.req("GET", "/app.js", token=None)
    assert b"innerHTML" not in js and b"textContent" in js
    assert b"replaceState" in js and b"Authorization" in js


def test_api_requires_bearer_token(world: tuple[Srv, Path, dict[str, Any]]) -> None:
    srv, _, _ = world
    for path in ("/api/summary", "/api/rooms", "/api/messages?room=ops", "/api/threads", "/api/board"):
        status, _, body = srv.req("GET", path, token=None)
        assert status == 401 and json.loads(body) == {"error": "unauthorized"}
        assert srv.req("GET", path, token="wrong")[0] == 401
        assert srv.req("GET", path + ("&" if "?" in path else "?") + f"token={TOKEN}", token=None)[0] == 401


def test_host_header_and_methods(world: tuple[Srv, Path, dict[str, Any]]) -> None:
    srv, _, _ = world
    assert srv.req("GET", "/api/summary", host="evil.example:80")[0] == 403
    assert srv.req("GET", "/api/summary", host=f"localhost:{srv.port}")[0] == 200
    for m in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        status, hdrs, _ = srv.req(m, "/api/messages", body=b"{}")
        assert status == 405 and hdrs["allow"] == "GET, HEAD", m
    assert srv.req("GET", "/nope")[0] == 404
    assert srv.req("GET", "/api/../identity/store.db")[0] in (403, 404)


def test_rooms_messages_threads_board(world: tuple[Srv, Path, dict[str, Any]]) -> None:
    srv, _, recs = world
    summary = srv.api("/api/summary")
    assert summary["name"] == "alice" and summary["records"] == 4 and "ops" in summary["rooms"]
    rooms = srv.api("/api/rooms")
    ops = next(r for r in rooms if r["room"] == "ops")
    assert ops["count"] == 2
    assert any(r["room"] is None and r["peer"] == "bob" for r in rooms)
    msgs = srv.api("/api/messages?room=ops")
    assert [m["env"]["subject"] for m in msgs] == ["rename table", "CONFIRMED"]
    assert msgs[0]["verified_fp8"] == FP[:8]
    assert msgs[0]["env"]["body"] == "plan <script>alert(1)</script>"  # raw JSON; the client renders as text
    threads = srv.api("/api/threads")
    t = next(x for x in threads if x["thread"] == recs["prop"]["env"]["eid"])
    assert t["count"] == 2 and t["subject"] == "rename table"
    thread = srv.api(f"/api/thread?eid={recs['prop']['env']['eid']}")
    assert len(thread) == 2
    board = srv.api("/api/board?room=ops")
    assert "NOT EFFECTIVE" in board["text"]  # carol's relayed CONFIRMED is not the owner's accept
    assert isinstance(board["board"], dict)


def test_no_secrets_and_no_writes(world: tuple[Srv, Path, dict[str, Any]]) -> None:
    srv, udir, _ = world
    before = snapshot(udir)
    dump = b""
    for path in ("/api/summary", "/api/rooms", "/api/messages?peer=bob", "/api/messages?room=ops",
                 "/api/threads", "/api/board", f"/api/thread?eid={'0' * 32}"):
        status, _, body = srv.req("GET", path)
        assert status == 200, path
        dump += body
    for m in json.loads(srv.req("GET", "/api/messages?peer=bob")[2]):
        assert "raw" not in m and "peer_fp" not in m
    assert b"PRIVATE-KEY-MATERIAL-SENTINEL" not in dump
    assert FP.encode() not in dump  # only the 8-char prefix is ever shown
    assert TOKEN.encode() not in dump
    assert b"sk-abcdefghij" not in dump and b"ghp_abcdef" not in dump and b"AKIAABCD" not in dump
    assert b"[redacted]" in dump
    assert snapshot(udir) == before  # log, cursors, everything byte-identical
    assert sorted(os.listdir(udir / "mailbox" / "cursors")) == ["sess-1.json"]


def test_cli_loopback_only_token_file_and_cleanup(tmp_path: Path) -> None:
    udir = tmp_path / "home" / "alice"
    Mailbox(udir)
    env = dict(os.environ, PHONING_HOME=str(tmp_path / "home"), PYTHONPATH=str(ROOT))
    bad = subprocess.run([sys.executable, "-c", "import sys,phoning.web as w; sys.exit(w.main(sys.argv[1:]))",
                          "alice", "--host", "0.0.0.0"], env=env, capture_output=True, text=True, timeout=30)
    assert bad.returncode == 2
    assert subprocess.run([sys.executable, "-c", "import sys,phoning.web as w; sys.exit(w.main(sys.argv[1:]))",
                           "nobody"], env=env, capture_output=True, text=True, timeout=30).returncode == 2
    p = subprocess.Popen([sys.executable, "-c", "import sys,phoning.web as w; sys.exit(w.main(sys.argv[1:]))",
                          "alice", "--port", "0"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert p.stdout is not None
        line = p.stdout.readline().strip()
        assert line.startswith("http://127.0.0.1:") and "/#token=" in line
        token = line.split("#token=", 1)[1]
        tok_file = udir / "web.token"
        assert tok_file.read_text().strip() == token
        assert stat.S_IMODE(tok_file.stat().st_mode) == 0o600
        port = int(line.split(":")[2].split("/")[0])
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/summary", headers={"Authorization": f"Bearer {token}"})
        assert c.getresponse().status == 200
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=10)
    time.sleep(0.1)
    assert not (udir / "web.token").exists()


def test_sender_is_transport_bound_and_claims_are_flagged(world: tuple[Srv, Path, dict[str, Any]]) -> None:
    srv, udir, _ = world
    spoof = inbound(Mailbox(udir), "bob", FP, "chat", "spoofed", principal="claude-lead", room="ops")
    msgs = srv.api("/api/messages?room=ops")
    m = next(x for x in msgs if x["seq"] == spoof["seq"])
    assert m["sender"] == "bob" and m["claimed"] == "claude-lead"
    honest = next(x for x in msgs if x["env"]["subject"] == "rename table")
    assert honest["sender"] == "bob" and honest["claimed"] is None
    mine = srv.api("/api/messages?peer=bob")
    assert next(x for x in mine if x["direction"] == "out")["sender"] == "alice"
    t = next(x for x in srv.api("/api/threads") if x["thread"] == spoof["env"]["eid"])
    assert t["participants"] == ["bob"]
    _, _, js = srv.req("GET", "/app.js", token=None)
    assert b"sender" in js and b"claims" in js


def test_url_file_is_private_and_removed(tmp_path: Path) -> None:
    udir = tmp_path / "home" / "alice"
    Mailbox(udir)
    env = dict(os.environ, PHONING_HOME=str(tmp_path / "home"), PYTHONPATH=str(ROOT))
    url_file = tmp_path / "link.txt"
    p = subprocess.Popen([sys.executable, "-c", "import sys,phoning.web as w; sys.exit(w.main(sys.argv[1:]))",
                          "alice", "--url-file", str(url_file)], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    try:
        assert p.stdout is not None
        line = p.stdout.readline().strip()
        assert url_file.read_text().strip() == line
        assert stat.S_IMODE(url_file.stat().st_mode) == 0o600
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=10)
    time.sleep(0.1)
    assert not url_file.exists()
