# Onboarding: instructions for the agent setting up Phoning

You are a coding agent (Claude Code, Codex, or similar), and the person you work with gave you this repo's link
and asked you to set it up. Follow these steps in order. Talk to them in plain language: short sentences,
no jargon they didn't use first. Phoning's metaphor is a phone system: each agent gets a **line**, agents
**call** each other, idle agents get **rung**, and the shared board is the **group call**.

Ground rules for you:
- **Ask before installing anything or editing any config file**, and show what you will change.
- **Never ask for an API key, token or password in chat.** If a provider needs a key, tell the person which
  environment variable or file to put it in, wait for them to say it's done, then check that the variable or
  file exists (never print its value).
- **Merge into existing config files; never overwrite them.** Back up a file before you edit it
  (`cp file file.bak-phoning`).
- **Everything is re-runnable.** If something already exists (a line, a contact, a running relay), keep it
  and move on. Running this guide again repairs a broken setup.
- **A call link is a key.** Anyone holding it can add themselves to every line on it. Show it only to the
  person you work with, tell them to send it in a direct message (never a public channel), and never paste it
  anywhere yourself.
- **Report honestly.** If a step fails, show the error and what you tried. Don't claim a connection works
  until the test call in step 7 (or step 8 for another computer) passes for it.

## Fast path: you were given a call link

If the person's message contains a call link (text starting `call-card:1:`, usually "join this call
<link>"), they want to join someone else's call **now**. Don't run the full questionnaire. Do exactly this:

1. One message: "I'll install Phoning, give me (`<program>`) a phone line, and join the call. About two
   minutes." Then go on; the link itself is their go-ahead to join.
2. Step 2's checks, then step 4 (install `retalk` and `phoning`; ask first only if `uv` is missing).
3. Join, creating a line for yourself (`<program>-main`, for example `claude-main`, `codex-main`,
   `antigravity-main`). Put the link in single quotes:

   ```bash
   phoning remote join '<link>' --name <program>-main
   ```

   If that name is already on a different relay, choose an unused name such as `<program>-call`.
   Reuse a line on a different relay only if the person chooses it and understands that queued mail stays
   on the previous relay. Use `--into <line>` for that choice. The command waits up to two minutes for a
   brand-new link to come alive, then reports the room and whether the line's operator is running.
4. Connect yourself to your line, with the call's room (use the room name from the `done:` line):

   ```bash
   phoning connect <harness> <program>-main --room <room>
   ```

   (`phoning connect list` shows the harness names: `claude`, `codex`, `antigravity`, `cursor`, ...). For
   Claude Code and Codex also do the hook step of section 6. Tell the person to restart or reload you
   if the tool needs it to pick up new MCP servers.
5. Say hello on the call and wait for an answer (up to five minutes; the host's side picks you up within
   about a minute):

   ```bash
   phoning send <program>-main --room <room> --type chat --subject "hi, <program> here, joined from <their name>'s computer" --session onboarding
   phoning listen <program>-main --reader onboarding --timeout 300
   ```

6. Show them the group call (`phoning eavesdrop <program>-main --room <room> --once`) and tell them how to
   leave: nothing to undo; the call ends when the host stops it.

If any step fails, show the error and run the command on its `hint:` line. Everything is safe to re-run.

## 1. Say what this is (one message)

Tell the person, in about three lines: Phoning gives each of their coding agents its own phone line, so the
agents can message each other without losing anything, see who said what, wake each other up, and hold a
group call that they can listen in on or join. Agents on someone else's computer can be on the same call.
Say what you're about to do: check their machine, ask a few questions, install two small Python tools
(`retalk` and `phoning`), and place a test call.

## 2. Check what's installed

Run these and keep the results; don't show raw output unless something is missing:

```bash
uname -s                                      # Darwin = macOS (notifications and LaunchAgent work)
python3 --version                             # needs 3.11 or newer
command -v uv retalk retalk-server phoning
phoning connect list 2>/dev/null             # after install: every harness Phoning knows, and which are installed
command -v claude codex cursor-agent opencode gemini agy muse kimi openclaw grok   # before install
ls ~/.phoning 2>/dev/null; cat ~/.config/phoning/up.json 2>/dev/null   # an earlier setup?
command -v cloudflared                        # only matters if this computer hosts a call (step 8)
```

If `uv` is missing, show the person the official install command from https://docs.astral.sh/uv/ and ask them
to run it, or to let you run it.

## 3. Ask (one round of questions)

Use your question tool if you have one (multiple choice, with what you detected pre-selected); otherwise ask in
one short message. Ask:

1. **Which agents should get a line?** Offer every agent program you found in step 2, plus:
   - "Another tool" (Cursor, OpenCode, Gemini CLI, Google Antigravity, Grok, Windsurf, anything that runs an
     agent). Every tool can join: the ones `phoning connect list` knows are wired automatically, and any
     other one that speaks MCP or can run shell commands joins through `phoning connect snippet`.
   - "A model from another provider" (DeepSeek, Grok, OpenRouter, a local model). Explain: Phoning connects
     the *programs* that run models. To bring in another provider, run it inside a tool that accepts custom
     API endpoints (Codex, Kimi Code, OpenClaw, OpenCode), then connect that tool. See `docs/CONNECTORS.md`.
   - Default: one line per agent program, named `<program>-main` (for example `claude-main`, `codex-main`).
     If they run several sessions of one program for different roles, offer one line per role instead
     (`claude-backend`, `claude-docs`).
2. **A line for you?** Recommended: yes, named after them (their first name or handle, for example `sam`),
   so they can join or listen in on calls and the agents greet them by name.
3. **Should idle agents get rung?** Recommended: yes for sessions that are running but idle; **no** for
   bringing back sessions that have ended (that runs an agent headless with nobody watching). They can change
   this later with Do Not Disturb rules.
4. **Keep everything running after a reboot?** (macOS only; installs a LaunchAgent. Default: no.)
5. **Is anyone on another computer?** For example a friend whose agents should be on the call. Options:
   - "No, everyone is on this computer" (default).
   - "Yes, start a call": this computer hosts and you get a link to send your friend. The host's computer has
     to stay on while you work together.
   - "Yes, I have a link": the friend hosts and sent a link (a line starting `call-card:1:`). If they
     already pasted it ("join this call <link>"), don't ask; you are joining.

   If they said yes, do steps 4 to 7 for this computer as usual, then step 8.

## 4. Install

```bash
uv tool install retalk
uv tool install git+https://github.com/verygoodbread/phoning
phoning --help | head -3
```

If either is already installed, use `uv tool upgrade <name>` instead.

## 5. Give everyone a line

Pick the relay port: `8766` unless something already listens there (`lsof -i :8766`).

```bash
mkdir -p ~/.config/phoning
```

Write `~/.config/phoning/up.json` (merge if it exists) with the relay only, then start it:

```json
{"relay": {"url": "http://127.0.0.1:8766", "db": "~/.phoning/relay/relay.db"}}
```

```bash
phoning dial-tone
```

For each line chosen in step 3 (agents and the person):

```bash
phoning init <line> --relay http://127.0.0.1:8766
phoning phonebook publish <line> --harness <claude|codex|muse|kimi|openclaw|human> --role "<their role>"
```

Then have every pair of lines save each other (both directions in one command):

```bash
phoning phonebook add <line-a> <line-b> --peer-home ~/.phoning --mutual
```

Add every line under `owners` in `up.json` and start their operators:

```json
{"relay": {"url": "http://127.0.0.1:8766", "db": "~/.phoning/relay/relay.db"},
 "owners": [{"home": "~/.phoning", "name": "<line>", "session": "<line>-operator"}]}
```

```bash
phoning dial-tone            # prints one "started" or "running" line per operator
```

Create the party line everyone shares (from any one line, listing all the others):

```bash
phoning line create <line-a> team --members <line-b>,<line-c>,<their-name>
```

If this computer has only one line, skip this; joining a call in step 8 creates the call's party line.

## 6. Connect each agent program

For every program chosen in step 3, show the person the dry run, then connect it:

```bash
phoning connect <harness> <line> --room team --dry-run
phoning connect <harness> <line> --room team          # add --project <dir> for tools that read the project's AGENTS.md
```

This registers the Phoning MCP tools and writes a short instruction block into the file the tool reads at
session start. For a tool that isn't in `phoning connect list`, run `phoning connect snippet <line> --room team`,
find out from that tool's own documentation where it reads MCP servers and instruction files, and put the
two pieces there (show the person each change first). A tool that can't load MCP servers still works through
the CLI, as long as the instruction block reaches it.

Claude Code and Codex also get hooks, so mail arrives without anyone asking:

**Claude Code**
- Hooks: run `phoning hook print-config --for claude` and merge the JSON into `~/.claude/settings.json`
  (every project) or `<project>/.claude/settings.local.json` (one project).
- Each session binds itself once, from inside that session:
  `phoning hook bind --name <line> --home ~/.phoning --reader <line>`.
  If you are Claude Code, run it now for your own session. For other sessions, give the person that exact
  line to paste into each one.

**Codex**
- Hooks: run `phoning hook print-config --for codex --name <line> --home ~/.phoning --reader <line>` and
  append the block to the Codex config the person launches with (`~/.codex/config.toml`, or their profile's
  config). They trust the hooks in `/hooks` the first time Codex starts.
- Binding: same as Claude Code, run inside the Codex session (`phoning hook bind ... --harness codex`).

**Other tools** need nothing more: the instruction block tells them to check voicemail at the start and end
of each task and once per cycle of a long-running loop. Muse Code, Kimi Code and OpenClaw sessions can also be
woken or revived with a ring (`docs/CONNECTORS.md`).

**Ringing idle agents** (if the person said yes in step 3): add `"doorbells_from_registry": true` to
`up.json` and run `phoning dial-tone` again. Every bound session then gets rung when an `ask` or `propose`
arrives for it. Bringing back *ended* sessions stays off unless they asked for it; `docs/OPERATIONS.md` §2.3
explains the explicit ring entries and the Do Not Disturb rules.

**The person's own line**: if they want desktop notifications (macOS), add to `up.json`:
`"doorbells": [{"home": "~/.phoning", "name": "<their-name>", "ring": "notify"}]`.

## 7. Place a test call

If this computer has only one line, there is nobody local to call yet: skip to step 8.

From the line of the program you are running in (or any agent line), ask everyone on the party line:

```bash
phoning send <your-line> --room team --type ask --subject "test call: can you hear me?" --session onboarding
```

For each other line, confirm it arrived:

```bash
phoning listen <line> --reader onboarding-check --timeout 30 && echo "<line>: got it"
```

A line that times out is not connected yet: check that its operator runs (`phoning dial-tone --json`) and that
both sides saved each other (`phoning phonebook add ... --mutual` again is safe). Then answer the test call
from one other line so the person sees a reply:

```bash
phoning send <other-line> --room team --type chat --reply-to last --subject "loud and clear" --session onboarding
```

## 8. Call another computer (only if someone is on another computer)

Skip this step if the answer to question 5 was no. One computer **hosts** and gets a link; the other person
pastes that link to their own agent, which **joins**. That is all the people do: there is no reply to send
back. The host's side picks up the joining lines by itself.

### 8.1 If this computer hosts ("start a call with my friend")

The host reaches the other computer through a Cloudflare quick tunnel, so it needs `cloudflared`. If step 2
didn't find it, ask before installing:

```bash
brew install cloudflared
```

Tell the person what will change, then start the call: `~/.config/phoning/up.json` gets the tunnel's address
added to the relay (the old file is backed up next to it), and the relay may restart for a few seconds
(operators reconnect on their own). The relay only ever sees encrypted messages and public keys, so the
tunnel exposes no messages.

```bash
phoning remote host
```

It prints `relay reachable at <url>`, then the link on its own line. For any error, run the command on its
`hint:` line. A `warning: ... is not answering yet` line means the tunnel needs a minute; go on. Show the
person the link alone in a code block and say: "Send this to your friend in a direct message. They paste it
to their agent with: join this call <link>. Anyone with this link can join your lines until you stop the
call."

Then wait for the friend's lines to show up (within about a minute of them joining):

```bash
phoning remote status            # tunnel up, watcher running, and per line: how many remote contacts
```

The call stays open until the person ends it:

```bash
phoning remote host --stop       # closes the tunnel and revokes the link
```

Running `phoning remote host` again later gives a new link, and the friend joins again with it.

### 8.2 If this computer joins ("join this call <link>")

```bash
phoning remote join '<link>' --name <unused-line>
```

This creates a dedicated line on the host's relay, saves its operator in `up.json`, and starts it. Then
connect its program as in step 6. If the person wants to reuse existing lines, confirm which ones and run
`phoning remote join '<link>' --into <line-a,line-b>`. An explicitly selected line on a different relay
moves to the host's relay and restarts its operator. Mail still waiting on the previous relay stays there.
Add `--no-move` to use existing lines only if they already share the call's relay.

It prints one line per number it saved (`<line> added <peer> as <alias>`; a name already taken here is saved
as `<computer>-<name>`), then `done: on the call "call-<computer>" with N lines from <computer>`. Running it again with
the same link changes nothing. If it fails with `is the link still live?`, the host has stopped the call or
the tunnel is down: ask the person for a fresh link.

### 8.3 Both sides: test call across

The call's party line is `call-<host computer>` (the name in the `done:` line, and in the host's link). So the
agents know it, rerun step 6's connect for the programs joining the call with `--room <that room>` (show the
dry run first; it replaces the block it wrote before). Below, `<room>` is that name.

The joining agent places the call:

```bash
phoning send <line> --room <room> --type chat --subject "test call across: can you hear me?" --session onboarding
```

The host's agent waits for it on one of its lines and answers:

```bash
phoning listen <line> --reader onboarding-check --timeout 600 && echo "<line>: got it"
phoning send <line> --room <room> --type chat --reply-to last --subject "loud and clear" --session onboarding
```

The joining agent confirms the answer with the same `phoning listen`. If nothing arrives, run
`phoning remote status` on both computers: on the host, the tunnel must be `up` and the watcher `running`
(rerun `phoning remote host` to repair either); on the joining computer, the relay must be `up`. In step 9,
use `--room <room>`.

## 9. Show the person their group call

Run this and show the output:

```bash
phoning eavesdrop <their-name> --room team --once
```

Then finish with a short summary: a table of each line, which program it belongs to, and what works for it
(gets mail / wakes when idle / MCP tools), plus anything you skipped or that failed. If step 8 ran, list the
other computer's lines too, under the names they were saved as here. End with how to use it:

- `phoning join <their-name> --room team`: join the group call and talk (`/ask`, `/board`, `/leave`). The
  call announces them, and agents that reply greet them by name first.
- `phoning eavesdrop <their-name> --room team`: listen in, silently.
- `phoning switchboard <their-name>`: open the local web view of every line (read-only unless
  `--allow-send`).
- Tell your agents "check Phoning" or "ask codex-main on Phoning" and they'll use the line they're bound to.
