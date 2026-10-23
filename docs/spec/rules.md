# Doorbell rules, resume guardrails and notify (build wave A, track "rules")

Extends `docs/SPEC-v0.2.md` §7.2, §7.9 and §7.10. Module: `phoning/doorbell.py` only. Stdlib only; `mypy --strict`
and `ruff` stay clean. Nothing here changes the wire envelope or any other module.

Safety invariants carried over, and they all still hold:
- Every string that wakes or prompts an agent or a person is fixed text built by this code. It never contains a sender,
  subject, body or mention list from a peer.
- Subprocesses take an argv list (never a shell), stdin `/dev/null`, and a timeout. Their output is never echoed.
- Fail closed: unknown liveness, unknown sandbox, unreadable rules or an unparsable approval all mean "don't act" or
  "use the strictest setting".

## 1. Per-target action rules (allow / approve / prohibit)

Style: ChatGPT "dots" Custom Rules. Each doorbell (one target) has a rule level per **action**.

| action   | what it covers |
|----------|----------------|
| `ring`   | `--ring codex-app-server`; `--ring codex-queue` (the queue path) |
| `resume` | `--ring claude-resume`; `--ring codex-queue --resume-dead` when the thread is dead (no `thread-writer-locks/T.lock`) |
| `notify` | `--ring notify` |

Levels: `allow` (act), `approve` (don't act; ask a human first), `prohibit` (never act).

**Flags.** `--rules '{"resume":"prohibit","ring":"allow"}'` and/or `--rules-file PATH` (a JSON object, same shape).
The file is applied first, then `--rules` overrides key by key. Unknown action keys, unknown levels, non-object JSON
or an unreadable file mean `error: ...` on stderr and exit 2, before any lock is taken.

**Defaults:** `ring=allow`, `resume=prohibit`, `notify=allow`.

**Resume opt-in.** `resume` is the only action that is not allowed out of the box. The explicit flags
`--resume-dead` and `--ring claude-resume` are themselves the opt-in: when either is present and the rules do not
name `resume`, the effective `resume` level is `allow`. A `resume` key in `--rules` or `--rules-file` always wins, so
`--ring claude-resume --rules '{"resume":"prohibit"}'` never resumes. (Without the flags nothing ever resumed,
so the default `prohibit` only bites when rules are stated; existing wiring and tests that pass the flags keep working.)

**Resume that is not permitted.** For `--ring codex-queue --resume-dead` on a dead thread, when `resume` is
`prohibit`, or `approve`-pending, or blocked by a guardrail (§2), nothing is spawned for the resume. The doorbell then
treats the event as an ordinary `ring` (the durable queue, which is what happens without `--resume-dead`), subject to the
`ring` level. `--ring claude-resume` has no fallback: it skips.

### Outcomes and state (one stderr line per event, in this exact form)

| situation | stderr line | `pending` | `last_ring` |
|-----------|-------------|-----------|-------------|
| level `prohibit` | `skip: prohibited: <action>` | cleared | unchanged |
| level `approve` | `needs approval: <action> (id <ID>)` | cleared | unchanged |
| resume blocked by a guardrail | `skip: resume rate-limited: <reason>` | kept (retry when due) | set to now |
| any action performed | the existing result line (`rang`, `rang: queued`, `resumed: rc=N`, `ring failed: ...`, `skip: ...`) | existing rules | existing rules |

When a resume is blocked and falls back to the ring, both lines appear: the skip line first, then the ring's line.

### Approvals

File: `<user_dir>/doorbell-approvals.jsonl`, created 0600, append-only, one JSON object per line. The directory is
`<user_dir>` (the same one as `doorbell-state.json`). Two line shapes:

```
{"id": "ab12cd34", "ts": 1760000000.0, "status": "pending", "action": "resume",
 "target": {"ring": "claude-resume", "home": "/abs/home", "hook_reader": "R", "codex_home": null,
            "codex_thread": null, "claude_session": "SID", "claude_home": null,
            "claude_token_file": null, "resume_dead": false, "max_sandbox": "danger-full-access", "count": 1}}
{"id": "ab12cd34", "ts": 1760000100.0, "status": "approved" | "denied"}
```

- `id` is 8 lowercase hex characters (`uuid.uuid4().hex[:8]`).
- The latest line per `id` is its status. Malformed lines are skipped when reading.
- `target` holds only values that came from the doorbell's own flags and the local principal name: no peer text,
  no sender, no subject, no token value (only the token file *path*).
- **Dedupe:** if a `pending` record with the same `action` and the same `target` (ignoring `count`) already exists, no second
  record is written; the existing id is logged again.
- `count` is informational (the pending actionable count when the record was written, `1` minimum). `--approve` for `notify`
  uses the live `state["pending_count"]` (minimum 1), not this number.

### `phoning doorbell NAME --approve ID` and `--deny ID`

Both are part of `doorbell.main`. They do **not** take `doorbell.lock`, so they work while the doorbell runs.
They need `<user_dir>/mailbox/log.jsonl` (else exit 2). Other flags such as `--codex-bin`, `--claude-bin`,
`--notify-cmd`, `--resume-min-interval` and `--max-resumes-per-day` are honoured; the *target* (ring kind, thread,
session, homes, reader) comes only from the approval record.

- `--deny ID`: append a `denied` line; exit 0. Unknown id, or one that is not `pending`: exit 2.
- `--approve ID`: unknown id, or one that is not `pending` (already approved or denied): exit 2, nothing runs.
  Otherwise re-validate the record (thread and session ids must match `^[A-Za-z0-9._:-]{1,128}$`, action must be one of the
  three, `target.ring` must be a known kind, else exit 2), then perform the action **once**, exactly as the doorbell would
  have if the level were `allow`:
  - it ignores `--min-interval` (a human click is not a flood) but **does** honour the resume guardrails (§2);
  - it sets `last_ring`, and records the resume in `resumes` when a resume child is spawned (§2);
  - the result line is printed to stderr;
  - the approval is consumed (an `approved` line appended, exit 0) if and only if the action was actually spawned or sent:
    results `rang`, `rang: queued`, `resumed: rc=N` (any N) and a notify that ran. Results starting `skip:` or
    `ring failed:` (and a resume refused by a guardrail) leave the record `pending` and exit 1.
  - for a `codex-queue` resume whose thread is no longer dead, the result is `skip: thread no longer dead` (exit 1, still pending).

State file access (`doorbell-state.json`) by the approver and by a running doorbell must not lose each other's updates:
the doorbell re-reads `doorbell-state.json` at the start of every pass, and every read-modify-write holds
`flock(LOCK_EX)` on `<user_dir>/doorbell-state.lock` (0600) for the short read-modify-write only, never across a child process.

## 2. Resume guardrails

State lives in `doorbell-state.json`, which gains `"resumes": [float, ...]` (spawn timestamps) and
`"pending_count": int` (§5). Both are optional on load (missing means `[]` and `0`; garbage is dropped).

- `--resume-min-interval SECS` (default `600`): at least this long since the last recorded resume for this target.
- `--max-resumes-per-day N` (default `6`): at most N resumes in any rolling 24 h window. Entries older than 24 h are pruned on load.
- **Shared limit.** `state.last_ring` is shared by every action: a resume sets it exactly as a ring does, so `--min-interval`
  gates ring and resume together and the two paths can never both fire for one interval.
- **What counts as a resume.** A resume is recorded at the moment the resume child is about to be spawned (before
  waiting on it), so a crash or timeout still counts. A `skip:` (live session, no session file, token not 0600, locked
  thread) never counts. The ring functions take an optional keyword `on_spawn: Callable[[], None] | None = None` that they
  call immediately before spawning the resume child; the doorbell passes a callback that appends `time.time()` to
  `state["resumes"]` and saves the state at once.
- Guard check order for a resume: level (§1), then the guardrails. Guardrail reasons: `min interval 600s` and `daily cap 6`
  (with the configured numbers).
- Pure helper `resume_blocked(resumes, now, min_interval, max_per_day) -> str | None` returns `None` when a resume may go
  ahead, else the reason string.

## 3. Sandbox inheritance for `codex exec resume`

Applies to the headless resume child only (the `exec ... resume` argv). The queue path spawns nothing sandboxed.

1. Find the thread's newest rollout (same lookup as `thread_cwd`: `<codex-home>/sessions/**/rollout-*-<T>.jsonl`, newest by
   mtime). Scan it for the **last** line whose `type` is `turn_context` and read `payload.sandbox_policy` and
   `payload.approval_policy`. `sandbox_policy` is `{"type": "read-only" | "workspace-write" | "danger-full-access", ...}`
   (a bare string with one of those values is also accepted). A large rollout must not be slurped whole: read from the end
   in chunks, at most 8 MiB, falling back to unknown.
2. Recognised sandbox modes, strictest first: `read-only`, `workspace-write`, `danger-full-access`. Recognised approval
   policies: `untrusted`, `on-failure`, `on-request`, `never`.
3. Pass exactly the recorded pair, **clamped** by `--resume-max-sandbox MODE` (default `danger-full-access`, i.e. no clamp): the
   sandbox is `min(recorded, cap)` in the order above. Never pass anything looser than the recorded mode. Never pass
   `--dangerously-bypass-approvals-and-sandbox`, `--full-auto` or `--yolo`.
4. If either value is missing or unrecognised (no rollout, no `turn_context`, malformed JSON, a mode not in the list, e.g.
   `external-sandbox`): use `read-only` and `never`, and log `resume: sandbox unknown, using read-only` on stderr.
5. **Where the flags go.** `codex exec resume` rejects `-s` (only `exec` accepts it, and it must precede `resume`, which would move
   the pinned positions in the existing argv contract `exec --skip-git-repo-check -C CWD resume T NUDGE`). `-s` is sugar for the config key
   `sandbox_mode`, so append the pair **after the nudge**:
   ```
   [codex, "exec", "--skip-git-repo-check", "-C", CWD, "resume", T, NUDGE,
    "-c", 'sandbox_mode="<mode>"', "-c", 'approval_policy="<policy>"']
   ```
   (verified against codex-cli 0.162.1: options after the positionals parse). The existing positions `argv[0..6]` do not change.
6. Pure helper `resume_policy_args(codex_home, thread_id, max_sandbox="danger-full-access") -> tuple[list[str], bool]` returns the
   `-c` argv tail and whether the policy was known. `ring_codex_queue` takes a keyword `max_sandbox: str = "danger-full-access"`
   and logs the "unknown" line itself.

Known consequence (document, don't fix): a read-only/`never` resume cannot write the reader cursor, so `phoning read` may fail
in the child; the hook-delivered mail still arrives. This is the price of "never looser".

## 4. @mention-aware actionability

`is_actionable(rec, types, own_eids, name: str | None = None)` (a new optional fourth parameter; existing 3-argument calls keep today's behaviour).

- **Direct record** (no effective room; `_effective_room(rec) is None`): unchanged. `env.type in types`, or `env.reply_to` is one of our own eids.
- **Room record**: actionable if and only if
  - `name` is given and `env.data.mentions` is a list containing the string `name` (exact match), **or**
  - `env.reply_to` is one of our own outbound eids.
  The `types` list does not apply to room records. A room `ask` that mentions nobody is not actionable.
- `env.data.mentions` of any other shape (missing, a string, a dict, a list with non-strings) contributes nothing. Non-string entries in a list are ignored.
- `_process_batch` passes the doorbell's NAME. Peer text, the mention list and the room name are never put in a nudge, notification or approval.

## 5. `notify` ring kind (HUMAN principal's mailbox)

`--ring notify` (a new choice). `--notify-cmd PATH` (default `shutil.which("osascript")`; tests pass a fake binary).

- Text is fixed: `phoning: N actionable message(s) for NAME`. N is `state["pending_count"]`: the number of actionable records
  seen since the last notification (counted per record, accumulated across coalesced batches; reset to 0 when the
  notification is sent, by the doorbell or by `--approve`, and when `pending` is cleared by `prohibit`; the `approve` level keeps it). Never the sender, subject, body, room or mention list.
  Helper: `build_notify_text(name: str, count: int) -> str`.
- Argv is built so no text is ever interpolated into AppleScript source:
  ```
  [cmd, "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "phoning"', "-e", "end run", "--", TEXT]
  ```
  stdin `/dev/null`, a 10 s timeout, output discarded. `NAME` appears only inside `TEXT`, which is a single argv item.
  Helper: `ring_notify(name, count, notify_cmd=None) -> str`.
- Results: `rang: notified` on exit 0; `ring failed: rc=N`; `ring failed: no notify command` (nothing found); `ring failed: timed out`;
  any exception is `ring failed: <one line>`. Failures keep `pending` and `pending_count` set (like any failed ring).
- Subject to `--min-interval` and `last_ring` like every ring, and to the `notify` rule.
- `--types` and §4 decide what is actionable, exactly as for any other ring kind.

## 6. CLI summary (new flags on `phoning doorbell`)

```
--ring none|codex-app-server|codex-queue|claude-resume|notify
--rules JSON            --rules-file PATH
--resume-min-interval SECS (600)      --max-resumes-per-day N (6)      --resume-max-sandbox MODE (danger-full-access)
--notify-cmd PATH
--approve ID            --deny ID
```
`--approve`/`--deny` are mutually exclusive with each other (exit 2) and need no `--ring`.
Invalid numbers (negative interval, `--max-resumes-per-day` < 0) exit 2.

## 7. Public helper API (what the unit tests pin)

```
RULE_ACTIONS = ("ring", "resume", "notify")      RULE_LEVELS = ("allow", "approve", "prohibit")
DEFAULT_RULES = {"ring": "allow", "resume": "prohibit", "notify": "allow"}
parse_rules(rules, rules_file=None, *, resume_opt_in=False) -> dict[str, str]     # ValueError on bad input
is_actionable(rec, types, own_eids, name=None) -> bool
resume_blocked(resumes, now, min_interval, max_per_day) -> str | None
resume_policy_args(codex_home, thread_id, max_sandbox="danger-full-access") -> tuple[list[str], bool]
build_notify_text(name, count) -> str
ring_notify(name, count, notify_cmd=None) -> str
approvals_path(user_dir) -> str
request_approval(user_dir, action, target) -> str            # returns the id (existing id on dedupe)
load_approvals(user_dir) -> dict[str, dict[str, Any]]        # id -> {"id","ts","action","target","status"}
decide_approval(user_dir, approval_id, status) -> None       # appends the approved|denied line
```
All of them are exported in `__all__`.

## 8. What wave B wires (not done here)

- `phoning/up.py` `_load_doorbell` / `_doorbell_argv`: new optional keys `rules` (object, rendered as `--rules '<json>'`), `rules_file`,
  `resume_min_interval`, `max_resumes_per_day`, `resume_max_sandbox`, `notify_cmd`; `ring` additionally accepts `"notify"`.
- `phoning/cli.py`: `doorbell` already delegates to `doorbell.main`; only the help text/usage mentions need the new flags.
- `docs/OPERATIONS.md` §2.3: document rules, approvals and notify.
