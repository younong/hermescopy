# Hermes Agent - Development Guide

Instructions for AI coding assistants and developers working on the hermes-agent codebase.

**Never give up on the right solution.**

This file is intentionally short so it can be loaded on every agent startup without context truncation. The full long-form guide was moved to [`docs/agents-reference.md`](docs/agents-reference.md). Read that reference when you need subsystem-specific details or the rationale behind these rules.

## What Hermes Is

Hermes is a personal AI agent that runs the same agent core across CLI, messaging gateway, TUI, dashboard, and desktop surfaces. It learns across sessions, delegates to subagents, runs scheduled jobs, and drives terminal/browser automation.

Two design constraints matter for almost every change:

- **Prompt caching is sacred.** Do not mutate past conversation context, swap toolsets, or rebuild the system prompt mid-conversation except through the existing context-compression path.
- **The core is a narrow waist.** Capability should usually live at the edges (commands, skills, plugins, service-gated tools, MCP), not as new always-present core tools.

## Default Workflow

- Prefer `.venv` (`source .venv/bin/activate`), with `venv` as fallback.
- Verify the real path, not only mocked units, for changes touching config propagation, security boundaries, remote backends, file/network I/O, gateway transport, or session state.
- Match surrounding code style and comments.
- Keep Terminal/TUI/dashboard/desktop compatibility in mind when changing shared gateway or agent code.
- Before restricting behavior, read the original intent (`git log -p -S <symbol>`) and preserve the feature purpose.

## Contribution Rubric

Wanted:

- Fix real bugs completely, including sibling call paths.
- Expand product reach at the edges: platform adapters, channels, providers, models, dashboard/TUI/desktop features.
- Refactor god-files into focused modules when the extraction itself is the goal.
- Preserve prompt caching, strict message role alternation, and byte-stable system prompts during a conversation.
- Write behavior/invariant tests rather than change-detector tests.
- Preserve external contributor authorship when building on their work.

Avoid/reject:

- Speculative hooks, managers, or extension points without a concrete consumer.
- New user-facing `HERMES_*` env vars for non-secret behavior. `.env` is for secrets only; behavioral settings belong in `config.yaml`.
- New core model tools when terminal/file, command+skill, service-gated tool, plugin, or MCP can solve it.
- Pagination/lazy-reading escape hatches on instructional tools that the agent must read fully.
- Security fixes that destroy the feature they protect.
- Outbound telemetry, usage attribution, or third-party identifiers without explicit opt-in gating.
- Plugins that special-case core files. Widen the generic plugin surface instead.
- Third-party/vendor product integrations under this repo's `plugins/`; publish standalone plugin repos instead.

## Capability Footprint Ladder

Choose the highest/least-permanent rung that solves the problem:

1. Extend existing code.
2. CLI command + skill.
3. Service-gated tool (`check_fn`).
4. Plugin.
5. MCP server in the catalog.
6. New core tool only as a last resort.

If several contributions target the same category, design one shared interface/orchestrator rather than merging one-offs.

## Important Architecture Pointers

- `run_agent.py` — `AIAgent`, core conversation loop.
- `model_tools.py` — tool orchestration and builtin tool discovery.
- `toolsets.py` — toolset definitions and core tool list.
- `cli.py` / `hermes_cli/commands.py` — CLI and slash command surfaces.
- `tui_gateway/server.py`, `tui_gateway/ws.py`, `tui_gateway/transport.py` — JSON-RPC gateway/session/event transport.
- `ui-tui/` — terminal UI.
- `web/` — dashboard React frontend.
- `apps/desktop/` — Electron desktop app.
- `hermes_state.py` — SQLite session store.
- `hermes_constants.py`, `hermes_logging.py` — profile-aware paths/logging.

Read `docs/agents-reference.md` before making larger changes in any of these subsystems.

## Gateway / TUI / Dashboard Rules

- GUI or dashboard chat should reuse `/api/ws` and `tui_gateway`; do not create a parallel agent runtime.
- Keep `/api/pty` and Terminal Chat working unless a migration explicitly removes them.
- Frontends should render structured gateway events, not parse ANSI text to recover semantics.
- Session create/resume/submit/interrupt flows must remain responsive while long handlers run.
- For dashboard assets, `npm run build --workspace web` emits into `hermes_cli/web_dist`.
- For TUI assets, `npm run build --workspace ui-tui` emits into `ui-tui/dist`.

## Config and Profiles

- User-facing behavioral config belongs in `config.yaml`.
- `.env` is only for secrets: API keys, tokens, passwords.
- Do not hardcode `~/.hermes`; use profile-aware helpers such as `get_hermes_home()` and related utilities.
- Profile-scoped code must bind reads/writes to the selected profile's home/state.
- Tests must not write to the real `~/.hermes`; use temp `HERMES_HOME`.

## Plugins and Skills

- Prefer skills/plugins for capability that does not need to live in the core model-tool schema.
- Skill files should be fully readable by the agent; do not add offset/limit pagination to instructional content.
- Plugins should live in their own directory/repo and use generic plugin interfaces; do not special-case plugin behavior in core files.
- If a plugin needs more core support, widen the generic plugin surface.

## Testing Guidance

- Use subprocess-per-test-file isolation when global registries/import-time state could leak.
- Prefer E2E/integration tests for resolution chains, config propagation, security boundaries, remote backends, and gateway/session behavior.
- Avoid change-detector tests such as fixed model counts, config version literals, or catalog snapshots.
- Assert invariants and behavior contracts instead.
- For frontend work, at minimum run relevant workspace typecheck/build; lint only touched files if repo-wide lint has pre-existing failures.

## Known Pitfalls

- Do not introduce new `simple_term_menu` usage.
- Do not use `\033[K` ANSI erase-to-EOL in spinner/display code; it breaks incremental rendering in some frontends.
- `_last_resolved_tool_names` in `model_tools.py` is process-global; avoid assumptions that it is per-session.
- Do not hardcode cross-tool references in schema descriptions.
- The gateway has multiple message/approval guards; bypass approval/control commands consistently.
- Squash merges from stale branches can silently revert recent fixes; inspect final diffs against current main.
- Do not wire in dead code without E2E validation.

## When You Need More Detail

Use [`docs/agents-reference.md`](docs/agents-reference.md) for the full original guide, including:

- full project structure notes,
- TypeScript style details,
- slash-command implementation steps,
- skin/theme system,
- plugin and skill frontmatter standards,
- delegation, curator, cron, kanban details,
- expanded profile rules,
- full testing examples.
