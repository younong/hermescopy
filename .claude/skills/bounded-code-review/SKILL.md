---
name: bounded-code-review
description: Review Hermes code changes with a strict budget of at most five Agent calls.
disable-model-invocation: true
allowed-tools:
  - Read
  - Grep
  - Glob
  - Bash(git status *)
  - Bash(git diff *)
  - Bash(git show *)
  - Bash(git log *)
  - Bash(git merge-base *)
  - Bash(git branch *)
  - Bash(git rev-parse *)
  - Bash(scripts/run_tests.sh *)
---

# Bounded Code Review

Review target: `$ARGUMENTS`

Perform and synthesize the review in the current/main conversation.

## Hard budget

- The global `Agent` budget for this complete user review request is **at most 5 calls**. Use fewer when direct inspection is sufficient.
- Finder, verifier, Explore, general-purpose, and candidate-specific review agents all count against the same budget; retries and follow-up calls count too.
- The `allowed-tools` list only pre-approves serial read-only tools; the five-call rule here and in `CLAUDE.md` is the governing review policy.
- Never call `Workflow` or another review skill, and never delegate work to an agent that could create more agents.
- If this skill is somehow loaded inside a subagent, stop and report that the review must run from the main conversation.
- If the target is too large to finish within the budget, state exactly what remains unreviewed. Do not exceed the limit, silently sample, or imply complete coverage.

## Scope

1. If the user supplied a commit, range, branch, PR-derived range, or path, review that exact target.
2. Otherwise inspect the working tree (`git status` and the relevant staged/unstaged diff).
3. Include uncommitted changes only when they are part of the requested target or when no explicit target was supplied.
4. Do not broaden a named target to unrelated files.

## Review workflow

1. Read the changed-file list and every in-scope diff hunk.
2. For each hunk, read the enclosing function or component and the closest focused test.
3. Follow the repository navigation rules in `CLAUDE.md`: use focused symbol/error/config searches first and expand by only one adjacent subsystem when needed.
4. Use up to five narrowly scoped Agent calls only when they materially improve coverage. Give each call a distinct, bounded scope and do not delegate orchestration.
5. Check, in order:
   - correctness and reachable failure paths;
   - removed guards or behavior not re-established by the change;
   - caller/callee contract changes and cross-file state or ordering assumptions;
   - security and isolation boundaries when in scope;
   - unnecessary duplication, complexity, or material hot-path waste.
6. Consolidate candidates in the main conversation. Do not create one agent or task per candidate.
7. Verify every candidate in the main conversation with direct reads, focused searches, relevant history, and the narrowest applicable test. Treat agent output as leads, not findings, and discard claims contradicted by the code or lacking a concrete reachable impact.
8. Do not modify files unless the user explicitly requested a fixing mode after the review. This skill itself is for review, not implementation.

## Output

- Return actionable findings first, ordered by severity.
- For each finding include a concise title, `path:line`, the concrete failure scenario, and why existing code or tests do not prevent it.
- Keep cleanup findings behind correctness/security findings and include them only when the cost is concrete.
- If no finding survives verification, say so explicitly.
- End with validation performed and any scope that was not reviewed.
