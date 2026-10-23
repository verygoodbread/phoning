# Spec: `phoning/harness_ring.py`, waker adapters for Kimi Code and OpenClaw

Track `providers`, build wave A (2026-10-11). This file is the
contract for the module, the acceptance tests (`tests/acceptance/test_harness_*.py`) and the unit tests
(`tests/unit/test_harness_ring.py`).

Stdlib only. `ruff check phoning tests` and `mypy phoning` (strict) stay clean. Wave A does not touch `cli.py`, `up.py`,
`mailbox.py`, `envelope.py` or `daemon.py`; wave B wires `ring_harness` into the doorbell.

## 1. Purpose

A registry of per-harness "waker" adapters, so a doorbell can start a turn in a session that runs on Kimi Code or OpenClaw
(and therefore on Grok, DeepSeek and the other providers those harnesses host). Each adapter answers three questions:

- `reachable(target)`: what is this session's state and which wake path is open right now?
- `wake(target, fixed_nudge)`: start a turn in a session that something already hosts.
- `resume_headless(target, fixed_nudge, cwd)`: start a turn in a session that nothing hosts.

The adapters never invent a session, never act when liveness is unknown, and never put anything but the caller's fixed
nudge into a prompt.

## 2. Public API

```python
HARNESSES: tuple[str, ...] = ("kimi", "openclaw")

@dataclass(frozen=True)
class Target:
    harness: str                 # "kimi" | "openclaw"
    session: str                 # kimi: session id (e.g. "session_<uuid>"); openclaw: sessionId or full session key
    cwd: str | None = None       # kimi: expected working dir (checked against the session file); openclaw: unused
    agent: str | None = None     # openclaw: agent id, to disambiguate; kimi: unused

@dataclass(frozen=True)
class Liveness:
    state: str    # "live" | "idle" | "dead" | "absent" | "unknown"
    path: str     # open wake path: "web" | "gateway" | "headless" | "none"
    detail: str   # short fixed-vocabulary reason (section 3); never external text

class Waker(Protocol):
    name: str
    def reachable(self, target: Target) -> Liveness: ...
    def wake(self, target: Target, fixed_nudge: str) -> str: ...
    def resume_headless(self, target: Target, fixed_nudge: str, cwd: str | None) -> str: ...

class KimiWaker:      # __init__(self, kimi_bin: str | None = None, kimi_home: str | None = None,
                      #          ps_bin: str | None = None, lsof_bin: str | None = None) -> None
class OpenClawWaker:  # __init__(self, openclaw_bin: str | None = None) -> None

def make_waker(harness: str, **options: Any) -> Waker      # ValueError on an unknown harness
def valid_nudge(text: str) -> bool
def ring_harness(harness: str, session: str, nudge: str, *, resume_dead: bool = False,
                 cwd: str | None = None, agent: str | None = None, **options: Any) -> str
def main(argv: Sequence[str] | None = None) -> int
```

`options` are the constructor keywords of the chosen waker. An explicit `*_bin` always wins; otherwise `shutil.which`.
Module constants (tests monkeypatch them): `PROBE_TIMEOUT = 15.0`, `WEB_TIMEOUT = 5.0`, `KIMI_RESUME_TIMEOUT = 900.0`,
`OPENCLAW_WAKE_TIMEOUT = 660.0`, `OPENCLAW_TURN_TIMEOUT = 600` (int seconds passed to `--timeout`),
`RECENT_WRITE_SECONDS = 45.0`, `MAX_NUDGE_CHARS = 4000`.

### Result lines

`wake`, `resume_headless` and `ring_harness` return ONE line, never raise, in the doorbell grammar so
`doorbell._is_failure` and wave B work unchanged:

| Line | Meaning |
|---|---|
| `rang: web` / `rang: gateway` | the wake request was accepted (HTTP code 0 / process rc 0) |
| `resumed: rc=N` | a headless resume process exited with N |
| `skip: <reason>` | nothing was done on purpose |
| `ring failed: <reason>` | something was attempted or required and went wrong |

No line contains subprocess output, HTTP body text, server messages, paths or tokens. Allowed variable parts: integers
(`rc=N`, `code=N`, `http=N`) and the fixed phrases below.

### Nudge validation (`valid_nudge`)

True only for a `str` of length 1..`MAX_NUDGE_CHARS`, with no character below U+0020 (so no newline or tab), not starting
with `-`. `wake` and `resume_headless` call it first and return `ring failed: invalid nudge` without running anything. The
adapter cannot prove the text is the fixed `doorbell.build_nudge` text; the caller must pass nothing else. The text goes into
the harness as a user-authority prompt, so it must never contain peer-supplied message bodies or subjects.

### Process rules (every subprocess)

argv list, never `shell=True`, `stdin=subprocess.DEVNULL`, `capture_output=True`, an explicit `timeout`. Output is parsed for
control decisions only and never echoed. A timeout or any exception becomes a result line, not an exception. Executable
resolution: explicit path must be an executable regular file, else `shutil.which(name)`; missing gives
`ring failed: no kimi executable` / `ring failed: no openclaw executable` from `wake`/`resume_headless`, and
`unknown / none / no executable` from `reachable`.

## 3. Kimi adapter (`KimiWaker`)

Home: `kimi_home` option, else `$KIMI_CODE_HOME`, else `~/.kimi-code`. When `kimi_home` is given it is passed to the child
as `KIMI_CODE_HOME`.

### `reachable`
Evaluate in order. Resolution (as built): a web-instance problem (loopback/token) or a process-scan failure gives `unknown` immediately; a kimi process in the session cwd or a recent write gives `live / none` even if a web UI is subscribed (the process may be a TUI holding the same session, so waking through the web would fork it); otherwise the web result (`live / web`, `live / none` busy, or `unknown`) stands; otherwise `dead`.

1. **Session file.** The session id must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, else `absent / none / bad session id`
   with no filesystem access. Look for `<home>/sessions/*/<sid>/state.json` (glob one level; newest by mtime if several).
   None: `absent / none / no session file`. Unreadable, invalid JSON, or no non-empty string `cwd`:
   `unknown / none / session file unreadable`. `cwd` from `state.json` is authoritative.
2. **Web servers.** For each `<home>/server/instances/*.json`:
   - unparsable file, or no integer `pid`/`port`: ignore it (`unknown` is not raised for garbage that names no server);
   - `pid` not alive (`os.kill(pid, 0)` raises `ProcessLookupError`): stale, ignore. `PermissionError` counts as alive;
   - `host` not one of `127.0.0.1`, `localhost`, `::1`: `unknown / none / web server not on loopback`;
   - token file `<home>/server.token` must be a regular non-symlink file with no group/other bits (mode & 0o077 == 0) and
     non-empty content, else `unknown / none / web token unusable`;
   - `GET /api/v1/connections` with `Authorization: Bearer <token>`. HTTP != 200, bad JSON, `code != 0`, or exception:
     `unknown / none / web server unreachable`. If any connection's `subscriptions` contains the session id:
     `live / web / ui subscribed` (remember this instance for `wake`);
   - otherwise `GET /api/v1/sessions/<sid>/status`; failure: `unknown / none / web server unreachable`;
     `data.busy` true: `live / none / busy in web server`.
3. **Process scan.** Run `<ps_bin or ps> -axo pid=,comm=`. rc != 0, timeout or exception: `unknown / none / process scan failed`.
   Lines are `<pid> <comm>`; consider those whose `basename(comm)` is `kimi` or `kimi-code`, excluding this process and
   the pids of live web instances found in step 2. For each, read its cwd (`/proc/<pid>/cwd` readlink if it exists, else
   `<lsof_bin or lsof> -a -d cwd -Fn -p <pid>` and take the first `n` line). Failure to read:
   `unknown / none / cannot read process cwd`. If `realpath(cwd) == realpath(session cwd)`:
   `live / none / kimi process in session cwd`. (A TUI shows as `kimi-code` and its argv does not name the session, so the
   cwd is the only available key. A false positive blocks a resume, which is the safe direction.)
4. **Recent write.** `stat` `<session dir>/agents/main/wire.jsonl`. If `now - mtime < RECENT_WRITE_SECONDS`:
   `live / none / recent session write`. Missing file is fine. Other `OSError`: `unknown / none / session file unreadable`.
5. Nothing found: `dead / headless / no live kimi found`.

### `wake`
`valid_nudge`, then executable check is NOT needed (HTTP only). Re-run `reachable`. Only `path == "web"` proceeds:
`POST /api/v1/sessions/<sid>/prompts` to the subscribed instance with JSON
`{"content": [{"type": "text", "text": <nudge>}]}`, bearer auth, `Content-Type: application/json`, timeout `WEB_TIMEOUT`.
HTTP 200 and JSON `code == 0`: `rang: web`. Otherwise: `ring failed: web code=N` (envelope code) or `ring failed: web http=N`;
exception: `ring failed: web unreachable`. Other paths:

| `reachable` | line |
|---|---|
| `live / none` | `skip: live (kimi sessions outside a server cannot be woken; the hook delivers on the next prompt)` |
| `dead / headless` | `skip: dead (use resume_headless)` |
| `absent` | `skip: absent: no session` |
| `unknown` | `skip: liveness unknown` |

### `resume_headless`
`valid_nudge`, then re-run `reachable`. Only `path == "headless"` proceeds; `live` (any path) gives
`skip: live (not resuming a session something may hold)`, `absent` and `unknown` as in `wake`. If the `cwd` argument is
given (or `target.cwd`) and `realpath` differs from the session file's `cwd`: `skip: cwd mismatch`. Run
`[kimi, "-S", <sid>, "-p", <nudge>]` with `cwd=<session cwd>`, timeout `KIMI_RESUME_TIMEOUT`. Result `resumed: rc=N`;
timeout `ring failed: timed out`; other exception `ring failed: <ExceptionClass>`. Kimi itself does NOT refuse a
concurrent writer (two resumes interleave into one `wire.jsonl`), so the liveness gate above is the only guard.

## 4. OpenClaw adapter (`OpenClawWaker`)

### Probes
- **Session lookup**: `[openclaw, "sessions", "--json", "--all-agents", "--limit", "all"]`, timeout `PROBE_TIMEOUT`. stdout is
  JSON with a `sessions` list of `{sessionId, key, agentId, status}`. An entry matches if `sessionId == target.session` or
  `key == target.session`, and, when `target.agent` is set, `agentId == target.agent`. Zero matches: absent. More than one
  match with different `(agentId, key)`: unknown (`ambiguous session`). Command failure, bad JSON or a missing `sessions`
  list: unknown.
- **Gateway state**: `[openclaw, "health", "--json"]`, timeout `PROBE_TIMEOUT`.
  `up`: rc 0 and JSON object with `ok is True`.
  `down`: rc != 0 and JSON object with `error.type == "gateway_transport_error"` and `error.kind == "closed"`.
  Anything else (other error types such as auth, timeout, exception, bad JSON, rc 0 without `ok`): `unknown`.

### `reachable`
Session lookup first, then the gateway:

| lookup | gateway | result |
|---|---|---|
| absent | any | `absent / none / no such session` |
| unknown | any | `unknown / none / session lookup failed` (ambiguous: `ambiguous session`) |
| found | up | `live / gateway / run in flight` if `status == "running"`, else `idle / gateway / gateway up` |
| found | down | `dead / headless / gateway down` |
| found | unknown | `unknown / none / gateway state unknown` |

The lookup comes first on purpose: `openclaw agent --session-id X` silently CREATES session X when it does not exist.

### `wake`
`valid_nudge`, executable check, re-run `reachable`; only `path == "gateway"` proceeds. argv exactly:

```
[openclaw, "agent", "--agent", <agentId from lookup>, "--session-id", <sessionId from lookup>,
 "--message", <nudge>, "--json", "--timeout", str(OPENCLAW_TURN_TIMEOUT)]
```

(no `--deliver`, `--channel`, `--to`, `--reply-*`, `--local`; the reply stays in the session). Timeout `OPENCLAW_WAKE_TIMEOUT`.
rc 0: `rang: gateway`; else `ring failed: rc=N`; timeout `ring failed: timed out`; exception `ring failed: <ExceptionClass>`.
The command blocks until the turn ends and is serialized behind an active run by the gateway. On timeout nothing is
retried (the gateway may still finish the turn). Other paths: `dead / headless` gives `skip: gateway down (use resume_headless)`;
`absent` / `unknown` as for Kimi.

### `resume_headless`
`valid_nudge`, executable check, re-run `reachable`; only `path == "headless"` (gateway proven down, session stored)
proceeds, otherwise `skip: live (gateway up; use wake)` for `gateway`, and the standard lines for `absent` / `unknown`.
argv exactly `[openclaw, "agent", "--local", "--agent", A, "--session-id", S, "--message", <nudge>, "--json", "--timeout",
str(OPENCLAW_TURN_TIMEOUT)]`, `cwd=<cwd>` when `cwd` is an existing directory, else inherited. OpenClaw's own state lock
refuses `--local` while any gateway owns the state, which backs the liveness decision. Result lines as `wake` but
`resumed: rc=N` on completion.

## 5. Registry helpers

- `make_waker(harness, **options)`: returns `KimiWaker(**options)` / `OpenClawWaker(**options)`. Unknown harness: `ValueError`.
- `ring_harness(...)`: unknown harness returns `ring failed: unknown harness`. Otherwise build the waker and the `Target`, call
  `reachable`, then: `path in ("web","gateway")` returns `wake(...)`; `path == "headless"` returns `resume_headless(...)` when
  `resume_dead` is true, else `skip: dead (resume_dead not set)`; any other path returns the same `skip:` line the waker's
  `wake` would give for that state. Never raises.
- `main(argv)`: only subcommand `reach`: `reach --harness H --session S [--cwd D] [--agent A] [--kimi-bin P] [--kimi-home H]
  [--ps-bin P] [--lsof-bin P] [--openclaw-bin P]` prints one JSON object
  `{"harness","session","state","path","detail"}` and returns 0. It is read-only: it never wakes or resumes. Unknown harness
  or missing arguments return 2 (argparse behavior is fine). `if __name__ == "__main__": raise SystemExit(main())`.

## 6. Safety invariants (tested)

1. Fixed text only: `valid_nudge` gate; the nudge is the only variable text sent to a harness, passed as one argv element
   or one JSON string.
2. Fail closed: unknown or ambiguous state never acts. `resume_headless` refuses unless `path == "headless"`; `wake` refuses
   unless `path in ("web","gateway")`. Each re-probes immediately before acting.
3. No invented sessions: absent sessions are skipped for both harnesses.
4. argv lists, no `shell=True`, stdin `/dev/null`, a timeout on every subprocess and HTTP request. Static scan test.
5. No output echo and no secrets in result lines. The Kimi token is read from a 0600 file, sent only as a bearer header to
   a loopback host, and never logged or returned.
6. Loopback only for Kimi web.
7. Never raise from `reachable`, `wake`, `resume_headless`, `ring_harness`.

## 7. Wave B wiring (informational, not built here)

New doorbell ring kinds `kimi` and `openclaw` call `ring_harness`. Proposed flags (same style as `--claude-session`):
`--ring kimi|openclaw`, `--kimi-session ID`, `--openclaw-session ID`, `--openclaw-agent ID`, `--kimi-bin`, `--kimi-home`,
`--openclaw-bin`, plus the existing `--resume-dead`. Proposed `up.json` doorbell keys: `kimi_session`, `kimi_cwd`,
`openclaw_session`, `openclaw_agent`, recorded by the registry at bind time next to `codex_thread` / `claude_session`.
Nudge text: reuse `doorbell.build_nudge(name, hook_reader, home)` (includes the `PHONING_HOME=` prefix, which both
harnesses' shells need because neither resume path sets it in the child env). Result lines feed `doorbell._is_failure`
unchanged. Kimi TUI sessions stay hook-only (`UserPromptSubmit` stdout is injected as a user-role message); do not claim a
running-TUI wake for them.
