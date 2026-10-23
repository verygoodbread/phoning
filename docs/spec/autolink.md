# Autolink: agents bound to their chats (wave A, track "hooks")

Extends `docs/SPEC-v0.2.md` §7.0 (session binding) and §7.1 (Stop hook). Module: `phoning/hooks.py` only.
Goal: nobody hand-copies a thread id. `phoning hook bind` records which chat a session lives in, and
`phoning hook targets` prints a ready doorbell target for each binding. Wave B turns those into
`up.json` doorbells.

## 1. `phoning hook bind` records `harness` and `thread`

New optional flags: `--harness {claude,codex,muse,unknown}` and `--thread ID`. Everything that `bind`
did before still holds (session-id resolution, reader default, mailbox check, 0600 registry, JSON
entry on stdout).

### 1.1 Session id resolution (extended, not changed)
Order: `--session-id`, then `$CLAUDE_CODE_SESSION_ID`, then `$CODEX_THREAD_ID`, then (new) `$MUSE_SESSION_ID`,
then `$MUSE_THREAD_ID`, then (new) `--thread`. If none gives a value, exit 2 with the existing
`_NO_SESSION_ID` text (updated to mention `--thread`). `_session_id_from_env()` keeps its current
behaviour and tests (claude, then codex, else None). A new helper adds the two Muse variables after
them. `unbind` uses the same extended order.

### 1.2 Harness detection
Detection reads the session id being bound (`sid`, from 1.1) and asks which env var holds it:

| env var | harness |
|---|---|
| `CLAUDE_CODE_SESSION_ID` | `claude` |
| `CODEX_THREAD_ID` | `codex` |
| `MUSE_SESSION_ID`, `MUSE_THREAD_ID` | `muse` |

Rules:
- Only a non-empty env value that **equals `sid`** counts. An env var that holds a different id describes
  a different session (for example `bind --session-id X` run from inside another session), so it is
  ignored.
- Exactly one harness matches: that harness. Zero or several match: `unknown`. Fail closed, never guess.
- The Muse variable names come from strings in the Muse 1.4.6 binary (`MUSE_SESSION_ID`, `MUSE_THREAD_ID`).
  They are not confirmed as set inside a Muse tool subprocess. `--harness muse --thread ID` is the reliable
  path, and flags always win.
- Explicit `--harness` replaces detection (also `--harness unknown`).

### 1.3 Thread resolution
- `--thread ID` if given.
- Else, when the harness is not `unknown`: `sid` (for Claude and Codex the hook `session_id` is the
  session or thread id; for Muse the id the session was bound under).
- Else `null`.

`--thread` and values taken from env must match `[A-Za-z0-9_.-]{1,128}`. A bad `--thread` or bad
`--harness` value exits 2 and writes nothing. A bad id taken from env or `sid` is stored as `thread: null`,
and detection yields `unknown` (nothing is guessed from an unsafe value).

### 1.4 Registry entry
The entry gains two keys, always present:

```json
{"name":"alice","home":"/abs/home","reader":"alice-main","bound_at":"...Z",
 "harness":"codex","thread":"019d...-uuid"}
```
`harness` is one of `claude|codex|muse|unknown`; `thread` is a string or `null`. Old entries without the
keys read as `harness: unknown`, `thread: null`. The printed JSON is the stored entry. Re-binding the same
session id overwrites the entry, including the two new keys.

## 2. `phoning hook targets [--json]`

Prints one target per registry entry, sorted by `(name, session_id)`. Exit 0 always; a missing or corrupt
registry gives an empty list (the existing corrupt-registry stderr note applies). It writes nothing and reads
no mailbox.

### 2.1 Ready targets
Common keys: `session_id`, `name`, `home`, `harness`, `ring`, `hook_reader`. `hook_reader` is the entry's
`reader`.

| harness | `ring` | extra key |
|---|---|---|
| `codex` | `"codex-queue"` | `"codex_thread": T` |
| `claude` | `"claude-resume"` | `"claude_session": T` |
| `muse` | `"muse"` | `"muse_session": T` |

`codex-queue` and `claude-resume` are the existing doorbell rings (§7.9, §7.10); `muse` is the ring name
reserved for the Muse doorbell (`muse session-message send --target`). Wave B decides whether `up` can
supervise it yet.

### 2.2 Not-ready entries
`unknown` harness, a missing `thread`, or an entry lacking `name`/`home` is listed with `"ring": null` and
`"skip": "<one-line reason>"` (`unknown harness`, `no thread`, `malformed entry`), and has no thread key. A
consumer must skip every target whose `ring` is null. A malformed (non-dict) registry value is also listed
that way, never raised.

### 2.3 Output
- `--json`: one JSON array of the target objects, `sort_keys` off, keys in the order above.
- Default: one tab-separated line per target: `name  harness  ring-or-"-"  thread-or-"-"  home`
  (columns joined with a tab). No header, no JSON.
- No peer text is ever included. Everything printed is registry data written by `bind`.

## 3. @mention semantics on Stop

Applies to `phoning hook stop` only. For each unread **inbound** record:

- **Direct record** (no room: neither the top-level `room` nor `env.room` is a non-empty string): unchanged.
  It blocks when `env.type` is in `--block-types`.
- **Room record**: it blocks only when BOTH hold:
  1. `env.type` is in `--block-types`; and
  2. it is addressed to us: `env.data.mentions` is a list containing the bound name exactly (case-sensitive
     string equality; a non-list or non-string entries never match), or `env.reply_to` equals the `env.eid`
     of an `out` record in our own log.

A room record that fails the test is not actionable: it does not trigger the Stop block. Everything else
about Stop is unchanged: when some other record does block, **all** unread inbound records (up to
`--limit`) are shown and committed as today; when nothing blocks, nothing is committed, so unaddressed
room traffic surfaces at the next prompt. `--block-types ""` still never blocks. `stop_hook_active` still
short-circuits.

`UserPromptSubmit` and `SessionStart` are unchanged: they surface everything unread.

The own-eid scan reads the log once, and only when a room record of a blocking type is present without a
mention. A 10k-record log must still finish well under 1 s.

## 4. Fixed text and PHONING_HOME (regression guard)
Nothing in this track changes emitted text. The waker paragraph, the `(+K more unread: …)` and `[+K chars: …]`
hints and the Stop send instruction all keep their `PHONING_HOME=<quoted absolute home>` prefix (§7.8), for the
default home as well. `targets` prints no commands. No message body, subject or claimed name enters any
text this code builds beyond what the hooks already display, framed as before.

## 5. Safety
- `targets` and `bind` never run subprocesses and never touch the network.
- Ids are validated before they are stored, because wave B will pass them to argv lists.
- `hook` exits 0 on every event failure (events); the human subcommands (`bind`, `unbind`, `bindings`,
  `targets`, `print-config`) keep exit 2 on usage errors.

## 6. Wave B wiring
- `cli.py`: nothing to add; `hook` already delegates to `hooks.main(argv)`, which now also dispatches `targets`.
- `up.py`: for each target with a non-null `ring`, build a doorbell entry
  `{"home","name","ring","hook_reader"}` plus `codex_thread` (ring `codex-queue`) or the claude session flag
  (ring `claude-resume`). `up.py`'s doorbell key whitelist has no `claude_session` key yet, and `doorbell.py`
  has no `muse` ring; both are wave B's to add. Targets with `ring: "muse"` must be skipped until then.
