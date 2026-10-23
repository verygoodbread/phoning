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
- **Report honestly.** If a step fails, show the error and what you tried. Don't claim a connection works
  until the test call in step 6 passes for it.

## 1. Say what this is (one message)

Tell the person, in about three lines: Phoning gives each of their coding agents its own phone line, so the
agents can message each other without losing anything, see who said what, wake each other up, and hold a
group call that they can listen in on or join. Say what you're about to do: check their machine, ask a few
questions, install two small Python tools (`retalk` and `phoning`), and place a test call.

## 2. Check what's installed

Run these and keep the results; don't show raw output unless something is missing:

```bash
uname -s                                      # Darwin = macOS (notifications and LaunchAgent work)
python3 --version                             # needs 3.11 or newer
command -v uv retalk retalk-server phoning
phoning connect list 2>/dev/null             # after install: every harness Phoning knows, and which are installed
command -v claude codex cursor-agent opencode gemini muse kimi openclaw grok   # before install
ls ~/.phoning 2>/dev/null; cat ~/.config/phoning/up.json 2>/dev/null   # an earlier setup?
```

If `uv` is missing, show the person the official install command from https://docs.astral.sh/uv/ and ask them
to run it, or to let you run it.

## 3. Ask (one round of questions)

Use your question tool if you have one (multiple choice, with what you detected pre-selected); otherwise ask in
one short message. Ask:

1. **Which agents should get a line?** Offer every agent program you found in step 2, plus:
   - "Another tool" (Cursor, OpenCode, Gemini CLI, Grok, Windsurf, anything that runs an agent). Every tool
     can join: the ones `phoning connect list` knows are wired automatically, and any other one that speaks
     MCP or can run shell commands joins through `phoning connect snippet`.
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

## 8. Show the person their group call

Run this and show the output:

```bash
phoning eavesdrop <their-name> --room team --once
```

Then finish with a short summary: a table of each line, which program it belongs to, and what works for it
(gets mail / wakes when idle / MCP tools), plus anything you skipped or that failed. End with how to use it:

- `phoning join <their-name> --room team`: join the group call and talk (`/ask`, `/board`, `/leave`). The
  call announces them, and agents that reply greet them by name first.
- `phoning eavesdrop <their-name> --room team`: listen in, silently.
- `phoning switchboard <their-name>`: open the local web view of every line (read-only unless
  `--allow-send`).
- Tell your agents "check Phoning" or "ask codex-main on Phoning" and they'll use the line they're bound to.
