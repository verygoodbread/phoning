# Phoning

**Your coding agents can't call each other.**

You run Claude Code in one window, Codex in another, maybe a third model somewhere else, all on the same
project. They can't talk to each other, so you end up as the messenger, copying questions and answers
between windows. Even when you give them a shared channel, things go wrong:
- messages get lost when two sessions read from the same place;
- nobody can tell which session said what;
- an agent that went idle just sits there until you nudge it;
- one agent says "approved" about work it doesn't own, and the others believe it.

**Phoning gives every agent its own phone line.** Agents call each other, and the call gets through:

- **Nothing gets lost.** Every message lands in the line's voicemail, an append-only log. Each session
  keeps its own place in it, so two sessions sharing a line both hear everything.
- **You know who's calling.** Each message shows the session that sent it, and the sender is checked
  against the encrypted connection, not just the name the message claims.
- **Idle agents get rung.** When a question arrives for an agent that has gone quiet, Phoning rings it and
  wakes it up, under Do Not Disturb rules you set.
- **Only the right agent can say yes.** A proposal counts as accepted only when the agent that owns that
  part of the project accepts it.
- **You can join the call.** Listen in on the agents' group call, or jump in and talk. The call announces
  you, and the agents say hi before they carry on.

It is a small, stdlib-only Python tool (3.11+) that runs on one machine. It's built on the
[`retalk`](https://github.com/xhluca/retalk) end-to-end-encrypted relay.

## Set it up: give your agent the link

Paste this into Claude Code or Codex:

> Set up Phoning for me: https://github.com/verygoodbread/phoning

The agent follows [ONBOARD.md](ONBOARD.md):
1. It checks what you already have installed.
2. It asks which agents and models you want on the call.
3. It installs Phoning and gives each agent its own line.
4. It connects each agent and places a test call.
5. It shows you the group call.

Phoning never asks for API keys in chat; the agent tells you where to put them.

Prefer doing it by hand? See [Manual setup](#manual-setup).

## How it works: the phone system

| On a phone | In Phoning | Command |
|---|---|---|
| your line | an identity for one agent role, with its own number (a key fingerprint) | `phoning init`, `phoning number` |
| the exchange | the local end-to-end-encrypted relay | `phoning dial-tone` keeps it, and every operator, running |
| the operator | the one process that picks up for a line, so no second reader can steal a message | `phoning operator` |
| voicemail | the log every message lands in; each session reads at its own pace | `phoning voicemail`, `phoning listen`, `phoning clear-voicemail` |
| the phonebook | cards that let agents on one machine find and add each other | `phoning phonebook` |
| a party line | a room several agents share | `phoning line create` |
| the group call | the board: who is doing what, open questions, proposals and decisions | `phoning group-call` |
| ringing | waking an idle agent, or notifying you | `phoning ring` |
| Do Not Disturb | allow / ask me first / never, per kind of wake-up | `phoning ring --rules` |
| caller ID | your role and scope, sent the first time you call someone | `phoning caller-id`, `phoning hello` |
| call forwarding | folding mail from a plain retalk identity into a line | `phoning forward` |
| listening in | a read-only live view of a call | `phoning eavesdrop` |
| joining the call | talking on a call from your terminal; the agents greet you by name | `phoning join` |
| the switchboard | a local web page showing every line | `phoning switchboard` |
| getting hooked up | putting any agent harness on a line | `phoning hook-up` (`phoning connect`) |

Every command also has a plain name (`board`, `doorbell`, `directory`, `web`, ...), and `phoning --help` lists both.

Messages are typed, so a call stays readable: `chat`, `status`, `ask`, `propose`, `accept`, `reject`,
`receipt` and `ack`. `phoning send --help` prints what each type needs.

## Who can be on the call

Any agent harness. An agent with a working shell still thinks it has no way to reach anyone until
something tells it otherwise, so `phoning connect` gives a harness three things: the Phoning MCP tools, a short
instruction block in the file it reads at session start (its line, how to check voicemail, how to answer, who
it can call), and hooks or a ring where the harness supports them.

```bash
phoning connect list                              # every known harness, installed or not, and what it gets
phoning connect codex codex-main --room team      # MCP tools + instructions, idempotent, backed up
phoning connect cursor cursor-main --project .    # harnesses that read AGENTS.md from the project
phoning connect snippet my-line --room team       # anything else: the MCP server (JSON, TOML) and the block to paste
```

Built in: Claude Code, Codex, Cursor, OpenCode, Gemini CLI, Muse Code, Kimi Code, OpenClaw and DeepSeek
Harness. Anything else that speaks MCP, or can run a shell command, joins through the snippet.

What each one gets beyond that:

| Agent | Gets mail | Wakes when idle | Brought back after it ended |
|---|---|---|---|
| Claude Code | hooks at session start, every prompt and turn end; MCP tools | yes: a background `phoning listen` ends and starts a turn | yes, headless (`--ring claude-resume`, off by default) |
| Codex | hooks; MCP tools | yes, through Codex's app-server (`--ring codex-app-server` or `codex-queue`) | yes, headless, never with a looser sandbox than before |
| Muse Code | the CLI, or MCP where the harness loads MCP servers | no: Muse only accepts session messages from other Muse sessions | yes, the session is revived with its history |
| Kimi Code | the CLI, or MCP where the harness loads MCP servers | yes, when hosted by `kimi web` | yes, headless |
| OpenClaw | the CLI, or MCP where the harness loads MCP servers | yes, through the gateway | yes, locally |
| You | `phoning join`, `phoning eavesdrop`, the switchboard | a desktop notification (`--ring notify`, macOS) | n/a |

**Any other model or provider.** Phoning connects the programs that run models, not the models themselves, so
there are two ways to bring in anything else:
- **Run the model inside a harness above.** Codex accepts custom providers (`model_providers`, for any API that
  serves the OpenAI Responses API). Kimi Code speaks OpenAI-compatible chat completions. OpenClaw has its own
  provider settings. The session then joins like any other.
- **Use the universal plugs.** Anything that speaks MCP gets `phoning_send`, `phoning_read`, `phoning_wait`,
  `phoning_board` and `phoning_whoami` from `phoning mcp`. Anything that can run a shell command can use the CLI.

Details for each, including where every file goes: [docs/CONNECTORS.md](docs/CONNECTORS.md).

## Manual setup

Requirements: [`uv`](https://docs.astral.sh/uv/), and `retalk` (it includes `retalk-server`).

```bash
uv tool install retalk
uv tool install git+https://github.com/verygoodbread/phoning
```

Give two agents a line each, connect them, and place a call:

```bash
mkdir -p ~/.config/phoning
echo '{"relay": {"url": "http://127.0.0.1:8766", "db": "~/.phoning/relay/relay.db"}}' > ~/.config/phoning/up.json
phoning dial-tone                                  # start the local relay

phoning init claude-main --relay http://127.0.0.1:8766
phoning init codex-main  --relay http://127.0.0.1:8766
phoning phonebook publish claude-main --harness claude --role lead
phoning phonebook publish codex-main  --harness codex  --role builder
phoning phonebook add claude-main codex-main --peer-home ~/.phoning --mutual   # both sides save each other

# list both lines under "owners" in ~/.config/phoning/up.json, then:
phoning dial-tone                                  # starts one operator per line

phoning line create claude-main team --members codex-main
phoning send claude-main --room team --type ask --subject "can you hear me?" --session main
phoning listen codex-main --reader main --timeout 60    # blocks until something arrives
phoning voicemail codex-main --reader main
phoning group-call claude-main
```

Then put each harness on its line (`phoning connect <harness> <line> --room team`), add the hooks for Claude
Code and Codex as in [docs/OPERATIONS.md](docs/OPERATIONS.md), and run
`phoning dial-tone --print-launchagent` if you want everything back after a reboot (macOS).

## Honest limits

- **One machine.** The phonebook is deliberately same-machine. Lines on two machines can still call each
  other through a shared relay, but you exchange numbers by hand.
- **Session names are self-reported.** On one machine that gives attribution, not authentication. The
  sender's line (its key) is verified; which session on that line sent a message is the sender's word.
- **The operator lock is cooperative.** A script that calls `retalk receive` directly on a line that has an
  operator can still steal its messages.
- **Voicemail holds decrypted text.** It is created `0600` inside `0700` folders, and that is its only
  protection.
- **Both sides must save each other first.** A message sent before that is dropped by the relay as coming
  from a stranger.

## Docs

- [ONBOARD.md](ONBOARD.md): the setup an agent follows for you.
- [docs/OPERATIONS.md](docs/OPERATIONS.md): running it day to day (keeping lines up, hooks, ringing,
  Do Not Disturb, the switchboard, MCP, joining a call).
- [docs/CONNECTORS.md](docs/CONNECTORS.md): every harness and how to bring in other providers.
- [docs/SPEC-v0.md](docs/SPEC-v0.md), [docs/SPEC-v0.2.md](docs/SPEC-v0.2.md) and [docs/spec/](docs/spec/):
  the contracts.
- [docs/prior-art.md](docs/prior-art.md): what else exists, and why the decision layer is the point.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m pytest -q        # unit tests plus end-to-end calls on a throwaway local relay
.venv/bin/ruff check phoning tests
.venv/bin/mypy phoning               # strict
```

Contributor rules are in [AGENTS.md](AGENTS.md).

## License

MIT. See [LICENSE](LICENSE).
