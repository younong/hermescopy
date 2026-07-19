---
name: claude-api-lite
description: Build, debug, or migrate Claude API, Anthropic SDK, Claude Agent SDK, prompt caching, tool use, streaming, and Managed Agents integrations without loading the full Claude API reference bundle.
allowed-tools:
  - Read
  - Grep
  - Glob
  - WebFetch
  - WebSearch
---

# Claude API Lite

Use a small routing layer first. Load only the reference files needed for the current task; never read every reference preemptively.

## Guard the provider boundary

Before editing, inspect the target code or project for provider markers such as `anthropic`, `@anthropic-ai/sdk`, `claude-*`, `openai`, `OpenAI(`, `langchain_openai`, or explicit provider-neutral abstractions.

- For an existing Anthropic integration, preserve its provider and architecture.
- For provider-neutral or non-Anthropic code, do not insert Anthropic-specific calls silently. Ask whether to convert the implementation or keep it provider-neutral when the request does not already decide that question.
- Prefer the official Anthropic SDK for supported languages. Use raw HTTP only when explicitly requested, when working in shell/cURL, or when no official SDK exists.
- Never use an OpenAI-compatible shim as a substitute for the Anthropic SDK.

## Route the task

Determine the language from the target file and manifests, then read the minimum matching references:

| Task | Read |
|---|---|
| Python Messages API, streaming, tools, files, structured output | `references/python.md` |
| TypeScript/JavaScript Messages API, streaming, tools, files, structured output | `references/typescript.md` |
| Claude Agent SDK | `references/agent-sdk.md` plus the matching language reference |
| Model selection, retired IDs, or migration | `references/models-and-migration.md` |
| Prompt caching or cache-hit debugging | `references/prompt-caching.md` plus the matching language reference |
| Managed Agents / server-hosted workspaces | `references/managed-agents.md` plus the matching language reference |
| SDK method or feature not explicitly covered above | `references/live-sources.md`, then fetch the exact official page or SDK source |

For Java, Go, Ruby, C#, PHP, Kotlin, Scala, or raw HTTP, start with `references/live-sources.md` and fetch the official language SDK documentation. Do not translate method names or types from another language by analogy.

## Implementation workflow

1. Read the target code and its nearest focused test.
2. Read only the routed reference files.
3. Inspect the installed SDK version in the lockfile or package metadata. Do not assume the newest documentation matches the installed version.
4. Verify any method name, beta header, model ID, request field, or response type not explicitly documented in the loaded reference against an official source from `references/live-sources.md`.
5. Implement with the existing project conventions. Keep API keys in environment/configuration, never in source.
6. Preserve the complete content-block model: responses may contain text, thinking, tool use, citations, or other block types. Do not assume `content[0].text` is always valid.
7. Prefer adaptive thinking for nontrivial work when the selected model supports it. Prefer streaming for long inputs, long outputs, or high output limits.
8. Run focused tests and, when practical, a mocked request-shape test. Do not make a paid/live API request unless the user requested it or the repository explicitly defines one as validation.

## Defaults

- Choose the latest capable Claude model available to the user's account and deployment. As of this skill revision, prefer `claude-fable-5`; use `claude-opus-4-8` when Fable 5 is unavailable or incompatible with the deployment.
- Treat model IDs, feature availability, beta headers, and SDK signatures as time-sensitive. Verify them before introducing or migrating production code.
- Use automatic prompt caching unless the application needs explicit cache boundaries or TTL control.
- Use the Messages API for normal calls and client-hosted tool loops. Use Managed Agents only when Anthropic should host the persistent agent session and execution workspace.

## Focused flows

### Migration

Before changing code, confirm the migration scope and target model when either is missing. Read `references/models-and-migration.md`, classify each affected integration by source model/provider/SDK, then explain each breaking change and test the resulting request shape.

### Managed Agents onboarding

Read `references/managed-agents.md`. Establish the job, required tools/data/credentials, environment, reusable agent configuration, and per-run session lifecycle before writing integration code.

## Context discipline

- Keep this entry skill lean.
- Search large references for the relevant heading before reading broad sections.
- Do not concatenate reference files into the conversation.
- Stop after loading enough documentation to verify the current change.
