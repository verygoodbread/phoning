# Calls across computers: `phoning remote` (module `phoning/remote.py`) and relay `bind`/`public` (`phoning/up.py`)

Two people, each on their own computer, put their agents on one call. One of them **hosts**:
`phoning remote host` opens a Cloudflare quick tunnel to their existing relay and prints **one link**.
They paste the link to the other person, whose agent runs `phoning remote join '<link>'`. That is the
whole setup: one paste, one direction. After that every line on both machines can call every line on
the other, share a party line, and deliberate on one board.

How one paste is enough: the link carries the host lines' fingerprints and a retalk **invite code**
per line. `join` redeems each code with `retalk request` (an end-to-end-encrypted contact request);
on the host a small **watcher** runs `retalk invite watch`, which accepts it and saves the joiner as a
contact. The owner daemon only ever receives from saved contacts, so the watcher and the owner never
read the same mail.

Why this is small: the relay is untrusted by design (it stores public keys and ciphertext; every request
is signed), groups are pairwise copies with the roster inside the encrypted envelope, and the board is a
pure fold over the messages each mailbox received. Nothing has to be synchronised except contacts.

The design goal is that an agent can do the whole setup from one sentence ("call my friend"). Every
command is non-interactive, idempotent, and ends by printing exactly what the human must do next.

Stdlib only. `ruff` (line length 110) and `mypy --strict` clean. Exit codes for every `remote`
subcommand: `0` ok, `1` failed at run time (retalk, a file write, the relay), `2` usage or missing input,
`3` refused (relay mismatch, a non-retalk process on the relay port, a card naming one of our own
fingerprints only). Errors are one stderr line starting `error:`, followed by at most one line starting
`hint:` with the command that fixes it. They never include retalk's stdout/stderr bodies.

## Part A: `phoning/up.py` relay keys

The `relay` object accepts two new optional keys (add them to the allowed set):
- `bind`: a non-empty string, the interface the relay listens on (`"0.0.0.0"` for every interface).
  Anything else is `ConfigError("relay 'bind' must be a non-empty string")`.
- `public`: a list of non-empty strings, extra URLs the relay accepts signatures for (other computers
  connect with these). Not a list, or an element that is not a non-empty string, is
  `ConfigError("relay 'public' must be a list of URLs")`. Duplicates are dropped keeping first order;
  an entry equal to `url` (after stripping whitespace and trailing `/`) is dropped.

`load_config` copies them into the result only when present (`bind` as given, `public` as the cleaned
list, omitted when it ends up empty). Everything else about the relay is unchanged: `url` stays the URL
local lines use, and `up` still only *starts* a relay whose `url` host is loopback.

Spawn command when starting: `[server, "--host", bind or url_host, "--port", port, "--audience",
",".join([url, *public]), "--db", db]`. Without the new keys the argv is byte-for-byte what it is today.

The relay item gains `"public": [...]` when the config has a non-empty `public`. `_format_item` prints
`relay URL STATE (also P1, P2)` in that case and exactly as today otherwise.

## Part B: `phoning/remote.py`

`main(argv: Sequence[str] | None = None) -> int`; runnable as `python -m phoning.remote`. `phoning/cli.py`
wires `phoning remote ...` and the phone alias `phoning long-distance ...` to `remote.main(argv[1:])`
(the same pattern as `directory`), and both appear in `phoning --help`.

Common options on every subcommand: `--home DIR` (else `$PHONING_HOME`, else `~/.phoning`), `--config PATH`
(else `$PHONING_UP_CONFIG`, else `~/.config/phoning/up.json`; reuse `up`'s resolver). The **state dir** is
the directory containing the config file.

A **line** is a subdirectory `<home>/NAME` with a legal name (`directory.is_legal_name`) and an
`identity/` subdirectory. "All lines" is every such directory, sorted by name. Its relay is
`Retalk(user_dir).relay`. Relay URLs are compared with `directory.normalize_relay`. Every subprocess runs
with `stdin=subprocess.DEVNULL` and a timeout (retalk calls: 60 s).

### The link (a call card)

One line of text: `call-card:1:` + base64url (no `=` padding) of the compact JSON (sorted keys,
separators `(",", ":")`). It is called "the link" in user-facing text.

```json
{"v": 1, "relay": "https://abc-def.trycloudflare.com", "who": "sam-mbp", "room": "call",
 "lines": [{"name": "claude-lead", "fingerprint": "<32 hex>", "harness": "claude", "role": "lead",
            "code": "D6Be9b2DsiNwqVnJjSH3Mw"}]}
```

- `relay`: the URL the *joining* computer must use. Must start with `http://` or `https://`.
- `who`: a label for the sending computer, `[a-z0-9][a-z0-9-]{0,23}`.
- `room`: a legal name or `null`.
- `lines`: 1 to 64 objects; `name` legal, `fingerprint` 32 lowercase hex, `harness`/`role` string or
  `null` (from that line's directory card when one is published, else `null`), `code` a string matching
  `[A-Za-z0-9_-]{8,64}` or `null`. Names unique.

`parse_card(text) -> dict`: remove every whitespace character from the text (chat apps wrap long lines),
find the first match of `call-card:1:[A-Za-z0-9_-]+`, decode, validate. A missing card, bad base64, bad
JSON or any broken rule raises `CardError` (CLI: exit 2, `error: not a valid link: <reason>`). Unknown
extra keys are ignored; a line without `code` parses as `code: null`. `make_card(dict) -> str` is the
inverse; `parse_card(make_card(c)) == c` for every valid `c` (with `code` present on every line).

`who` default: `socket.gethostname()` up to the first `.`, lowercased, every run of characters outside
`[a-z0-9]` replaced by `-`, stripped of `-`, cut to 24; `computer` if empty. `--who LABEL` overrides
(illegal: exit 2).

`pick_alias(name, who, taken) -> str` returns `name` if not in `taken`, else `f"{who}-{name}"` cut to 64
if free, else that with `-2`, `-3`, ... appended (cutting the base so the whole stays at most 64) until free.

### `phoning remote host [--url URL] [--room R] [--who LABEL] [--json]` and `phoning remote host --stop`

Makes this computer's relay reachable and prints the link.

1. **Public URL.** `--url URL` if given. Otherwise a Cloudflare quick tunnel:
   - Binary: `$PHONING_CLOUDFLARED` if set, else `cloudflared` on `PATH`, else the first existing of
     `/opt/homebrew/bin/cloudflared`, `/usr/local/bin/cloudflared`. None: exit 2,
     `error: cloudflared is not installed`, `hint: brew install cloudflared (or pass --url)`.
   - PORT is the port of the config relay `url` (default 8766 when there is no config).
   - **Reuse:** if `<state>/tunnel.json` holds `{"pid", "url", "port"}` with `port == PORT`, the pid is
     alive and `ps -o command= -p PID` contains `cloudflared`, reuse its URL.
   - **Start:** otherwise write an empty file `<state>/cloudflared-quick.yml` (0600) and spawn
     `[bin, "tunnel", "--config", "<state>/cloudflared-quick.yml", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"]`.
     The explicit empty config matters: by default cloudflared reads `~/.cloudflared/config.yml`, whose
     ingress rules (often ending in a catch-all `http_status:404`) override `--url`, so every request would
     get Cloudflare's empty 404 and never reach the relay.
     detached (`start_new_session=True`), stdout and stderr to `<state>/tunnel.log` (truncated, 0600).
     Poll the log every 0.2 s for up to 45 s for the first match of `https://[a-z0-9-]+\.trycloudflare\.com`.
     Found: write `<state>/tunnel.json` (atomic, 0600). Not found or the process exited: kill it, exit 1,
     `error: the Cloudflare tunnel did not start; see <state>/tunnel.log`.
2. **Config.** Read the config JSON (missing: start from
   `{"relay": {"url": "http://127.0.0.1:8766", "db": "~/.phoning/relay/relay.db"}, "owners": []}`; invalid
   JSON: exit 2). In `relay.public`: drop entries containing `.trycloudflare.com` other than URL, then put
   URL first (no duplicates). Only when `--url` was given and its host is neither loopback nor a
   `https://` URL: set `relay.bind = "0.0.0.0"` (a tunnel needs no extra interface). If nothing changed,
   do not write. Otherwise back up the old file to `<path>.bak-YYYYMMDD-HHMMSS` (when it existed), write
   atomically (tmp, fsync, `os.replace`, 0600), and check with `up.load_config` (failure: restore, exit 2).
3. **Restart the relay when needed.** If the relay `url` accepts connections (`up.relay_up`), find the
   listener with `lsof -nP -iTCP:PORT -sTCP:LISTEN -t` and its command with `ps -o command= -p PID`. Not
   `retalk-server`: exit 3 (`error: port PORT is held by another program`). Its `--audience` value already
   contains URL (and `--host 0.0.0.0` when bind was set): no restart. Else SIGTERM it and wait up to 10 s
   for the port to close (still open: exit 1). Then `up.reconcile(up.load_config(path))`; a relay item
   state `failed`: exit 1. Running owners are left alone; they reconnect on their own.
4. **Invite codes.** For each line (all lines in `--home`): if `<user_dir>/remote-invite` holds a code that
   `retalk invite list --json` still lists, reuse it; else run `retalk invite new --permanent --json`, take
   `code` from the JSON line, and write it to `<user_dir>/remote-invite` (0600).
5. **Watcher.** Ensure one `remote watch` process for the home: if `<home>/.remote-watch.pid` names a live
   pid whose command contains `phoning.remote watch`, keep it; else spawn
   `[sys.executable, "-m", "phoning.remote", "watch", "--home", HOME]` detached, output to
   `<home>/.remote-watch.log` (0600), and write the pid file.
6. **Reachability check.** A fresh quick tunnel can take over a minute before requests reach the relay
   (measured: ~75 s). POST `{"tool": "info", "args": {}}` as JSON to `URL` (header `content-type:
   application/json` and a browser-like `user-agent`, as retalk sends) with a 10 s timeout, retrying every
   2 s for up to `$PHONING_REMOTE_WAIT` seconds (default 120). It counts as reached when the response body
   parses as a JSON object (whatever the HTTP status). Never reached: print `warning: URL is not answering
   yet; wait a minute before the other side joins` and continue (exit 0).
7. **Output.** Room is `--room`, default `call`. No lines: exit 0 printing
   `no lines yet: create one with "phoning init NAME --relay http://127.0.0.1:PORT", then rerun this`.
   Text: `relay reachable at URL`, then `send this link to the other computer (a DM, not a public channel):`,
   the link on its own line, then `they tell their agent: join this call <link>`.
   `--json`: `{"url": URL, "config": path, "restarted": bool, "tunnel": bool, "link": str|null}`.

`--stop`: SIGTERM the tunnel (from `tunnel.json`, only if its command contains `cloudflared`) and the
watcher (pid file, only if its command contains `phoning.remote watch`), run `retalk invite revoke CODE`
for each line's `remote-invite` and delete those files, remove the tunnel URL from `relay.public` (backup
+ atomic write as above; no relay restart), delete `tunnel.json` and the pid file. Prints `stopped`; exit 0
even when nothing was running.

### `phoning remote watch [--interval S] [--once]` (the host's watcher)

A loop (default interval 2 s; `--once` does one pass and exits 0). Each pass, for each line L:
1. **Accept.** `retalk invite watch --json` for L (one shot, not `--follow`). For every JSON line with
   `"kind": "contact_accepted"` and a legal `from` fingerprint F: the contact was saved under the
   joiner's self-chosen name, which is usually empty. Give it a temporary alias `guest-<first 8 hex of F>`:
   `retalk contacts --remove <saved name, or F when the name is empty>` then `Retalk(L).add(F, temp)`. Skip
   the rename if the saved name is already a legal name not used by another contact or local line. Record
   `{F: {"line": L, "alias": temp}}` in `<home>/.remote-pending.json` (atomic, 0600).
2. **Name.** For each pending entry of L: look through L's mailbox log (`Mailbox(user_dir)` records) for an
   inbound record with `peer_fp == F` whose envelope has a legal `from.principal` P. Found: `alias =
   pick_alias(P, "guest", taken)` (taken = L's contact aliases + local line names, minus the temp alias),
   then `retalk contacts --remove temp` and `Retalk(L).add(F, alias)`; drop the pending entry; log
   `L: F8 is now ALIAS`. The owner picks up contact changes within 30 s.
Errors on one line are logged (one line each, to stderr) and never stop the loop.

### `phoning remote join TEXT [--into A,B] [--name NEW] [--no-move] [--who LABEL] [--json]`

`TEXT` is the link or any text containing it; `-` reads stdin. `phoning remote accept ...` is an alias.
1. **Parse** (exit 2 on error). Then wait for the link's relay exactly as `host` step 6 does (same
   POST probe and `$PHONING_REMOTE_WAIT`); never reached: exit 1,
   `error: the link's relay is not answering`, `hint: ask for a fresh link (the host may have stopped the call)`.
2. **Local lines** = `--into` (comma list; unknown: exit 2) else all lines. None and `--name` given: create
   that line the way `phoning init NAME --relay CARD_RELAY` does (call `cli.init_line`, no subprocess). None
   and no `--name`: exit 2, `hint: phoning remote join '<link>' --name YOUR-LINE`.
3. **Own link.** Every card fingerprint is a local line's: exit 3 (`error: this is your own link`). Card
   lines whose fingerprint is local are skipped.
4. **Relay.** Local line L with relay R_L **matches** card relay C when `normalize(R_L) == normalize(C)`, or
   when the up config relay has `url == R_L` and C in its `public` (we are the host). Lines that do not match
   are **moved** (check every line first, then change them):
   - `--no-move`: exit 3, `error: L uses relay R_L, the link uses C`, `hint: rerun without --no-move`;
     nothing changes.
   - otherwise: `Retalk(L).register(C)` (`retalk register --dir ... -r C -np`), rewrite `<L>/relay` to C
     (atomic, 0600, like init). If `up.owner_holder(L)` names a pid, SIGTERM it and wait up to 5 s. After all
     moves, if the config file exists, `up.reconcile(up.load_config(path))` so owners restart on C. Print
     `moved L to C (mail still waiting on the old relay stays there)`.
5. **Contacts.** For each local L and each remaining card line X:
   - L already has a contact with X's fingerprint (`Retalk(L).contacts_fp()`): keep it.
   - Else `alias = pick_alias(X.name, card.who, taken)` (taken = L's aliases + local line names). If
     `X.code`: `Retalk(L).request(X.fingerprint, X.code, alias)` (`retalk request FP --code CODE --peer ALIAS`;
     it verifies and saves the contact, then sends the encrypted contact request); else `Retalk(L).add(...)`.
     Failure: exit 1, `error: could not reach X for L (is the link still live?)`.
6. **Intros.** For each contact added in 5, `intro.send_intro(user_dir, L, alias)`. It is what tells the
   host the joiner's line name. Failures are `warning:` lines.
7. **Room.** If the card has a room: for each L, members = L's aliases for every card line + every other local
   line that L has as a contact. No group yet: `retalk group create ROOM --members ...`; else
   `Retalk(L).group_add(ROOM, missing)`. Then send one envelope into the room from L: type `status`,
   subject `joined the call`, via `Retalk(L).send_group` with `envelope.encode` (use the same envelope
   fields `phoning send` uses), so the host's lines learn the roster. Group failure: exit 1.
8. **Output.** One line per added contact `L added X as ALIAS`, per kept `L already has X as ALIAS`, then
   `done: on the call "ROOM" with N lines from WHO; their side picks you up within a minute` (without a room:
   `done: every line here can now call every line on the link`). `--json`:
   `{"added": [{"line","peer","alias"}], "kept": [...], "moved": [names], "room": R|null}`.
Running it twice with the same link changes nothing the second time and exits 0.

### `phoning remote card [NAME ...] [--url URL] [--room R] [--who LABEL] [--json]`

A link without invite codes (`code: null`), for calling back by hand. URL: `--url`, else the config relay's
`public[0]` when every named line's relay is the config relay `url`, else the lines' shared relay if its host
is not loopback, else exit 2 with `hint: phoning remote host`. Lines on different relays: exit 3.

### `phoning remote status [--json]`

Prints `tunnel URL up|down|none`, `watcher running|stopped`, then one line per local line:
`NAME  RELAY  up|down  N remote contacts` (remote = alias not a local line name; up = `up.relay_up`).
`--json`: `{"tunnel": url|null, "watcher": bool, "lines": [...]}`. Exit 0 even if something is down.

## Security notes (put them in the module docstring and the CLI help of `host`)
- The relay only ever sees ciphertext and public keys, so exposing it through a tunnel exposes no messages.
- **The link is a key to your lines**: anyone holding it can add themselves as a contact of every line on it
  until `phoning remote host --stop` revokes the codes. Send it in a DM, never a public channel.
- Names learned from intros are cosmetic. Authority (who can accept a proposal) stays bound to fingerprints.

## Clarifications (from writing the acceptance tests)
- `pick_alias` suffix: the base is cut so base + suffix is at most 64, e.g. `cut64[:62] + "-2"`.
- `PHONING_CLOUDFLARED` set but not an existing executable file means "not installed" (exit 2); never fall
  back to `PATH` or the Homebrew paths in that case.
- `status --json` line objects: `{"name", "relay", "up": bool, "remote_contacts": int}`; with no tunnel the
  text line is `tunnel none`.
- `join` "with N lines from WHO": N is the number of card lines that are not ours.
- `host` with no lines still ensures the watcher (harmless) and reports `"link": null`.

## Changes made while building (2026-11-22, implemented and tested)
- **Pending guests** are keyed `"LINE:F"`: one joining line requests every host line, so a fingerprint can be
  pending on several host lines at once.
- **Renames are in place**: `retalk add F --peer ALIAS --verify` on a saved fingerprint relabels it and keeps
  its session (verified by experiment). The watcher never removes a contact.
- **`join` step 8, retry delivery**: retalk does not deliver mail a joiner sends before the host accepts its
  request (the intros and the room status); the mail waits in the sender's outbox until the sender's next
  `send` or `sync`. `join` therefore spawns `phoning remote resync --lines L1,L2` detached (log
  `<home>/.remote-resync.log`), which runs `retalk sync` for each line every 5 s for 180 s.
- **`join --name`** also adds `{"home", "name", "session": "NAME-operator", "interval": 2}` to the up config's
  owners (creating the config with `relay.url` = the call's relay when it is missing, so `up` never starts a
  local relay on a computer that only joins) and runs `up.reconcile`, then prints
  `NAME's operator is running (...)`.
- **`--json`** output is exactly one JSON object on stdout; text lines and warnings go to stderr or are
  skipped.
- **`host`** ends with `they paste this into their coding agent: Set up Phoning and join this call:
  https://github.com/verygoodbread/phoning <link>` (ONBOARD.md's fast path handles that sentence).
- **`status`**: `tunnel URL up` means the cloudflared process is alive (no URL probe: a fresh tunnel's name
  can take over a minute to resolve).
- **Board attribution by fingerprint**: `board.fold(..., aliases={fp: current_alias})` attributes inbound
  records through `peer_fp`, so a decision received while a guest was still `guest-<fp8>` counts under its
  real name (without it the trusted name and the claimed principal differ and the decision fails closed).
  `cli board`, MCP board, `web` and `call` pass `ops.current_aliases(user_dir)`.
- **`host` restart** waits for the port to close, not for the pid to vanish (an unreaped relay is a zombie).
- `Retalk.invite_codes()` returns only active codes (`retalk invite list` keeps revoked ones, `active: false`).
- The quick tunnel is spawned with its own empty `--config` (see host step 1).

## Review fixes (independent skeptic review, 2026-11-22)
- #1 Host-side guests are always `guest-<intro name>` (`-N` when taken); a joiner-chosen name is never kept.
  `board._same_principal` treats `guest-<claimed>[-N]` as the claimed sender so guests' decisions are not
  void, while authority goes to the alias.
- #2/#5 `host` checks the origin before any tunnel: config relay must be loopback; if it is up, every listener
  (lsof; failure = exit 1) must be `retalk-server` (basename of one of the first argv tokens), else exit 3.
- #3 `join` drops `RETALK_API_KEY` from its environment; it refuses (exit 3) to move a line whose identity store
  has a saved relay access key.
- #4 Default room is `call-<who>`.
- #6 `_sigterm` ignores pid <= 1; the move path signals an owner only if its command is `phoning ... own NAME`.
- #7 `host` holds an exclusive flock on `<state>/remote-host.lock`; a tunnel it started is closed on failure.
- #8 `--stop` deletes a line's code record only once retalk lists it inactive; else exit 1 (re-run retries).
  A minted code that can't be recorded is revoked at once.
- #9 `host` starts a relay that is down.
- #11 Joining lines save each other; only the first creates/extends the room and announces it.
- #13 Owner daemon drains its queue before and after stopping its child (phoning/daemon.py).
- #15 `host` exits 1 rather than print a link with a missing code or without a running watcher.
- #16 Group rows are `alias<TAB>fp`; an unchanged join sends nothing and starts no resync.
- #17 `parse_card` tries the full payload, then cuts back at each original whitespace; malformed URLs are
  `CardError`. #18 hints never echo the link. #19 logs open with O_NOFOLLOW.
- Not fixed (known issues): #10 a rejected/failed first request is not re-sent by a later join (the contact
  exists); #12 recovery after a partial multi-line move; #14 a joiner already saved as a GLOBAL retalk contact
  on the host; #18 the invite code is visible in local process arguments of `retalk request`.
