---
name: bounded-code-review
description: Review Hermes code changes serially in the main thread with a strict zero-subagent budget.
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

Perform the entire review serially in the current/main conversation.

## Hard budget

- The global `Agent` budget for this complete review is **0**.
- The `allowed-tools` list only pre-approves the serial read-only tools; the zero-agent rule here and in `CLAUDE.md` is the governing review policy.
- Never call `Agent`, `Workflow`, or another review skill.
- Never create finder, verifier, Explore, general-purpose, or candidate-specific subagents.
- Never delegate work to an agent that could create more agents.
- If this skill is somehow loaded inside a subagent, stop and report that the review must run in the main conversation.
- If the target is too large to finish, state exactly what remains unreviewed. Do not fan out, silently sample, or imply complete coverage.

## Scope

1. If the user supplied a commit, range, branch, PR-derived range, or path, review that exact target.
2. Otherwise inspect the working tree (`git status` and the relevant staged/unstaged diff).
3. Include uncommitted changes only when they are part of the requested target or when no explicit target was supplied.
4. Do not broaden a named target to unrelated files.

## Serial review workflow

1. Read the changed-file list and every in-scope diff hunk.
2. For each hunk, read the enclosing function or component and the closest focused test.
3. Follow the repository navigation rules in `CLAUDE.md`: use focused symbol/error/config searches first and expand by only one adjacent subsystem when needed.
4. Check, in order:
   - correctness and reachable failure paths;
   - removed guards or behavior not re-established by the change;
   - caller/callee contract changes and cross-file state or ordering assumptions;
   - security and isolation boundaries when in scope;
   - unnecessary duplication, complexity, or material hot-path waste.
5. Record candidates locally in the current reasoning. Do not create one task or process per candidate.
6. Verify every candidate yourself with direct reads, focused searches, relevant history, and the narrowest applicable test. Discard claims contradicted by the code or lacking a concrete reachable impact.
7. Do not modify files unless the user explicitly requested a fixing mode after the review. This skill itself is for review, not implementation.

## Output

- Return actionable findings first, ordered by severity.
- For each finding include a concise title, `path:line`, the concrete failure scenario, and why existing code or tests do not prevent it.
- Keep cleanup findings behind correctness/security findings and include them only when the cost is concrete.
- If no finding survives verification, say so explicitly.
- End with validation performed and any scope that was not reviewed.
