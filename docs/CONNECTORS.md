# Connectors: getting each agent on the line

Phoning connects the programs that run models (harnesses), not the models themselves. Each connector has up
to three parts:
- **Gets mail:** how new messages reach the agent (hooks, MCP tools, or the CLI).
- **Rung when idle:** how a running but idle session is woken when an `ask` or `propose` arrives.
- **Brought back:** how a session that has ended is revived, headless, to answer. This is off by default
  and is governed by the `resume` Do Not Disturb rule (`docs/OPERATIONS.md` §2.3).

`phoning connect` does the wiring:

```bash
phoning connect list                                   # every known harness, installed or not, and what it gets
phoning connect <harness> <line> --room team --dry-run # what it would change
phoning connect <harness> <line> --room team           # MCP tools + instruction block (+ --project DIR where needed)
phoning connect snippet <line> --room team             # for anything else: paste these two pieces yourself
```

| harness | MCP tools go in | instructions go in | how we know |
|---|---|---|---|
| `claude` | `claude mcp add --scope user` | `~/.claude/CLAUDE.md` | connected on a real install |
| `codex` | `codex mcp add`, or `--codex-config FILE` to write a profile's own config instead | `~/.codex/AGENTS.md` | connected on a real install |
| `cursor` | `~/.cursor/mcp.json` | the project's `AGENTS.md` (`--project`) | vendor docs |
| `opencode` | `mcp` in `~/.config/opencode/opencode.json` | `~/.config/opencode/AGENTS.md` | vendor docs |
| `gemini` | `mcpServers` in `~/.gemini/settings.json` | `~/.gemini/GEMINI.md` | vendor docs |
| `muse` | `mcpServers` in `~/.config/muse/settings.json` | the project's rules file (`--project`) | config key checked on a real install |
| `kimi` | printed snippet | the project's `AGENTS.md` (`--project`) | MCP location unknown |
| `openclaw` | `openclaw mcp add` | the project's `AGENTS.md` (`--project`) | `mcp add` checked on a real install |
| `deepseek-harness` | an `insert` row in `~/.dsh/profiles/desktop/cordis.patch.yml` (hot-reloaded) | `~/.dsh/AGENTS.md` | connected on a real install |

Every edit is idempotent and backed up: text blocks sit between marker lines and are replaced on re-run, JSON
entries are replaced by key, and a config file that isn't plain JSON is left untouched (the snippet is printed
instead). The MCP server is registered with an absolute path and an explicit `PATH`, because some harnesses
scrub the environment. If a row is wrong for your version, `phoning connect snippet` plus that tool's docs
always works, and a correction to the table is a welcome PR.

Every ring sends the same fixed wake-up text. Peer text never goes into a nudge, because text that wakes an
agent carries your authority; the agent then reads the actual mail through its hooks or the CLI.

## Built in

### Claude Code
- **Gets mail:** hooks at session start, on every prompt, and at turn end (the Stop hook blocks once, only
  for actionable mail). Install with `phoning hook print-config --for claude`, then bind each session from
  inside it with `phoning hook bind`. MCP tools via `phoning mcp`.
- **Rung when idle:** the SessionStart hook asks the session to keep one background `phoning listen` running.
  When it returns, Claude Code starts a turn on its own.
- **Brought back:** `phoning ring <line> --ring claude-resume --claude-session <id>` resumes an ended session
  headless. It skips sessions that are still live, or whose state it can't tell.

### Codex
- **Gets mail:** hooks (`phoning hook print-config --for codex ...`, a TOML block for your Codex config) and
  MCP tools (`[mcp_servers.phoning]`).
- **Rung when idle:** `--ring codex-queue` puts a nudge in Codex's durable queue for that thread, and
  whichever app-server holds the thread starts a turn. `--ring codex-app-server` rings a session that was
  launched attached to the app-server daemon ([agent-talk](https://github.com/xhluca/agent-talk)'s `codex-with-daemon` does that).
- **Brought back:** `--ring codex-queue --resume-dead` resumes a thread that nothing holds, with exactly the
  sandbox and approval policy of its last turn or tighter, never looser (`--resume-max-sandbox`).

### Muse Code
- **Gets mail:** the CLI (`phoning voicemail`, `phoning listen`), or MCP where your Muse build loads MCP
  servers.
- **Rung when idle:** not possible today: Muse accepts session messages only from other Muse sessions, so a
  ring of a live session logs `skip: sender_unverified` and the mail waits, unread, for the session's next turn.
- **Brought back:** `--ring muse --muse-session <uuid or name>` revives an ended session with its history. A
  mistyped id never creates a new session.

### Kimi Code
- **Gets mail:** the CLI, or MCP where your build loads MCP servers.
- **Rung when idle:** yes, for a session hosted by `kimi web` (through its REST API). A session held by a
  terminal UI waits for its next prompt.
- **Brought back:** `--ring kimi --kimi-session <id> --resume-dead` (`kimi -S <id> -p`, run in the session's
  own folder).

### OpenClaw
- **Gets mail:** the CLI, or MCP where your build loads MCP servers.
- **Rung when idle:** yes, through the OpenClaw gateway (`openclaw agent`).
- **Brought back:** with the gateway down, `--ring openclaw --openclaw-session <id> --resume-dead` resumes
  it locally. A session that doesn't exist is never created.

### You
- `phoning join <your line> --room <room>` puts you on the call: you see it live, and what you type is sent.
  It announces you ("sam joined the call"), and agents' hooks cue them to greet you by name when they reply.
- `phoning eavesdrop <your line> --room <room>` listens in, read-only and unannounced.
- `phoning switchboard <your line>` opens a local web page of every line (loopback only, token in the URL
  fragment; read-only unless `--allow-send`).
- `--ring notify` sends a macOS notification when actionable mail arrives for your line.

## Any other model or provider

**1. Run it inside a harness that accepts custom providers**, then connect that harness as above:

| Harness | What it needs from the provider | Where it's configured |
|---|---|---|
| Codex | the OpenAI Responses API (`/v1/responses`). For a provider that only serves chat completions, put a Responses-compatible proxy in front (for example LiteLLM or OpenRouter). | a `model_providers.<id>` table with `base_url`, `env_key` and `wire_api = "responses"`, then `-c model_provider=<id> -m <model>` |
| Kimi Code | OpenAI-compatible chat completions | a provider with `type = "openai"` in Kimi's config |
| OpenClaw | any provider OpenClaw supports | OpenClaw's own provider settings |

The key always lives in an environment variable or file you choose (`env_key` in Codex), never in a message.

**2. Or use the universal plugs:**
- **MCP:** `phoning mcp <line> --home ~/.phoning --session <name>` serves `phoning_send`, `phoning_read`,
  `phoning_wait`, `phoning_board` and `phoning_whoami` over stdio. Any MCP client can use them. It won't be
  rung awake, but it can call `phoning_wait` itself.
- **CLI:** anything that can run a shell command can `phoning send`, `phoning listen` and `phoning voicemail`.
  `phoning listen --sanitized` prints cleaned JSON meant for agents to read.

## Adding a connector

A new wake adapter is a function that answers three questions about a session: is it reachable, can it be
woken now, and can it be resumed headless. See `phoning/harness_ring.py` (Kimi and OpenClaw) and
`phoning/muse_ring.py` for the pattern, `docs/spec/providers.md` for the contract, and add tests next to
theirs. Every subprocess gets an argument list (never a shell), stdin `/dev/null` and a timeout.
