"""Acceptance: `phoning web` activity view, living board page and opt-in sending (docs/spec/web-activity.md)."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from phoning import ops, web
from phoning.mailbox import Mailbox
from phoning.retalk import RetalkError
from tests.acceptance.helpers import ROOT, inbound, outbound, snapshot

FP = "a3f1c2d4e5b60718293a4b5c6d7e8f90"
TOKEN = "t0ken-for-tests-only-abcdefghijklmnop"
SECRET_BODY = "BODY-SENTINEL-must-not-appear-in-activity"
LONG_HEX = "ab" * 24  # 48 hex chars


class Srv:
    def __init__(self, user_dir: Path, *, allow_send: bool = False, owners: dict[str, Any] | None = None) -> None:
        kw: dict[str, Any] = {"allow_send": True} if allow_send else {}
        self.httpd = web.make_server(str(user_dir), host="127.0.0.1", port=0, token=TOKEN, owners=owners or {}, **kw)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def req(self, method: str, path: str, *, token: str | None = TOKEN, host: str | None = None,
            body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, data

    def api(self, path: str) -> Any:
        status, _, data = self.req("GET", path)
        assert status == 200, (path, status, data)
        return json.loads(data)

    def send(self, payload: Any, *, token: str | None = TOKEN, origin: str | None = "default",
             ctype: str | None = "application/json", raw: bytes | None = None) -> tuple[int, dict[str, str], Any]:
        headers: dict[str, str] = {}
        if origin == "default":
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        elif origin is not None:
            headers["Origin"] = origin
        if ctype is not None:
            headers["Content-Type"] = ctype
        body = raw if raw is not None else json.dumps(payload).encode()
        status, hdrs, data = self.req("POST", "/api/send", token=token, body=body, headers=headers)
        try:
            parsed: Any = json.loads(data)
        except ValueError:
            parsed = data
        return status, hdrs, parsed

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def udir(tmp_path: Path) -> Path:
    d = tmp_path / "home" / "alice"
    Mailbox(d)
    (d / "identity").mkdir()
    (d / "identity" / "store.db").write_text("PRIVATE-KEY-MATERIAL-SENTINEL")
    return d


@pytest.fixture
def srv_factory(udir: Path) -> Iterator[Any]:
    made: list[Srv] = []

    def make(**kw: Any) -> Srv:
        s = Srv(udir, **kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


class Sent:
    """Records what the fake retalk was asked to send."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []
        self.fail = False


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Sent:
    rec = Sent()

    class FakeRetalk:
        def __init__(self, user_dir: str) -> None:
            pass

        def send_peer(self, peer: str, wire: str) -> str:
            if rec.fail:
                raise RetalkError(1, ["retalk", "send", "SECRET-ARGV-SENTINEL"])
            rec.items.append((peer, wire))
            return f"tid-{len(rec.items)}"

        def send_group(self, room: str, wire: str) -> list[str]:
            if rec.fail:
                raise RetalkError(1, ["retalk", "send", "SECRET-ARGV-SENTINEL"])
            rec.items.append((room, wire))
            return [f"tid-{len(rec.items)}"]

    monkeypatch.setattr(ops, "Retalk", FakeRetalk)
    return rec


def populate(udir: Path) -> dict[str, Any]:
    box = Mailbox(udir)
    r: dict[str, Any] = {}
    r["ask1"] = inbound(box, "bob", FP, "ask", "who owns the schema?", SECRET_BODY, principal="bob",
                        session="vega", room="ops")
    r["ask2"] = inbound(box, "bob", FP, "ask", "dm question", SECRET_BODY, principal="bob", session="vega")
    r["chat"] = inbound(box, "bob", FP, "chat", "hello", principal="bob", session="vega")
    r["other"] = inbound(box, "bob", FP, "status", "busy", principal="bob", session="lyra",
                         data={"state": "ACTIVE"})
    r["spoof"] = inbound(box, "carol", "c" * 32, "chat", "spoofed", principal="claude-lead", session="x")
    r["answered"] = inbound(box, "carol", "c" * 32, "ask", "already handled", principal="carol", session="x")
    r["reply"] = outbound(box, "alice", "carol", "ack", "ok", reply_to=r["answered"]["env"]["eid"], session="main")
    r["prop"] = inbound(box, "bob", FP, "propose", "rename table", principal="bob", session="vega", room="ops",
                        data={"scope": "db/schema"})
    r["acc"] = inbound(box, "carol", "c" * 32, "accept", "CONFIRMED", principal="carol", session="x", room="ops",
                       reply_to=r["prop"]["env"]["eid"], thread=r["prop"]["env"]["eid"])
    r["receipt"] = inbound(box, "bob", FP, "receipt", "delivered it", SECRET_BODY, principal="bob", session="vega",
                           room="ops", reply_to=r["prop"]["env"]["eid"], refs=["commit:abc"])
    return r


# ---------------------------------------------------------------- activity


def test_activity_requires_token_and_has_the_documented_shape(udir: Path, srv_factory: Any) -> None:
    populate(udir)
    srv = srv_factory()
    status, _, body = srv.req("GET", "/api/activity", token=None)
    assert status == 401 and json.loads(body) == {"error": "unauthorized"}
    act = srv.api("/api/activity")
    assert set(act) == {"name", "senders", "open_asks", "doorbell", "approvals"}
    assert act["name"] == "alice"


def test_activity_groups_by_transport_sender_and_session(udir: Path, srv_factory: Any) -> None:
    r = populate(udir)
    act = srv_factory().api("/api/activity")
    by = {s["sender"]: s for s in act["senders"]}
    assert set(by) == {"alice", "bob", "carol"}
    assert [s["sender"] for s in act["senders"]] == sorted(by)
    assert by["alice"]["self"] is True and by["bob"]["self"] is False
    assert by["alice"]["verified_fp8"] is None
    assert by["bob"]["verified_fp8"] == FP[:8]
    sess = {s["session"]: s for s in by["bob"]["sessions"]}
    assert set(sess) == {"vega", "lyra"}
    assert sess["vega"]["counts"] == {"ask": 2, "chat": 1, "propose": 1, "receipt": 1}
    assert sess["vega"]["total"] == 5
    assert sess["lyra"]["counts"] == {"status": 1}
    assert sess["vega"]["last_seen"] >= r["ask1"]["env"]["ts"]
    assert by["bob"]["last_seen"] == max(s["last_seen"] for s in by["bob"]["sessions"])
    # a spoofed principal never becomes the identity: it is a flagged claim under carol
    carol = {s["session"]: s for s in by["carol"]["sessions"]}["x"]
    assert "claude-lead" not in by
    assert carol["claimed"] == ["claude-lead"]
    assert carol["counts"]["chat"] == 1


def test_open_asks_addressed_to_us_exclude_answered_and_outbound(udir: Path, srv_factory: Any) -> None:
    r = populate(udir)
    act = srv_factory().api("/api/activity")
    eids = {a["eid"] for a in act["open_asks"]}
    assert eids == {r["ask1"]["env"]["eid"], r["ask2"]["env"]["eid"]}  # "already handled" was answered
    room_ask = next(a for a in act["open_asks"] if a["eid"] == r["ask1"]["env"]["eid"])
    assert room_ask["sender"] == "bob" and room_ask["session"] == "vega" and room_ask["room"] == "ops"
    assert room_ask["subject"] == "who owns the schema?" and room_ask["peer"] == "bob"
    dm_ask = next(a for a in act["open_asks"] if a["eid"] == r["ask2"]["env"]["eid"])
    assert dm_ask["room"] is None
    by = {s["sender"]: s for s in act["senders"]}
    vega = {s["session"]: s for s in by["bob"]["sessions"]}["vega"]
    assert vega["open_asks"] == 2
    assert {s["session"]: s for s in by["carol"]["sessions"]}["x"]["open_asks"] == 0
    # answering one removes it
    outbound(Mailbox(udir), "alice", "bob", "ack", "on it", reply_to=r["ask2"]["env"]["eid"])
    again = srv_factory().api("/api/activity")
    assert {a["eid"] for a in again["open_asks"]} == {r["ask1"]["env"]["eid"]}


def test_activity_leaks_nothing_and_writes_nothing(udir: Path, srv_factory: Any) -> None:
    populate(udir)
    (udir / "doorbell-state.json").write_text(json.dumps(
        {"last_ring": 1760000000.0, "pending": True, "introduced": ["bob", FP], "token": "TOKEN-SENTINEL"}))
    (udir / "doorbell-approvals.jsonl").write_text(json.dumps(
        {"id": "a1", "peer": "bob", "text": "APPROVAL-TEXT-SENTINEL", "fingerprint": FP}) + "\n")
    srv = srv_factory()
    before = snapshot(udir)
    other_files = sorted(p.name for p in udir.iterdir())
    status, _, raw = srv.req("GET", "/api/activity")
    assert status == 200
    assert snapshot(udir) == before
    assert sorted(p.name for p in udir.iterdir()) == other_files  # no new files (no token file here: made by main only)
    for needle in (SECRET_BODY, "PRIVATE-KEY-MATERIAL-SENTINEL", TOKEN, FP, "c" * 32, "APPROVAL-TEXT-SENTINEL",
                   "TOKEN-SENTINEL", '"raw"', '"peer_fp"', '"body"'):
        assert needle.encode() not in raw, needle
    assert sorted(os.listdir(udir / "mailbox" / "cursors")) == []


def test_doorbell_state_is_read_only_and_summarised(udir: Path, srv_factory: Any) -> None:
    populate(udir)
    state = udir / "doorbell-state.json"
    srv = srv_factory()
    assert srv.api("/api/activity")["doorbell"] == {
        "present": False, "last_ring": None, "last_ring_iso": None, "pending": False, "introduced": [],
        "introduced_count": 0, "resume_counts": None, "resumes_total": None}
    state.write_text(json.dumps({"last_ring": 1760000000.0, "pending": True, "introduced": ["bob", "carol", 7],
                                 "resume_counts": {"sess-1": 2, "sess-2": 1, "bad": "x"}, "secret": "NOPE"}))
    before = state.read_bytes()
    d = srv.api("/api/activity")["doorbell"]
    assert d["present"] is True and d["pending"] is True
    assert d["last_ring"] == 1760000000.0 and d["last_ring_iso"] == "2025-10-09T08:53:20Z"
    assert d["introduced"] == ["bob", "carol"] and d["introduced_count"] == 2
    assert d["resume_counts"] == {"sess-1": 2, "sess-2": 1} and d["resumes_total"] == 3
    assert "NOPE" not in json.dumps(d)
    assert state.read_bytes() == before
    state.write_text(json.dumps({"last_ring": 0, "pending": "yes", "resumes": 4}))
    d = srv.api("/api/activity")["doorbell"]
    assert d["last_ring"] is None and d["pending"] is False and d["resumes_total"] == 4 and d["resume_counts"] is None
    for junk in ("not json", "[1,2]", ""):
        state.write_text(junk)
        d = srv.api("/api/activity")["doorbell"]
        assert d["present"] is False and d["introduced"] == []


def test_doorbell_hex_ids_are_shortened(udir: Path, srv_factory: Any) -> None:
    populate(udir)
    (udir / "doorbell-state.json").write_text(json.dumps({"introduced": [FP], "resume_counts": {LONG_HEX: 1}}))
    raw = srv_factory().req("GET", "/api/activity")[2]
    assert FP.encode() not in raw and LONG_HEX.encode() not in raw
    d = json.loads(raw)["doorbell"]
    assert d["introduced"] == [FP[:8] + ".."]
    assert list(d["resume_counts"]) == [LONG_HEX[:8] + ".."]


def test_approvals_pending_allowlist_and_resolution(udir: Path, srv_factory: Any) -> None:
    populate(udir)
    srv = srv_factory()
    assert srv.api("/api/activity")["approvals"] == {"present": False, "pending_count": 0, "pending": []}
    lines = [
        {"id": "a1", "ts": "2026-10-11T10:00:00Z", "peer": "bob", "session": "vega", "ring": "codex-queue",
         "action": "resume", "status": "pending", "text": "APPROVAL-TEXT-SENTINEL", "token": "TOK-SENTINEL",
         "fingerprint": FP, "reason": "REASON-SENTINEL"},
        {"id": "a2", "peer": "carol", "action": "ring"},                      # no status -> pending
        {"id": "a3", "peer": "dave", "status": "pending"},
        {"id": "a3", "peer": "dave", "status": "approved"},                   # later line resolves a3
        {"id": "a4", "peer": "erin", "status": "denied"},
        {"peer": "frank", "session": LONG_HEX},                              # no id -> keyed by line number
    ]
    (udir / "doorbell-approvals.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n\n")
    before = (udir / "doorbell-approvals.jsonl").read_bytes()
    raw = srv.req("GET", "/api/activity")[2]
    ap = json.loads(raw)["approvals"]
    assert (udir / "doorbell-approvals.jsonl").read_bytes() == before
    assert ap["present"] is True and ap["pending_count"] == 3
    assert [p.get("id") for p in ap["pending"]] == ["a1", "a2", ap["pending"][2].get("id")]
    first = ap["pending"][0]
    assert list(first) == ["id", "ts", "peer", "session", "ring", "action", "status"]
    assert first["peer"] == "bob" and first["status"] == "pending"
    assert ap["pending"][1]["status"] == "pending"
    for needle in ("APPROVAL-TEXT-SENTINEL", "TOK-SENTINEL", "REASON-SENTINEL", FP, LONG_HEX, "dave", "erin"):
        assert needle not in json.dumps(ap), needle
    assert ap["pending"][2]["session"] == LONG_HEX[:8] + ".."


# ---------------------------------------------------------------- living board


def test_board_view_is_structured_and_free_of_bodies(udir: Path, srv_factory: Any) -> None:
    r = populate(udir)
    srv = srv_factory(owners={"db/": "bob"})
    raw = srv.req("GET", "/api/board")[2]
    assert SECRET_BODY.encode() not in json.dumps(json.loads(raw)["view"]).encode()
    b = json.loads(raw)
    assert set(b) == {"board", "text", "view"}
    view = b["view"]
    assert set(view) == {"status", "proposals", "open_asks", "receipts"}
    assert [(s["principal"], s["session"], s["state"]) for s in view["status"]] == [("bob", "lyra", "ACTIVE")]
    prop = view["proposals"][0]
    assert prop["eid"] == r["prop"]["env"]["eid"] and prop["scope"] == "db/schema"
    assert prop["state"] == "OPEN" and prop["owners"] == ["bob"] and prop["by"] == {"principal": "bob", "session": "vega"}
    dec = prop["decisions"][0]
    assert dec["type"] == "accept" and dec["effective"] is False and "bob" in dec["reason"]
    assert dec["by"]["principal"] == "carol"
    asks = {a["eid"]: a for a in view["open_asks"]}
    assert set(asks) == {r["ask1"]["env"]["eid"], r["ask2"]["env"]["eid"]}
    assert asks[r["ask1"]["env"]["eid"]]["sender"] == "bob"
    rc = view["receipts"][0]
    assert rc["sender"] == "bob" and rc["refs"] == ["commit:abc"] and rc["subject"] == "delivered it"
    # room filter still applies to the view
    ops_view = srv.api("/api/board?room=ops")["view"]
    assert {a["eid"] for a in ops_view["open_asks"]} == {r["ask1"]["env"]["eid"]}


def test_board_view_uses_transport_sender_not_the_claim(udir: Path, srv_factory: Any) -> None:
    box = Mailbox(udir)
    a = inbound(box, "carol", "c" * 32, "ask", "forged ask", principal="claude-lead", session="s")
    view = srv_factory().api("/api/board")["view"]
    ask = next(x for x in view["open_asks"] if x["eid"] == a["env"]["eid"])
    assert ask["sender"] == "carol"


def test_board_view_is_redacted_and_one_line(udir: Path, srv_factory: Any) -> None:
    box = Mailbox(udir)
    inbound(box, "bob", FP, "propose", "key sk-abcdefghijklmnopqrstuvwx now", principal="bob", session="vega",
            data={"scope": "a\nb\x1b[31m"})
    view = srv_factory().api("/api/board")["view"]
    p = view["proposals"][0]
    assert "sk-abcdefghij" not in p["subject"] and "[redacted]" in p["subject"]
    assert "\n" not in p["scope"] and "\x1b" not in p["scope"]


def test_board_page_shell_and_scripts(udir: Path, srv_factory: Any) -> None:
    populate(udir)
    srv = srv_factory()
    for path in ("/board", "/board.js"):
        status, hdrs, body = srv.req("GET", path, token=None)
        assert status == 200, path
        assert "default-src 'none'" in hdrs["content-security-policy"] and hdrs["cache-control"] == "no-store"
        assert b"rename table" not in body and SECRET_BODY.encode() not in body
    _, _, html = srv.req("GET", "/board", token=None)
    assert b'id="board-page"' in html and b"/board.js" in html and b"/app.js" in html and b"/app.css" in html
    assert b"<script>" not in html and b"<style" not in html and b" style=" not in html
    _, hdrs, js = srv.req("GET", "/board.js", token=None)
    assert hdrs["content-type"].startswith("application/javascript")
    assert b"innerHTML" not in js and b"insertAdjacentHTML" not in js
    assert b"phoningUI" in js and b"/api/board" in js and b"setInterval" in js
    assert b"visibilitychange" in js
    _, _, root = srv.req("GET", "/", token=None)
    assert b'href="/board"' in root
    # the old guarantees about the shared shell/script are unchanged
    for path in ("/", "/app.js", "/app.css"):
        assert b"proposal" not in srv.req("GET", path, token=None)[2], path
    _, _, appjs = srv.req("GET", "/app.js", token=None)
    assert b"innerHTML" not in appjs and b"phoningUI" in appjs and b"/api/activity" in appjs
    assert b"from.principal" not in appjs and b"frm.principal" not in appjs
    assert srv.req("GET", "/board.js", token=None, host="evil.example:1")[0] == 403


def test_activity_section_in_shell(udir: Path, srv_factory: Any) -> None:
    srv = srv_factory()
    _, _, html = srv.req("GET", "/", token=None)
    assert b'id="activity"' in html and b'id="compose"' in html and b"hidden" in html


# ---------------------------------------------------------------- sending: off by default


def test_default_is_still_read_only(udir: Path, srv_factory: Any, sent: Sent) -> None:
    populate(udir)
    srv = srv_factory()
    assert srv.api("/api/summary")["can_send"] is False
    for m in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        status, hdrs, _ = srv.req(m, "/api/send", body=b"{}", headers={
            "Origin": f"http://127.0.0.1:{srv.port}", "Content-Type": "application/json"})
        assert status == 405 and hdrs["allow"] == "GET, HEAD", m
    assert sent.items == []
    assert srv.send({"to": "bob", "type": "chat", "subject": "x"})[0] == 405
    assert srv.api("/api/activity")  # still readable


def test_enabled_server_still_405s_everything_else(udir: Path, srv_factory: Any, sent: Sent) -> None:
    srv = srv_factory(allow_send=True)
    assert srv.api("/api/summary")["can_send"] is True
    hdrs = {"Origin": f"http://127.0.0.1:{srv.port}", "Content-Type": "application/json"}
    for m in ("PUT", "PATCH", "DELETE", "OPTIONS"):
        status, h, _ = srv.req(m, "/api/send", body=b"{}", headers=hdrs)
        assert status == 405 and h["allow"] == "GET, HEAD, POST", m
    for path in ("/api/summary", "/api/messages", "/", "/nope"):
        status, h, _ = srv.req("POST", path, body=b"{}", headers=hdrs)
        assert status == 405 and h["allow"] == "GET, HEAD, POST", path
    assert sent.items == []


# ---------------------------------------------------------------- sending: gates


GOOD = {"to": "bob", "type": "chat", "subject": "hello from the web", "body": "hi"}


def test_send_gates_are_all_403_and_send_nothing(udir: Path, srv_factory: Any, sent: Sent) -> None:
    srv = srv_factory(allow_send=True)
    for kw in (
        {"token": None},
        {"token": "wrong"},
        {"origin": None},
        {"origin": "null"},
        {"origin": "http://evil.example"},
        {"origin": f"http://127.0.0.1:{srv.port + 1}"},
        {"origin": f"https://127.0.0.1:{srv.port}"},
        {"ctype": None},
        {"ctype": "text/plain"},
        {"ctype": "application/x-www-form-urlencoded"},
        {"ctype": "application/jsonx"},
    ):
        status, hdrs, body = srv.send(GOOD, **kw)
        assert status == 403, kw
        assert body == {"error": "forbidden"}, kw
        assert "content-security-policy" in hdrs
    assert srv.req("POST", "/api/send", body=json.dumps(GOOD).encode(), host="evil.example:1", headers={
        "Origin": "http://evil.example:1", "Content-Type": "application/json"})[0] == 403
    assert sent.items == []
    assert [r for r in Mailbox(udir)._read_all() if r.get("direction") == "out"] == []


def test_send_accepts_json_content_type_parameters_and_localhost_origin(udir: Path, srv_factory: Any,
                                                                         sent: Sent) -> None:
    srv = srv_factory(allow_send=True)
    assert srv.send(GOOD, ctype="Application/JSON; charset=utf-8")[0] == 200
    status, _, _ = srv.req("POST", "/api/send", host=f"localhost:{srv.port}", body=json.dumps(GOOD).encode(),
                           headers={"Origin": f"http://localhost:{srv.port}", "Content-Type": "application/json"})
    assert status == 200


# ---------------------------------------------------------------- sending: behaviour


def test_send_dm_is_attributed_to_the_principal_and_session_web(udir: Path, srv_factory: Any, sent: Sent) -> None:
    srv = srv_factory(allow_send=True)
    status, _, resp = srv.send({**GOOD, "from": "mallory"})
    assert status == 400 and "from" in resp["error"]  # a body cannot choose the sender
    status, _, resp = srv.send(GOOD)
    assert status == 200 and resp["ok"] is True and resp["sender"] == "alice" and resp["session"] == "web"
    assert len(sent.items) == 1 and sent.items[0][0] == "bob"
    wire = sent.items[0][1]
    assert wire.startswith("@hd1 chat ") and resp["eid"] in wire
    msgs = srv.api("/api/messages?peer=bob")
    m = next(x for x in msgs if x["env"]["eid"] == resp["eid"])
    assert m["direction"] == "out" and m["sender"] == "alice"
    assert m["env"]["from"] == {"principal": "alice", "session": "web"}
    assert m["env"]["subject"] == "hello from the web" and m["env"]["body"] == "hi"


def test_send_to_room_with_data_and_reply(udir: Path, srv_factory: Any, sent: Sent) -> None:
    r = populate(udir)
    srv = srv_factory(allow_send=True)
    ask = r["ask1"]["env"]["eid"]
    status, _, resp = srv.send({"room": "ops", "type": "ack", "subject": "on it", "reply_to": ask})
    assert status == 200
    assert sent.items[0][0] == "ops"
    out = next(x for x in srv.api("/api/messages?room=ops") if x["env"]["eid"] == resp["eid"])
    assert out["env"]["reply_to"] == ask and out["env"]["thread"] == r["ask1"]["env"]["thread"]
    assert {a["eid"] for a in srv.api("/api/activity")["open_asks"]} == {r["ask2"]["env"]["eid"]}
    status, _, resp2 = srv.send({"room": "ops", "type": "status", "subject": "working",
                                 "data": {"state": "ACTIVE"}})
    assert status == 200
    view = srv.api("/api/board")["view"]
    assert any(s["principal"] == "alice" and s["session"] == "web" and s["state"] == "ACTIVE"
               for s in view["status"])
    status, _, _ = srv.send({"to": "bob", "type": "ack", "subject": "ok", "reply_to": "last"})
    assert status == 200


def test_envelope_validation_errors_are_400_with_the_joined_message(udir: Path, srv_factory: Any,
                                                                      sent: Sent) -> None:
    srv = srv_factory(allow_send=True)
    status, _, resp = srv.send({"to": "bob", "type": "propose", "subject": "x" * 81})
    assert status == 400
    assert "subject must be 1..80 chars" in resp["error"] and "propose needs data.scope" in resp["error"]
    assert "; " in resp["error"]
    status, _, resp = srv.send({"to": "bob", "type": "accept", "subject": "yes"})
    assert status == 400 and "accept needs reply_to" in resp["error"]
    status, _, resp = srv.send({"to": "bob", "type": "nonsense", "subject": "yes"})
    assert status == 400 and "type must be one of" in resp["error"]
    status, _, resp = srv.send({"to": "bob", "room": "ops", "type": "chat", "subject": "both"})
    assert status == 400 and "exactly one of" in resp["error"]
    status, _, resp = srv.send({"type": "chat", "subject": "nowhere"})
    assert status == 400 and "exactly one of" in resp["error"]
    status, _, resp = srv.send({"to": "bob", "type": "chat", "subject": "multi\nline"})
    assert status == 400 and "subject" in resp["error"] and "\n" not in resp["error"]
    status, _, resp = srv.send({"to": "bob", "type": "ack", "subject": "x", "reply_to": "last"})
    assert status == 400  # nothing to reply to in an empty log
    assert sent.items == []


def test_malformed_requests_are_400(udir: Path, srv_factory: Any, sent: Sent) -> None:
    srv = srv_factory(allow_send=True)
    assert srv.send(None, raw=b"{not json")[::2] == (400, {"error": "invalid JSON"})
    assert srv.send(None, raw=b"\xff\xfe")[::2] == (400, {"error": "invalid JSON"})
    assert srv.send(None, raw=b"[1,2]")[::2] == (400, {"error": "invalid JSON"})
    assert srv.send({"to": "bob", "subject": "no type"})[::2] == (400, {"error": "type is required"})
    assert srv.send({"to": "bob", "type": "chat"})[::2] == (400, {"error": "subject is required"})
    assert srv.send({"to": "bob", "type": 5, "subject": "s"})[0] == 400
    assert srv.send({"to": "bob", "type": "chat", "subject": "s", "body": 7})[0] == 400
    assert srv.send({"to": "bob", "type": "chat", "subject": "s", "data": [1]})[0] == 400
    status, _, resp = srv.send({"to": "bob", "type": "chat", "subject": "s", "zzz": 1, "aaa": 2})
    assert status == 400 and resp == {"error": "unknown field(s): aaa, zzz"}
    status, _, resp = srv.send({"to": "bob", "type": "chat", "subject": "s", "refs": ["x"], "thread": "t"})
    assert status == 400  # refs/thread are not accepted from the page
    status, _, _ = srv.req("POST", "/api/send", body=b"{}", headers={
        "Origin": f"http://127.0.0.1:{srv.port}", "Content-Type": "application/json", "Content-Length": "-5"})
    assert status in (400, 403)
    assert sent.items == []


def test_size_limits(udir: Path, srv_factory: Any, sent: Sent) -> None:
    srv = srv_factory(allow_send=True)
    ok_body = "a" * 20480
    assert srv.send({**GOOD, "body": ok_body})[0] == 200
    status, _, resp = srv.send({**GOOD, "body": ok_body + "a"})
    assert status == 413 and resp == {"error": "body too large"}
    status, _, resp = srv.send({**GOOD, "body": "é" * 10241})  # 20482 bytes in UTF-8
    assert status == 413
    assert srv.send({**GOOD, "body": "é" * 10240})[0] == 200
    assert len(sent.items) == 2
    try:
        status, _, _ = srv.send({**GOOD, "body": "a" * 100_000})
    except (ConnectionError, http.client.HTTPException, OSError):
        status = 413  # server may close before the client finishes writing
    assert status == 413
    assert len(sent.items) == 2


def test_rate_limit_is_30_per_minute(udir: Path, srv_factory: Any, sent: Sent) -> None:
    srv = srv_factory(allow_send=True)
    for i in range(30):
        status, _, _ = srv.send({**GOOD, "subject": f"m{i}"})
        assert status == 200, i
    status, hdrs, resp = srv.send(GOOD)
    assert status == 429 and resp == {"error": "rate limited"}
    assert int(hdrs["retry-after"]) >= 1
    assert "content-security-policy" in hdrs
    assert len(sent.items) == 30
    # failed gates do not consume the budget, but the limit holds for the token holder
    assert srv.send(GOOD, token="wrong")[0] == 403
    assert srv.send(GOOD)[0] == 429
    # reads are never limited
    assert srv.req("GET", "/api/summary")[0] == 200


def test_invalid_requests_count_towards_the_limit(udir: Path, srv_factory: Any, sent: Sent) -> None:
    srv = srv_factory(allow_send=True)
    for _ in range(30):
        assert srv.send({"type": "chat"})[0] == 400
    assert srv.send(GOOD)[0] == 429


def test_each_server_has_its_own_limiter(udir: Path, srv_factory: Any, sent: Sent) -> None:
    a = srv_factory(allow_send=True)
    b = srv_factory(allow_send=True)
    for _ in range(30):
        assert a.send(GOOD)[0] == 200
    assert a.send(GOOD)[0] == 429
    assert b.send(GOOD)[0] == 200


def test_transport_failure_is_502_without_leaking(udir: Path, srv_factory: Any, sent: Sent) -> None:
    srv = srv_factory(allow_send=True)
    sent.fail = True
    status, _, resp = srv.send(GOOD)
    assert status == 502 and resp == {"error": "send failed"}
    assert "SECRET-ARGV-SENTINEL" not in json.dumps(resp)
    assert [r for r in Mailbox(udir)._read_all() if r.get("direction") == "out"] == []  # nothing recorded
    sent.fail = False
    assert srv.send(GOOD)[0] == 200


def test_unexpected_error_is_500_generic(udir: Path, srv_factory: Any, sent: Sent,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    srv = srv_factory(allow_send=True)

    def boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("SECRET-INTERNAL-SENTINEL")

    monkeypatch.setattr(ops, "send_message", boom)
    status, _, resp = srv.send(GOOD)
    assert status == 500 and resp == {"error": "internal error"}


def test_sending_does_not_weaken_read_surfaces(udir: Path, srv_factory: Any, sent: Sent) -> None:
    populate(udir)
    srv = srv_factory(allow_send=True)
    srv.send(GOOD)
    dump = b""
    for path in ("/api/summary", "/api/rooms", "/api/messages", "/api/threads", "/api/board", "/api/activity"):
        status, hdrs, body = srv.req("GET", path)
        assert status == 200
        assert "content-security-policy" in hdrs
        dump += body
    assert TOKEN.encode() not in dump and FP.encode() not in dump and b'"raw"' not in dump
    assert b"PRIVATE-KEY-MATERIAL-SENTINEL" not in dump
    assert srv.req("GET", "/api/activity", token=None)[0] == 401
    assert srv.req("GET", "/api/activity", host="evil.example:1")[0] == 403


def test_compose_script_is_static_and_safe(udir: Path, srv_factory: Any) -> None:
    on = srv_factory(allow_send=True)
    off = srv_factory()
    _, _, js_on = on.req("GET", "/app.js", token=None)
    _, _, js_off = off.req("GET", "/app.js", token=None)
    assert js_on == js_off  # the script is the same; can_send decides at runtime
    assert b"can_send" in js_on and b"/api/send" in js_on and b"application/json" in js_on
    assert b"innerHTML" not in js_on and b"<form" not in js_on and b"insertAdjacentHTML" not in js_on
    assert js_on.count(b"oneLine(") >= 5 and js_on.count(b"block(") >= 2


# ---------------------------------------------------------------- CLI


def run_cli(udir: Path, tmp_path: Path, *extra: str) -> tuple[subprocess.Popen[str], str]:
    env = dict(os.environ, PHONING_HOME=str(tmp_path / "home"), PYTHONPATH=str(ROOT))
    p = subprocess.Popen([sys.executable, "-c", "import sys,phoning.web as w; sys.exit(w.main(sys.argv[1:]))",
                          "alice", "--port", "0", *extra], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    assert p.stdout is not None
    return p, p.stdout.readline().strip()


def test_cli_flag_prints_notice_and_enables_sending(udir: Path, tmp_path: Path) -> None:
    p, line = run_cli(udir, tmp_path, "--allow-send")
    try:
        assert line.startswith("http://127.0.0.1:") and "/#token=" in line
        token = line.split("#token=", 1)[1]
        port = int(line.split(":")[2].split("/")[0])
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/summary", headers={"Authorization": f"Bearer {token}"})
        assert json.loads(c.getresponse().read())["can_send"] is True
        c.close()
    finally:
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=10)
    assert "sending enabled: POST /api/send as alice (session web)" in err
    time.sleep(0.1)
    assert not (udir / "web.token").exists()


def test_cli_default_is_read_only(udir: Path, tmp_path: Path) -> None:
    p, line = run_cli(udir, tmp_path)
    try:
        token = line.split("#token=", 1)[1]
        port = int(line.split(":")[2].split("/")[0])
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/summary", headers={"Authorization": f"Bearer {token}"})
        assert json.loads(c.getresponse().read())["can_send"] is False
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/send", body=b"{}", headers={
            "Authorization": f"Bearer {token}", "Origin": f"http://127.0.0.1:{port}",
            "Content-Type": "application/json"})
        assert c.getresponse().status == 405
        c.close()
    finally:
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=10)
    assert "sending enabled" not in err


def test_parser_has_allow_send() -> None:
    parser = web.build_parser()
    assert parser.parse_args(["alice"]).allow_send is False
    assert parser.parse_args(["alice", "--allow-send"]).allow_send is True


# ---------------------------------------------------------------- review regressions


def test_board_view_reports_declared_sessions(udir: Path, srv_factory: Any) -> None:
    r = populate(udir)
    view = srv_factory().api("/api/board")["view"]
    ask = next(a for a in view["open_asks"] if a["eid"] == r["ask1"]["env"]["eid"])
    assert ask["session"] == "vega"
    assert view["receipts"][0]["session"] == "vega"


def test_approval_ids_are_scrubbed_too(udir: Path, srv_factory: Any) -> None:
    populate(udir)
    lines = [{"id": FP, "peer": "a"}, {"id": "sk-abcdefghijklmnopqrstuvwx", "peer": "b"},
             {"id": "x\x1b[31my", "peer": "c"}, {"peer": "d"}]
    (udir / "doorbell-approvals.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    raw = srv_factory().req("GET", "/api/activity")[2]
    assert FP.encode() not in raw and b"sk-abcdefghij" not in raw and b"\\u001b" not in raw
    pending = json.loads(raw)["approvals"]["pending"]
    assert [p["peer"] for p in pending] == ["a", "b", "c", "d"]
    assert pending[0]["id"] == FP[:8] + ".."
    assert pending[3]["id"].startswith("line:")
    assert all(isinstance(p["id"], str) and p["id"] for p in pending)


def test_activity_strings_from_peers_are_scrubbed(udir: Path, srv_factory: Any) -> None:
    box = Mailbox(udir)
    inbound(box, "bob", FP, "chat", "hi", principal="sk-abcdefghijklmnopqrstuvwx",
            session="s\x1b[31mess\nion-sk-abcdefghijklmnopqrstuvwx", room="r\x07oom")
    inbound(box, "bob", FP, "ask", "question", principal="bob", session="ok", room="r\x07oom")
    raw = srv_factory().req("GET", "/api/activity")[2]
    assert b"sk-abcdefghij" not in raw
    for bad in (b"\\u001b", b"\\u0007", b"\\n"):
        assert bad not in raw, bad
    act = json.loads(raw)
    bob = next(s for s in act["senders"] if s["sender"] == "bob")
    assert {s["session"] for s in bob["sessions"]} == {"s[31mess ion-[redacted]", "ok"}
    assert act["open_asks"][0]["room"] == "room" and act["open_asks"][0]["subject"] == "question"


def test_handler_has_a_socket_timeout() -> None:
    timeout = web._Handler.timeout
    assert isinstance(timeout, (int, float)) and 0 < timeout <= 120


NODE_HARNESS = r"""
const calls = [];
const listeners = {};
function node() { return { textContent: '', hidden: false, children: [], appendChild(c) { this.children.push(c); } }; }
const nodes = {};
global.document = { hidden: HIDDEN, getElementById: (id) => (nodes[id] = nodes[id] || node()),
  createElement: () => node(), addEventListener: (n, f) => { listeners[n] = f; } };
global.window = {};
global.location = { search: '', hash: '', pathname: '/board' };
global.sessionStorage = { getItem: () => 'tok', setItem() {} };
global.history = { replaceState() {} };
global.fetch = (p) => { calls.push(p); return new Promise(() => {}); };
let ticks = 0;
global.setInterval = () => { ticks += 1; return 1; };
eval(APP_JS);
eval(BOARD_JS);
const initial = calls.length;
document.hidden = false;
if (listeners.visibilitychange) { listeners.visibilitychange(); }
console.log(JSON.stringify({ initial, afterVisible: calls.length, ticks }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_board_page_fetches_even_when_the_tab_starts_hidden(tmp_path: Path) -> None:
    script = (NODE_HARNESS.replace("HIDDEN", "true").replace("APP_JS", json.dumps(web.JS))
              .replace("BOARD_JS", json.dumps(web.BOARD_JS)))
    # app.js only runs the mailbox page when #places exists; here every id "exists", so strip that path
    script = script.replace(json.dumps(web.JS), json.dumps(web.JS.replace("if (document.getElementById('places'))", "if (false)")))
    f = tmp_path / "h.js"
    f.write_text(script)
    out = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["initial"] == 1  # first fetch is not gated on document.hidden
    assert got["afterVisible"] >= 2  # becoming visible refreshes at once
    assert got["ticks"] >= 1
