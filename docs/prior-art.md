# Prior-art survey: phoning

Survey date: 2026-10-11. Method: 22 searches, 18 fetches. [fetched] = read directly; [snippet] = search result or secondary coverage only; UNVERIFIED = no primary source confirmed it. The OpenAI primary page returned 403.

## Summary

1. No OpenAI product called "Spaces" is confirmed. The nearest items are ChatGPT group chats (humans plus ChatGPT, not agent-to-agent) and one unverified report of a "ChatGPT Space" workspace.
2. Anthropic ships the strongest first-party primitives: Claude Code cross-session messaging, which wakes idle sessions, and agent teams, which are experimental and off by default. Both are Claude-only and plain-text, with no typed messages and no shared board.
3. Cross-vendor agent protocols exist. A2A is Linux Foundation-governed and reports v1.0 as its first stable spec. It covers agent cards and task delegation, not rooms, boards, or decisions.
4. A crowded open-source layer already does shared rooms and mailboxes for Claude Code and Codex. These projects are mostly pull-based and small, with 0 to 2.2k stars.
5. The closest matches are agent-bus (typed ASK/REPLY/TASK, a Kanban board, SQLite) and MCP Agent Mail (acks, advisory file leases). Agent Mail's README now says it is superseded.
6. Human-to-cloud-agent products (Copilot agents, Agent HQ, Linear, Cursor cloud agents, Slack Code, Devin) dispatch tasks. They do not coordinate agents with each other or gate decisions.
7. The transport (durable room, cursors, acks) is commodity. The defensible part is the decision layer: owner-gated accept, a board derived from typed messages, exact-session attribution, and cross-vendor wake-on-news.
8. Build on MCP as the bus surface, since every harness in scope reaches it. Use A2A cards and task envelopes for cross-vendor interop.
9. Do not use ACP for this. The Zed/JetBrains ACP is editor-to-agent, and the name collides with IBM's older ACP, which merged into A2A.
10. Verdict: the transport layer is too similar to agent-bus and Agent Mail. Phoning is worth building only if the decision and ownership model is the product.

## What "Spaces" probably means

- **ChatGPT group chats** [snippet] https://cryptobriefing.com/chatgpt-group-chats-sharing-features/ : up to 20 people join one ChatGPT chat by invite link; launch dated 13 Nov 2025. Status conflicts: a May 2026 report says generally available, a July 2026 headline (https://www.iclarified.com/99055/openai-pilots-group-chats-in-chatgpt-letting-you-collaborate-with-friends-and-ai) says pilot. Human-plus-ChatGPT, not agent-to-agent.
- **"ChatGPT Space"**: one secondary outlet says it is a DevDay 2026 shared workspace where teams and agents edit Pages (UNVERIFIED, single source).
- **Codex multi-agent**: third-party blogs describe subagents and path-addressed messaging (https://codex.danielvaughan.com/2026/03/30/codex-cli-multi-agent-v2-path-addressing-structured-messaging [snippet]). No OpenAI primary found; the "eight parallel subagents" claim is UNVERIFIED.

## Comparison table

Licence "not verified" means I did not check the repository's licence file.

| Product / protocol | What it is | Cross-vendor | Local / cloud | Delivery | Typed messages | Shared board | Human in loop | Licence | URL |
|---|---|---|---|---|---|---|---|---|---|
| Claude Code agent teams (experimental, off by default) | Lead plus teammate sessions with a JSON mailbox and shared task list | No (Claude only) | Local | Sender told if the mailbox write fails; in-process teammates not restored on resume | Protocol messages (plan approval, shutdown) | Task list with dependencies and file-locked claims | Lead approves permissions; can message any teammate | Not verified | [fetched] https://code.claude.com/docs/en/agent-teams |
| Claude Code cross-session messaging (v2.1.224+) | ListAgents and SendMessage between your sessions | No | Local socket; cross-machine via Anthropic servers | Idle receiver starts a turn; held (max 100) or refused; no durable store documented | Plain text only | No | Inbound setting accept/hold/refuse; approval dialog | Not verified | [fetched] https://code.claude.com/docs/en/cross-session-messaging |
| Claude Code channels (research preview) | MCP server pushes Telegram, Discord, or iMessage events into a session | No | Local session | Only while the session is open | No | No | Sender allowlist | Not verified | [fetched] https://code.claude.com/docs/en/channels |
| Claude Code agent view (research preview) | Dispatch and monitor background sessions, each in its own worktree | No | Local supervisor | n/a | No | Status groups only | Peek and reply | Not verified | [fetched] https://code.claude.com/docs/en/agent-view |
| Claude Tag (Anthropic, Slack beta, 23 Jun 2026) | Shared @Claude identity per Slack channel | No | Cloud (Slack) | Slack threads | No | Channel memory, not a board | Admin scoping, audit logs | Proprietary | [snippet] https://mer.vin/2026/06/claude-tag-explained-anthropics-multiplayer-ai-teammate-for-slack-beta/ (no Anthropic primary reached) |
| Slack Code / agent channels | Per-task channel with agent, diffs, PR, preview | Partly; named agents conflict by source | Cloud | Slack | No | PR and diff links | Approve and comment | Proprietary | [fetched] https://slack.com/blog/news/slack-is-where-agents-work (Apr 2026 post; Aug 2026 launch UNVERIFIED) |
| ChatGPT group chats | Up to 20 humans plus ChatGPT in one chat | No | Cloud | Chat | No | No | Humans only | Proprietary | [snippet] https://cryptobriefing.com/chatgpt-group-chats-sharing-features/ |
| GitHub Agent HQ / Mission Control | Run Copilot, Claude, and Codex agents from GitHub and VS Code | Yes (Claude, Codex, Copilot) | Cloud | Task-based | No | Session dashboard | Assign, watch, review | Proprietary | [snippet] https://github.blog/2026-02-04-pick-your-agent-use-claude-and-codex-on-agent-hq/ |
| GitHub Copilot coding agent and agents panel | Issue- and PR-driven cloud agent | No | Cloud | Task | No | Issues and PRs | Reviews the PR | Proprietary | [snippet] https://github.blog/2025-08-19-agents-panel-launch-copilot-coding-agent-tasks-anywhere-on-github/ |
| Linear agents | Delegate issues to Codex, Cursor, Copilot, Devin, Factory, and others | Yes (listing) | Cloud | Issue-scoped agent session | No | Issue tracker | Delegate, comment | Proprietary | [fetched] https://linear.app/integrations/agents (no dates shown; Claude Code via third parties only) |
| Cursor cloud agents (formerly background agents) | Cloud VM agents started from Slack, Linear, GitHub, or web | No | Cloud | Task | No | No | Share read-only view; take control of desktop | Proprietary | [fetched] https://cursor.com/docs/background-agent |
| Google A2A (Linux Foundation) | Agent cards and task delegation protocol | Yes | Any HTTP host | Task lifecycle; push and streaming details UNVERIFIED | Message, Artifact, Task model UNVERIFIED (spec page not read) | No | Not specified in fetched text | Apache 2.0 | [fetched] https://a2a-protocol.org/latest/ and https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year |
| ACP (Zed and JetBrains) | Editor-to-agent protocol, JSON-RPC over stdio | Yes (editor side) | Local | Request and response | n/a | No | Editor UI | Apache 2.0 (per search) | [fetched] https://zed.dev/blog/acp-registry |
| MCP with Tasks extension | Tool access; long jobs as polled tasks | Yes | Local or remote | Polling (tasks/get, tasks/update, tasks/cancel) | n/a | No | Client-controlled | Not verified | [snippet] https://modelcontextprotocol.net/seps/2663-tasks-extension (date disputed) |
| MCP Agent Mail | Mailboxes, threads, acks, advisory file leases over MCP | Yes (any MCP client) | Local, self-hosted | Pull (fetch_inbox); ack_required | Importance levels only | File reservations | Web UI; HumanOverseer messages | Not verified | [fetched] https://github.com/Dicklesworthstone/mcp_agent_mail (README: "Superseded by mcp_agent_mail_rust") |
| agent-bus | Local MCP chat bus with tasks and decisions | Yes (MCP) | Local | Pull; blocking waits; tasks at-least-once; "sending does not wake a closed session" | ASK, REPLY, TASK, broadcast | Kanban: Todo, Accepted, Doing, Review, Done | Read-only local UI; nudges | MIT | [fetched] https://github.com/MustaphaSteph/agent-bus (20 stars) |
| agent-chat-mcp | Room messaging over SQLite, three tools | Yes (MCP) | Local | Pull by message-ID cursor | None | None | None documented | MIT | [fetched] https://github.com/TobiasCoding/agent-chat-mcp (0 stars) |
| Gas Town (Steve Yegge) | Mayor-led orchestrator; work state in Beads ledger in git | Yes (Claude, Copilot, Codex, Gemini) | Local | Mail injected via hooks; nudges (mechanism not documented on page) | Not documented | Beads task state | Escalates to Overseer | MIT | [fetched] https://github.com/gastownhall/gastown (18.3k stars; repo appears under steveyegge/ in search results) |
| AutoGen GroupChat | In-process shared transcript; LLM picks speaker | Model-agnostic, in-process | Local | In memory for session | No | No | Not verified | Not verified | [fetched] https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/selector-group-chat.html (maintenance date conflicts: Oct 2025 vs early 2026, per [snippet] https://atlan.com/know/ai-agent/what-is-autogen/) |
| OpenAI Agents SDK handoffs | Transfer of control between agents in one run | Model-agnostic | Local | In run | No | No | Guardrails | Not verified | [fetched] https://openai.github.io/openai-agents-python/handoffs/ (no peer messaging; return of control not stated) |
| Letta shared memory | Agents share memory blocks; cross-agent tools | Via Letta server | Local or hosted | Tool-based; sync and async claims UNVERIFIED | No | Memory blocks | Not specified | Not verified | [snippet] https://docs.letta.com/tutorials/shared-memory-blocks (multi-agent page fetch returned no messaging detail) |
| ANP (Agent Network Protocol) | DID-based peer identity and encryption | Yes | Any | UNVERIFIED | UNVERIFIED | No | n/a | Community; "proposed" per one listing | [snippet] https://atlan.com/know/mcp/how-to-choose-mcp-a2a-anp/ (spec not fetched) |
| Gemini CLI remote subagents | Local subagents; remote over A2A | Yes (A2A) | Local and remote | Delegation and result return | n/a | No | Config per agent | Not verified | [snippet] https://geminicli.com/docs/core/remote-agents/ (A2A marked experimental) |

Also seen, not fetched (features UNVERIFIED): Joind (https://github.com/haddadrm/Joind), agent-room (https://github.com/agent-room-alkl/agent-room), itismelime/agent-chat (https://github.com/itismelime/agent-chat), agentbus (https://github.com/Ahmed/agentbus), squad (https://github.com/rjwalters/squad). Claude Squad (https://claudeskills.info/harness/claude-squad/) and Conductor (https://www.conductor.build/workflows) manage parallel worktrees with no messaging seen.

## Closest three competitors and how phoning differs

1. **agent-bus** (MIT, 20 stars). Nearest in shape: SQLite WAL, typed ASK/REPLY/TASK messages, a Kanban board, at-least-once tasks, a human cockpit ([fetched] https://github.com/MustaphaSteph/agent-bus). Its inbox is pull-based with blocking waits, and "sending does not wake a closed session," so wake-on-news would be new. The README does not describe per-session sender attribution, owner-gated accept, or encryption. These are candidate differences, not confirmed gaps.
2. **MCP Agent Mail** (2.2k stars). Closest in intent: threads, acks, advisory file reservations, git-plus-SQLite storage, a human overseer ([fetched] https://github.com/Dicklesworthstone/mcp_agent_mail). The README says the Python version is superseded and no longer maintained. Reservations are advisory locks, not consent. Phoning's owner-accepts rule is what it lacks.
3. **Claude Code cross-session messaging and agent teams** (Anthropic). First-party, no setup, and on one machine it already wakes idle sessions and gives each message a sender name and reply address ([fetched] https://code.claude.com/docs/en/cross-session-messaging). Limits: Claude-only, plain text, no typed kinds, no shared board, and cross-machine traffic goes through Anthropic servers. Sender identity is by session name, with a short identifier added when several sessions share a name or Claude Code cannot check every location.

Gas Town (18.3k stars, MIT) is the largest adjacent project, but it is hierarchical, with a central Mayor, not peer rooms ([fetched] https://github.com/gastownhall/gastown).

## Which protocols phoning should interoperate with

- **MCP as the bus surface.** agent-bus, MCP Agent Mail, and agent-chat-mcp all ship as MCP servers. Agent Mail's README lists Claude Code, Codex, Gemini CLI, and Factory Droid as clients ([snippet] https://github.com/Dicklesworthstone/mcp_agent_mail). Cursor's MCP client support is UNVERIFIED here.
- **A2A for cross-vendor identity and task envelopes.** The Linux Foundation release dated 9 Apr 2026 calls v1.0 "its first stable specification" and mentions Signed Agent Cards ([fetched] https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year). A2A's landing page lists Apache 2.0 and a steering committee ([fetched] https://a2a-protocol.org/latest/). A2A has no room, board, or decision model, so phoning still has to supply those. Map a proposal to an A2A task and receipts to task status or artifacts (mapping is my proposal, not a documented pattern).
- **Not ACP.** The Zed/JetBrains ACP is editor-to-agent ([fetched] https://zed.dev/blog/acp-registry). IBM's ACP merged into A2A in August 2025, per a single glossary source (UNVERIFIED, https://howaiworks.ai/glossary/agent-communication-protocol).
- **MCP Tasks for long jobs.** SEP-2663 moves tasks into an extension with polling. That is not a wake mechanism, so it does not replace phoning's notifications ([snippet] https://modelcontextprotocol.net/seps/2663-tasks-extension, date disputed).
- **ANP only if phoning crosses organizations.** Its DID-based identity is interesting, but its status is "proposed" and I did not read the spec ([snippet] https://atlan.com/know/mcp/how-to-choose-mcp-a2a-anp/).

## Is this too similar to X?

**At the transport layer, yes.** A durable room with per-reader cursors, acks, and SQLite storage already exists in agent-bus, agent-chat-mcp, MCP Agent Mail, and at least six other GitHub projects found in one extended search. Building that again would be reinvention.

**At the decision layer, no evidence of a match.** Owner-gated accept, a board derived from typed messages, exact-session attribution, and cross-vendor wake-on-news together do not appear in any source I read. agent-bus comes closest on typed messages and a board. Claude Code comes closest on wake and attribution, but only for Claude.

**Human-to-agent products are different.** Copilot agents, Agent HQ, Linear, Cursor cloud agents, and Slack Code dispatch tasks to cloud agents. They do not coordinate agents with one another or gate decisions.

**Recommendation.** Keep phoning only as the decision and ownership layer. Run on an existing MCP bus or A2A envelope rather than a new chat store. Before writing a server, test whether agent-bus's board and decisions already cover the case. The search covered 22 queries, so an unseen project with an accept model could exist.

## Unverified or conflicting

- OpenAI, Spaces, and Codex subagent claims rest on secondary sources (OpenAI primary returned 403).
- Slack Code's August 2026 launch and supported agents conflict. Claude Tag's Anthropic primary was not reached.
- AutoGen maintenance dates conflict. Letta's messaging behavior and the MCP Tasks SEP date are unconfirmed.
- Repos were checked from their README pages, not their code. Licences for Claude Code, Cursor, Copilot, Linear, and Agent Mail were not checked.
