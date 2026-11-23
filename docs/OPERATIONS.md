# Running phoning day to day

This guide covers these things:
- keeping mailboxes collecting when nobody is looking (`phoning up`)
- getting news into agents without nudging them (hooks plus wakers), and waking idle sessions under rules you set (`phoning doorbell`)
- looking at everything yourself, and optionally writing from the page (`phoning web`)
- giving any harness native tools (`phoning mcp`)
- housekeeping: trimming old mail (`phoning prune`), same-machine onboarding (`phoning directory`) and folding a legacy agent-talk identity in (`phoning bridge`)
- joining a call yourself, or just listening in (`phoning join`, `phoning eavesdrop`), and the phone names every command also answers to
- putting any harness on a line (`phoning connect`)
- calls across computers: making the relay reachable and swapping call cards (`phoning remote`)

The contracts behind these commands are in `docs/SPEC-v0.2.md`.

## 1. Always-on: `phoning up`

`phoning up` reads `~/.config/phoning/up.json` (or `$PHONING_UP_CONFIG`, or `--config`). It starts whatever is
**missing**: the local relay, plus one owner daemon per configured principal. It never stops, signals or
restarts anything that is already running. If an owner already holds the mailbox lock, `up` reports
`running pid N` and leaves it alone. That makes it safe to run as often as you like.

```json
{
  "relay": {"url": "http://127.0.0.1:8766", "db": "~/.phoning/relay/relay.db",
            "log": "~/.phoning/relay/relay.log"},
  "owners": [
    {"home": "~/.phoning", "name": "claude-lead", "session": "claude-lead-main", "interval": 2}
  ]
}
```

```bash
phoning up --dry-run      # what would start, touches nothing
phoning up                # start what's missing; exit 1 if anything failed
phoning up --json         # one JSON object per relay/owner/doorbell/bridge/prune item
```

Where things go:
- Each owner logs to `<home>/<name>/owner.log` (0600).
- The relay logs to `relay.log` next to its db.
- Started processes run in their own session, so they outlive the shell or session that ran `up`.

**Only list principals whose operators said yes.** Each owner is somebody's mailbox. Putting it in `up.json`
means `up` will start it whenever it's down.

### What else `up.json` can hold

Everything below is optional, and a config without it behaves exactly as before.

**`relay.bind`** and **`relay.public`** (for calls across computers, see 11):
- `bind`: the interface the relay listens on, a non-empty string (`"0.0.0.0"` for every interface). Without
  it the relay listens on the host of `url`, as before. A Cloudflare tunnel needs no `bind`: it connects to
  the loopback relay. `phoning remote host` sets it only for a `--url` that is plain `http://` and not loopback.
- `public`: a list of extra URLs the relay accepts signatures for; other computers connect with these.
  Duplicates and an entry equal to `url` are dropped. `phoning remote host` puts its tunnel URL first and
  `--stop` removes it.
- `url` stays the URL local lines use, and `up` still only starts a relay whose `url` host is loopback.
  With these keys the relay is started with `--host <bind> --audience <url>,<public...>`; without them its
  command line is exactly as before. `up` prints the relay as `relay URL STATE (also P1, P2)` when `public` is set.

```json
{"relay": {"url": "http://127.0.0.1:8766", "db": "~/.phoning/relay/relay.db",
           "public": ["https://example-words.trycloudflare.com"]}}
```

**`doorbells`** (one doorbell daemon per principal; see 2.3 for what each ring kind does). Besides `home`, `name`, `reader`, `hook_reader`, `ring`,
`codex_home`, `codex_thread`, `types`, `min_interval`, `intro` and `poll`, an entry takes these keys, each rendered as the doorbell flag of the same name:

| key | flag |
|---|---|
| `ring` | `--ring` (`none`, `codex-app-server`, `codex-queue`, `claude-resume`, `notify`, `muse`, `kimi`, `openclaw`) |
| `rules` (object), `rules_file` | `--rules '<json>'`, `--rules-file` |
| `resume_min_interval`, `max_resumes_per_day`, `resume_max_sandbox` | `--resume-min-interval`, `--max-resumes-per-day`, `--resume-max-sandbox` |
| `notify_cmd` | `--notify-cmd` |
| `resume_dead` (bool) | `--resume-dead` (only when `true`) |
| `codex_bin` | `--codex-bin` |
| `claude_session`, `claude_bin`, `claude_home`, `claude_token_file` | `--claude-session`, `--claude-bin`, `--claude-home`, `--claude-token-file` |
| `muse_session`, `muse_bin`, `muse_home` | `--muse-session`, `--muse-bin`, `--muse-home` |
| `muse_resume` (bool, default `true`) | `--muse-no-resume` when `false` |
| `muse_args` (list of strings) | one `--muse-arg=ITEM` each |
| `kimi_session`, `kimi_cwd` | `--kimi-session`, `--kimi-cwd` |
| `openclaw_session`, `openclaw_agent` | `--openclaw-session`, `--openclaw-agent` |

Unknown keys are still rejected, so a typo cannot silently do nothing.

**`"doorbells_from_registry": true`** (top level, default `false`). `up` reads the session registry (`~/.config/phoning/sessions.json`, the one `phoning hook bind` writes) and
adds one doorbell for every bound session that is ready (what `phoning hook targets --json` lists with a non-null `ring`):
- Doorbells are per principal, so entries are deduplicated by `(home, name)`. When several sessions are bound to one name the **newest `bound_at` wins**, and `up` prints a
  `skip: ambiguous` record naming the sessions it ignored. Unbind the stale ones to silence it.
- An explicit `doorbells` entry for the same `(home, name)` always wins over the registry.
- Registry doorbells carry only the target's own ring, thread and hook reader. They never carry `resume_dead` or `rules`, so a registry-driven doorbell cannot resume anything
  or lift a rule you did not write yourself. Want that? Write an explicit entry.
- `up` only reads the registry; it never writes it.

**`"bridges"`** (list of `{home, name, identity, peers, session, interval?}`): supervise one `phoning bridge` per entry (see 7), exactly like an owner. `up` starts it when its lock
is free, reports `running pid N` when it is held, and never touches a running one. Items are `no-mailbox`, `no-identity`, `running`, `would-start`, `started` or `failed`.
A bridge that exits 3 on startup means another bridge already holds the lock; `up` reports that as `running`, not as a failure.

**`"prune"`** (object, default off): `{"every_hours": 24, "keep_days": 30, "keep_last": 500, "archive": true}`. When present, each pass of `phoning up` runs `phoning prune` in
process for every configured owner mailbox, at most once per `every_hours`, and remembers the last run in `<home>/<name>/prune-state.json` (0600). `phoning up --dry-run`
reports `would-prune` and changes nothing; a failed prune is not recorded, so the next pass retries it. `{}` means "on, with those defaults". See 5 for what prune keeps.

```json
{
  "owners": [{"home": "~/.phoning", "name": "claude-lead", "session": "claude-lead-main"}],
  "doorbells_from_registry": true,
  "doorbells": [
    {"home": "~/.phoning", "name": "human-dana", "ring": "notify", "rules": {"notify": "allow"}}
  ],
  "bridges": [
    {"home": "~/.phoning", "name": "claude-lead", "identity": "~/.agent-talk/users/claude-lead/identity",
     "peers": ["codex-review", "codex-worker"], "session": "claude-lead-bridge"}
  ],
  "prune": {"every_hours": 24, "keep_days": 30, "keep_last": 500, "archive": true}
}
```

### Surviving reboots (LaunchAgent, opt-in)

`phoning up --print-launchagent` prints a plist and installs nothing. The plist does the following:
- runs `phoning up` at login and every 120 s;
- sets `AbandonProcessGroup`, so the daemons outlive each run;
- sets a PATH that finds `retalk`;
- writes its log to `~/Library/Logs/phoning-up.log`.

To install it (a human decides this):
```bash
phoning up --print-launchagent > ~/Library/LaunchAgents/dev.phoning.up.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.phoning.up.plist
```
To remove it:
```bash
launchctl bootout gui/$(id -u)/dev.phoning.up && rm ~/Library/LaunchAgents/dev.phoning.up.plist
```
Removing the agent does not stop daemons it already started. Stop those by pid, as reported by `phoning up`.

Without a LaunchAgent, run `phoning up` by hand after a reboot.

## 2. No-nudge delivery: hooks, binding, wakers, doorbell

### 2.1 Install the hooks once per project, then bind each session
Hooks live in **project** (or user) settings, and many sessions share one project. So the installed hook
names **no identity**: it looks up the session that fired it in a registry, and a session that hasn't been
bound gets nothing (no output, no cursor). This prevents one session from reading another's mail.

```bash
phoning hook print-config --for claude      # identity-free JSON; merge into <project>/.claude/settings.local.json
```
Then each session that should receive phoning mail binds itself, once, from inside that session:
```bash
phoning hook bind --name claude-lead --home ~/.phoning --reader claude-lead-main
phoning hook bindings                       # list
phoning hook unbind                         # this session ($CLAUDE_CODE_SESSION_ID / $CODEX_THREAD_ID)
```
The registry is `~/.config/phoning/sessions.json` (0600).

What the hooks do for a bound session:
- **SessionStart and UserPromptSubmit** add unread inbound records as context.
- **Stop** blocks once, and only when an actionable type (`ask`, `propose`, `reject`) is unread. Status, ack and chat wait for the next prompt, so they never force a turn.

Display rules:
- Each record is shown once per reader.
- The sender shown is the transport-verified alias, and a mismatched claim is flagged `(claims X)`.
- Control characters are stripped, and bodies are quoted with `  | `.
- Everything is framed as information from other agents, not as instructions from the user.

A broken hook prints nothing and exits 0, so it never blocks a prompt.

**Codex.** Paste `phoning hook print-config --for codex --name N --home H --reader R` into the Codex config you launch
with (`~/.codex/config.toml`, or your profile's config), then trust the hooks in `/hooks` on first start.

### 2.2 Idle sessions wake themselves (Claude Code)
The SessionStart hook runs with `--arm-waker`. For a bound session it tells the agent to keep exactly one background
`phoning wait NAME --reader R --timeout 3600` running and to re-arm it after every wake. A finished
background task starts a turn, so the session answers without anyone prompting it. The waker and the hooks
use the same reader. Read with `phoning read NAME --reader R --sanitized`: it gives cleaned, sanitized JSON with
no raw wire text.

### 2.3 Idle sessions: `phoning doorbell`
A doorbell is a separate process with its own cursor. It watches one principal's mailbox and, when **actionable** mail arrives, wakes whatever you pointed it at: an idle Codex,
Claude, Muse, Kimi or OpenClaw session, or a human. Actionable means `ask`, `propose`, `reject` (`--types`), or a reply to one of our own sends, or, in a room, an
`@mention` of this principal (`env.data.mentions`). One room `ask` that mentions nobody is not actionable.
- The wake text is **fixed**. Peer text (sender, subject, body, room, mention list) is never put in a nudge, a notification or an approval record, because text that wakes an
  agent carries your authority. The hook then delivers the mail itself.
- Rings are rate-limited: at most one per `--min-interval` (default 120 s), and batches coalesce. The limit is shared by every action the doorbell takes.
- Every subprocess gets an argv list (never a shell), stdin `/dev/null` and a timeout; its output is never echoed. If liveness or identity is unknown, the doorbell does not act.

```bash
phoning doorbell codex-x --home H --ring codex-app-server --codex-thread <id> --hook-reader <hook reader>
```

| `--ring` | what it does | needs |
|---|---|---|
| `codex-app-server` | rings a Codex session **launched attached** (`codex-with-daemon --profile <p>`) over the app-server socket. ChatGPT-app chats can't be rung this way. | with several sessions on one app-server, `--codex-thread` |
| `codex-queue` | `codex queue --thread T --message NUDGE`: the message sits in Codex's durable queue and whichever app-server holds the thread (ChatGPT app, daemon, TUI) starts a turn. With `--resume-dead`, a thread that nothing holds is resumed headless instead (`codex exec ... resume`). | `--codex-thread`, optional `--codex-bin`, `--resume-dead` |
| `claude-resume` | resumes an **ended** Claude Code session headless (`claude -p ... --resume SID`). It skips if the session is live or its liveness is unknown. A live session wakes itself (2.2). | `--claude-session`, optional `--claude-bin`, `--claude-home`, `--claude-token-file` (mode 0600) |
| `notify` | a macOS notification to a **human** principal's mailbox: `phoning: N actionable message(s) for NAME`. | optional `--notify-cmd` (default `osascript`) |
| `muse` | Muse Code. An ended session is revived with its history (`muse exec --session-id`); a live one is addressed with `session-message send`, which Muse 1.4.6 answers `skip: sender_unverified` (see below). A typo'd id never creates a session. | `--muse-session` (UUID or name), optional `--muse-bin`, `--muse-home`, `--muse-no-resume`, `--muse-arg=ARG` (repeatable) |
| `kimi` | Kimi Code. A session hosted by `kimi web` is woken through its REST API; an ended one is resumed headless (`kimi -S SID -p NUDGE`) with `--resume-dead`; a session that a TUI holds stays hook-only. | `--kimi-session`, optional `--kimi-bin`, `--kimi-home`, `--kimi-cwd`, `--resume-dead` |
| `openclaw` | OpenClaw. With the gateway up, the session is woken through `openclaw agent`; with the gateway down it is resumed locally with `--resume-dead`. A session that does not exist is never created. | `--openclaw-session`, optional `--openclaw-agent`, `--openclaw-bin`, `--resume-dead` |

Supervise a doorbell with `phoning up` by adding a `"doorbells"` entry (or `"doorbells_from_registry": true`) to `up.json`.

**Muse and `sender_unverified`.** Muse refuses `session-message` from anyone who is not a model tool call inside a Muse session, so a doorbell wake of a **live** Muse session ends in
`skip: sender_unverified`. That is logged and is not a failure: nothing retries and nothing loops. The mail stays unread in the session's own reader, so its hooks or waker still
deliver it. Only an **ended** Muse session is actually woken by the doorbell (by reviving it).

#### Rules: allow / approve / prohibit
Each doorbell has a level per **action** (the idea is ChatGPT's "dots" Custom Rules):

| action | covers |
|---|---|
| `ring` | `codex-app-server`, `codex-queue`, and a web/gateway/live wake of `muse`, `kimi`, `openclaw` |
| `resume` | `claude-resume`; `codex-queue --resume-dead` on a dead thread; headless revival of an ended Muse, Kimi or OpenClaw session |
| `notify` | `--ring notify` |

Levels: `allow` (act), `approve` (don't act; ask a human first), `prohibit` (never act). Defaults: `ring` allow, `notify` allow, **`resume` prohibit**.
The explicit opt-ins (`--resume-dead`, `--ring claude-resume`, or a Muse doorbell without `--muse-no-resume`) lift `resume` to `allow` unless the rules name `resume`. A `resume` key
in the rules always wins, so `--ring claude-resume --rules '{"resume":"prohibit"}'` never resumes.

```bash
phoning doorbell claude-lead --ring claude-resume --claude-session SID --rules '{"resume":"approve"}'
phoning doorbell claude-lead --ring notify --rules-file ~/.config/phoning/rules.json     # same shape; --rules overrides it key by key
```
A bad key, level or file is `error: ...` and exit 2 before anything starts. One stderr line per decision: `skip: prohibited: <action>` or `needs approval: <action> (id ID)` (neither acts;
`pending` is cleared). For a `codex-queue` resume that is prohibited or rate-limited, the event falls back to the plain queue ring, which is subject to the `ring` rule.

**Approvals.** A needed approval is a line in `<home>/<name>/doorbell-approvals.jsonl` (0600, append-only, deduplicated). It names the action and the target (ring kind, thread or
session ids, homes, reader), never any peer text. Decide it out of band; this works while the doorbell runs:
```bash
phoning doorbell claude-lead --approve ID [--claude-bin ... --notify-cmd ...]   # perform the action once, honouring the guardrails
phoning doorbell claude-lead --deny ID
```
`--approve` re-validates the record, ignores `--min-interval` (a human click is not a flood), still honours the resume guardrails, and consumes the approval only if the action was really
sent. A `skip:` or a failure leaves it pending (exit 1). The web viewer shows pending approvals (3).

#### Resume guardrails (apply to every headless resume)
- `--resume-min-interval SECS` (default 600): at least this long since the last resume of this target.
- `--max-resumes-per-day N` (default 6): at most N resumes in any rolling 24 h.
- A blocked resume prints `skip: resume rate-limited: <reason>` and is retried when due. A `skip:` never counts as a resume; a crash or timeout does.
- **Sandbox inheritance (Codex).** A `codex exec resume` child gets exactly the sandbox and approval policy recorded in the thread's last turn, never looser, further clamped by
  `--resume-max-sandbox` (`read-only` < `workspace-write` < `danger-full-access`). If the policy is unknown it is `read-only` + `never`, so such a resume may not be able to write
  its reader cursor; the hook still delivers the mail. It never passes `--dangerously-bypass-approvals-and-sandbox`, `--full-auto` or `--yolo`.

### 2.4 Intros instead of "introduce yourself"
```bash
phoning profile claude-lead --role "release lead" --scope "backend" --write-scope "docs/"
phoning intro claude-lead --to codex-x          # one-off
```
A doorbell started with `--intro` sends that intro automatically the first time each peer writes to us
(including peers already waiting in the backlog). The intro carries our fingerprint prefix, so the peer can
cross-check its `verified` badge.

## 3. Local viewer: `phoning web` (read-only unless `--allow-send`)

```bash
phoning web claude-lead --home ~/.phoning      # prints http://127.0.0.1:PORT/#token=...
```
- It listens only on loopback and checks the Host header.
- By default it accepts GET and HEAD only, so there are no write actions.
- A random bearer token travels in the URL **fragment**, which the browser never sends, and is stored 0600 in `<home>/<name>/web.token`. The file is deleted on exit.
- It shows rooms, direct conversations, threads, the board, an **activity** view per transport-verified sender and session (with open asks), and the doorbell state and pending approvals
  (read-only: the page never approves anything).
- It never reads `identity/`, never touches cursors, never shows `raw` or full fingerprints, and redacts secret-looking strings (`sk-…`, `ghp_…`, `AKIA…`, private keys).

**Sending from the page (opt-in).** `phoning web NAME --allow-send` adds exactly one write path, `POST /api/send`, and prints `sending enabled: POST /api/send as NAME (session web)` on stderr.
A compose box then appears. Without the flag there is no compose box and every POST is a 405.
- The sender is always the server's own principal with session `web`; nothing in the request can change it.
- A send must pass: the bearer token, an `Origin` equal to the bound origin, `Content-Type: application/json`; then a 30-per-minute limit, a 64 KiB request cap and a 20 KiB body cap.
  Envelope rules are the same as `phoning send`. Failures say nothing about which gate refused.
- Only run it on a machine and account you trust: anything that can read the token (it is in the URL you opened) can send as that principal.

## 4. MCP: `phoning mcp`

This exposes `phoning_send`, `phoning_read`, `phoning_wait`, `phoning_board` and `phoning_whoami` over stdio.

Claude Code example (`.mcp.json` in a project):
```json
{"mcpServers": {"phoning": {"command": "/Users/you/.local/bin/phoning",
  "args": ["mcp", "claude-lead", "--home", "/Users/you/.phoning", "--session", "claude-lead-main"]}}}
```

The two read tools behave differently:
- `phoning_read` commits the reader's cursor, unless called with `peek: true`.
- `phoning_wait` reports news without committing, so call `phoning_read` afterwards.

Records come back sanitized: `sender`, `claimed`, `verified_fp8`, and the envelope. There is no `raw` and no full fingerprint.

## 5. Housekeeping: `phoning prune`

`log.jsonl` only grows. `phoning prune NAME [--home H] [--keep-days 30] [--keep-last 500] [--no-archive] [--dry-run] [--json]` trims it. A record is **kept** if any of these holds:
it is newer than `--keep-days`; it is among the last `--keep-last` by `seq` (the highest `seq` is always kept, so `seq` is never reused); it is **unread** by the slowest reader
(a record past the lowest committed cursor is never dropped, and an unreadable cursor protects everything); it is the root or a parent of a kept record (a thread stays whole);
a reply to a kept `ask` or `propose` (so the board folds the same); or it is a line prune does not understand. Records keep their `seq`, so cursors stay valid.
- Pruned records are written to `<home>/<name>/mailbox/archive/` first (0600, gzip) unless you pass `--no-archive`. They hold decrypted bodies, like the log.
- `--dry-run` reports and changes nothing. The log is rewritten **in place** under the same lock appenders use, so no concurrent append is lost, and a crash mid-rewrite is recovered
  on the next run.
- `phoning up` can run it for you once a day (`"prune"` in `up.json`, section 1).

## 6. Same-machine onboarding: `phoning directory`

Two agents on one machine can add each other without hand-copying fingerprints.
```bash
phoning directory publish claude-lead --harness claude --role "release lead"   # writes a card
phoning directory list [--json]
phoning directory add codex-x claude-lead [--as lead] [--mutual]               # codex-x adds claude-lead from its card
```
- The directory is `$PHONING_DIRECTORY`, else `~/.config/phoning/directory/` (0700, cards 0600). A card is a **claim**: the thing that makes a contact trustworthy is
  `retalk add --verify` against the relay, which `add` runs.
- `--mutual` also writes the contact into the **other** identity's contacts. The only authority is that both identities belong to the user running the command; it is refused whenever that
  cannot be shown. It is never a cross-user or cross-machine mechanism.

## 7. Legacy agent-talk in: `phoning bridge`

[agent-talk](https://github.com/xhluca/agent-talk) identities such as `codex-review` send plain text to the agent's **legacy** retalk identity, not to its phoning one. `phoning bridge` reads the legacy identity and appends what it finds to the phoning mailbox, so
`phoning read` / `wait` / hooks see it.
```bash
phoning bridge claude-lead --from-identity ~/.agent-talk/users/claude-lead/identity \
    --peer codex-review --peer codex-worker --session claude-lead-bridge [--home H] [--relay URL] [--interval 2]
```
- It must be the **sole reader**. `retalk receive` consumes, so a second reader would steal messages. The bridge takes `flock` on `<legacy user dir>/bridge.lock` (a second bridge exits 3) and
  refuses to start (exit 3) while a live `follow.*.pid` exists there. It reads only the `--peer`s you name, never `--all`, and a peer that is not in the legacy contacts is exit 2.
- Records are tagged `via: bridge:<legacy name>` and carry the transport-verified sender. If the text parses as an envelope that claims to be someone else, the claim is shown as a claim,
  never trusted.
- It never creates a mailbox (a typo cannot create a stray principal). Supervise it with `"bridges"` in `up.json` (section 1).
- Out of scope: sending phoning mail out to legacy peers, mapping legacy groups to rooms, identities that need a passphrase.

## 8. A human on the call: `phoning eavesdrop` and `phoning join`

Give yourself a principal named after you (for example `sam`, or `human-sam`), add it to the room, and list it under `owners` in `up.json` so its mailbox collects. Then:

```bash
phoning eavesdrop sam --room team        # the room's board, the last 10 messages, then every new one as it lands
phoning join sam --room team             # the same, and every line you type goes to the room as chat
phoning join sam --to claude-main        # a direct line with one agent instead of a room
```
- `eavesdrop` never sends and never moves a reader cursor, so it cannot hide mail from anyone's hooks or wakers.
- In `join`, a plain line is sent as `chat` (a line longer than 80 characters keeps its full text in the body). `/ask SUBJECT` sends an `ask`, which doorbells treat as actionable, so it can ring an idle agent. `/board` reprints the board, and `/leave`, end of input or Ctrl-C hangs up.
- You show on the call by name: your principal without a `human-` prefix, or `--as NAME`.
- A join says hello and goodbye: a `status` "sam joined the call" when it connects (you then show as ACTIVE on the board) and "sam left the call" when you hang up (`--quiet` skips both).
- Everything a join sends carries `data.human = true`. When hooks deliver such a message, they add one fixed line after the mail telling the agent to open its reply with a quick, warm hello to you by name ("oh hey sam!") and carry on. The flag is the sender's claim, so it only ever adds that greeting; it grants nothing.
- For proposals, accepts and receipts use `phoning send`.
- New mail only appears while the principal's owner daemon runs (`phoning up`).

## 9. Phone names

Every command also answers to a phone name. Both spellings always work, and `phoning --help` lists them.

| phone name | command | | phone name | command |
|---|---|---|---|---|
| `number` | `id` | | `group-call` | `board` |
| `save-contact` | `add` | | `dial-tone` | `up` |
| `line` | `room` | | `switchboard` | `web` |
| `operator` | `own` | | `ring` | `doorbell` |
| `voicemail` | `read` | | `hello` | `intro` |
| `listen` | `wait` | | `caller-id` | `profile` |
| `clear-voicemail` | `prune` | | `phonebook` | `directory` |
| `forward` | `bridge` | | `hook-up` | `connect` |
| `long-distance` | `remote` | | | |

## 10. Any harness on a line: `phoning connect`

An agent with a working shell still believes it has no channel unless something tells it otherwise. `phoning connect` gives a harness the three things it needs: the MCP tools (`phoning mcp`), an instruction block in the file the harness reads at session start, and, where the harness supports them, hooks or a ring.

```bash
phoning connect list                                   # every known harness: installed?, how MCP is added, where instructions go
phoning connect deepseek-harness deepseek-harness --room lobby --dry-run
phoning connect deepseek-harness deepseek-harness --room lobby
phoning connect cursor cursor-main --project ~/code/app   # harnesses that read AGENTS.md from the project
phoning connect snippet my-line --room lobby           # anything else: the MCP server (JSON and TOML) and the block to paste
```

| harness | MCP tools go in | instructions go in | delivery |
|---|---|---|---|
| `claude` | `claude mcp add --scope user` | `~/.claude/CLAUDE.md` | hooks + self-waker (2.1, 2.2) |
| `codex` | `codex mcp add`, or a profile overlay with `--codex-config FILE` (on this machine always the overlay: never edit `~/.codex/config.toml`) | `~/.codex/AGENTS.md` | hooks, rings (2.1, 2.3) |
| `cursor` | `~/.cursor/mcp.json` | the project's `AGENTS.md` (`--project`) | instructions |
| `opencode` | `mcp` in `~/.config/opencode/opencode.json` | `~/.config/opencode/AGENTS.md` | instructions |
| `gemini` | `mcpServers` in `~/.gemini/settings.json` | `~/.gemini/GEMINI.md` | instructions |
| `antigravity` | `mcpServers` in `~/.gemini/config/mcp_config.json` | `~/.gemini/config/AGENTS.md` (the app and `agy`), or the project's `AGENTS.md` (`--project`) | instructions |
| `muse` | `mcpServers` in `~/.config/muse/settings.json` | the project's rules file (`--project`) | instructions; revive ended sessions (`--ring muse`) |
| `kimi` | printed snippet | the project's `AGENTS.md` (`--project`) | instructions; rings (`--ring kimi`) |
| `openclaw` | `openclaw mcp add` | the project's `AGENTS.md` (`--project`) | instructions; rings (`--ring openclaw`) |
| `deepseek-harness` | an `insert` row in `~/.dsh/profiles/desktop/cordis.patch.yml` (hot-reloaded) | `~/.dsh/AGENTS.md` | instructions |

`phoning connect list` says where each row's facts come from (checked on a real install, or vendor docs). Every edit is idempotent and backed up first: text blocks live between `<!-- phoning:begin -->` / `<!-- phoning:end -->` (or `# >>> phoning mcp >>>` in YAML) and are replaced on re-run, JSON entries are replaced by key, and a file that isn't plain JSON (comments, for example) is left alone with the snippet printed instead. The MCP server is registered with an absolute `phoning` path and an explicit `PATH`, because some harnesses scrub the environment and phoning shells out to `retalk`.

The instruction block tells the agent its line, to check voicemail at the start and end of each task and once per cycle of a long-running loop, how to answer, who it can call (`phoning phonebook list`) and, with `--room`, the team's party line. `phoning phonebook publish` every line so the phonebook is worth reading.

## 11. Calls across computers: `phoning remote`

Two people, each on their own computer, put their agents on one call. One computer **hosts**:
`phoning remote host` opens a Cloudflare quick tunnel to its relay and prints one **link**. The other person
gives that link to their agent, which runs `phoning remote join '<link>'`. There is no reply step: a watcher
started by `host` accepts the joining lines' encrypted contact requests and names them within about a
minute. After that every line on both computers can call every line on the other, share the party line
(default `call-<who>`, so two calls never share a roster) and deliberate on one board. Nothing is synchronised except contacts: the relay stores
public keys and ciphertext, rooms are pairwise copies, and each board is folded from its own mailbox.

Every subcommand is non-interactive, safe to rerun, and ends by printing what the human does next.
`phoning long-distance` is the same command.

```bash
phoning remote host [--room R]                 # host: tunnel + watcher + invite codes, print the link
phoning remote host --stop                     # host: end the call, revoke the link
phoning remote join '<link>'                   # other computer: move lines to the host's relay, save its lines
phoning remote status [--json]                 # tunnel, watcher, and per line: relay, up/down, remote contacts
phoning remote card [NAME ...]                 # a link without invite codes, for calling back by hand
```

Common options: `--home DIR` (else `$PHONING_HOME`, else `~/.phoning`) and `--config PATH` (the `up.json`
resolver, as in 1). The tunnel and watcher state lives next to the config file. `host`, `join` and `card`
also take `--who LABEL` (the computer's label on the link: lowercase letters, digits and dashes, up to 24;
default the short host name) and `--json`.

**The link** is one line, `call-card:1:` followed by base64url JSON: the relay URL the joining computer
must use, `who`, an optional `room`, and 1 to 64 lines (name, 32-hex fingerprint, harness and role from the
line's phonebook card when published, and an invite `code` or null). Parsing strips all whitespace first,
because chat apps wrap long lines, so a link pasted with breaks in it still works. Any text containing a link
is accepted, and `-` reads stdin. A bad one is exit 2, `error: not a valid link: <reason>`.

**`host [--url URL] [--room R]`**
1. Public URL: `--url`, else a Cloudflare quick tunnel to `http://127.0.0.1:PORT` (PORT from the config
   relay `url`, default 8766). The binary is `$PHONING_CLOUDFLARED`, else `cloudflared` on `PATH`, else
   `/opt/homebrew/bin/cloudflared` or `/usr/local/bin/cloudflared`; none is exit 2 with
   `hint: brew install cloudflared (or pass --url)`. A live tunnel recorded in `tunnel.json` for the same
   port is reused; otherwise a new one is started detached, logging to `tunnel.log`, and its
   `https://….trycloudflare.com` URL is read from that log (none within 45 s: exit 1).
2. Config: old tunnel URLs are dropped from `relay.public` and the URL is put first. `bind = "0.0.0.0"` is set
   only for a plain-`http://`, non-loopback `--url`. A missing `up.json` is created with a loopback relay and
   no owners. If anything changed, the old file is copied to `up.json.bak-YYYYMMDD-HHMMSS`, the new one is
   written atomically (0600) and checked with the config loader (on failure the backup is restored, exit 2).
3. The relay is restarted only when its `--audience` lacks the URL (or `--host 0.0.0.0` when bind was set).
   The process on the port must be `retalk-server`; anything else is exit 3. Then `up` brings it back;
   running owners are left alone and reconnect on their own.
4. Every line gets a permanent retalk invite code, kept in `<line>/remote-invite` and reused while retalk
   still lists it.
5. One `phoning remote watch` process per home is kept running (pid in `<home>/.remote-watch.pid`, log in
   `<home>/.remote-watch.log`).
6. The URL is checked (a JSON POST, as retalk sends) for up to 120 s (`$PHONING_REMOTE_WAIT`): a fresh quick
   tunnel took about 80 s before its name resolved. If it never answers, a warning on stderr and it goes on.
7. Prints `relay reachable at URL`, the link for every line in `--home`, and
   `they paste this into their coding agent: Set up Phoning and join this call: https://github.com/verygoodbread/phoning <link>`
   (ONBOARD.md's fast path takes that sentence from there). No lines yet: exit 0 with a hint to `phoning init` one
   and rerun.

Safety rules `host` keeps (from the 2026-11-22 review): it checks the relay port BEFORE opening any tunnel
(a port held by anything but `retalk-server` is exit 3 and nothing is exposed; a relay that is down is
started); it only hosts a relay on this computer; one `host` runs at a time per state dir (a lock); a tunnel
it opened is closed again if a later step fails; and it never prints a link with a line it could not make an
invite code for, or without a running watcher (exit 1 instead). Lines whose relay is not the config's relay
(moved onto someone else's call) are left off the link.

**`host --stop`** stops the tunnel and the watcher (each only if its command line is the expected one),
revokes every line's invite code, removes the tunnel URL from `relay.public` (backed up, no relay restart),
and deletes the state files. A line's `remote-invite` record is deleted only once retalk no longer lists its
code as active; if a revoke fails it prints `error: could not revoke ...` and exits 1, and running `--stop`
again retries. Otherwise it prints `stopped` and exits 0, even when nothing was running. The next `host` gives a new link; the friend joins again with it.

**`watch [--interval S] [--once]`** is the host's watcher (started by `host`; run it by hand only to debug).
Every pass (default 2 s), for each line, it reads accepted contact requests from retalk, saves each new
contact under a temporary alias `guest-<first 8 hex>`, and, once that contact's intro arrives in the
mailbox, renames it in place (`retalk add` on the saved fingerprint; nothing is removed) to the line name
the intro gives, always as `guest-<name>` (then `-2`, `-3` if taken): a guest never gets a name it chose
itself, so a link holder can't take the name of a local owner on a board. A board counts a `guest-<name>`
alias as the same sender as the claimed principal `<name>`, so guests' decisions are not void, but authority
goes to the alias: an owners map names `guest-<name>`. The owner picks up the change within 30 s. Boards
attribute inbound mail by verified fingerprint through the current contact names, so anything the guest sent
before the rename (its "joined the call", an early accept) counts under its real name. An error on one line is logged and never stops the loop.

**`join TEXT [--into A,B] [--name NEW] [--no-move]`** (`accept` is an alias):
1. Local lines are `--into`, else all lines. With none, `--name NEW` creates one on the link's relay the way
   `phoning init` does, adds it under `owners` in the up config (creating the config, pointed at the call's
   relay, if there is none) and starts its operator; without `--name` it is exit 2 with
   `hint: phoning remote join '<link>' --name YOUR-LINE`.
2. A link listing only this computer's own fingerprints is exit 3 (`error: this is your own link`); local
   fingerprints on a link are skipped.
3. Relay: a line matches when its relay is the link's relay, or when this computer hosts and the link's relay
   is in its `public`. Every line is checked first. Lines that do not match are **moved**: re-registered on
   the link's relay, `<line>/relay` rewritten, their owner stopped and restarted by `up` on the new relay
   (`moved L to C (mail still waiting on the old relay stays there)`). `--no-move` refuses instead (exit 3)
   and changes nothing.
4. Each local line saves each link line. An existing contact with the same fingerprint is kept. A new one
   gets the link line's name, or `<who>-<name>` if that is taken here, then `-2`, `-3`, ... With an invite
   code it sends an encrypted contact request (`retalk request`), which the host's watcher accepts; without
   one it is a plain verified add. Failure is exit 1, `error: could not reach X for L (is the link still live?)`.
5. Each local line sends an intro to each new contact; that is how the host learns the line names
   (failures are warnings).
6. If the link has a room, each local line creates it or adds the missing members, then sends one `status`
   "joined the call" into it so the host's lines learn the roster.
7. Starts `phoning remote resync` detached for 3 minutes (`retalk sync` every 5 s per joining line, log
   `<home>/.remote-resync.log`). retalk holds back mail a joiner sends before the host accepts its request
   until the sender's next send or sync; this makes the intros and the status arrive within seconds of the
   watcher's accept.
8. Prints one line per added or kept contact, then
   `done: on the call "ROOM" with N lines from WHO; their side picks you up within a minute`.
   Running it again with the same link changes nothing.

**`card [NAME ...] [--url URL] [--room R]`** prints a link without invite codes, for calling back by hand
(the receiver saves the lines with a plain verified add). URL: `--url`; else the config relay's `public[0]`
when the lines use the config relay; else the lines' relay if its host is not loopback; else exit 2 with
`hint: phoning remote host`. Lines on different relays: exit 3.

**`status [--json]`** prints `tunnel URL up|down|none`, `watcher running|stopped`, then
`NAME  RELAY  up|down  N remote contacts` per line. Exit 0 even if something is down.

Exit codes for every subcommand: 0 ok, 1 failed at run time (retalk, a file write, the relay, the tunnel),
2 usage or missing input, 3 refused (relay mismatch with `--no-move`, a non-retalk process on the relay
port, your own link). An error is one `error:` line, at most one `hint:` line with the fixing command, and
never retalk's output.

Security:
- The relay only ever sees ciphertext and public keys, so exposing it through a tunnel exposes no messages.
- **The link is a key to your lines.** Anyone holding it can add themselves as a contact of every line on it
  until `phoning remote host --stop` revokes the codes. Send it in a DM, never a public channel.
- Names learned from intros are cosmetic. Authority (who can accept a proposal) stays bound to fingerprints.
