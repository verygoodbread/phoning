"""Read-only local viewer for a phoning mailbox (docs/SPEC-v0.2.md §3).

Serves a small single-page app plus a JSON API over loopback only. Every
response is gated on a bearer token, the ``Host`` header is checked against
the bound address, and only ``GET``/``HEAD`` are allowed by default. The
viewer reads ``log.jsonl`` directly, read-only, and never constructs a
:class:`Mailbox`, never touches cursors and never reads ``identity/``.

With ``--allow-send`` (docs/spec/web-activity.md §3) the viewer additionally
accepts ``POST /api/send`` as the local principal (session ``web``), behind a
token, exact-origin, content-type and rate-limit gate.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import webbrowser
from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from phoning import board as board_mod
from phoning import ops as ops_mod
from phoning import text as text_mod
from phoning.envelope import EnvelopeError
from phoning.retalk import RetalkError

__all__ = ["make_server", "main"]

EXIT_OK = 0
EXIT_USAGE = 2

ALLOWED_HOSTS = ("127.0.0.1", "::1", "localhost")

_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", _CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("X-Frame-Options", "DENY"),
)

_REDACTIONS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
)

_HEX_RUN: re.Pattern[str] = re.compile(r"[0-9a-fA-F]{20,}")
_SCRUB_MAX = 120
_APPROVAL_READ_LIMIT = 1024 * 1024
_APPROVAL_MAX = 100
_INTRODUCED_MAX = 100
_RESUME_MAX = 100
_OPEN_ASK_MAX = 100

_APPROVAL_KEYS: tuple[str, ...] = ("id", "ts", "peer", "session", "ring", "action", "status")

_SEND_MAX_BYTES = 65536
_SEND_MAX_BODY_BYTES = 20480
_SEND_RATE = 30
_SEND_WINDOW = 60.0
_SEND_FIELDS: tuple[str, ...] = ("to", "room", "type", "subject", "body", "reply_to", "data")

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>phoning</title>
<link rel="stylesheet" href="/app.css">
</head>
<body>
<header id="top">
  <h1>phoning</h1>
  <p id="who"></p>
  <p id="status">loading</p>
  <p><a href="/board">Board</a></p>
</header>
<main>
  <section>
    <h2>Rooms and peers</h2>
    <ul id="places"></ul>
  </section>
  <section>
    <h2 id="messages-title">Messages</h2>
    <ol id="messages"></ol>
  </section>
  <section>
    <h2>Threads</h2>
    <ul id="threads"></ul>
  </section>
  <section>
    <h2>Activity</h2>
    <div id="activity"></div>
  </section>
  <section id="compose" hidden></section>
  <section>
    <h2>Board</h2>
    <pre id="board"></pre>
  </section>
</main>
<script src="/app.js"></script>
</body>
</html>
"""

BOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>phoning board</title>
<link rel="stylesheet" href="/app.css">
</head>
<body>
<header id="top">
  <h1>phoning board</h1>
  <p><a href="/">Back to mailbox</a></p>
  <p id="status">loading</p>
</header>
<main id="board-page">
  <section>
    <h2>Who is doing what</h2>
    <ul id="board-status"></ul>
  </section>
  <section>
    <h2>Proposals</h2>
    <ul id="board-proposals"></ul>
  </section>
  <section>
    <h2>Open asks</h2>
    <ul id="board-asks"></ul>
  </section>
  <section>
    <h2>Receipts</h2>
    <ul id="board-receipts"></ul>
  </section>
</main>
<script src="/app.js"></script>
<script src="/board.js"></script>
</body>
</html>
"""

CSS = """body { font: 14px/1.45 system-ui, sans-serif; margin: 0 auto; max-width: 60rem; padding: 1rem; }
h1 { font-size: 1.3rem; margin: 0 0 .25rem; }
h2 { font-size: 1rem; margin: 1.2rem 0 .4rem; }
#status { color: #666; margin: 0 0 1rem; }
ul, ol { list-style: none; margin: 0; padding: 0; }
li { border-top: 1px solid #ddd; padding: .4rem 0; }
li > button { background: none; border: 0; cursor: pointer; font: inherit; padding: 0; text-align: left; }
.meta { color: #666; font-size: .85em; }
pre { background: #f6f6f6; overflow-x: auto; padding: .6rem; white-space: pre-wrap; }
"""

JS = """'use strict';
(function () {
  var TOKEN_KEY = 'phoning-token';
  var token = sessionStorage.getItem(TOKEN_KEY) || '';
  var hash = location.hash || '';
  var marker = '#token=';
  if (hash.indexOf(marker) === 0) {
    token = decodeURIComponent(hash.slice(marker.length));
    sessionStorage.setItem(TOKEN_KEY, token);
    history.replaceState(null, '', location.pathname + location.search);
  }
  function oneLine(s) {
    s = String(s === undefined || s === null ? '' : s);
    s = s.replace(/\\r\\n|\\r|\\n/g, ' ');
    s = s.replace(/\\t/g, ' ');
    s = s.replace(/[\\x00-\\x1F\\x7F-\\x9F]/g, '');
    return s;
  }
  function block(s) {
    s = String(s === undefined || s === null ? '' : s);
    s = s.replace(/\\r\\n|\\r/g, '\\n');
    s = s.replace(/\\t/g, '    ');
    s = s.replace(/[\\x00-\\x08\\x0B-\\x1F\\x7F-\\x9F]/g, '');
    return s;
  }
  function el(tag, text) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }
  function api(path) {
    return fetch(path, { headers: { Authorization: 'Bearer ' + token } }).then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      return r.json();
    });
  }
  function getToken() { return token; }
  window.phoningUI = { oneLine: oneLine, block: block, el: el, api: api, getToken: getToken };
  function setStatus(text) { document.getElementById('status').textContent = oneLine(text); }
  function renderPlaces(places) {
    var list = document.getElementById('places');
    list.textContent = '';
    places.forEach(function (p) {
      var li = el('li');
      var label = (p.room === null ? 'direct ' + p.peer : 'room ' + p.room);
      var btn = el('button', oneLine(label + ' (' + p.count + ')'));
      btn.addEventListener('click', function () { state.place = p; refresh(); });
      li.appendChild(btn);
      li.appendChild(el('div', oneLine('last ' + p.last_ts)));
      list.appendChild(li);
    });
  }
  function renderMessages(records) {
    var list = document.getElementById('messages');
    list.textContent = '';
    records.forEach(function (r) {
      var env = r.env || {};
      var frm = env.from || {};
      var li = el('li');
      var who = oneLine((r.sender || '?') + '/' + (frm.session || '?') +
        (r.claimed ? ' (claims ' + r.claimed + ')' : ''));
      var head = oneLine('#' + r.seq + ' ' + (env.ts || r.recv_ts || '') + ' ' + who +
        ' [' + (r.verified_fp8 ? 'verified ' + r.verified_fp8 : 'unverified') + '] ' +
        (env.type || '?') + ': ' + (env.subject || ''));
      li.appendChild(el('div', head));
      var body = el('pre', block(env.body || ''));
      body.style.whiteSpace = 'pre-wrap';
      li.appendChild(body);
      list.appendChild(li);
    });
  }
  function renderThreads(threads) {
    var list = document.getElementById('threads');
    list.textContent = '';
    threads.forEach(function (t) {
      var li = el('li');
      var btn = el('button', oneLine(t.subject + ' (' + t.count + ')'));
      btn.addEventListener('click', function () {
        api('/api/thread?eid=' + encodeURIComponent(t.thread)).then(renderMessages);
      });
      li.appendChild(btn);
      li.appendChild(el('div', oneLine('last ' + t.last_ts + ' ' +
        t.participants.map(function (x) { return oneLine(x); }).join(', '))));
      list.appendChild(li);
    });
  }
  function renderBoard(text) {
    document.getElementById('board').textContent = block(text);
  }
  var composeRoot = null;
  var composeNodes = null;
  function composeDestination() {
    if (!composeNodes) { return null; }
    var useRoom = composeNodes.dest.value === 'room';
    var value = composeNodes.target.value;
    if (!value) { return null; }
    return useRoom ? { room: value } : { to: value };
  }
  function fillDestination(ask) {
    if (!composeNodes) { return; }
    var place = state.place;
    if (ask.room) {
      composeNodes.dest.value = 'room';
      composeNodes.target.value = ask.room;
      return;
    }
    if (ask.peer) {
      composeNodes.dest.value = 'to';
      composeNodes.target.value = ask.peer;
      return;
    }
    if (place) {
      if (place.room === null) {
        composeNodes.dest.value = 'to';
        composeNodes.target.value = place.peer;
      } else {
        composeNodes.dest.value = 'room';
        composeNodes.target.value = place.room;
      }
    }
  }
  function composeStatus(text) {
    if (composeNodes) { composeNodes.status.textContent = oneLine(text); }
  }
  function doSend() {
    var dest = composeDestination();
    if (dest === null) { composeStatus('error: choose a destination'); return; }
    composeStatus('sending');
    var reply = composeNodes.reply.value;
    var payload = {
      type: composeNodes.type.value,
      subject: composeNodes.subject.value,
      body: composeNodes.body.value
    };
    if (dest.room !== undefined) { payload.room = dest.room; } else { payload.to = dest.to; }
    if (reply) { payload.reply_to = reply; }
    var raw = composeNodes.data.value.trim();
    if (raw) {
      try { payload.data = JSON.parse(raw); } catch (err) {
        composeStatus('error: data is not JSON');
        return;
      }
    }
    fetch('/api/send', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (res && res.ok) {
        composeStatus('sent ' + oneLine(String(res.eid || '')).slice(0, 8));
        composeNodes.subject.value = '';
        composeNodes.body.value = '';
        refresh();
      } else {
        composeStatus('error: ' + oneLine((res && res.error) || 'send failed'));
      }
    }).catch(function (err) { composeStatus('error: ' + oneLine(err.message)); });
  }
  function buildCompose() {
    var root = document.getElementById('compose');
    if (!root) { return; }
    root.textContent = '';
    var types = ['chat', 'status', 'ask', 'propose', 'accept', 'reject', 'receipt', 'ack'];
    var typeSel = el('select');
    types.forEach(function (t) {
      var opt = el('option', oneLine(t));
      opt.value = t;
      typeSel.appendChild(opt);
    });
    var dest = el('select');
    var toOpt = el('option', 'to (direct)');
    toOpt.value = 'to';
    dest.appendChild(toOpt);
    var roomOpt = el('option', 'room');
    roomOpt.value = 'room';
    dest.appendChild(roomOpt);
    var target = el('input');
    target.type = 'text';
    target.placeholder = 'peer or room';
    var place = state.place;
    if (place) {
      if (place.room === null) {
        dest.value = 'to';
        target.value = place.peer || '';
      } else {
        dest.value = 'room';
        target.value = place.room || '';
      }
    }
    var subject = el('input');
    subject.type = 'text';
    subject.maxLength = 80;
    subject.placeholder = 'subject';
    var bodyArea = el('textarea');
    bodyArea.placeholder = 'body';
    var reply = el('input');
    reply.type = 'text';
    reply.placeholder = 'reply_to (optional)';
    var dataArea = el('textarea');
    dataArea.placeholder = 'data JSON (optional)';
    var send = el('button', 'Send');
    send.addEventListener('click', doSend);
    var status = el('p');
    composeNodes = { dest: dest, target: target, type: typeSel, subject: subject,
      body: bodyArea, reply: reply, data: dataArea, status: status };
    [
      el('h3', 'Send'),
      el('p', 'type'), typeSel,
      el('p', 'destination'), dest, target,
      el('p', 'subject'), subject,
      el('p', 'body'), bodyArea,
      el('p', 'reply_to'), reply,
      el('p', 'data'), dataArea,
      send, status
    ].forEach(function (node) { root.appendChild(node); });
    root.hidden = false;
  }
  function renderActivity(act) {
    var root = document.getElementById('activity');
    root.textContent = '';
    var canSend = composeRoot !== null;
    root.appendChild(el('h3', 'Senders'));
    var senders = el('ul');
    (act.senders || []).forEach(function (s) {
      var li = el('li');
      var head = oneLine(s.sender + (s.self ? ' (you)' : '') + ' ' +
        (s.verified_fp8 ? 'verified ' + s.verified_fp8 : 'unverified') +
        ' last ' + s.last_seen);
      li.appendChild(el('div', head));
      var subs = el('ul');
      (s.sessions || []).forEach(function (se) {
        var parts = [];
        Object.keys(se.counts || {}).sort().forEach(function (k) {
          parts.push(k + ' ' + se.counts[k]);
        });
        var line = se.session + ': ' + parts.join(', ') +
          ' (total ' + se.total + ', open asks ' + se.open_asks + ')';
        if (se.claimed && se.claimed.length) {
          line = line + ' [claims ' + se.claimed.join(', ') + ']';
        }
        subs.appendChild(el('li', oneLine(line)));
      });
      li.appendChild(subs);
      senders.appendChild(li);
    });
    root.appendChild(senders);
    root.appendChild(el('h3', 'Open asks for you'));
    var asks = el('ul');
    var list = act.open_asks || [];
    if (!list.length) { asks.appendChild(el('li', 'none')); }
    list.forEach(function (a) {
      var li = el('li');
      var text = a.subject + ' by ' + a.sender + '/' + a.session +
        ' (' + (a.room === null ? 'direct' : 'room ' + a.room) + ') ' + a.ts;
      li.appendChild(el('div', oneLine(text)));
      if (canSend) {
        var replyBtn = el('button', 'Reply');
        replyBtn.addEventListener('click', function () {
          if (composeNodes) {
            composeNodes.reply.value = a.eid;
            fillDestination(a);
            composeStatus('reply to ' + a.eid.slice(0, 8));
          }
        });
        li.appendChild(replyBtn);
      }
      asks.appendChild(li);
    });
    root.appendChild(asks);
    root.appendChild(el('h3', 'Doorbell'));
    var d = act.doorbell || {};
    if (!d.present) {
      root.appendChild(el('p', 'doorbell: no state'));
    } else {
      var res = d.resumes_total === null ? '?' : String(d.resumes_total);
      root.appendChild(el('p', oneLine('last ring ' + (d.last_ring_iso || 'never') +
        ', pending ' + d.pending + ', introduced ' + d.introduced_count +
        ', resumes ' + res)));
    }
    root.appendChild(el('h3', 'Approvals'));
    var ap = act.approvals || {};
    if (!ap.present || !(ap.pending || []).length) {
      root.appendChild(el('p', 'approvals: none'));
    } else {
      var alist = el('ul');
      ap.pending.forEach(function (p) {
        alist.appendChild(el('li', oneLine((p.id || '?') + ' ' + (p.peer || '?') + ' ' +
          (p.action || '?') + ' ' + (p.status || 'pending'))));
      });
      root.appendChild(alist);
    }
  }
  var state = { place: null };
  function refresh() {
    var place = state.place;
    var path = place === null ? '/api/messages?limit=200'
      : (place.room === null ? '/api/messages?peer=' + encodeURIComponent(place.peer)
        : '/api/messages?room=' + encodeURIComponent(place.room));
    var title = place === null ? 'Messages'
      : (place.room === null ? 'Messages with ' + place.peer : 'Messages in ' + place.room);
    document.getElementById('messages-title').textContent = oneLine(title);
    Promise.all([api('/api/summary'), api('/api/rooms'), api('/api/messages?limit=200'),
                 api('/api/threads'), api('/api/board'), api(path), api('/api/activity')]).then(function (res) {
      var summary = res[0];
      document.getElementById('who').textContent =
        oneLine(summary.name + ' - ' + summary.records + ' records');
      renderPlaces(res[1]);
      if (summary.can_send && composeRoot === null) {
        composeRoot = document.getElementById('compose');
        if (composeRoot) { buildCompose(); }
      }
      renderMessages(res[5]);
      renderThreads(res[3]);
      renderBoard(res[4].text);
      renderActivity(res[6]);
      setStatus('updated ' + new Date().toLocaleTimeString());
    }).catch(function (err) { setStatus('error: ' + err.message); });
  }
  if (document.getElementById('places')) {
    refresh();
    setInterval(refresh, 5000);
  }
})();
"""

BOARD_JS = """'use strict';
(function () {
  if (!document.getElementById('board-page')) { return; }
  var UI = window.phoningUI;
  var oneLine = UI.oneLine;
  var el = UI.el;
  var api = UI.api;
  var params = new URLSearchParams(location.search);
  var room = params.get('room');
  var boardPath = room ? '/api/board?room=' + encodeURIComponent(room) : '/api/board';
  function setStatus(text) { document.getElementById('status').textContent = oneLine(text); }
  function section(id, items, render) {
    var list = document.getElementById(id);
    list.textContent = '';
    if (!items || !items.length) { list.appendChild(el('li', 'none')); return; }
    items.forEach(function (item) { list.appendChild(render(item)); });
  }
  function renderStatus(s) {
    return el('li', oneLine(s.principal + '/' + s.session + ' ' + s.state + ': ' +
      s.subject + ' (' + s.ts + ')'));
  }
  function renderProposal(p) {
    var li = el('li');
    var owners = (p.owners || []).join('/') || '(unowned)';
    li.appendChild(el('div', oneLine('[' + p.state + '] ' + p.subject +
      ' scope=' + p.scope + ' owner=' + owners +
      ' by ' + p.by.principal + '/' + p.by.session)));
    var decs = el('ul');
    (p.decisions || []).forEach(function (d) {
      var tag = d.effective ? 'effective' : ('NOT EFFECTIVE (' + (d.reason || '') + ')');
      decs.appendChild(el('li', oneLine(d.type + ' by ' + d.by.principal + '/' +
        d.by.session + ': ' + tag)));
    });
    li.appendChild(decs);
    return li;
  }
  function renderAsk(a) {
    return el('li', oneLine(a.subject + ' by ' + a.sender + '/' + a.session +
      ' (' + (a.room === null ? 'direct' : 'room ' + a.room) + ') ' + a.ts));
  }
  function renderReceipt(r) {
    var refs = (r.refs || []).join(', ');
    return el('li', oneLine(r.subject + ' <- ' + r.sender + '/' + r.session +
      ' refs=' + refs + ' (' + r.ts + ')'));
  }
  function refresh() {
    api(boardPath).then(function (data) {
      var v = data.view || {};
      section('board-status', v.status, renderStatus);
      section('board-proposals', v.proposals, renderProposal);
      section('board-asks', v.open_asks, renderAsk);
      section('board-receipts', v.receipts, renderReceipt);
      setStatus('updated ' + new Date().toLocaleTimeString());
    }).catch(function (err) { setStatus('error: ' + err.message); });
  }
  function tick() {
    if (!document.hidden) { refresh(); }
  }
  refresh();
  setInterval(tick, 5000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { refresh(); } });
})();
"""


def _redact(text: str) -> str:
    """Replace secret-looking substrings in ``text`` with ``[redacted]``."""
    for pat in _REDACTIONS:
        text = pat.sub("[redacted]", text)
    return text


def _scrub(text: str, *, hex_short: bool = False) -> str:
    """Clean, redact and cap ``text`` for display (optionally hex-shortened)."""
    out = text_mod.clean_line(text)
    out = _redact(out)
    if hex_short:
        out = _HEX_RUN.sub(lambda m: m.group(0)[:8] + "..", out)
    return out[:_SCRUB_MAX]


def read_records(user_dir: str) -> list[dict[str, Any]]:
    """Read ``<user_dir>/mailbox/log.jsonl`` read-only, skipping bad lines."""
    log_path = os.path.join(user_dir, "mailbox", "log.jsonl")
    out: list[dict[str, Any]] = []
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def _sanitized(rec: dict[str, Any], local: str) -> dict[str, Any]:
    """Sanitize ``rec`` via :func:`ops.sanitize`, then redact secrets."""
    out = ops_mod.sanitize(rec, local=local)
    env_any = out.get("env")
    if isinstance(env_any, dict):
        subject_any = env_any.get("subject")
        if isinstance(subject_any, str):
            env_any["subject"] = _redact(subject_any)
        body_any = env_any.get("body")
        if isinstance(body_any, str):
            env_any["body"] = _redact(body_any)
    return out


def _effective_room(rec: dict[str, Any]) -> Any:
    """Room for filtering: top-level ``room`` else ``env.room``."""
    top = rec.get("room")
    if top is not None:
        return top
    env = rec.get("env")
    if isinstance(env, dict):
        return env.get("room")
    return None


def _env_of(rec: dict[str, Any]) -> dict[str, Any]:
    """Envelope mapping of ``rec`` (empty when absent or malformed)."""
    env = rec.get("env")
    return env if isinstance(env, dict) else {}


def _ts_of(rec: dict[str, Any]) -> str:
    """Best-effort timestamp for a record."""
    env = _env_of(rec)
    ts = env.get("ts")
    if isinstance(ts, str) and ts:
        return ts
    recv = rec.get("recv_ts")
    return recv if isinstance(recv, str) else ""


def _seq_of(rec: dict[str, Any]) -> int:
    """Sequence number of ``rec`` (0 when absent or malformed)."""
    seq = rec.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return 0
    return seq


def _json_safe(value: Any) -> Any:
    """Recursively convert ``value`` to a JSON-serializable structure."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            if isinstance(key, tuple):
                name = "/".join(str(part) for part in key)
            elif isinstance(key, str):
                name = key
            else:
                name = str(key)
            out[name] = _json_safe(val)
        return out
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _rooms_view(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group records into rooms (or direct conversations by peer)."""
    groups: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for rec in records:
        room_any = _effective_room(rec)
        room = room_any if isinstance(room_any, str) and room_any else None
        peer_any = rec.get("peer")
        peer = peer_any if isinstance(peer_any, str) and peer_any else None
        key = (room, None) if room is not None else (None, peer)
        entry = groups.get(key)
        ts = _ts_of(rec)
        if entry is None:
            groups[key] = {"room": room, "peer": None if room is not None else peer,
                           "count": 1, "last_ts": ts}
        else:
            entry["count"] = int(entry["count"]) + 1
            if ts and (not entry["last_ts"] or ts > str(entry["last_ts"])):
                entry["last_ts"] = ts
    return [groups[key] for key in groups]


def _threads_view(records: list[dict[str, Any]], local: str) -> list[dict[str, Any]]:
    """Group records by thread key (``env.thread`` else ``env.eid``).

    Participants are ``sender`` values: the local name for outbound
    records and the transport peer for inbound records.
    """
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for rec in records:
        env = _env_of(rec)
        thread_any = env.get("thread")
        if isinstance(thread_any, str) and thread_any:
            key = thread_any
        else:
            eid_any = env.get("eid")
            key = eid_any if isinstance(eid_any, str) and eid_any else ""
        if not key:
            continue
        subject_any = env.get("subject")
        subject = subject_any if isinstance(subject_any, str) else ""
        if rec.get("direction") == "out":
            sender = local
        else:
            peer_any: Any = rec.get("peer")
            sender = peer_any if isinstance(peer_any, str) and peer_any else ""
        ts = _ts_of(rec)
        entry = groups.get(key)
        if entry is None:
            entry = {"thread": key, "subject": subject, "count": 0, "last_ts": "",
                     "participants": []}
            groups[key] = entry
            order.append(key)
        entry["count"] = int(entry["count"]) + 1
        if subject and not entry["subject"]:
            entry["subject"] = subject
        if ts and (not entry["last_ts"] or ts > str(entry["last_ts"])):
            entry["last_ts"] = ts
        participants = entry["participants"]
        if isinstance(participants, list) and sender and sender not in participants:
            participants.append(sender)
    return [groups[key] for key in order]


def _fetch_str(rec: dict[str, Any], key: str) -> str:
    """Non-empty string ``rec[key]`` else ``""``."""
    val: Any = rec.get(key)
    return val if isinstance(val, str) and val else ""


def _std_state(state: Any) -> str | None:
    """Return a valid status state string, else ``None``."""
    return state if isinstance(state, str) and state in ("ACTIVE", "IDLE", "BLOCKED") else None


def _status_view(folded: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the sorted ``status`` list of the structured board view.

    Accepts both the folded mapping form (``(principal, session) -> {...}``)
    and a list of status entries.
    """
    status_any: Any = folded.get("status", {})
    rows: list[tuple[str, str, str, str, str]] = []
    if isinstance(status_any, dict):
        for key, val in status_any.items():
            if not (isinstance(key, tuple) and len(key) == 2):
                continue
            pr, se = str(key[0]), str(key[1])
            if not isinstance(val, dict):
                continue
            state = _std_state(val.get("state"))
            if state is None:
                continue
            subject_any: Any = val.get("subject")
            ts_any: Any = val.get("ts")
            rows.append((pr, se, state,
                         subject_any if isinstance(subject_any, str) else "",
                         ts_any if isinstance(ts_any, str) else ""))
    elif isinstance(status_any, list):
        for val in status_any:
            if not isinstance(val, dict):
                continue
            state = _std_state(val.get("state"))
            if state is None:
                continue
            pr_any: Any = val.get("principal")
            se_any: Any = val.get("session")
            subj_any: Any = val.get("subject")
            ts_any2: Any = val.get("ts")
            rows.append((str(pr_any) if isinstance(pr_any, str) else "?",
                         str(se_any) if isinstance(se_any, str) else "?",
                         state,
                         subj_any if isinstance(subj_any, str) else "",
                         ts_any2 if isinstance(ts_any2, str) else ""))
    out: list[dict[str, Any]] = []
    for pr, se, state, subject, ts in sorted(rows, key=lambda r: (r[0], r[1])):
        out.append({"principal": _scrub(pr), "session": _scrub(se),
                    "state": _scrub(state), "subject": _scrub(subject),
                    "ts": _scrub(ts)})
    return out


def _decision_view(dec: dict[str, Any]) -> dict[str, Any]:
    """Render one decision entry of the structured board view."""
    by_any: Any = dec.get("by")
    by: dict[str, str] = by_any if isinstance(by_any, dict) else {}
    return {
        "type": _scrub(str(dec.get("type", ""))),
        "by": {
            "principal": _scrub(str(by.get("principal", "?"))),
            "session": _scrub(str(by.get("session", "?"))),
        },
        "claimed": _scrub(str(dec.get("claimed", ""))),
        "effective": bool(dec.get("effective", False)),
        "reason": _scrub(str(dec.get("reason", ""))),
        "ts": _scrub(str(dec.get("ts", ""))),
    }


def _proposal_view(prop: dict[str, Any]) -> dict[str, Any]:
    """Render one proposal entry of the structured board view."""
    by_any: Any = prop.get("by")
    by: dict[str, str] = by_any if isinstance(by_any, dict) else {}
    owners_any: Any = prop.get("owners", [])
    owners = [_scrub(str(o)) for o in owners_any] if isinstance(owners_any, list) else []
    decisions_any: Any = prop.get("decisions", [])
    decisions = [_decision_view(d) for d in decisions_any
                 if isinstance(d, dict)] if isinstance(decisions_any, list) else []
    return {
        "eid": str(prop.get("eid", "")),
        "scope": _scrub(str(prop.get("scope", ""))),
        "subject": _scrub(str(prop.get("subject", ""))),
        "state": _scrub(str(prop.get("state", "OPEN"))),
        "owners": owners,
        "by": {
            "principal": _scrub(str(by.get("principal", "?"))),
            "session": _scrub(str(by.get("session", "?"))),
        },
        "claimed": _scrub(str(prop.get("claimed", ""))),
        "decisions": decisions,
    }


def _board_names(records: list[dict[str, Any]], name: str) -> dict[str, str]:
    """Map ``env.eid`` -> transport-bound sender for every record."""
    out: dict[str, str] = {}
    for rec in records:
        env = _env_of(rec)
        eid_any = env.get("eid")
        if not isinstance(eid_any, str) or not eid_any:
            continue
        out[eid_any] = name if rec.get("direction") == "out" else _fetch_str(rec, "peer")
    return out


def _sender_for(senders: dict[str, str], env: dict[str, Any]) -> str:
    """Transport-bound sender for the record with this ``env.eid``."""
    eid_any = env.get("eid")
    if isinstance(eid_any, str) and eid_any in senders:
        return senders[eid_any]
    return "?"


def _board_structured(folded: dict[str, Any], records: list[dict[str, Any]],
                      name: str) -> dict[str, Any]:
    """Build the ``view`` payload of ``/api/board`` from a folded board."""
    senders = _board_names(records, name)
    status = _status_view(folded)
    props_any: Any = folded.get("proposals", [])
    proposals = [_proposal_view(p) for p in props_any
                 if isinstance(p, dict)] if isinstance(props_any, list) else []
    open_any: Any = folded.get("open_asks", folded.get("asks", []))
    open_asks: list[dict[str, Any]] = []
    if isinstance(open_any, list):
        for a in open_any:
            if not isinstance(a, dict):
                continue
            room_any: Any = a.get("room")
            room = _scrub(room_any) if isinstance(room_any, str) and room_any else None
            open_asks.append({
                "eid": str(a.get("eid", "")),
                "sender": _scrub(_sender_for(senders, a)),
                "session": _scrub(_session_of(a)),
                "subject": _scrub(str(a.get("subject", ""))),
                "ts": _scrub(str(a.get("ts", ""))),
                "room": room,
            })
    rec_any: Any = folded.get("receipts", [])
    receipts: list[dict[str, Any]] = []
    if isinstance(rec_any, list):
        for r in rec_any:
            if not isinstance(r, dict):
                continue
            refs_any: Any = r.get("refs", [])
            refs = [_scrub(str(x)) for x in refs_any] if isinstance(refs_any, list) else []
            receipts.append({
                "eid": str(r.get("eid", "")),
                "sender": _scrub(_sender_for(senders, r)),
                "session": _scrub(_session_of(r)),
                "subject": _scrub(str(r.get("subject", ""))),
                "refs": refs,
                "ts": _scrub(str(r.get("ts", ""))),
            })
    return {"status": status, "proposals": proposals, "open_asks": open_asks,
            "receipts": receipts}


def _board_view(records: list[dict[str, Any]], owners: dict[str, Any],
                name: str, room: str | None) -> dict[str, Any]:
    """Fold and render the board, returning JSON-safe ``text``, ``board`` and ``view``."""
    subset = records
    if room is not None:
        subset = [r for r in records if _effective_room(r) == room]
    folded = board_mod.fold(subset, owners, local=name)
    text = board_mod.render(folded)
    view = _board_structured(folded, subset, name)
    return {"text": text, "board": _json_safe(folded), "view": view}


def _session_of(env: dict[str, Any]) -> str:
    """Declared session of an envelope (``"?"`` when absent or malformed)."""
    frm_any: Any = env.get("from")
    if not isinstance(frm_any, dict):
        return "?"
    se_any = frm_any.get("session")
    return se_any if isinstance(se_any, str) and se_any else "?"


def _principal_of(env: dict[str, Any]) -> str | None:
    """Declared principal of an envelope (``None`` when absent or malformed)."""
    frm_any: Any = env.get("from")
    if not isinstance(frm_any, dict):
        return None
    pr_any = frm_any.get("principal")
    return pr_any if isinstance(pr_any, str) and pr_any else None


def _ask_views(records: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Inbound, unanswered ``ask`` records, oldest first, newest 100 kept."""
    replied_to: set[str] = set()
    for rec in records:
        env = _env_of(rec)
        rt_any: Any = env.get("reply_to")
        if isinstance(rt_any, str) and rt_any:
            replied_to.add(rt_any)
    out: list[dict[str, Any]] = []
    for rec in records:
        if rec.get("direction") == "out":
            continue
        env = _env_of(rec)
        if not env:
            continue
        if env.get("type") != "ask":
            continue
        eid_any: Any = env.get("eid")
        if not isinstance(eid_any, str) or not eid_any or eid_any in replied_to:
            continue
        peer_any: Any = rec.get("peer")
        peer_raw = peer_any if isinstance(peer_any, str) and peer_any else "?"
        room_any: Any = _effective_room(rec)
        room_val = _scrub(room_any) if isinstance(room_any, str) and room_any else None
        out.append({
            "eid": eid_any,
            "sender": _scrub(peer_raw),
            "session": _scrub(_session_of(env)),
            "peer": _scrub(peer_raw),
            "room": room_val,
            "subject": _scrub(str(env.get("subject", ""))),
            "ts": _scrub(_ts_of(rec)),
        })
    return out[-_OPEN_ASK_MAX:]


def _activity_view(records: list[dict[str, Any]], name: str,
                   user_dir: str) -> dict[str, Any]:
    """Fold records into the activity view (senders, sessions, asks, doorbell)."""
    groups: dict[str, dict[str, Any]] = {}
    for rec in records:
        env_any: Any = rec.get("env")
        if not isinstance(env_any, dict):
            continue
        san = ops_mod.sanitize(rec, local=name)
        sender_any: Any = san.get("sender")
        sender_raw = sender_any if isinstance(sender_any, str) and sender_any else "?"
        sender = _scrub(sender_raw)
        session = _scrub(_session_of(env_any))
        ts = _scrub(_ts_of(rec))
        entry = groups.get(sender)
        if entry is None:
            entry = {"sender": sender, "self": rec.get("direction") == "out",
                     "verified_fp8": None, "last_seen": ts, "sessions": {}}
            groups[sender] = entry
        se = entry["sessions"].get(session)
        if se is None:
            se = {"session": session, "last_seen": ts, "counts": {}, "claimed": set()}
            entry["sessions"][session] = se
        if ts and (not se["last_seen"] or ts > se["last_seen"]):
            se["last_seen"] = ts
        if ts and (not entry["last_seen"] or ts > entry["last_seen"]):
            entry["last_seen"] = ts
        typ_any: Any = env_any.get("type")
        typ = typ_any if isinstance(typ_any, str) and typ_any else "?"
        counts = se["counts"]
        counts[typ] = int(counts.get(typ, 0)) + 1
        if rec.get("direction") != "out":
            fp_any2: Any = rec.get("peer_fp")
            if isinstance(fp_any2, str) and fp_any2:
                entry["verified_fp8"] = fp_any2[:8]
            claimed_any = _principal_of(env_any)
            if claimed_any is not None:
                claimed = _scrub(claimed_any)
                if claimed != sender:
                    se["claimed"].add(claimed)
    asks = _ask_views(records, name)
    per_session: dict[tuple[str, str], int] = {}
    for a in asks:
        key = (a["sender"], a["session"])
        per_session[key] = per_session.get(key, 0) + 1
    senders: list[dict[str, Any]] = []
    for sender_key in sorted(groups):
        entry = groups[sender_key]
        sessions_out: list[dict[str, Any]] = []
        sessions = entry["sessions"]
        for se_key in sorted(sessions):
            se = sessions[se_key]
            counts = se["counts"]
            total = sum(int(counts[k]) for k in counts)
            sessions_out.append({
                "session": se["session"],
                "last_seen": se["last_seen"],
                "total": total,
                "counts": {k: int(counts[k]) for k in sorted(counts)},
                "open_asks": per_session.get((entry["sender"], se["session"]), 0),
                "claimed": sorted(se["claimed"]),
            })
        senders.append({
            "sender": entry["sender"],
            "self": bool(entry["self"]),
            "verified_fp8": entry["verified_fp8"],
            "last_seen": entry["last_seen"],
            "sessions": sessions_out,
        })
    return {"name": name, "senders": senders,
            "open_asks": list(asks),
            "doorbell": _doorbell_view(user_dir),
            "approvals": _approvals_view(user_dir)}


def _missing_doorbell() -> dict[str, Any]:
    """The doorbell view for a missing or unusable state file."""
    return {"present": False, "last_ring": None, "last_ring_iso": None, "pending": False,
            "introduced": [], "introduced_count": 0, "resume_counts": None,
            "resumes_total": None}


def _iso_of(seconds: float) -> str | None:
    """UTC ISO-8601 ``...Z`` form of a unix timestamp, or ``None``."""
    try:
        return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _resume_view(raw: dict[str, Any]) -> tuple[dict[str, int] | None, int | None]:
    """Extract ``(resume_counts, resumes_total)`` from a doorbell state."""
    counts = raw.get("resume_counts")
    resumes = raw.get("resumes")
    if isinstance(counts, dict):
        out: dict[str, int] = {}
        for key, val in counts.items():
            if isinstance(key, str) and isinstance(val, int) and not isinstance(val, bool) \
                    and val >= 0 and len(out) < _RESUME_MAX:
                out[_scrub(key, hex_short=True)] = val
        total = sum(out.values()) if out else 0
        return out, total
    if isinstance(resumes, dict):
        out2: dict[str, int] = {}
        for key, val in resumes.items():
            if isinstance(key, str) and isinstance(val, int) and not isinstance(val, bool) \
                    and val >= 0 and len(out2) < _RESUME_MAX:
                out2[_scrub(key, hex_short=True)] = val
        total2 = sum(out2.values()) if out2 else 0
        return out2, total2
    if isinstance(resumes, int) and not isinstance(resumes, bool) and resumes >= 0:
        return None, resumes
    return None, None


def _doorbell_view(user_dir: str) -> dict[str, Any]:
    """Read-only summary of ``<user_dir>/doorbell-state.json``."""
    path = os.path.join(user_dir, "doorbell-state.json")
    try:
        with open(path, encoding="utf-8") as fh:
            raw: Any = json.load(fh)
    except (OSError, ValueError):
        return _missing_doorbell()
    if not isinstance(raw, dict):
        return _missing_doorbell()
    ring_any: Any = raw.get("last_ring")
    last_ring: float | None = None
    if isinstance(ring_any, (int, float)) and not isinstance(ring_any, bool) and ring_any > 0:
        last_ring = float(ring_any)
    last_ring_iso = _iso_of(last_ring) if last_ring is not None else None
    pending_any: Any = raw.get("pending")
    pending = bool(pending_any) if isinstance(pending_any, bool) else False
    introduced_raw: Any = raw.get("introduced", [])
    introduced: list[str] = []
    full_count = 0
    if isinstance(introduced_raw, list):
        full_count = sum(1 for item in introduced_raw if isinstance(item, str))
        for item in introduced_raw:
            if not isinstance(item, str):
                continue
            if len(introduced) >= _INTRODUCED_MAX:
                break
            introduced.append(_scrub(item, hex_short=True))
    resume_counts, resumes_total = _resume_view(raw)
    return {"present": True, "last_ring": last_ring, "last_ring_iso": last_ring_iso,
            "pending": pending, "introduced": introduced, "introduced_count": full_count,
            "resume_counts": resume_counts, "resumes_total": resumes_total}


def _read_approvals(path: str) -> bytes | None:
    """Read at most the last 1 MiB of ``path`` (``None`` when unreadable)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            offset = max(0, size - _APPROVAL_READ_LIMIT)
            fh.seek(offset)
            return fh.read()
    except OSError:
        return None


def _approval_entry(value: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Allowlisted view of one approvals entry (``None`` when not pending).

    ``key`` is the entry's own key (its string ``id`` else ``line:<n>``),
    emitted as ``id`` so callers can identify the entry.
    """
    status_any: Any = value.get("status")
    if status_any is not None and status_any != "pending":
        return None
    out: dict[str, Any] = {"id": _scrub(key, hex_short=True)}
    for field in _APPROVAL_KEYS:
        if field == "id" or field not in value:
            continue
        val: Any = value[field]
        if isinstance(val, str):
            out[field] = _scrub(val, hex_short=True)
        elif (
            isinstance(val, (int, float))
            and not isinstance(val, bool)
            and val == val
            and abs(val) != float("inf")
        ):
            out[field] = val
    if "status" not in out:
        out["status"] = "pending"
    return out


def _approvals_view(user_dir: str) -> dict[str, Any]:
    """Read-only summary of ``<user_dir>/doorbell-approvals.jsonl``."""
    path = os.path.join(user_dir, "doorbell-approvals.jsonl")
    data = _read_approvals(path)
    if data is None:
        return {"present": False, "pending_count": 0, "pending": []}
    entries: dict[str, dict[str, Any] | None] = {}
    order: list[str] = []
    for index, line in enumerate(data.decode("utf-8", "replace").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj: Any = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        id_any: Any = obj.get("id")
        key = id_any if isinstance(id_any, str) and id_any else f"line:{index}"
        view = _approval_entry(obj, key)
        if key not in entries:
            order.append(key)
        entries[key] = view
    pending: list[dict[str, Any]] = []
    for key in order:
        view = entries[key]
        if view is not None:
            pending.append(view)
    return {"present": True, "pending_count": len(pending),
            "pending": pending[-_APPROVAL_MAX:]}


def _media_type(header: str | None) -> str:
    """Media type (lowercase) of a ``Content-Type`` header value."""
    if not isinstance(header, str):
        return ""
    return header.split(";", 1)[0].strip().lower()


class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    """IPv6 loopback server used when binding to ``::1``."""

    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    """Request handler bound to one user directory, token and owner map."""

    server_version = "phoning-web"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30

    user_dir: str
    name: str
    token: str
    owners: dict[str, Any]
    allow_send: bool = False
    # Per-server limiter state (created on the bound subclass in make_server).
    sends: deque[float]
    send_lock: threading.Lock

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence request logging."""

    def _send(self, status: int, body: bytes, content_type: str,
              extra: tuple[tuple[str, str], ...] = ()) -> None:
        """Send one response with the mandatory security headers."""
        self.send_response(status)
        for key, value in SECURITY_HEADERS:
            self.send_header(key, value)
        for key, value in extra:
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

    def _send_json(self, status: int, obj: Any) -> None:
        """Send ``obj`` as JSON with the mandatory security headers."""
        payload = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, payload, "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        """True when the ``Host`` header matches the bound address."""
        port_any: Any = getattr(self.server, "server_port", 0)
        port = int(port_any) if isinstance(port_any, int) else 0
        host_any = self.headers.get("Host")
        if not isinstance(host_any, str):
            return False
        return host_any in (
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
        )

    def _authorized(self) -> bool:
        """True when the ``Authorization`` header carries the bearer token."""
        header_any = self.headers.get("Authorization")
        if not isinstance(header_any, str):
            return False
        prefix = "Bearer "
        if not header_any.startswith(prefix):
            return False
        return hmac.compare_digest(header_any[len(prefix):], self.token)

    def _records(self) -> list[dict[str, Any]]:
        """All records from the on-disk log (read-only)."""
        return read_records(self.user_dir)

    def do_HEAD(self) -> None:  # noqa: N802
        """Handle a HEAD request like GET, minus the body."""
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        """Route a GET request."""
        if not self._host_ok():
            self._send_json(403, {"error": "forbidden"})
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/board":
            self._send(200, BOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/app.css":
            self._send(200, CSS.encode("utf-8"), "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self._send(200, JS.encode("utf-8"), "application/javascript; charset=utf-8")
            return
        if path == "/board.js":
            self._send(200, BOARD_JS.encode("utf-8"),
                       "application/javascript; charset=utf-8")
            return
        if path.startswith("/api/"):
            self._api(path, query)
            return
        self._send_json(404, {"error": "not found"})

    def _api(self, path: str, query: dict[str, list[str]]) -> None:
        """Dispatch an authenticated ``/api/`` request."""
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if path == "/api/summary":
            self._api_summary()
            return
        if path == "/api/rooms":
            self._send_json(200, _rooms_view(self._records()))
            return
        if path == "/api/messages":
            self._api_messages(query)
            return
        if path == "/api/threads":
            self._send_json(200, _threads_view(self._records(), self.name))
            return
        if path == "/api/thread":
            self._api_thread(query)
            return
        if path == "/api/board":
            room = query.get("room", [None])[0]
            self._send_json(200, _board_view(self._records(), self.owners, self.name,
                                             room if isinstance(room, str) else None))
            return
        if path == "/api/activity":
            self._send_json(200, _activity_view(self._records(), self.name, self.user_dir))
            return
        self._send_json(404, {"error": "not found"})

    def _api_summary(self) -> None:
        """Return ``{"name", "records", "rooms", "peers", "can_send"}``."""
        records = self._records()
        rooms: list[str] = []
        peers: list[str] = []
        for rec in records:
            room_any = _effective_room(rec)
            if isinstance(room_any, str) and room_any and room_any not in rooms:
                rooms.append(room_any)
            peer_any = rec.get("peer")
            if isinstance(peer_any, str) and peer_any and peer_any not in peers:
                peers.append(peer_any)
        self._send_json(200, {
            "name": self.name,
            "records": len(records),
            "rooms": rooms,
            "peers": peers,
            "can_send": bool(self.allow_send),
        })

    def _api_messages(self, query: dict[str, list[str]]) -> None:
        """Return the newest-last sanitized records for a room, peer or all."""
        room = query.get("room", [None])[0]
        peer = query.get("peer", [None])[0]
        raw_limit = query.get("limit", ["200"])[0]
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 200
        if limit < 1:
            limit = 200
        if limit > 1000:
            limit = 1000
        records = self._records()
        if isinstance(room, str) and room:
            records = [r for r in records if _effective_room(r) == room]
        elif isinstance(peer, str) and peer:
            records = [r for r in records if r.get("peer") == peer]
        records = records[-limit:]
        self._send_json(200, [_sanitized(r, self.name) for r in records])

    def _api_thread(self, query: dict[str, list[str]]) -> None:
        """Return the sanitized records of one thread."""
        eid = query.get("eid", [""])[0]
        if not isinstance(eid, str) or not eid:
            self._send_json(200, [])
            return
        out: list[dict[str, Any]] = []
        for rec in self._records():
            env = _env_of(rec)
            thread_any = env.get("thread")
            key = thread_any if isinstance(thread_any, str) and thread_any else env.get("eid")
            if key == eid:
                out.append(_sanitized(rec, self.name))
        self._send_json(200, out)

    def _allow(self) -> str:
        """The ``Allow`` header value for the current server mode."""
        return "GET, HEAD, POST" if self.allow_send else "GET, HEAD"

    def _method_not_allowed(self, status: int = 405) -> None:
        """Reply 405 with the allowed methods for the current mode."""
        payload = b'{"error":"method not allowed"}'
        self._send(status, payload, "application/json; charset=utf-8",
                   (("Allow", self._allow()),))

    def do_POST(self) -> None:  # noqa: N802
        """Route a POST: only ``/api/send`` with sending enabled is accepted."""
        if not self._host_ok():
            self._send_json(403, {"error": "forbidden"})
            return
        if not self.allow_send:
            self._method_not_allowed()
            return
        if urlsplit(self.path).path != "/api/send":
            self._method_not_allowed()
            return
        self._send_message()

    def _gate_ok(self) -> bool:
        """Run the three 403 gates: token, bound origin and JSON content type."""
        if not self._authorized():
            return False
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not isinstance(origin, str) or not isinstance(host, str):
            return False
        if origin != f"http://{host}":
            return False
        return _media_type(self.headers.get("Content-Type")) == "application/json"

    def _rate_ok(self) -> tuple[bool, int]:
        """Sliding-window limiter over the last 60s of passed requests."""
        now = time.monotonic()
        with self.send_lock:
            window = self.sends
            while window and now - window[0] > _SEND_WINDOW:
                window.popleft()
            if len(window) >= _SEND_RATE:
                retry = int(_SEND_WINDOW - (now - window[0])) + 1
                return False, max(1, retry)
            window.append(now)
        return True, 0

    def _read_body(self) -> tuple[bytes | None, int | None]:
        """Read exactly ``Content-Length`` bytes.

        Returns ``(body, status)``: ``status`` is non-None on an error and
        ``body`` is None.
        """
        raw_len = self.headers.get("Content-Length")
        if not isinstance(raw_len, str):
            return None, 400
        try:
            length = int(raw_len)
        except ValueError:
            return None, 400
        if length < 0:
            return None, 400
        if length > _SEND_MAX_BYTES:
            return None, 413
        if length == 0:
            return b"", None
        return self.rfile.read(length), None

    def _send_status(self, status: int, error: str,
                     extra: tuple[tuple[str, str], ...] = ()) -> None:
        """Reply with a JSON error body."""
        payload = json.dumps({"error": error}, ensure_ascii=False).encode("utf-8")
        self._send(status, payload, "application/json; charset=utf-8", extra)

    def _send_message(self) -> None:
        """Handle an authenticated, gated ``POST /api/send``."""
        if not self._gate_ok():
            self._send_json(403, {"error": "forbidden"})
            return
        ok, retry = self._rate_ok()
        if not ok:
            payload = json.dumps({"error": "rate limited"}).encode("utf-8")
            self._send(429, payload, "application/json; charset=utf-8",
                       (("Retry-After", str(retry)),))
            return
        body, status = self._read_body()
        if status is not None:
            if status == 413:
                self.close_connection = True
                self._send_status(413, "too large", (("Connection", "close"),))
            else:
                self._send_status(400, "bad content-length")
            return
        if body is None:
            self._send_status(400, "bad content-length")
            return
        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError:
            self._send_status(400, "invalid JSON")
            return
        try:
            parsed: Any = json.loads(decoded)
        except ValueError:
            self._send_status(400, "invalid JSON")
            return
        if not isinstance(parsed, dict):
            self._send_status(400, "invalid JSON")
            return
        self._dispatch_send(parsed)

    def _dispatch_send(self, payload: dict[str, Any]) -> None:
        """Validate fields and perform the send."""
        unknown = sorted(k for k in payload if k not in _SEND_FIELDS)
        if unknown:
            self._send_status(400, "unknown field(s): " + ", ".join(unknown))
            return
        typ = payload.get("type")
        if not isinstance(typ, str):
            self._send_status(400, "type is required")
            return
        subject = payload.get("subject")
        if not isinstance(subject, str):
            self._send_status(400, "subject is required")
            return
        body = payload.get("body", "")
        if not isinstance(body, str):
            self._send_status(400, "body must be a string")
            return
        data = payload.get("data")
        if data is not None and not isinstance(data, dict):
            self._send_status(400, "data must be an object")
            return
        for field in ("to", "room", "reply_to"):
            if field not in payload:
                continue
            val = payload[field]
            if val is not None and not isinstance(val, str):
                self._send_status(400, f"{field} must be a string")
                return
        to = payload.get("to") if isinstance(payload.get("to"), str) else None
        room = payload.get("room") if isinstance(payload.get("room"), str) else None
        reply_to = payload.get("reply_to") if isinstance(payload.get("reply_to"), str) else None
        if len(body.encode("utf-8")) > _SEND_MAX_BODY_BYTES:
            self._send_status(413, "body too large")
            return
        self._perform_send(typ, subject, body, to, room, reply_to,
                           data if isinstance(data, dict) else None)

    def _perform_send(self, typ: str, subject: str, body: str, to: str | None,
                      room: str | None, reply_to: str | None,
                      data: dict[str, Any] | None) -> None:
        """Call ``ops.send_message`` and map the outcome to a response."""
        try:
            with self.send_lock:
                eid = ops_mod.send_message(
                    self.user_dir, self.name, "web", msg_type=typ, subject=subject,
                    body=body, to=to, room=room, reply_to=reply_to, data=data)
        except (EnvelopeError, ValueError) as exc:
            message = _redact(text_mod.clean_line(str(exc)))
            self._send_status(400, message)
            return
        except RetalkError:
            self._send_json(502, {"error": "send failed"})
            return
        except Exception:
            self._send_json(500, {"error": "internal error"})
            return
        self._send_json(200, {"ok": True, "eid": eid, "sender": self.name, "session": "web"})

    def _unsupported(self) -> None:
        """Reply 405 with the allowed methods."""
        self._method_not_allowed()

    do_PUT = _unsupported
    do_PATCH = _unsupported
    do_DELETE = _unsupported
    do_OPTIONS = _unsupported


def make_server(user_dir: str, *, host: str, port: int, token: str,
                owners: dict[str, Any] | None = None,
                allow_send: bool = False) -> ThreadingHTTPServer:
    """Build (but do not serve) the viewer HTTP server.

    ``allow_send`` enables ``POST /api/send`` as the local principal; the
    rate-limit window and lock are per-server, not module globals.
    """
    name = os.path.basename(os.path.normpath(user_dir))
    handler = type(
        "_BoundHandler",
        (_Handler,),
        {"user_dir": user_dir, "name": name, "token": token, "owners": owners or {},
         "allow_send": bool(allow_send), "sends": deque(),
         "send_lock": threading.Lock()},
    )
    cls: type[ThreadingHTTPServer] = (
        _IPv6ThreadingHTTPServer if host == "::1" else ThreadingHTTPServer
    )
    return cls((host, port), handler)


def _write_private_file(path: str, content: str) -> None:
    """Write ``content`` plus a newline to ``path`` with mode 0600."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def _write_token_file(path: str, token: str) -> None:
    """Write ``token`` to ``path`` with mode 0600."""
    _write_private_file(path, token)


def build_parser() -> argparse.ArgumentParser:
    """Build the ``phoning web`` argument parser."""
    parser = argparse.ArgumentParser(prog="phoning web", description="read-only local viewer")
    parser.add_argument("name", help="principal name")
    parser.add_argument("--home", default=None, help="phoning home directory")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (loopback only)")
    parser.add_argument("--port", type=int, default=0, help="bind port (0 picks a free one)")
    parser.add_argument("--token-file", dest="token_file", default=None, help="token file path")
    parser.add_argument("--url-file", dest="url_file", default=None,
                        help="write the full URL (token included) to PATH with mode 0600")
    parser.add_argument("--open", action="store_true",
                        help="open the viewer URL in a browser after binding")
    parser.add_argument("--owners", default=None, help="owners JSON file")
    parser.add_argument("--allow-send", dest="allow_send", action="store_true",
                        help="allow POST /api/send as this principal (session web)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``phoning web``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.host not in ALLOWED_HOSTS:
        print(f"error: --host must be one of {', '.join(ALLOWED_HOSTS)}", file=sys.stderr)
        return EXIT_USAGE
    home = args.home or os.environ.get("PHONING_HOME") or os.path.expanduser("~/.phoning")
    user_dir = os.path.join(home, args.name)
    log_path = os.path.join(user_dir, "mailbox", "log.jsonl")
    if not os.path.exists(log_path):
        print(f"error: no mailbox log at {log_path}", file=sys.stderr)
        return EXIT_USAGE
    owners: dict[str, Any] = {}
    if args.owners is not None:
        try:
            with open(args.owners, encoding="utf-8") as fh:
                loaded: Any = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"error: cannot load --owners file: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if not isinstance(loaded, dict):
            print("error: owners file must contain a JSON object", file=sys.stderr)
            return EXIT_USAGE
        owners = loaded
    token = secrets.token_urlsafe(32)
    token_file_any: Any = args.token_file
    token_file = str(token_file_any) if token_file_any is not None else os.path.join(
        user_dir, "web.token")
    url_file_any: Any = args.url_file
    url_file = str(url_file_any) if url_file_any is not None else None
    open_browser = bool(args.open)
    try:
        _write_token_file(token_file, token)
    except OSError as exc:
        print(f"error: cannot write token file: {exc}", file=sys.stderr)
        return EXIT_USAGE
    server: ThreadingHTTPServer | None = None
    try:
        server = make_server(user_dir, host=args.host, port=int(args.port), token=token,
                             owners=owners, allow_send=bool(args.allow_send))
        bound_port = int(server.server_address[1])
        if args.host == "::1":
            url_host = "[::1]"
        elif args.host == "localhost":
            url_host = "localhost"
        else:
            url_host = "127.0.0.1"
        url = f"http://{url_host}:{bound_port}/#token={token}"
        if url_file is not None:
            try:
                _write_private_file(url_file, url)
            except OSError as exc:
                print(f"error: cannot write url file: {exc}", file=sys.stderr)
                return EXIT_USAGE
        print(url, flush=True)
        if args.allow_send:
            print(f"sending enabled: POST /api/send as {args.name} (session web)",
                  file=sys.stderr, flush=True)
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception as exc:
                print(f"warning: cannot open browser: {exc}", file=sys.stderr)

        def _shutdown(signum: int, frame: Any) -> None:
            threading.Thread(target=server.shutdown, daemon=True).start()

        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(ValueError, OSError, RuntimeError):
                signal.signal(sig, _shutdown)
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
        return EXIT_OK
    finally:
        if server is not None:
            with contextlib.suppress(OSError):
                server.server_close()
        with contextlib.suppress(OSError):
            os.unlink(token_file)
        if url_file is not None:
            with contextlib.suppress(OSError):
                os.unlink(url_file)


if __name__ == "__main__":
    sys.exit(main())
