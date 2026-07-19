# Claude Agent SDK

Use the Claude Agent SDK when building a custom agent runtime around Claude Code capabilities. Do not confuse it with the Messages API tool loop or Managed Agents.

## Choose the surface first

- **Messages API + tools:** the application owns the loop, tools, state, and runtime.
- **Claude Agent SDK:** the application embeds a Claude Code-style agent runtime and configures its tools, permissions, hooks, sessions, and streaming events.
- **Managed Agents:** Anthropic hosts persistent agent configuration, sessions, and execution workspaces.

If the requested behavior is a single call or a deterministic workflow, prefer the Messages API.

## Documentation rule

Agent SDK interfaces evolve quickly. Before writing imports, option names, hook signatures, MCP configuration, resume/session code, or result parsing:

1. Inspect the project's installed Agent SDK package and version.
2. Fetch the matching official Agent SDK documentation or source from `references/live-sources.md`.
3. Copy signatures from that source; do not infer them from Claude Code CLI APIs or the Messages SDK.

## Design checklist

- Keep the initial instruction explicit and scoped.
- Grant the smallest tool set required for the task.
- Define a permission strategy for file writes, shell commands, network access, and MCP tools.
- Treat hooks as policy/automation boundaries; keep hook payload handling defensive.
- Stream structured SDK events rather than scraping terminal output.
- Preserve and resume SDK session identifiers only when continuity is intended.
- Put durable application state outside transient agent context.
- Bound turns, cost, wall-clock time, and output size.
- Surface cancellation and tool failures to callers.
- Test with deterministic fake tools before live execution.

## Validation

Verify initialization, event ordering, permission denial, hook failures, cancellation, session resume, malformed tool results, and terminal result extraction. Never validate only the final text while ignoring structured error/result events.
