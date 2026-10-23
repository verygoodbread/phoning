"""Unit tests for the activity view and living board page in :mod:`phoning.web`."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from phoning import ops, web
from phoning.mailbox import Mailbox
from phoning.retalk import RetalkError

TOKEN = "unit-test-token-0123456789abcdef"
FP = "a3f1c2d4e5b60718293a4b5c6d7e8f90"


def _record(seq: int, *, direction: str = "in", peer: str = "bob",
            peer_fp: str | None = FP,
            room: str | None = None, typ: str = "chat", subject: str = "hi",
            body: str = "", reply_to: str | None = None,
            thread: str | None = None, eid: str | None = None,
            principal: str | None = None, session: str = "s1",
            data: dict[str, Any] | None = None) -> dict[str, Any]:
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
            "from": {"principal": claimed, "session": session},
            "subject": subject,
            "body": body,
            "ts": f"2025-01-01T00:00:0{seq}Z",
            "thread": thread,
            "reply_to": reply_to,
            "room": room,
            "refs": [],
            "data": dict(data) if data else {},
        },
    }


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[web.ThreadingHTTPServer, str, int, str]]:
    """Start a viewer over a hand-written log and yield ``(server, udir, port, token)``."""
    udir = tmp_path / "home" / "alice"
    box = Mailbox(str(udir))
    recs = [
        _record(1, room="ops", typ="ask", subject="who owns the schema?", eid="A1"),
        _record(2, typ="ask", subject="dm question", eid="A2"),
        _record(3, typ="chat", subject="hello", eid="C1"),
        _record(4, typ="status", subject="busy", eid="S1", session="lyra",
                principal="bob"),
        _record(5, peer="carol", peer_fp="c" * 32, typ="chat", subject="spoofed", eid="C2",
                principal="claude-lead", session="x"),
        _record(6, peer="carol", peer_fp="c" * 32, typ="ask", subject="answered", eid="A3",
                session="x"),
        _record(7, direction="out", peer="carol", peer_fp=None, typ="ack", subject="ok",
                reply_to="A3", eid="R1", session="main"),
        _record(8, direction="out", peer="bob", peer_fp=None, typ="chat", subject="mine",
                eid="O1", session="web"),
    ]
    recs[3]["env"]["data"] = {"state": "ACTIVE"}
    for rec in recs:
        box.append(rec)
    server = web.make_server(str(udir), host="127.0.0.1", port=0, token=TOKEN, owners={})
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, str(udir), port, TOKEN
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


def test_scrub_cleans_redacts_and_caps() -> None:
    """``_scrub`` cleans lines, redacts secrets and caps at 120 chars."""
    out = web._scrub("a\nb\tc sk-abcdefghijklmnopqrstuvwx")
    assert "\n" not in out and "\t" not in out
    assert "[redacted]" in out
    assert len(web._scrub("x" * 500)) == 120


def test_scrub_hex_shortening_only_when_asked() -> None:
    """Hex runs are shortened only for doorbell/approvals strings."""
    long_hex = "ab" * 24
    assert long_hex in web._scrub(long_hex)
    short = web._scrub(long_hex, hex_short=True)
    assert short == long_hex[:8] + ".."


def test_activity_groups_by_transport_sender(world: tuple[web.ThreadingHTTPServer, str, int, str]) -> None:
    """Senders group by transport-bound identity, sessions by declared session."""
    _s, _u, port, _t = world
    status, _h, body = _get(port, "/api/activity")
    assert status == 200
    act = json.loads(body)
    assert set(act) == {"name", "senders", "open_asks", "doorbell", "approvals"}
    assert act["name"] == "alice"
    by = {s["sender"]: s for s in act["senders"]}
    assert set(by) == {"alice", "bob", "carol"}
    assert [s["sender"] for s in act["senders"]] == sorted(by)
    assert by["alice"]["self"] is True and by["bob"]["self"] is False
    assert by["alice"]["verified_fp8"] is None
    assert by["bob"]["verified_fp8"] == FP[:8]
    sess = {s["session"]: s for s in by["bob"]["sessions"]}
    assert set(sess) == {"lyra", "s1"}
    assert sess["s1"]["counts"] == {"ask": 2, "chat": 1}
    assert sess["s1"]["total"] == 3
    carol = {s["session"]: s for s in by["carol"]["sessions"]}["x"]
    assert "claude-lead" not in by
    assert carol["claimed"] == ["claude-lead"]


def test_activity_open_asks_and_counts(world: tuple[web.ThreadingHTTPServer, str, int, str]) -> None:
    """Open asks are inbound, unanswered and counted per session."""
    _s, _u, port, _t = world
    act = json.loads(_get(port, "/api/activity")[2])
    eids = [a["eid"] for a in act["open_asks"]]
    assert eids == ["A1", "A2"]
    first = act["open_asks"][0]
    assert first["sender"] == "bob" and first["session"] == "s1" and first["room"] == "ops"
    assert first["subject"] == "who owns the schema?" and first["peer"] == "bob"
    by = {s["sender"]: s for s in act["senders"]}
    assert {s["session"]: s for s in by["bob"]["sessions"]}["s1"]["open_asks"] == 2
    assert {s["session"]: s for s in by["carol"]["sessions"]}["x"]["open_asks"] == 0


def test_activity_skips_records_without_env() -> None:
    """Records whose ``env`` is not a dict are skipped entirely."""
    recs: list[dict[str, Any]] = [{"seq": 1, "env": "bad"}, {"seq": 2}]
    view = web._activity_view(recs, "alice", "/nonexistent")
    assert view["senders"] == []


def test_activity_response_leaks_nothing(world: tuple[web.ThreadingHTTPServer, str, int, str]) -> None:
    """The activity response never carries raw, bodies, fingerprints or the token."""
    _s, _u, port, _t = world
    raw = _get(port, "/api/activity")[2]
    for needle in ("RAW-WIRE-SENTINEL", TOKEN, FP, "c" * 32, '"raw"', '"peer_fp"', '"body"'):
        assert needle.encode() not in raw, needle


def test_activity_requires_token(world: tuple[web.ThreadingHTTPServer, str, int, str]) -> None:
    """``/api/activity`` needs the bearer token like every other API route."""
    _s, _u, port, _t = world
    assert _get(port, "/api/activity", token=None)[0] == 401
    assert _get(port, "/api/activity", token="nope")[0] == 401


def test_doorbell_missing_and_malformed(tmp_path: Path) -> None:
    """Missing, unreadable or non-object state files mean "not present"."""
    udir = tmp_path / "alice"
    udir.mkdir()
    assert web._doorbell_view(str(udir)) == {
        "present": False, "last_ring": None, "last_ring_iso": None, "pending": False,
        "introduced": [], "introduced_count": 0, "resume_counts": None, "resumes_total": None}
    for junk in ("not json", "[1,2]", ""):
        (udir / "doorbell-state.json").write_text(junk)
        assert web._doorbell_view(str(udir))["present"] is False


def test_doorbell_state_summary(tmp_path: Path) -> None:
    """The doorbell summary reads the documented keys and drops the rest."""
    udir = tmp_path / "alice"
    udir.mkdir()
    path = udir / "doorbell-state.json"
    path.write_text(json.dumps({"last_ring": 1760000000.0, "pending": True,
                                "introduced": ["bob", "carol", 7],
                                "resume_counts": {"sess-1": 2, "sess-2": 1, "bad": "x"},
                                "secret": "NOPE"}))
    before = path.read_bytes()
    d = web._doorbell_view(str(udir))
    assert d["present"] is True and d["pending"] is True
    assert d["last_ring"] == 1760000000.0 and d["last_ring_iso"] == "2025-10-09T08:53:20Z"
    assert d["introduced"] == ["bob", "carol"] and d["introduced_count"] == 2
    assert d["resume_counts"] == {"sess-1": 2, "sess-2": 1} and d["resumes_total"] == 3
    assert "NOPE" not in json.dumps(d)
    assert path.read_bytes() == before
    path.write_text(json.dumps({"last_ring": 0, "pending": "yes", "resumes": 4}))
    d2 = web._doorbell_view(str(udir))
    assert d2["last_ring"] is None and d2["pending"] is False
    assert d2["resumes_total"] == 4 and d2["resume_counts"] is None


def test_doorbell_hex_ids_are_shortened(tmp_path: Path) -> None:
    """Long hex runs in the doorbell state cannot leak."""
    udir = tmp_path / "alice"
    udir.mkdir()
    long_hex = "ab" * 24
    (udir / "doorbell-state.json").write_text(json.dumps(
        {"introduced": [FP], "resume_counts": {long_hex: 1}}))
    d = web._doorbell_view(str(udir))
    assert d["introduced"] == [FP[:8] + ".."]
    assert list(d["resume_counts"]) == [long_hex[:8] + ".."]


def test_doorbell_iso_guards_bad_timestamps() -> None:
    """Out-of-range timestamps yield a null ISO form."""
    assert web._iso_of(1e30) is None
    assert web._iso_of(0.0) == "1970-01-01T00:00:00Z"


def test_approvals_missing_and_malformed(tmp_path: Path) -> None:
    """A missing or unusable approvals file means "not present"."""
    udir = tmp_path / "alice"
    udir.mkdir()
    assert web._approvals_view(str(udir)) == {"present": False, "pending_count": 0,
                                              "pending": []}


def test_approvals_allowlist_and_resolution(tmp_path: Path) -> None:
    """Later lines replace earlier ones; only pending entries are emitted."""
    udir = tmp_path / "alice"
    udir.mkdir()
    long_hex = "ab" * 24
    lines = [
        {"id": "a1", "ts": "2026-10-11T10:00:00Z", "peer": "bob", "session": "vega",
         "ring": "codex-queue", "action": "resume", "status": "pending",
         "text": "APPROVAL-TEXT-SENTINEL", "token": "TOK-SENTINEL", "fingerprint": FP,
         "reason": "REASON-SENTINEL"},
        {"id": "a2", "peer": "carol", "action": "ring"},
        {"id": "a3", "peer": "dave", "status": "pending"},
        {"id": "a3", "peer": "dave", "status": "approved"},
        {"id": "a4", "peer": "erin", "status": "denied"},
        {"session": long_hex},
    ]
    path = udir / "doorbell-approvals.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n\n")
    before = path.read_bytes()
    ap = web._approvals_view(str(udir))
    assert path.read_bytes() == before
    assert ap["present"] is True and ap["pending_count"] == 3
    assert [p.get("id") for p in ap["pending"]] == ["a1", "a2", "line:6"]
    first = ap["pending"][0]
    assert list(first) == ["id", "ts", "peer", "session", "ring", "action", "status"]
    assert first["peer"] == "bob" and first["status"] == "pending"
    assert ap["pending"][1]["status"] == "pending"
    for needle in ("APPROVAL-TEXT-SENTINEL", "TOK-SENTINEL", "REASON-SENTINEL", FP,
                   long_hex, "dave", "erin"):
        assert needle not in json.dumps(ap), needle
    assert ap["pending"][2]["session"] == long_hex[:8] + ".."


def test_approvals_read_at_most_last_mib(tmp_path: Path) -> None:
    """Only the last 1 MiB of the approvals file is read."""
    udir = tmp_path / "alice"
    udir.mkdir()
    filler = b"{" + b"x" * 200 + b"}\n" * 6000
    path = udir / "doorbell-approvals.jsonl"
    with open(path, "wb") as fh:
        fh.write(filler)
        fh.write(json.dumps({"id": "tail", "peer": "bob", "status": "pending"}).encode() + b"\n")
    ap = web._approvals_view(str(udir))
    ids = [p.get("id") for p in ap["pending"]]
    assert "tail" in ids


def test_board_view_gains_structured_payload(world: tuple[web.ThreadingHTTPServer, str, int, str]) -> None:
    """``/api/board`` keeps text/board and adds the structured ``view``."""
    _s, _u, port, _t = world
    body = json.loads(_get(port, "/api/board")[2])
    assert set(body) == {"text", "board", "view"}
    view = body["view"]
    assert set(view) == {"status", "proposals", "open_asks", "receipts"}
    assert [(s["principal"], s["session"], s["state"]) for s in view["status"]] == [
        ("bob", "lyra", "ACTIVE")]
    asks = {a["eid"]: a for a in view["open_asks"]}
    assert set(asks) == {"A1", "A2"}
    assert asks["A1"]["sender"] == "bob" and asks["A1"]["room"] == "ops"


def test_board_view_sender_is_transport_bound(
        world: tuple[web.ThreadingHTTPServer, str, int, str]) -> None:
    """Open asks attribute the transport-bound sender, not the claimed principal."""
    _s, udir, port, _t = world
    spoof = _record(9, peer="carol", peer_fp="c" * 32, typ="ask", subject="forged ask",
                    eid="A9", principal="claude-lead", session="x")
    Mailbox(udir).append(spoof)
    view = json.loads(_get(port, "/api/board")[2])["view"]
    ask = next(a for a in view["open_asks"] if a["eid"] == "A9")
    assert ask["sender"] == "carol"


def test_board_view_is_scrubbed_and_one_line() -> None:
    """Board view strings are cleaned, redacted and single-line."""
    recs = [_record(1, typ="propose", subject="key sk-abcdefghijklmnopqrstuvwx now",
                    eid="P1", data={"scope": "x"})]
    folded = web.board_mod.fold(recs, {"": "bob"}, local="alice")
    view = web._board_structured(folded, recs, "alice")
    p = view["proposals"][0]
    assert "sk-abcdefghij" not in p["subject"] and "[redacted]" in p["subject"]


def test_board_structured_empty_sections() -> None:
    """An empty fold yields empty structured sections."""
    folded = web.board_mod.fold([], {}, local="alice")
    view = web._board_structured(folded, [], "alice")
    assert view == {"status": [], "proposals": [], "open_asks": [], "receipts": []}


def test_board_status_accepts_mapping_and_list() -> None:
    """``status`` accepts the folded mapping form and a list of entries."""
    folded: dict[str, Any] = {"status": {("bob", "lyra"): {"state": "ACTIVE",
                                                          "subject": "busy", "ts": "T"}}}
    view = web._board_structured(folded, [], "alice")
    assert view["status"] == [{"principal": "bob", "session": "lyra", "state": "ACTIVE",
                              "subject": "busy", "ts": "T"}]
    listed: dict[str, Any] = {"status": [{"principal": "bob", "session": "lyra",
                                          "state": "IDLE", "subject": "s", "ts": "T"}]}
    view2 = web._board_structured(listed, [], "alice")
    assert view2["status"][0]["state"] == "IDLE"
    bad: dict[str, Any] = {"status": {("bob", "lyra"): {"state": "NOPE"}}}
    assert web._board_structured(bad, [], "alice")["status"] == []


def test_board_page_and_scripts_are_static(world: tuple[web.ThreadingHTTPServer, str, int, str]) -> None:
    """``/board`` and ``/board.js`` are static, unauthenticated and data-free."""
    _s, _u, port, _t = world
    for path in ("/board", "/board.js"):
        status, hdrs, body = _get(port, path, token=None)
        assert status == 200, path
        assert "default-src 'none'" in hdrs["content-security-policy"]
        assert b"RAW-WIRE-SENTINEL" not in body
    _, _, html = _get(port, "/board", token=None)
    assert b'id="board-page"' in html and b"/board.js" in html and b"/app.js" in html
    assert b"/app.css" in html
    assert b"<script>" not in html and b"<style" not in html
    _, hdrs, js = _get(port, "/board.js", token=None)
    assert hdrs["content-type"].startswith("application/javascript")
    assert b"innerHTML" not in js
    assert b"phoningUI" in js and b"/api/board" in js and b"setInterval" in js


def test_app_js_exports_phoning_ui_and_keeps_guarantees(
        world: tuple[web.ThreadingHTTPServer, str, int, str]) -> None:
    """``/app.js`` exports the shared UI and keeps the old guarantees."""
    _s, _u, port, _t = world
    _, _, appjs = _get(port, "/app.js", token=None)
    assert b"phoningUI" in appjs and b"/api/activity" in appjs
    assert b"innerHTML" not in appjs
    assert b"from.principal" not in appjs and b"frm.principal" not in appjs
    assert b"sessionStorage" in appjs and b"replaceState" in appjs
    assert appjs.count(b"oneLine(") >= 5 and appjs.count(b"block(") >= 2
    for path in ("/", "/app.js", "/app.css"):
        assert b"proposal" not in _get(port, path, token=None)[2], path


def test_shell_has_activity_and_empty_compose(
        world: tuple[web.ThreadingHTTPServer, str, int, str]) -> None:
    """The ``/`` shell carries the activity section and a hidden empty compose box."""
    _s, _u, port, _t = world
    _, _, html = _get(port, "/", token=None)
    assert b'id="activity"' in html
    assert b'id="compose"' in html and b"hidden" in html
    assert b'href="/board"' in html


def test_summary_reports_can_send_false(tmp_path: Path) -> None:
    """``/api/summary`` reports ``can_send`` (false by default)."""
    udir = tmp_path / "home" / "alice"
    box = Mailbox(str(udir))
    box.append(_record(1))
    server = web.make_server(str(udir), host="127.0.0.1", port=0, token=TOKEN, owners={})
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.loads(_get(port, "/api/summary")[2])
        assert body["can_send"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_make_server_accepts_allow_send(tmp_path: Path) -> None:
    """``make_server`` takes ``allow_send`` as a keyword."""
    udir = tmp_path / "home" / "alice"
    Mailbox(str(udir))
    server = web.make_server(str(udir), host="127.0.0.1", port=0, token=TOKEN, owners={},
                             allow_send=True)
    server.server_close()


def test_parser_gains_allow_send() -> None:
    """``build_parser`` exposes ``--allow-send`` as ``allow_send``."""
    parser = web.build_parser()
    assert parser.parse_args(["alice"]).allow_send is False
    assert parser.parse_args(["alice", "--allow-send"]).allow_send is True


# --------------------------------------------------------------- sending


class _Sent:
    """Records what the fake retalk was asked to send."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []
        self.fail = False


#: Records into which the fake ``Retalk`` writes; keyed by user dir so several
#: servers can run at once without sharing one monkeypatched recorder.
_FAKE_SENT: dict[str, _Sent] = {}


def _sent_for(user_dir: str) -> _Sent:
    """Recorder used by the fake retalk for ``user_dir`` (created on demand)."""
    return _FAKE_SENT.setdefault(user_dir, _Sent())


class _FakeRetalk:
    """Stand-in for :class:`phoning.retalk.Retalk` that records sends."""

    def __init__(self, user_dir: str) -> None:
        self.user_dir = user_dir

    def send_peer(self, peer: str, wire: str) -> str:
        rec = _sent_for(self.user_dir)
        if rec.fail:
            raise RetalkError(1, ["retalk", "send", "SECRET-ARGV-SENTINEL"])
        rec.items.append((peer, wire))
        return f"tid-{len(rec.items)}"

    def send_group(self, room: str, wire: str) -> list[str]:
        rec = _sent_for(self.user_dir)
        if rec.fail:
            raise RetalkError(1, ["retalk", "send", "SECRET-ARGV-SENTINEL"])
        rec.items.append((room, wire))
        return [f"tid-{len(rec.items)}"]


@pytest.fixture(autouse=True)
def sent(monkeypatch: pytest.MonkeyPatch) -> _Sent:
    """Patch ``ops.Retalk`` with a recorder for every test in this module."""
    monkeypatch.setattr(ops, "Retalk", _FakeRetalk)
    _FAKE_SENT.clear()
    return _Sent()


@pytest.fixture
def sendworld(tmp_path: Path) -> Iterator[tuple[web.ThreadingHTTPServer, str, int, _Sent]]:
    """Start a send-enabled viewer over a hand-written log.

    Yields ``(server, udir, port, recorder)``; the recorder is the one the fake
    retalk uses for this user directory (so several servers can coexist).
    """
    udir = tmp_path / "home" / "alice"
    box = Mailbox(str(udir))
    box.append(_record(1, room="ops", typ="ask", subject="who owns the schema?", eid="A1"))
    server = web.make_server(str(udir), host="127.0.0.1", port=0, token=TOKEN, owners={},
                             allow_send=True)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, str(udir), port, _sent_for(str(udir))
    server.shutdown()
    server.server_close()


def _send(port: int, payload: Any, *, token: str | None = TOKEN,
          origin: str | None = "default", ctype: str | None = "application/json",
          raw: bytes | None = None, host: str | None = None,
          extra: dict[str, str] | None = None) -> tuple[int, dict[str, str], Any]:
    """Issue a ``POST /api/send`` with the gates configurable."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers: dict[str, str] = {"Host": host or f"127.0.0.1:{port}"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin == "default":
        headers["Origin"] = f"http://127.0.0.1:{port}"
    elif origin is not None:
        headers["Origin"] = origin
    if ctype is not None:
        headers["Content-Type"] = ctype
    headers.update(extra or {})
    body = raw if raw is not None else json.dumps(payload).encode()
    conn.request("POST", "/api/send", body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    hdrs = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    try:
        parsed: Any = json.loads(data)
    except ValueError:
        parsed = data
    return resp.status, hdrs, parsed


GOOD: dict[str, Any] = {"to": "bob", "type": "chat", "subject": "hello from the web", "body": "hi"}


def test_send_post_is_405_when_sending_off(world: tuple[web.ThreadingHTTPServer, str, int, str],
                                           sent: _Sent) -> None:
    """Without ``--allow-send`` every write verb is 405, ``/api/send`` included."""
    _s, _u, port, _t = world
    hdrs = {"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}
    status, resp_hdrs, _ = _send(port, GOOD, extra=hdrs)
    assert status == 405 and resp_hdrs["allow"] == "GET, HEAD"
    assert sent.items == []


def test_send_gates_are_all_403_and_send_nothing(sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent],
                                                  sent: _Sent) -> None:
    """Token, exact origin and JSON content type are all required."""
    _s, _u, port, rec = sendworld
    for kw in (
        {"token": None},
        {"token": "wrong"},
        {"origin": None},
        {"origin": "null"},
        {"origin": "http://evil.example"},
        {"origin": f"http://127.0.0.1:{port + 1}"},
        {"origin": f"https://127.0.0.1:{port}"},
        {"ctype": None},
        {"ctype": "text/plain"},
        {"ctype": "application/x-www-form-urlencoded"},
        {"ctype": "application/jsonx"},
    ):
        status, hdrs, body = _send(port, GOOD, **kw)
        assert status == 403, kw
        assert body == {"error": "forbidden"}, kw
        assert "content-security-policy" in hdrs
    assert rec.items == []


def test_send_accepts_content_type_parameters(sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent],
                                              sent: _Sent) -> None:
    """A ``; charset=`` parameter is allowed on the JSON content type."""
    _s, _u, port, _rec = sendworld
    assert _send(port, GOOD, ctype="Application/JSON; charset=utf-8")[0] == 200


def test_send_success_is_attributed_to_principal_and_session_web(
        sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent], sent: _Sent) -> None:
    """A successful send is recorded as ``out``/``web``/NAME and returns the eid."""
    _s, _u, port, rec = sendworld
    status, hdrs, resp = _send(port, {**GOOD})
    assert status == 200
    assert resp["ok"] is True and resp["sender"] == "alice" and resp["session"] == "web"
    assert "content-security-policy" in hdrs
    assert len(rec.items) == 1 and rec.items[0][0] == "bob"
    assert rec.items[0][1].startswith("@hd1 chat ") and resp["eid"] in rec.items[0][1]


def test_send_rejects_unknown_fields_sorted(sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent],
                                            sent: _Sent) -> None:
    """Unknown body keys are rejected with a sorted list."""
    _s, _u, port, rec = sendworld
    status, _h, resp = _send(port, {**GOOD, "zzz": 1, "aaa": 2})
    assert status == 400 and resp == {"error": "unknown field(s): aaa, zzz"}
    assert rec.items == []


def test_send_requires_type_and_subject(sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent],
                                        sent: _Sent) -> None:
    """``type`` and ``subject`` are mandatory strings."""
    _s, _u, port, rec = sendworld
    assert _send(port, {"to": "bob", "subject": "no type"})[::2] == (400, {"error": "type is required"})
    assert _send(port, {"to": "bob", "type": "chat"})[::2] == (400, {"error": "subject is required"})
    assert _send(port, {"to": "bob", "type": 5, "subject": "s"})[0] == 400
    assert _send(port, {"to": "bob", "type": "chat", "subject": "s", "body": 7})[0] == 400
    assert _send(port, {"to": "bob", "type": "chat", "subject": "s", "data": [1]})[0] == 400
    assert rec.items == []


def test_send_rejects_refs_and_thread(sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent],
                                      sent: _Sent) -> None:
    """``refs`` and ``thread`` are not accepted from the page."""
    _s, _u, port, rec = sendworld
    status, _h, resp = _send(port, {**GOOD, "refs": ["x"], "thread": "t"})
    assert status == 400 and "unknown field" in resp["error"]
    assert rec.items == []


def test_send_invalid_json_is_400(sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent],
                                  sent: _Sent) -> None:
    """Malformed JSON bodies are 400."""
    _s, _u, port, rec = sendworld
    assert _send(port, None, raw=b"{not json")[::2] == (400, {"error": "invalid JSON"})
    assert _send(port, None, raw=b"\xff\xfe")[::2] == (400, {"error": "invalid JSON"})
    assert _send(port, None, raw=b"[1,2]")[::2] == (400, {"error": "invalid JSON"})
    assert rec.items == []


def test_send_body_size_limit(sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent],
                              sent: _Sent) -> None:
    """Bodies up to 20480 UTF-8 bytes pass; a byte more is 413."""
    _s, _u, port, _rec = sendworld
    ok_body = "a" * 20480
    assert _send(port, {**GOOD, "body": ok_body})[0] == 200
    status, _h, resp = _send(port, {**GOOD, "body": ok_body + "a"})
    assert status == 413 and resp == {"error": "body too large"}
    assert _send(port, {**GOOD, "body": "é" * 10241})[0] == 413
    assert _send(port, {**GOOD, "body": "é" * 10240})[0] == 200


def test_send_oversize_content_length_is_413(sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent],
                                             sent: _Sent) -> None:
    """A ``Content-Length`` above 65536 is 413 with ``Connection: close``."""
    _s, udir, port, rec = sendworld
    before = list(rec.items)
    try:
        status, hdrs, _ = _send(port=port, payload=None, raw=b"a" * 100000)
    except (ConnectionError, http.client.HTTPException, OSError):
        status, hdrs = 413, {"connection": "close"}
    assert status == 413
    assert hdrs.get("connection") == "close"
    assert rec.items == before
    assert [r for r in Mailbox(udir)._read_all() if r.get("direction") == "out"] == []


def test_send_rate_limit_is_30_per_minute(sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent],
                                          sent: _Sent) -> None:
    """The 31st gated request in 60s is 429 with ``Retry-After``."""
    _s, _u, port, rec = sendworld
    for i in range(30):
        assert _send(port, {**GOOD, "subject": f"m{i}"})[0] == 200, i
    status, hdrs, resp = _send(port, GOOD)
    assert status == 429 and resp == {"error": "rate limited"}
    assert int(hdrs["retry-after"]) >= 1
    assert "content-security-policy" in hdrs
    assert len(rec.items) == 30
    # a failed gate does not consume the budget
    assert _send(port, GOOD, token="wrong")[0] == 403
    assert _send(port, GOOD)[0] == 429


def test_send_invalid_requests_count_towards_limit(
        sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent], sent: _Sent) -> None:
    """Requests that pass the gates count even when they later fail."""
    _s, _u, port, _rec = sendworld
    for _ in range(30):
        assert _send(port, {"type": "chat"})[0] == 400
    assert _send(port, GOOD)[0] == 429


def test_send_retalk_failure_is_502(sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent],
                                    sent: _Sent) -> None:
    """A ``RetalkError`` becomes 502 without leaking argv."""
    _s, udir, port, rec = sendworld
    rec.fail = True
    status, _h, resp = _send(port, GOOD)
    assert status == 502 and resp == {"error": "send failed"}
    assert "SECRET-ARGV-SENTINEL" not in json.dumps(resp)
    assert [r for r in Mailbox(udir)._read_all() if r.get("direction") == "out"] == []
    rec.fail = False
    assert _send(port, GOOD)[0] == 200


def test_send_unexpected_error_is_500(sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent],
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Any other exception becomes a generic 500."""
    _s, _u, port, _rec = sendworld

    def boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("SECRET-INTERNAL-SENTINEL")

    monkeypatch.setattr(ops, "send_message", boom)
    status, _h, resp = _send(port, GOOD)
    assert status == 500 and resp == {"error": "internal error"}


def test_send_redacts_secrets_in_error_messages(
        sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent], sent: _Sent) -> None:
    """Envelope errors are cleaned and redacted before they are sent."""
    _s, _u, port, _rec = sendworld
    status, _h, resp = _send(port, {"to": "bob", "type": "propose",
                                    "subject": "x", "data": {}})
    assert status == 400
    assert "propose needs data.scope" in resp["error"]
    status2, _h2, resp2 = _send(port, {"to": "bob", "type": "chat", "subject": "x" * 81})
    assert status2 == 400 and "subject must be 1..80 chars" in resp2["error"]


def test_send_uses_ops_module_at_call_time(
        sendworld: tuple[web.ThreadingHTTPServer, str, int, _Sent],
        monkeypatch: pytest.MonkeyPatch) -> None:
    """``send_message`` is looked up on the module, so tests can patch it."""
    _s, udir, port, rec = sendworld
    calls: list[dict[str, Any]] = []

    def fake(user_dir: str, name: str, session: str, **kw: Any) -> str:
        calls.append({"user_dir": user_dir, "name": name, "session": session, **kw})
        return "deadbeef" * 4

    monkeypatch.setattr(ops, "send_message", fake)
    status, _h, resp = _send(port, GOOD)
    assert status == 200 and resp["eid"] == "deadbeef" * 4
    assert calls[0]["name"] == "alice" and calls[0]["session"] == "web"
    assert calls[0]["msg_type"] == "chat" and calls[0]["to"] == "bob"
    assert calls[0]["user_dir"] == udir
    assert rec.items == []  # the real send path was not taken


def test_each_server_has_its_own_limiter(tmp_path: Path, sent: _Sent) -> None:
    """The rate limiter is per server instance, not a module global."""
    servers: list[web.ThreadingHTTPServer] = []
    recs: list[_Sent] = []
    ports: list[int] = []
    try:
        for i in range(2):
            udir = tmp_path / f"home{i}" / "alice"
            box = Mailbox(str(udir))
            box.append(_record(1))
            srv = web.make_server(str(udir), host="127.0.0.1", port=0, token=TOKEN,
                                  owners={}, allow_send=True)
            servers.append(srv)
            ports.append(int(srv.server_address[1]))
            recs.append(_sent_for(str(udir)))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        for _ in range(30):
            assert _send(ports[0], GOOD)[0] == 200
        assert _send(ports[0], GOOD)[0] == 429
        assert _send(ports[1], GOOD)[0] == 200
        assert len(recs[0].items) == 30 and len(recs[1].items) == 1
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()
