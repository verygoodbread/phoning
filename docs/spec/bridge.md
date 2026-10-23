# Legacy bridge: `phoning bridge` (module `phoning/bridge.py`)

## Problem
[agent-talk](https://github.com/xhluca/agent-talk) identities such as `codex-review` and `codex-worker` send **plain text** to an
agent-talk retalk identity, for example `~/.agent-talk/users/claude-lead/identity`. They do not
send to the agent's phoning identity (`~/.phoning/claude-lead/identity`). So a phoning agent that is
reachable only through its phoning identity never sees them. The bridge reads the legacy identity and
appends what it finds to the agent's phoning mailbox, so the agent reads legacy mail with the usual
`phoning read` / `wait` / hooks.

The module exposes `main(argv: Sequence[str] | None = None) -> int` and runs as
`python -m phoning.bridge` (`if __name__ == "__main__": sys.exit(main())`). It does not touch
`phoning/cli.py`; wave B wires `phoning bridge ...` to `bridge.main(argv[1:])`.
Stdlib only. `ruff` and `mypy --strict` clean. Subprocess rules: argv list, never `shell=True`,
stdin `/dev/null`, stderr discarded.

## Command
```
phoning bridge NAME --from-identity DIR --peer P [--peer Q ...] --session S
              [--home DIR] [--relay URL] [--interval N]
```
- `NAME`: the phoning principal. Its user dir is `<home>/NAME`, where home is `--home`, else
  `$PHONING_HOME`, else `~/.phoning`. `<home>/NAME/mailbox/log.jsonl` must already exist (the principal was
  initialised and its mailbox created). Otherwise exit 2 (`no mailbox for NAME`). The bridge never
  creates a mailbox, so a typo cannot create a stray principal.
- `--from-identity DIR`: the **legacy retalk identity directory** (the one passed to `retalk --dir`).
  It must be an existing directory, else exit 2. The legacy user dir is its parent (`DIR/..`).
- `--peer P` (repeatable, at least one): a saved peer name in the legacy identity's contacts, or a
  32-hex user id. Each must match `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`, else exit 2, so a value can never
  look like a retalk option. Duplicates are dropped, order is kept.
- `--session S`: a non-empty label recorded in the lock file and the stderr log lines. It is not a
  principal or a reader name.
- `--relay URL`: the legacy relay. Default: the first line of `<legacy user dir>/relay` if present,
  else retalk's own default (no `-r`).
- `--interval N`: seconds between polls, default 2 (a positive number; passed to retalk).

Exit codes: `0` stopped by SIGTERM/SIGINT; `1` unexpected failure while running (logged as
`phoning bridge: fatal: <ExceptionType>`, the lock is released); `2` usage or missing input; `3` refused (below).

## Safety: the bridge must be the SOLE reader
`retalk receive` consumes: "messages the server already handed over are never served again". If anything
else reads the same legacy identity for the same peers, messages are split between the readers and some
are lost to the bridge. So the bridge refuses to start unless it can show it is alone.

1. **Lock.** `<DIR>/../bridge.lock` (the legacy user dir, next to `identity/`). The bridge opens it
   (create, mode 0600) and takes `flock(LOCK_EX | LOCK_NB)`. If another process holds the flock, the bridge
   exits **3** and says so on stderr: `bridge already running for <DIR> (pid N)`, with the pid from
   the lock body when readable. The lock body is `{"pid", "since", "name", "session", "peers"}`, written
   after the lock is taken. The lock is held for the life of the process and released when it exits
   or crashes. A lock file left behind by a dead bridge holds no flock, so it does not block the next
   start; a *living* holder always does. (This is the one deliberate difference from "refuse if the file
   exists": a plain existence check would wedge forever after a crash.)
2. **No legacy follower.** Refuse with exit **3** if any `<DIR>/../follow.*.pid` file names a live process
   (`os.kill(pid, 0)` succeeds; a pid that is not an integer, or does not exist, is stale and ignored).
   Message: `legacy follower <file> is live (pid N); stop it first so the bridge is the only reader`.
   There is no override flag. The operator stops the follower or deletes a stale pid file.
3. **Peers exist.** Before taking the lock, load the legacy contacts with `Retalk(<DIR>/..).contacts()`
   (the legacy identity dir is `DIR` itself, not `<parent>/identity`, so build the client with
   `Retalk(parent, relay=...)` and set `client.identity_dir = DIR`). Every `--peer` that is not
   a 32-hex id must be in the contacts, else exit 2 (`unknown legacy peer P`). If the contacts cannot be
   listed at all (retalk error or an empty list), exit 2: fail closed, never read `--all`.
4. The bridge **only ever** runs `receive` with explicit `--peer` flags (never `--all`, never
   `--peers-only`). The argv comes from `Retalk.follow_cmd(interval, peers)`.

The order of checks: arguments, mailbox exists, identity dir exists, peers exist (3), follower pids (2),
lock (1). The lock is taken last, so a refused start never leaves a lock body behind.

## The loop
Same approach as `phoning/daemon.py`; the code is local to `bridge.py` (copy the pattern, do not import
daemon's private helpers):
- Spawn `retalk receive --peer P ... --follow --interval N --quiet --save --dir DIR [-r URL] -np`
  as a child with stdout piped, stderr discarded, stdin `/dev/null`, in a new process group/session
  so a signal to the bridge can also stop it.
- A background thread reads the child's stdout into a `queue.Queue`; the main loop polls the queue and the
  stop flag at least every 0.2 s and never blocks indefinitely. A `None` sentinel marks EOF.
- When the child exits (or cannot be spawned), restart it with exponential backoff: start 1 s, double
  each time, cap 30 s; reset to 1 s after a child that delivered at least one line. The wait is
  interruptible by stop.
- On SIGTERM/SIGINT: stop the child (terminate, 2 s grace, then kill), release the lock, exit 0.
  The signal handlers are installed before any startup work, so a stop request never kills the process
  with the default disposition.
- **Nothing consumed may be dropped.** The child has already taken those messages off the relay. So
  when the loop ends (stop requested, or the child hit EOF), first stop the child, then join the reader
  thread (up to 2 s) and process **every line still queued**, appending each to the mailbox, before
  returning. A line is never discarded because the stop flag is set.
- An unexpected exception in the loop (for example `Mailbox.append` raising `OSError`) is logged and
  the bridge exits 1; it must not exit 0 silently.
- `run_bridge(...)` is the testable core. Required keyword-only injectables: `popen`, `stop_event`,
  `max_restarts` (bounded loops for tests), `interval`; `mailbox` and `client` may be injected. When
  `client` is not given, `run_bridge` builds it itself from `identity_dir` exactly as `main` does
  (`Retalk(dirname(abspath(identity_dir)))` with `.identity_dir = identity_dir`); it never silently does
  nothing. Tests
  must not need real sleeps longer than a few hundred milliseconds.

## What is appended
For every stdout line of the child that is a JSON object with a string `text`:
```python
peer   = rec["name"] if a non-empty str else rec["from"]   # the legacy alias, else the fingerprint
env    = envelope.decode(text, fallback_from=peer)          # plain text becomes a legacy chat
mailbox.append({
    "direction": "in",
    "tid": rec.get("id"),             # str or None
    "peer": peer,
    "peer_fp": rec.get("from"),       # transport-verified sender fingerprint (str or None)
    "room": None,                     # always null: legacy mail has no phoning room
    "recv_ts": <UTC ISO-8601 Z now>,
    "raw": text,                      # the exact text received
    "env": env,
    "via": "bridge:<legacy>",
})
```
- `<legacy>` is the legacy user name: `basename(dirname(abspath(DIR)))` when `basename(DIR) == "identity"`,
  else `basename(abspath(DIR))`. For `~/.agent-talk/users/claude-lead/identity` that is
  `bridge:claude-lead`.
- Evidence is `peer_fp` and `raw`, exactly as the owner daemon records them. `env` is an **untrusted
  claim**: a legacy peer can send text that parses as an `@hd1` envelope claiming another principal. The
  record still carries the transport sender in `peer`/`peer_fp`, so the display layer can flag the
  mismatch. The bridge never "fixes" or rewrites `env`.
- Lines that are not JSON, not objects, have no string `text`, or are `kind: "contact"` records are
  ignored (no record, no crash).
- `Mailbox.append` de-duplicates on `(direction, tid)`, so a replay of the same transport id does not
  duplicate.
- The bridge writes **only** `log.jsonl` through `Mailbox.append` and its own lock. It never touches
  `owner.lock`, any cursor, the relay for the phoning identity, or the legacy identity's files except
  through retalk.

## Logging
One line per event on stderr, prefixed `phoning bridge:`: `started <legacy> -> NAME peers=P,Q`,
`appended seq=N peer=P` (never the message text), `child exited rc=N; restart in Ss` (`rc=?` when the
child cannot report one), `fatal: <ExceptionType>`, `stopped`. `stopped` is only logged after `started`
(a refused start logs just its reason).

## Out of scope
Sending phoning mail out to legacy peers, mapping legacy groups to rooms, and reading legacy identities that
need a passphrase.
