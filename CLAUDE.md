# Hermes Repository Navigation

Use this file to route work quickly. [`AGENTS.md`](AGENTS.md) remains the
universal engineering guide; [`docs/agents-reference.md`](docs/agents-reference.md)
contains detailed architecture and rationale. When they differ from the code,
follow the implementation and its closest focused tests.

## Search Workflow

1. Start with the ownership map below, then read the target region and closest
   focused test.
2. Search symbols, routes, configuration keys, or exact error text in the
   relevant source and test paths.
3. Expand into one adjacent subsystem only when the focused path is insufficient.
4. Use a repository-wide search only to locate an unknown entry point—not as the
   default starting point.
5. Do not use the `Explore` agent for routine repository navigation. Prefer
   focused `rg` and direct file reads; reserve `Explore` for genuinely broad,
   uncertain discovery after targeted searching is insufficient.
6. Use `rg --no-ignore` only when generated or ignored output is explicitly in
   scope.
7. For intent-sensitive behavior, follow the existing convention:
   `git log -p -S <symbol>`.
8. For Claude API, Anthropic SDK, Claude Agent SDK, prompt caching, tool use,
   streaming, model migration, or Managed Agents work, invoke the project
   `claude-api-lite` skill. Do not invoke the disabled bundled `claude-api`
   skill; its eager reference bundle exceeds this project's context budget.

## Bounded Code Reviews

- Code reviews in this repository have a global Claude Code `Agent` budget of
  **0**. Review and verify findings serially in the main conversation.
- Do not call `Agent` or `Workflow`, delegate review work, or spawn finder,
  verifier, Explore, general-purpose, or candidate-specific subagents.
- Never invoke `code-review` or another review skill from inside a subagent, and
  never ask a delegated agent to invoke a review skill or create more agents.
- Prefer the project `/bounded-code-review` skill for explicit reviews. Use
  focused searches, direct reads, relevant history, and applicable tests.
- If the scope is too large for the main conversation, identify what remains
  unreviewed instead of fanning out or implying complete coverage.

Examples:

```bash
rg -n "OwnerWorkerSupervisor|owner_worker_env_for" \
  hermes_cli/owner_worker hermes_cli/owner_runtime.py \
  tests/hermes_cli/test_owner_worker.py tests/hermes_cli/test_owner_runtime.py

rg -n "session_detail_payload|resolve_resume_session_id" \
  hermes_cli/session_api.py tests/hermes_state/test_resolve_resume_session_id.py
```

## Automated Development Lifecycle

For every request that changes repository files:

1. Use exactly one dedicated worktree for the entire task, including after
   context compaction or session resumption. Before editing, determine whether
   the task already has an active worktree:
   - If the current checkout is already under `.claude/worktrees/`, continue in
     it and do not call `EnterWorktree` again.
   - If this task already has another registered worktree, re-enter it with
     `EnterWorktree(path=...)` instead of creating a new one.
   - Otherwise, use `EnterWorktree` once to create a descriptive worktree. The
     project setting `worktree.baseRef: "fresh"` makes it branch from the latest
     `origin/main`; do not develop in the primary checkout or from its current
     feature branch.
   Context compaction and session resumption continue the existing task; they
   never justify creating a replacement worktree.
2. Keep all implementation and validation inside that task's worktree.
3. Run the focused validation required by the **Validation** section below.
   Here, "required validation" means those prescribed local checks for the
   current change; do not expand it into an implicit requirement to wait for
   GitHub CI or other remote checks. If a prescribed local check fails, do not
   publish changes; report the blocker instead. A release waits for remote CI
   only when its release procedure explicitly requires that gate.
4. After validation succeeds, review the final diff and repository status,
   commit all task changes with the required Claude co-author trailer, push the
   branch to `origin`, and create a GitHub pull request targeting `main` with a
   concise summary and test results.
5. The repository owner has durably authorized commit, push, and PR creation as
   the default completion steps for development tasks in this repository. Do
   not ask for those instructions again. Still request confirmation for force
   pushes, destructive operations, merging, deployment, release, or publishing
   anywhere other than the task branch and its PR.
6. Documentation-only changes to Claude workflow/configuration follow the same
   lifecycle. Pure research, review, explanation, and read-only verification do
   not require a worktree or PR.

## Choose a Work Path

- **Fast:** the target file and closest focused test are known, the change is
  local, and no Strict trigger in `AGENTS.md` applies.
- **Standard:** the default; use the ownership map and focused-search workflow
  above, expanding into one adjacent subsystem only when necessary.
- **Strict:** follow the matching ownership row, read the relevant reference
  heading, and use the real-path validation policy in `AGENTS.md`.

Escalate to Strict before editing when work reaches owner-worker, session/resume,
gateway/approval/security, profiles or config propagation, remote I/O, or another
client surface.

## High-Frequency Ownership Map

| Change area | Start with | Focused validation |
| --- | --- | --- |
| Owner-worker lifecycle, leases, startup | `hermes_cli/owner_worker/supervisor.py` | `tests/hermes_cli/test_owner_worker.py` |
| Owner-worker WebSocket and PTY routing | `hermes_cli/owner_worker/ws_routes.py` | `tests/hermes_cli/test_owner_worker_ws_bridge.py` |
| Owner runtime paths and environment isolation | `hermes_cli/owner_runtime.py` | `tests/hermes_cli/test_owner_runtime.py` |
| Session API and resume semantics | `hermes_cli/session_api.py` | `tests/hermes_state/test_resolve_resume_session_id.py`, then the closest session API test |
| MCP discovery and startup sequencing | `hermes_cli/mcp_startup.py` | `tests/hermes_cli/test_mcp_startup.py` |
| Dashboard server integration | Search a known route or handler inside `hermes_cli/web_server.py` | Select the matching `tests/hermes_cli/test_web_server_*.py` concern test |
| Dashboard frontend | `web/`, after locating its API/server path | Relevant workspace typecheck/build |
| TUI and gateway transport | `tui_gateway/`, then `ui-tui/` | Relevant gateway/TUI test and workspace check |

Do not treat `hermes_cli/owner_worker/` or `hermes_cli/web_server.py` as
monoliths. For owner-worker work, choose the concern-specific module first:
`supervisor.py` for process lifecycle and fencing, `ws_routes.py` for WebSocket,
PTY, attach-token, and event behavior, and `owner_runtime.py` for controlled
paths and environments. For `web_server.py`, search by a known route, handler,
request field, or subsystem identifier, then read only the matching region.

## Validation

Use the canonical test runner rather than direct `pytest`:

```bash
scripts/run_tests.sh tests/path/to/affected_test.py
```

Fast work normally stops at the narrowest affected test file. Standard work
expands only across the directly affected boundary. Strict work follows the
real-path integration guidance in `AGENTS.md` for configuration propagation,
security boundaries, session state, file/network I/O, and gateway transport. For
frontend changes, run the applicable workspace typecheck and build described in
`AGENTS.md`.

### Dashboard browser authentication

Before browser-validating a password-protected Hermes dashboard, run:

```bash
python3 scripts/playwright_dashboard_login.py [--url <dashboard-base-url>]
```

The helper reads the ignored local `.env.local`, logs in without exposing the
credentials in command arguments, and leaves the authenticated
`hermes-validation` Playwright CLI session open. The file must contain
`HERMES_DASHBOARD_BROWSER_USERNAME` and `HERMES_DASHBOARD_BROWSER_PASSWORD`
and have permissions `0600`. Continue validation with
`playwright-cli -s=hermes-validation ...`, then close it with
`playwright-cli -s=hermes-validation close`.

Never read, print, manually copy, or `source` `.env.local`. The repository's
`.worktreeinclude` is the only permitted propagation mechanism: Claude Code
automatically copies the ignored file into newly created worktrees without the
agent inspecting its contents. If the helper reports missing or unsafe
credentials, ask the user to edit the file locally; never ask them to paste a
password into the conversation.
