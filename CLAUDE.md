# Hermes Agent Development Guide

The canonical instructions for this repository are maintained in
[`AGENTS.md`](AGENTS.md). Claude Code should read and follow that file for the
same project rules used by Codex CLI and other coding agents.

For detailed architecture, rationale, and implementation guidance, consult
[`docs/agents-reference.md`](docs/agents-reference.md).

## Claude Code Development Workflow

After `ExitPlanMode` succeeds, continue the approved implementation instead of
ending the turn. Enter or resume the current session's owned Claude Code
worktree before using any development tool; never modify the primary checkout.
When implementation is complete, record the successful verification for the
exact code snapshot with:

```text
python .claude/hooks/development-workflow.py verify -- <verification command and arguments>
```

Do not bypass the ownership or verification gates, and do not manually commit,
push, or create the PR. The synchronous Stop hook validates the owned worktree
and verified snapshot, then performs those Git and GitHub operations in order.
Hooks cannot invoke `EnterWorktree` themselves: continuation context, primary
checkout tool blocking, and the Stop gate enforce this workflow together.
