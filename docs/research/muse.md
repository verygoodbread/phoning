# Muse Code as a phoning participant: research notes (2026-10-11)

Question: can Muse revive an old session to ask it for context, and can a
*running* Muse session be woken by phoning? Everything below was measured on throwaway sessions in an isolated
store (`XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `XDG_RUNTIME_DIR` pointed at a scratch dir, `--provider echo`,
`MUSE_NO_AUTO_UPDATE=1`, `MUSE_LOGIN=0`). No existing session was written, no login was made, no paid call was
made, and no `meta` turn was run. Muse here is **1.4.6**.

## Verdicts

| Claim | Verdict | Evidence |
|---|---|---|
| Muse can revive an **ended** session and continue it WITH its context | **YES** (mechanism proven; model recall not tested because that needs a paid `meta` turn) | below, E1-E3 |
| Revive by **name** | **PARTIAL**: `muse resume <name>` (TUI) accepts a name; `exec`/`export` accept a UUID only. The adapter resolves name to UUID itself | E4 |
| A **running** Muse session can be woken from outside (phoning doorbell) | **NO** in 1.4.6. `session-message send` refuses every non-Muse sender with `sender_unverified` | E6-E8 |
| A running Muse session can be woken by another Muse session | **UNPROVEN**: this is what `session-message` is built for; it needs a model-issued tool call, so it can't be tested with the echo provider | E8 |
| A live session can be hijacked/forked by a careless resume | **NO**: Muse holds a `flock` on the session and `exec --session-id <live>` exits 1 `session ... is already in use` | E5 |

## Facts

**Where sessions live.** Data root `${XDG_DATA_HOME:-~/.local/share}/muse` (the `initialize` reply calls it
`museHome`). Sessions: `sessions/YYYY/MM/DD/<uuid>/session.jsonl` (append-only event log), plus `.session.lock`,
`cron.db`, `cli-*.log`, `subagent/`, `approval-review/`. A view index lives in `sessions/.msp-view-v1/<uuid>/`.
`session-index.db` (SQLite, table `sessions`) caches id, name, workspace, provider, title; it is a cache, not
the source of truth (automatic names were NULL in it for throwaway sessions). Config: `${XDG_CONFIG_HOME:-~/.config}/muse`
(`settings.json`, `auth.json`, `trust.json`, `approval-policy.json`).
`muse` itself is a bash launcher (`~/.local/bin/muse`) that execs `muse-bin-<version>` and **checks for updates
and may swap the binary in the background** every hour unless `MUSE_NO_AUTO_UPDATE=1`.

**Ids and names.** Session id is a UUID (v7 when Muse picks it; any UUID with `exec --session-id`). Every session gets an
automatic two-word name (`quiet-harbor`) recorded as a `session.name.changed` line in `session.jsonl`
(`new_name`, `source: automatic|manual`). Session metadata is the `runtime.session.metadata` line:
`workspace_root`, `provider_id`, `model_id`, build. A `runtime.session.route_facts` line records the opener's `pid`.

**E1. `exec --session-id <existing>` continues that session.** Two runs on one id: the second log has
`session.opened.observed {resume:true}` then `session.resumed {prior_turn_count:1, resumed_from_sequence:73}`;
stream sequence numbers continue; `muse export` reports `turn_count` 1, then 2, then 3. (This also explains the
oddity in the brief: the two "random uuid" runs that landed in one stream were two runs on the SAME id.
In isolation every fresh id is honoured, and a repeated id resumes.)

**E2. Context is really sent to the model.** `muse trace inspect --session-log <session.jsonl> --all-runs
--render-mode verbose` prints the model input of each run. Run 1: `messages=2 ... external_history=false`.
Run 2 (same id): `messages=4 ... external_history=true`, with groups `history/external_history user (37 B)` and
`history/external_history assistant (59 B)` followed by `current_user (20 B)`. The prior user and assistant turns
travel in the request. The echo provider only echoes the last prompt, so it cannot show recall; the trace shows the
request instead.

**E3. Constraints on resuming.**
- Workspace must match: from another cwd Muse refuses `session X was created in workspace W; refusing to resume in workspace C; pass --workspace W or --allow-workspace-switch`. `--workspace W` fixes it (verified from a foreign cwd).
- Provider must match: a session created with `meta` refuses `echo` ("start a new session to switch providers"). So reviving a real old session means a `meta` turn: auth plus real cost. `exec` only offers `echo|meta`; `local` sessions cannot be resumed by `exec`.
- **A missing id silently creates a NEW empty session** (no error). The adapter must check that the session exists first, or a typo yields a context-free session that looks like success.
- Without credentials: `missing meta credentials: run muse login or set META_API_KEY ...`, rc 1.
- `--` before the prompt works, so a nudge can never be parsed as a flag. Prompt text is passed as argv; `--prompt-file` also exists.
- Default approval mode is `on-request`, sandbox on; how a headless run treats a `phoning read` bash call is **untested** (needs a model). `exec` takes `--approval-mode`, `--permission-profile`, `--yolo`; wave B should pick one deliberately.

**E4. Names.** `muse resume quiet-harbor --provider echo --trust-workspace` in a pty restored the conversation
(`resumed session 5eed0001-...`, transcript replayed). `muse export --session <name>` and `muse exec --session-id <name>`
fail (`expected a UUID`). `muse resume` is a TUI and needs a terminal; it is not a headless path.

**E5. Liveness.** Every live holder (TUI, resumed TUI, `muse serve` host) holds an exclusive `flock` on
`<session dir>/.session.lock` (the file is empty; the lock is the signal). A non-blocking shared `flock` from another
process is refused (EWOULDBLOCK) while the session is live and succeeds once it is not. The OS drops the lock if the holder is
killed, so a crash cannot leave a stale "live" state. A Muse TUI that never took a turn deletes its empty session dir on exit.
Muse enforces the same rule itself: `exec --session-id <live>` => rc 1, `session <id> is already in use`.
The runtime registry (`runtime/muse/ms-*.sock` + `.lease` with a pid) is for TUI peers and showed only stale pre-existing leases of dead pids; `muse serve` hosts do not register there.

**E6. `session-message` and the gate.** `session-message list|send` first requires the experimental flag
`MUSE_EXPERIMENTAL_EXTERNAL_AGENT_INGRESS=1` in the environment of the CALLER; without it both exit 1 with
`external_agent_ingress_closed` (this is the response in the brief). With it, `list --json` returns
`{"sessions":[{"session_id","session_name","workspace_label"}]}` for **live TUI sessions only** (empty when none; empty for a
`muse serve` host). The flag is a per-process env var, not a persisted setting, and not in the downloaded
`feature-config` gates (those list `local_session_messaging: true`, no ingress gate).

**E7. `send` resolves targets but refuses the sender.** `send --target <uuid-or-name> --json < body` resolves a live
TUI session by UUID and by name (`target_session_id` comes back), then fails `sender_unverified`, rc 1. Dead or unknown
target: `target_gone / target_not_found`. Tried: plain shell; `MUSE_SESSION_ID` set to a bogus uuid; set to a real live
session's id; and typed into a *live session's own* `!` shell (which has `MUSE_SESSION_ID`, `MUSE_THREAD_ID`,
`MUSE_TOOL_USE_ID`). All `sender_unverified`. The sender identity is bound to a model-issued tool call, so the env var is not
enough and spoofing does not work. (The adapter must not try to spoof it either.)

**E8. What the protocol implies, from the binary's strings (not exercised).** Receipts stages `target_resolved,
transport_accepted, target_admission, durable_delivery, wake_requested, processing_observed`; wake policies
`do_not_wake | wake_when_idle | wake_at_safe_point` (exec turns are recorded `wake_policy: start_once`,
`delivery_policy: session_current`); error codes `target_not_running`, `target_surface_excluded`,
`ambiguous_target_name`, `sender_unverified`, `self_send_wrong_path`. Targets include external mailbox kinds
`claude_code`, `codex`, `local-endpoint`. So the shipped design is Muse-to-Muse (and Muse-to-others), not
anyone-to-Muse. Local ingress for an outside process is "closed" by design in this build.

**E9. Other running-session levers (UNTESTED, for wave B/C).**
- Hooks: Muse has Claude-style hooks (events `SessionStart, PreToolUse, PostToolUse, PreLLMCall, Stop*, Notification, SessionEnd, ...`,
  outputs with `hookSpecificOutput`/`additionalContext`; manifest `hooks/hooks.json`). `phoning hook` may be adaptable the way it serves Claude and Codex. Needs a config-location test.
- The model has `cron_create` (session-scoped scheduled prompts; each tick is a paid turn), `monitor` (its gate was `false` in the downloaded feature-config) and background `bash`; a self-arming waker like `--arm-waker` is conceivable.
- `muse serve` (MSP over stdio, JSON-RPC lines; `initialize`, then the `initialized` notification, then `session/resume`, `turn/start`) can host and drive a session programmatically. It registered nothing for `session-message`, and it would hold the session lock for as long as it lives.
- Writing into a TUI's pty is possible only for whoever owns the pty. Not a supported path.

## What the adapter does with this

- `reachable(target)` resolves id/name, finds the session dir, reads workspace and provider, and probes the lock.
- `resume_headless` is the real wake for **ended** sessions: `muse exec --json --provider P --workspace W --session-id UUID -- NUDGE`.
- `wake` runs `session-message send` for **live** sessions. Today it returns `skip: sender_unverified` from any doorbell process. It is kept (cheap, fail-closed, correct if a later build opens ingress, and correct when phoning itself runs inside a Muse session's tool call). Wave B must treat that skip as "deliver via hooks / self-armed waker instead".
- Fail closed: lock unreadable, lock file missing, session unresolvable, ambiguous name, unsupported provider, missing workspace: no action.

## Open risks

1. Real recall by a `meta` model of an old session is unproven (needs one paid turn and a login; cross-version 1.3.0 sessions resumed by 1.4.6 are unproven too).
2. The `.session.lock`-is-a-flock behaviour, the on-disk layout, and the `session.name.changed` / `runtime.session.metadata` records are internals of a fast-moving closed-source client. The adapter degrades to `skip` when it can't parse, never to action.
3. Headless approvals: a revived session may be unable to run `phoning read/send` without an approval flag.
4. Auto-update can change behaviour between calls; the adapter pins `MUSE_NO_AUTO_UPDATE=1` for its own children.
