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
9. Before reading release instructions or running commands for any publish,
   deploy, tag, rollback, or production-status operation, first invoke the
   project `hermes-release` skill.

## Chat GUI UI Boundary

When working on the Chat GUI, do not use the Admin Dashboard as a visual or
layout reference. Prefer the existing Chat GUI patterns in
`web/src/pages/GuiChatPage.tsx`, `web/src/features/gui-chat/**`, and
`web/src/components/ChatSessionList.tsx`.

For Chat GUI changes:

- Treat Chat GUI as a conversation-first product surface.
- Preserve the visual priority of the conversation stream and message composer.
- Do not copy or infer UI patterns from Admin Dashboard pages or components.
- Do not use Dashboard page headers, metric cards, dense settings forms,
  analytics layouts, admin tables, or other dashboard-specific visual patterns
  unless the task explicitly requests it.
- If a suitable Chat GUI pattern does not exist, create a Chat GUI-specific
  pattern instead of adapting a Dashboard pattern.

Before finishing a Chat GUI change, verify that the result still looks and
behaves like a chat workspace rather than an administration console.

## Replacement and Cleanup Policy

- Prefer modifying or reusing existing implementations over adding parallel
  ones. Before adding a function, state field, configuration key, abstraction,
  or execution path, search for the implementation it replaces or can extend.
- When a new implementation supersedes an old one, remove the old code, callers,
  compatibility branches, imports, configuration, comments, and test fixtures
  in the same change unless an explicit compatibility requirement exists.
- Do not introduce `v2`, wrapper, fallback, or temporary compatibility paths
  without documenting why callers cannot migrate directly and what will remove
  the compatibility path.
- After implementation, perform a simplification pass proportional to the chosen
  engineering path. For Fast work, review the focused diff directly; invoke
  `/simplify` only when the change adds one of the structural elements listed
  above or non-trivial duplication. For Standard and Strict work, use `/simplify`
  when available. Run focused validation on the resulting code, then review the
  final diff for duplication and unreachable code. If later cleanup edits affect
  behavior, rerun the corresponding checks.
- In the completion report, identify obsolete code removed. If none was removed,
  explain why all affected existing code remains necessary. Do not optimize for
  a negative line count; optimize for the smallest complete implementation.

## Bounded Subagent Concurrency

- At most **2 subagents total** may be active concurrently across the main
  conversation and every nested delegation level, regardless of task type or
  how they are launched.
- Delegated agents must not launch descendants unless the main conversation has
  explicitly reserved one of those two global slots for that descendant.
- This limits concurrency, not total calls. A completed subagent frees its slot.

## Bounded Code Reviews

- Code reviews in this repository have a global Claude Code `Agent` budget of
  **at most 5 calls per user review request**. Use fewer when focused review in
  the main conversation is sufficient.
- Call review agents only from the main conversation. Do not call `Workflow`,
  and do not ask a delegated agent to invoke a review skill or create agents.
- Count finder, verifier, Explore, general-purpose, and candidate-specific
  review agents against the same five-call budget; verification agents do not
  receive a separate allowance.
- Prefer the project `/bounded-code-review` skill for explicit reviews. Use
  focused searches, direct reads, relevant history, and applicable tests, then
  verify and synthesize all agent output in the main conversation.
- If the scope is too large for the five-call budget, identify what remains
  unreviewed instead of exceeding the budget or implying complete coverage.

Examples:

```bash
rg -n "OwnerWorkerSupervisor|owner_worker_env_for" \
  hermes_cli/owner_worker hermes_cli/owner_runtime.py \
  tests/hermes_cli/test_owner_worker.py tests/hermes_cli/test_owner_runtime.py

rg -n "session_detail_payload|resolve_resume_session_id" \
  hermes_cli/session_api.py tests/hermes_state/test_resolve_resume_session_id.py
```

## Automated Development Lifecycle

Treat one coherent user goal, including its follow-up requests, as one task and
run one development lifecycle for it. Reuse the task's worktree, branch, and pull
request throughout.

For every task that changes repository files:

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
   never justify creating a replacement worktree. If the primary-checkout guard
   blocks an edit after either event, use the registered candidates in its error
   message to identify the task's original worktree, then call
   `EnterWorktree(path=...)` with that exact path. Do not create a replacement
   with `EnterWorktree(name=...)` or a pathless call. If multiple candidates are
   ambiguous, resolve them from the current task and branch or ask the user; only
   create a worktree after confirming that none belongs to the current task.
2. Keep all implementation and validation inside that task's worktree.
3. Run the focused validation required by the **Validation** section below.
   Here, "required validation" means those prescribed local checks for the
   current change. If a check exposes a product failure, do not publish changes;
   report the blocker instead. If a check is blocked only by a local environment
   or tooling problem, immediately use the browser fallback described below
   instead of repeatedly repairing the environment. Releases do not wait for
   GitHub CI or other remote checks; only the prescribed local validation (or its
   permitted browser fallback) and checks performed by the release procedure
   itself gate a release.
4. Once the task is coherent and validation succeeds, review the final diff and
   repository status, then leave exactly one task commit on top of the branch's
   merge base with `origin/main`. The commit must include the required Claude
   co-author trailer. Push the branch to `origin` and create a GitHub pull
   request targeting `main` with a concise summary and test results. Before each
   push, verify that the PR branch contains exactly one task commit. If the task
   already has a PR, amend that commit rather than adding follow-up commits,
   update it with `--force-with-lease`, and update PR metadata only when its
   summary or reported validation materially changes. If a task branch already
   accumulated multiple commits, squash them into that single task commit before
   the next push.
5. The repository owner has durably authorized commit, push, and PR creation as
   the default completion steps for development tasks in this repository. They
   have also authorized lease-protected rewrites of the current task's own PR
   branch solely to preserve the single-commit invariant; verify the observed
   remote tip and use `--force-with-lease`, never an unconditional force push.
   Do not ask for those instructions again. Still request confirmation for any
   other force push, destructive operation, merge, deployment, release, or
   publishing anywhere other than the task branch and its PR.
6. Documentation-only changes to Claude workflow/configuration follow the same
   lifecycle. Pure research, review, explanation, and read-only verification do
   not require a worktree or PR.

## Choose a Work Path

- **Fast:** the target file and closest focused test are known, the change is
  local, and no Strict trigger in `AGENTS.md` applies. Apply the narrowest scope
  prescribed by the search, cleanup, and validation sections. Fast work does not
  use Plan Mode, task tracking, or subagents by default; add only the mechanism
  needed if the user requests it or the task no longer meets the Fast criteria.
- **Standard:** the default; use the ownership map and focused-search workflow
  above, expanding into one adjacent subsystem only when necessary.
- **Strict:** follow the matching ownership row, read the relevant reference
  heading, and use the real-path validation policy in `AGENTS.md`.

Choose orchestration by need, not file count: use Plan Mode only for unresolved
choices that require user alignment, task tracking for staged or dependent work,
and subagents for bounded independent work or broad unresolved discovery.

Escalate to Strict before editing when work reaches owner-worker, session/resume,
gateway/approval/security, profiles or config propagation, remote I/O, or another
client surface.

## High-Frequency Ownership Map

| Change area | Start with | Focused validation |
| --- | --- | --- |
| Model catalog, registration, activation (all five kinds) | `hermes_cli/model_plane/`, then `hermes_cli/model_registrations.py`; extension rules in `docs/model-plane.md` | `tests/hermes_cli/test_model_plane.py`, `tests/hermes_cli/test_model_registrations.py` |
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

### Browser fallback for environment failures

When a prescribed validation command cannot run because of a local environment
or tooling problem—such as unavailable dependencies, incompatible host tooling,
or a broken local test service—do not repeatedly troubleshoot or treat that as a
product failure. Start the affected application through an available working
path and immediately validate the same user-visible behavior in a real browser.
Exercise the changed flow and its closest regression path, record the browser
steps and result in the PR, and clearly identify which automated command was
replaced and why. Browser validation is a fallback for environment-only
blockers; it does not replace a reproducible failing test, type error, build
error, or other product defect.

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
