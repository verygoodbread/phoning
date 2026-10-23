# phoning SPEC v0

## Overview

phoning is a typed agent-talk layer over the `retalk` relay. Each principal
(`NAME`) owns one mailbox directory (`$PHONING_HOME/<name>`). Exactly one
owner daemon drains the relay into the mailbox; many readers consume via
cursors. All envelope `from.*` claims are untrusted; the transport-verified
sender fingerprint and the raw wire text are kept as evidence on every record.

## Commands

- `phoning init NAME --relay URL` — create identity and mailbox; print fingerprint.
- `phoning id NAME` — print fingerprint.
- `phoning add NAME FINGERPRINT --as ALIAS` — save a peer (local alias).
- `phoning room create NAME ROOM --members a,b` — create a group room.
- `phoning own NAME --session SID [--interval 2]` — run the owner daemon in foreground.
- `phoning send NAME (--room ROOM | --to ALIAS) --type T --subject S [--body B | --body-file F] [--session SID] [--thread EID] [--reply-to EID] [--data JSON] [--refs a,b]` — send; print `{"eid": ...}`.
- `phoning read NAME --reader R [--types a,b] [--room ROOM] [--peek] [--json]` — read new records.
- `phoning wait NAME --reader R [--timeout 600] [--json]` — wait for new records.
- `phoning board NAME [--room ROOM] [--owners FILE.json]` — render the folded board.

Exit codes: 0 ok, 1 nothing/timeout, 2 usage/validation, 3 refused (mailbox owned).

## Envelope

Typed envelope with `eid`, `type`, `from {principal, session}`, `subject` (1..80 chars), `body`, `ts`, optional `thread`, `reply_to`, `room`, `refs`, `data`. Encoded as canonical JSON text on the wire. `from.*` is a claim and MUST NOT be trusted without `peer_fp` evidence.

## Mailbox record

Mailbox record (JSONL): `{"direction": "in"|"out", "tid": str|null, "peer": str, "peer_fp": str|null, "room": str|null, "recv_ts": str, "raw": str, "env": object}` — inbound `peer` is the local alias, `peer_fp` is the retalk `from` verified 32-hex fingerprint, `raw` is the exact retalk `text` wire string, `env` is the decoded (untrusted) envelope; outbound records use `peer_fp: null` with `raw` the encoded text that was sent.

## Owner daemon

The owner holds the mailbox owner lock, follows only saved contacts by named `--peer` (never `--all`), polls stop at least every 0.2 s, refreshes contacts every 30 s (changed set restarts the child), waits without a child when empty, and restarts the child with capped exponential backoff (1 s → 30 s max).

## Board

`board` folds all records by thread/reply chain; owners file maps principal → owner string for display.
