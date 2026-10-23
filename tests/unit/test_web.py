"""Unit tests for :mod:`phoning.web` (read-only local viewer)."""

from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from phoning import web
from phoning.mailbox import Mailbox

TOKEN = "unit-test-token-0123456789abcdef"


def _record(seq: int, *, direction: str = "in", peer: str = "bob",
            peer_fp: str | None = "a3f1c2d4e5b60718293a4b5c6d7e8f90",
            room: str | None = None, typ: str = "chat", subject: str = "hi",
            body: str = "", reply_to: str | None = None,
            thread: str | None = None, eid: str | None = None,
            principal: str | None = None) -> dict[str, Any]:
    """Build one mailbox record without touching a Mailbox."""
    claimed = principal if principal is not None else peer
    return {
        "seq": seq,
        "direction": direction,
        "tid": eid,
        "peer": peer,
        "peer_fp": peer_fp,
        "room": room,
        "recv_ts": f"2025-01-01T00:00:0{seq}Z",
        "raw": "RAW-WIRE-SENTINEL",
        "env": {
            "eid": eid or f"e{seq}",
            "type": typ,
            "from": {"principal": claimed, "session": "s1"},
            "subject": subject,
            "body": body,
            "ts": f"2025-01-01T00:00:0{seq}Z",
            "thread": thread,
            "reply_to": reply_to,
            "room": room,
            "refs": [],
            "data": {},
        },
    }


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[web.ThreadingHTTPServer, str, int]]:
    """Start a viewer over a hand-written log and yield ``(server, udir, port)``."""
    udir = tmp_path / "home" / "alice"
    box = Mailbox(str(udir))
    recs = [
        _record(1, room="ops", subject="proposal", eid="E1"),
        _record(2, room="ops", subject="reply", reply_to="E1", thread="E1", eid="E2"),
        _record(3, peer="carol", peer_fp=None, subject="direct", eid="E3"),
        _record(4, direction="out", peer="bob", peer_fp=None, subject="mine", eid="E4"),
    ]
    for rec in recs:
        box.append(rec)
    server = web.make_server(str(udir), host="127.0.0.1", port=0, token=TOKEN, owners={})
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, str(udir), port
    server.shutdown()
    server.server_close()


def _get(port: int, path: str, *, token: str | None = TOKEN) -> tuple[int, dict[str, str], bytes]:
    """Issue a GET with an optional bearer token."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": f"127.0.0.1:{port}"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    hdrs = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, hdrs, body


def test_redact_patterns() -> None:
    """All five redaction patterns are applied."""
    text = ("sk-abcdefghijklmnopqrstuvwx and ghp_abcdefghijklmnopqrstuvwxyz0123 and "
            "AKIAABCDEFGHIJKLMNOP and xoxb-abcdefghijkl and "
            "-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----")
    out = web._redact(text)
    assert "sk-" not in out
    assert "ghp_" not in out
    assert "AKIA" not in out
    assert "xoxb-" not in out
    assert "PRIVATE KEY" not in out
    assert out.count("[redacted]") == 5


def test_sanitized_uses_ops_and_redacts() -> None:
    """Web sanitizing delegates to ops (sender/claimed) and redacts secrets."""
    assert not hasattr(web, "sanitize")
    spoof = _record(7, subject="s sk-abcdefghijklmnopqrstuvwx",
                    body="ghp_abcdefghijklmnopqrstuvwxyz0123",
                    peer_fp=None, principal="mallory")
    out = web._sanitized(spoof, "alice")
    assert out["sender"] == "bob"
    assert out["claimed"] == "mallory"
    assert out["verified_fp8"] is None
    assert "raw" not in out and "peer_fp" not in out
    assert "[redacted]" in str(out["env"]["subject"])
    assert "[redacted]" in str(out["env"]["body"])
    assert set(out["env"]) <= {"eid", "type", "from", "subject", "body", "ts", "thread",
                               "reply_to", "room", "refs", "data"}
    honest = web._sanitized(_record(8, principal="bob"), "alice")
    assert honest["sender"] == "bob" and honest["claimed"] is None
    assert honest["verified_fp8"] == "a3f1c2d4"
    mine = web._sanitized(_record(9, direction="out", peer="bob", peer_fp=None), "alice")
    assert mine["sender"] == "alice" and mine["claimed"] == "bob"


def test_rooms_and_threads_views() -> None:
    """Room grouping separates direct peers; threads use sender values."""
    recs = [
        _record(1, room="ops", eid="E1"),
        _record(2, room="ops", reply_to="E1", thread="E1", eid="E2"),
        _record(3, peer="carol", peer_fp=None, eid="E3"),
        _record(4, direction="out", peer="bob", peer_fp=None, eid="E4"),
    ]
    rooms = web._rooms_view(recs)
    ops = next(r for r in rooms if r["room"] == "ops")
    assert ops["count"] == 2
    assert any(r["room"] is None and r["peer"] == "bob" for r in rooms)
    threads = web._threads_view(recs, "alice")
    t = next(x for x in threads if x["thread"] == "E1")
    assert t["count"] == 2 and t["subject"] == "hi"
    assert t["participants"] == ["bob"]
    own = next(x for x in threads if x["thread"] == "E4")
    assert own["participants"] == ["alice"]
    spoof = _record(5, room="ops", eid="E5", principal="mallory")
    mixed = web._threads_view([spoof], "alice")
    assert mixed[0]["participants"] == ["bob"]


def test_board_view_is_json_safe() -> None:
    """The folded board is converted to JSON-serializable structures."""
    recs = [_record(1, room="ops", typ="propose", subject="do it", eid="P1")]
    view = web._board_view(recs, {"ops": "bob"}, "alice", "ops")
    assert isinstance(view["text"], str)
    payload = json.dumps(view["board"])
    assert "P1" in payload


def test_json_safe_handles_tuple_keys_and_objects() -> None:
    """Tuple dict keys become slash names and unknown objects become strings."""
    class Odd:
        def __str__(self) -> str:
            return "odd"

    data: dict[Any, Any] = {("a", "b"): [1, {"c": Odd()}], 3: None}
    out = web._json_safe(data)
    assert out["a/b"][1]["c"] == "odd"


def test_host_check_and_methods(world: tuple[web.ThreadingHTTPServer, str, int]) -> None:
    """Bad Host headers get 403; write verbs get 405 with Allow."""
    _server, _udir, port = world
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/summary", headers={"Host": "evil.example"})
    assert conn.getresponse().status == 403
    conn.close()
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(method, "/api/summary", headers={"Host": f"127.0.0.1:{port}",
                                                      "Authorization": f"Bearer {TOKEN}"})
        resp = conn.getresponse()
        assert resp.status == 405
        assert resp.getheader("Allow") == "GET, HEAD"
        resp.read()
        conn.close()


def test_static_routes_carry_no_data(world: tuple[web.ThreadingHTTPServer, str, int]) -> None:
    """Static assets are served unauthenticated but contain no record data."""
    _server, _udir, port = world
    for path in ("/", "/app.css", "/app.js"):
        status, _hdrs, body = _get(port, path, token=None)
        assert status == 200
        assert b"proposal" not in body
        assert b"RAW-WIRE-SENTINEL" not in body
    _s, _u, js = _get(port, "/app.js", token=None)
    assert b"innerHTML" not in js
    assert b"sessionStorage" in js
    assert b"replaceState" in js


def test_js_uses_sender_and_claims(world: tuple[web.ThreadingHTTPServer, str, int]) -> None:
    """The client labels messages by sender and flags differing claims."""
    _server, _udir, port = world
    _s, _h, js = _get(port, "/app.js", token=None)
    assert b"sender" in js
    assert b"claims" in js
    assert b"frm.principal" not in js
    assert b"from.principal" not in js


def test_api_sender_claims_and_participants(
        world: tuple[web.ThreadingHTTPServer, str, int]) -> None:
    """API records are transport-bound; threads list sender participants."""
    _server, udir, port = world
    box = Mailbox(udir)
    spoof = _record(5, room="ops", subject="spoofed", eid="E5", principal="mallory")
    box.append(spoof)
    status, _hdrs, body = _get(port, "/api/messages?room=ops")
    assert status == 200
    msgs = json.loads(body)
    mine = next(m for m in msgs if m["seq"] == 5)
    assert mine["sender"] == "bob" and mine["claimed"] == "mallory"
    honest = next(m for m in msgs if m["env"]["subject"] == "proposal")
    assert honest["sender"] == "bob" and honest["claimed"] is None
    status, _hdrs, body = _get(port, "/api/messages?peer=bob")
    assert status == 200
    mine_out = next(m for m in json.loads(body) if m["direction"] == "out")
    assert mine_out["sender"] == "alice"
    status, _hdrs, body = _get(port, "/api/threads")
    assert status == 200
    thread = next(t for t in json.loads(body) if t["thread"] == "E5")
    assert thread["participants"] == ["bob"]


def test_make_server_picks_ipv6_class(tmp_path: Path) -> None:
    """``make_server`` uses an IPv6 server for ``::1`` and IPv4 otherwise."""
    udir = tmp_path / "home" / "alice"
    Mailbox(str(udir))
    ipv6 = web.make_server(str(udir), host="::1", port=0, token=TOKEN, owners={})
    assert isinstance(ipv6, web._IPv6ThreadingHTTPServer)
    assert ipv6.address_family == socket.AF_INET6
    ipv6.server_close()
    ipv4 = web.make_server(str(udir), host="127.0.0.1", port=0, token=TOKEN, owners={})
    assert not isinstance(ipv4, web._IPv6ThreadingHTTPServer)
    assert ipv4.address_family == socket.AF_INET
    ipv4.server_close()


def test_messages_limit_clamped(world: tuple[web.ThreadingHTTPServer, str, int]) -> None:
    """``limit`` is clamped to 1000 and defaults to 200."""
    _server, _udir, port = world
    for limit, expected in (("1", 1), ("0", 4), ("junk", 4), ("100000", 4)):
        status, _hdrs, body = _get(port, f"/api/messages?limit={limit}")
        assert status == 200
        assert len(json.loads(body)) == expected
    status, _hdrs, body = _get(port, "/api/messages?peer=carol&limit=50")
    assert status == 200
    recs = json.loads(body)
    assert [r["direction"] for r in recs] == ["in"]
    assert all(r["env"]["subject"] == "direct" for r in recs)


def test_unauthorized_and_unknown_routes(world: tuple[web.ThreadingHTTPServer, str, int]) -> None:
    """API routes need the exact token; unknown paths 404."""
    _server, _udir, port = world
    for path in ("/api/summary", "/api/rooms", "/api/messages", "/api/threads",
                 "/api/thread?eid=E1", "/api/board"):
        assert _get(port, path, token=None)[0] == 401
        assert _get(port, path, token="nope")[0] == 401
    assert _get(port, "/nope")[0] == 404
    assert _get(port, "/api/nope")[0] == 404


def test_head_has_no_body(world: tuple[web.ThreadingHTTPServer, str, int]) -> None:
    """HEAD returns the GET status with an empty body."""
    _server, _udir, port = world
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("HEAD", "/app.css", headers={"Host": f"127.0.0.1:{port}"})
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.read() == b""
    conn.close()
    _s, _h, body = _get(port, "/app.css")
    assert body


def test_helpers_direct() -> None:
    """Small helpers behave on missing and malformed input."""
    assert web._effective_room({"env": {"room": "r"}}) == "r"
    assert web._effective_room({"room": None, "env": "bad"}) is None
    assert web._ts_of({"env": {"ts": "T"}}) == "T"
    assert web._ts_of({"recv_ts": "R", "env": {}}) == "R"
    assert web._seq_of({"seq": True}) == 0
    assert web._seq_of({"seq": 4}) == 4
    assert web._env_of({"env": []}) == {}
    assert web.read_records("/nonexistent/path") == []


def test_token_file_mode_and_cli(tmp_path: Path) -> None:
    """``main`` rejects non-loopback hosts and missing logs; token file is 0600."""
    udir = tmp_path / "home" / "alice"
    Mailbox(str(udir))
    assert web.main(["alice", "--host", "0.0.0.0", "--home", str(tmp_path / "home")]) == 2
    assert web.main(["ghost", "--home", str(tmp_path / "home")]) == 2
    token_file = str(udir / "web.token")
    web._write_token_file(token_file, "tok")
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    os.unlink(token_file)
    assert not os.path.exists(token_file)


def test_parser_supports_url_file_and_open() -> None:
    """``build_parser`` accepts ``--url-file`` and ``--open``."""
    parser = web.build_parser()
    ns = parser.parse_args(["alice", "--url-file", "/tmp/u.txt", "--open"])
    assert ns.url_file == "/tmp/u.txt"
    assert ns.open is True
    plain = parser.parse_args(["alice"])
    assert plain.url_file is None
    assert plain.open is False


def test_private_file_write_is_0600_with_newline(tmp_path: Path) -> None:
    """Private files (token and URL) are written 0600 with a trailing newline."""
    target = tmp_path / "link.txt"
    web._write_private_file(str(target), "http://127.0.0.1:1/#token=x")
    assert target.read_text(encoding="utf-8") == "http://127.0.0.1:1/#token=x\n"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_js_display_filters_cover_every_string(
        world: tuple[web.ThreadingHTTPServer, str, int]) -> None:
    """The client filters headers with oneLine and bodies with block."""
    _server, _udir, port = world
    _s, _h, js = _get(port, "/app.js", token=None)
    assert b"oneLine" in js and b"block" in js
    assert b"innerHTML" not in js and b"textContent" in js
    assert b"pre" in js and b"pre-wrap" in js
    assert web.JS.count("oneLine(") >= 5
    assert web.JS.count("block(") >= 2
    assert b"whiteSpace" in js or b"pre-wrap" in js
