# `phoning web`: activity view, living board, opt-in sending (track "web", wave A)

Extends SPEC-v0.2 §3 and §6.6. **Every guarantee in §3 still holds by default**: loopback only, token on every
`/api/` route, Host check, CSP and security headers on every response, redaction, read-only reads, JS that renders only
through `textContent`/`createElement` with the `oneLine`/`block` filters, and 405 for every non-GET/HEAD method. This
document only adds. Module: `phoning/web.py`. No new storage: all views are folds over `log.jsonl` plus two read-only
files that the doorbell writes.

Existing tests must keep passing unchanged (`tests/unit/test_web.py`, `tests/acceptance/test_web_acceptance.py`). In
particular the static routes `/`, `/app.css` and `/app.js` must never contain the lowercase substring `proposal`, and
`/app.js` must not contain `innerHTML`, `from.principal` or `frm.principal`. (Hence the board page script lives in a
separate `/board.js`; see 2.)

## 0. CLI and `make_server`

- `phoning web NAME ... [--allow-send]`. Off by default. When given, print one line to **stderr**:
  `sending enabled: POST /api/send as NAME (session web)`. The first stdout line (the URL) is unchanged.
- `make_server(user_dir, *, host, port, token, owners=None, allow_send=False)`. `allow_send` is keyword-only with a default
  of `False`, so existing calls still work.
- `/api/summary` gains the key `"can_send": bool` (`allow_send`). All its other keys are unchanged.
- `build_parser()` exposes `args.allow_send` (`dest="allow_send"`, `store_true`).

## 1. Activity: `GET /api/activity` (+ UI section "Activity")

Requires the Bearer token like every other `/api/` route (401 otherwise). Response (JSON):

```
{
  "name": "alice",
  "senders": [
    {"sender": "bob", "self": false, "verified_fp8": "a3f1c2d4" | null, "last_seen": "<ts>",
     "sessions": [
       {"session": "vega", "last_seen": "<ts>", "total": 3,
        "counts": {"ask": 1, "chat": 2}, "open_asks": 1, "claimed": ["someone-else"]}
     ]}
  ],
  "open_asks": [
    {"eid": "...", "sender": "bob", "session": "vega", "peer": "bob", "room": "ops" | null,
     "subject": "...", "ts": "<ts>"}
  ],
  "doorbell": {
    "present": true, "last_ring": 1760000000.0 | null, "last_ring_iso": "2026-10-09T..Z" | null,
    "pending": false, "introduced": ["bob"], "introduced_count": 1,
    "resume_counts": {"...": 2} | null, "resumes_total": 2 | null
  },
  "approvals": {"present": true, "pending_count": 1,
                "pending": [{"id": "...", "ts": "...", "peer": "...", "session": "...", "ring": "...", "action": "...", "status": "pending"}]}
}
```

**Senders and sessions.**
- Group every record by the **transport-bound sender** (same rule as `ops.sanitize`: `rec.peer` for inbound, the local
  name for outbound; outbound has `"self": true`) and then by the declared `env.from.session` (missing -> `"?"`).
  Records without an `env` are skipped. Never group by the claimed principal.
- `last_seen` is the maximum `env.ts` (else `recv_ts`) as a string compare, as in `_ts_of`. The sender-level `last_seen` is
  the max over its sessions.
- `counts` maps envelope `type` to the number of records. `total` is the sum. An unknown or non-string type counts as
  `"?"`.
- `claimed` lists the distinct `env.from.principal` values that differ from the sender (sorted). It is untrusted
  metadata, shown flagged as a claim by the UI, never as the identity.
- `verified_fp8` is the first 8 chars of the `peer_fp` of the sender's newest inbound record that has one, else `null`.
  The full fingerprint never appears anywhere in the response.
- `senders` are sorted by name, `sessions` by session name (deterministic).

**Open asks addressed to us.** An *inbound* record (`direction != "out"`) of `env.type == "ask"` whose `env.eid` is not the
`reply_to` of any record in the log (any direction). This is the same replied-to rule as `board.fold`. Each entry has
`eid`, `sender`, `session`, `peer`, `room`, `subject`, `ts`. Sorted oldest first, newest 100 kept. **No bodies, no
`raw`.** `sessions[].open_asks` is the per-session count of the same set.

**Doorbell** (read-only, from `<user_dir>/doorbell-state.json`; written by `phoning doorbell`, never by web):
- Missing, unreadable, malformed or non-object file: `{"present": false, "last_ring": null, "last_ring_iso": null,
  "pending": false, "introduced": [], "introduced_count": 0, "resume_counts": null, "resumes_total": null}`.
- `last_ring` is returned only if it is a real number (not bool) > 0 (else `null`); `last_ring_iso` is the UTC ISO-8601
  `...Z` form of it. `pending` only if a bool (else false). `introduced` is the list of string entries (scrubbed, at most
  100), `introduced_count` its full length.
- Resume counts, if the state file has them: key `resume_counts` as an object of `str -> non-negative int`, else key
  `resumes` as an object (same shape) or a bare non-negative int. `resume_counts` is the object (scrubbed keys, non-int
  values dropped, at most 100 entries) or `null`; `resumes_total` is the sum of the counts, or the bare int, or `null`.
- Any other key in the state file is ignored (never echoed).

**Approvals** (read-only, from `<user_dir>/doorbell-approvals.jsonl`; this track does not define who writes it, so be
lenient):
- Missing or unreadable file: `{"present": false, "pending_count": 0, "pending": []}`.
- Read at most the last 1 MiB. Each line is a JSON object; skip bad lines. The key of an entry is its string `id`, else
  `"line:<n>"`. A later line with the same key **replaces** an earlier one (so the file may be append-only events). An
  entry is pending when its `status` is absent or equals `"pending"`.
- Output only these allowlisted keys of a pending entry, in this order: `id`, `ts`, `peer`, `session`, `ring`, `action`,
  `status` (defaults to `"pending"`). `id` is always present: the entry's own string `id`, else `line:<n>`. It is scrubbed
  (with hex shortening) like every other string, because it is attacker-writable text too. A string value is scrubbed (below); a finite int/float passes; any other value type
  is dropped. **Every other key (text, body, subject, reason, token, fingerprint ...) is never emitted.** At most 100
  newest kept; `pending_count` is the full pending count.

**Scrub** (`_scrub`) applied to every string from the doorbell state/approvals and to subjects, senders, peers, rooms,
sessions, claimed principals and timestamps in the activity view (grouping uses the scrubbed values): `text.clean_line`, then `_redact`, then cap at 120 chars. For doorbell and approvals strings additionally
shorten any run of 20 or more hex characters to its first 8 plus `..` (so a full fingerprint or token-ish id can't leak).
Message eids are NOT hex-shortened (they are 32-hex ids and the compose box needs them to reply).

**Never in the activity response:** the bearer token, `raw`, `peer_fp`, any message `body`, full fingerprints, anything
from `identity/`. `GET /api/activity` creates no files and changes no bytes under the user dir.

**UI.** A new `<section>` "Activity" in the `/` shell with `id="activity"`: a list per sender (name, self marker, `verified
<fp8>` or `unverified`, last seen), per session counts like `ask 1, chat 2` and open-ask count, any `claims X` flagged; an
"Open asks for you" list; a "Doorbell" paragraph (last ring, pending, introduced count, resumes) or `doorbell: no state`;
an "Approvals" list or `approvals: none`. It refreshes with the page's existing 5 s poll. Rendered with `createElement` /
`textContent` only, every string through `oneLine`.

## 2. Living board page: `GET /board`

- `GET /board` is a static shell (no auth, no data): same security headers, links `/app.css`, `/app.js` and `/board.js`,
  contains `<main id="board-page">`, a link back to `/` and no inline script or style. The `/` shell gains a "Board" link to
  `/board`. Both are plain same-tab `<a href>` links so `sessionStorage` carries the token; `/board#token=...` also works
  because the token-from-hash code runs in `/app.js` on every page.
- `/app.js` is shared: it runs the mailbox-page logic only when an element with id `places` exists, and exposes
  `window.phoningUI = {oneLine, block, el, api, getToken}` for `/board.js`. `/board.js` (static, no data, no `innerHTML`,
  builds its DOM only through the `window.phoningUI` helpers, i.e. `el` = `createElement` + `textContent`, and never `innerHTML`/`insertAdjacentHTML`; the lowercase substring `proposal` is allowed there) runs
  only when `#board-page` exists. It fetches `/api/board[?room=R]` (the `room` comes from the page's own query string) and
  renders the `view` below, then refreshes every 5 s. The first fetch always runs (even when `document.hidden`, so the page is never blank); interval
  ticks are skipped while hidden, and a `visibilitychange` to visible refreshes at once, and shows
  `updated HH:MM:SS` or `error: ...` in `#status`.
- Layout: a "Who is doing what" list, a "Proposals" list (each with state, scope, owners, proposer, and its decisions marked
  `effective` or `NOT EFFECTIVE (reason)`), an "Open asks" list and a "Receipts" list. Empty sections say `none`.
- `/api/board` keeps `{"board", "text"}` exactly as before and **adds** `"view"`, built from the same `board.fold` result
  (no new storage, honouring `?room=` and `--owners`):

```
"view": {
  "status":   [{"principal", "session", "state", "subject", "ts"}],              # sorted by (principal, session)
  "proposals": [{"eid", "scope", "subject", "state", "owners": [..], "by": {"principal", "session"}, "claimed",
                 "decisions": [{"type", "by": {"principal", "session"}, "claimed", "effective": bool, "reason", "ts"}]}],
  "open_asks": [{"eid", "sender", "session", "subject", "ts", "room"}],
  "receipts":  [{"eid", "sender", "session", "subject", "refs": [..], "ts"}]
}
```
  Every string is `clean_line` + `_redact`. **No bodies.** `sender` for `open_asks` and `receipts` is the transport-bound
  sender looked up from the record whose `env.eid` matches (inbound: `peer`; outbound: the local name; unknown -> `"?"`),
  not the claimed principal. `session` of `open_asks` and `receipts` is the declared `env.from.session` (`"?"` when absent),
  not a top-level `session` key (envelopes have none). `view.open_asks` includes every unanswered ask in the room scope (also ones we sent); the
  activity view lists only inbound ones.

## 3. Human participation: `POST /api/send` (only with `--allow-send`)

Without `--allow-send`: unchanged. Every POST/PUT/PATCH/DELETE/OPTIONS returns 405 with `Allow: GET, HEAD` (including
`POST /api/send`), and `/api/summary.can_send` is false.

With `--allow-send`, the only non-GET/HEAD request accepted is `POST /api/send`. Any other method or path combination
returns 405 with `Allow: GET, HEAD, POST`. Checks run in this order and stop at the first failure:

1. **Host** check as for every request: 403.
2. **Method/path** as above: 405.
3. **Gates**, each 403 `{"error":"forbidden"}` and nothing about which gate failed: (a) `Authorization: Bearer <token>`
   (`hmac.compare_digest`); (b) `Origin` header present and exactly equal to `"http://" + <Host header>` (the Host already
   passed step 1, so this is the bound origin; no `Origin` or `null` fails); (c) `Content-Type` media type equal to
   `application/json` (case-insensitive, parameters such as `; charset=utf-8` allowed).
4. **Rate limit**: sliding 60 s window, at most **30** requests that passed step 3, counted whether they later succeed or
   not. The 31st returns 429 `{"error":"rate limited"}` with `Retry-After: <seconds>`.
5. **Size**: the `Content-Length` header must be a non-negative integer (else 400 `{"error":"bad content-length"}`). More
   than 65536 -> 413 `{"error":"too large"}` with `Connection: close` (the body is not read). Else read exactly that many
   bytes.
6. **JSON**: not valid UTF-8 JSON or not an object -> 400 `{"error":"invalid JSON"}`.
7. **Fields**. Accepted keys: `to`, `room`, `type`, `subject`, `body`, `reply_to`, `data`. Unknown keys -> 400
   `{"error":"unknown field(s): a, b"}` (sorted). `type` missing or not a string -> 400 `type is required`. `subject` missing
   or not a string -> 400 `subject is required`. `body` (default `""`) must be a string, `to`/`room`/`reply_to` strings or
   null, `data` an object or null: otherwise 400 `<field> must be a ...`. A `body` longer than **20480 UTF-8 bytes** -> 413
   `{"error":"body too large"}` (checked before the envelope).
8. **Send**: `ops.send_message(user_dir, name, "web", msg_type=type, subject=..., body=..., to=..., room=..., reply_to=...,
   data=...)`, looked up at call time via the `ops` module so tests can monkeypatch `ops.Retalk`. The sender is the
   server's principal NAME, session `web`, never anything from the request. `reply_to: "last"` works as in the CLI.
   - `EnvelopeError` or `ValueError` (envelope validation: bad type, subject length, missing `data.scope`, missing
     `reply_to`, not exactly one of `to`/`room`, ...) -> 400 `{"error": "<message>"}` where `<message>` is the exception
     text (already `; `-joined across all unmet rules) passed through `clean_line` and `_redact`.
   - `RetalkError` -> 502 `{"error":"send failed"}` (no stderr, no argv).
   - Any other exception -> 500 `{"error":"internal error"}`.
   - Success -> 200 `{"ok": true, "eid": "<eid>", "sender": "<NAME>", "session": "web"}`.
   The record is then in the log as a normal `direction:"out"` record, visible through `/api/messages` and the board.

`refs` and `thread` are deliberately not accepted (a `receipt` therefore cannot be sent from the page; it fails with the
envelope's own message). Sends are serialised with a lock. The limiter and lock belong to the server instance, not module
globals. A send never prints or logs the body.

**UI.** `<section id="compose" hidden>` exists in the `/` shell but is empty; `/app.js` fills and unhides it only when
`/api/summary.can_send` is true, so without `--allow-send` there is no compose box and no sending code path is exercised.
Controls (all `createElement`, no `<form>` element, since CSP has `form-action 'none'`): a `type` select with the eight
message types, a destination (radio or select `to` / `room` plus a text input prefilled from the selected place), `subject`
input (maxlength 80), `body` textarea, `reply_to` input, `data` textarea (JSON, optional), and a Send button. Send does
`fetch('/api/send', {method:'POST', headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'}, body: JSON.stringify(...)})`,
shows the server's `error` (through `oneLine`) or `sent <eid8>` in a status line, clears subject and body on success and
refreshes. Each open ask in the Activity section gets a "Reply" button, shown only when sending is allowed. It fills
`reply_to` with the ask's eid and sets the destination to the ask's `room` if it has one, else to `to` = the ask's `peer`.
The chosen `type` is left alone.

## 4. Safety notes

- Read-only by default; `--allow-send` is an explicit, per-process opt-in, printed on start.
- A page script cannot send without the token, which lives in `sessionStorage` and the URL fragment. A cross-origin page
  cannot send because of the exact `Origin` gate and the JSON Content-Type (which forces a CORS preflight, answered 405).
- Nothing the web view displays can reach an agent as an instruction: the send path forwards only what the human typed,
  as a normal phoning message, attributed to NAME/web.
- The handler sets a socket `timeout` (30 s) so a client that stalls mid-request cannot pin a thread forever.
- `GET`-side guarantees (no writes, no `Mailbox` construction on reads) hold: `Mailbox` is only constructed inside
  `ops.send_message`, only on the send path.

## 5. Acceptance (`tests/acceptance/test_web_activity_acceptance.py`)

Covers: activity counts/last_seen/claims/open-ask logic and the no-leak invariants (no `raw`, `peer_fp`, bodies, token,
full fingerprints); doorbell state and approvals read-only (bytes unchanged, missing/malformed files, allowlist, hex
shortening, resolved approvals dropped); `view` in `/api/board` and the `/board` shell and `/board.js`; default 405 and
`can_send` false; every 403 gate; 400 messages; 413; 429 after 30; successful send recorded as `out`/`web`/NAME; fake
`Retalk` failure -> 502; CLI flag and stderr line; and that the earlier guarantees (CSP on every response incl. 403/405/429,
no `innerHTML`, `oneLine`/`block` present) still hold.
