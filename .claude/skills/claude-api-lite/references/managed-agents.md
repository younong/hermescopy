# Managed Agents

Managed Agents are for server-managed, stateful agents with persistent configuration and hosted execution workspaces. Confirm the feature is available on the user's provider before recommending it.

## Mandatory lifecycle

Use the lifecycle **create an agent once, create a session for each run**.

- Store the reusable agent ID returned by agent creation.
- Reference that agent ID, or an explicitly pinned version, when creating sessions.
- Do not create a new agent inside every request path.
- Open the event stream before sending work when the current API guidance requires stream-first ordering.

## Choose Managed Agents when

- Anthropic should host the tool-execution workspace.
- Sessions are long-running or stateful.
- Agent configuration should be persisted and versioned.
- Hosted files, skills, MCP connections, memory, deployments, or multiagent sessions are required.

Use a normal Messages API tool loop when the application hosts its own compute and needs direct control over execution.

## Onboarding sequence

1. Define the job and expected deliverables.
2. Identify tools, data, credentials, network access, and error-recovery needs.
3. Choose a hosted or supported self-hosted environment.
4. Create and persist the reusable agent configuration.
5. Create a session and attach required resources/credential vaults using current documented APIs.
6. Connect to the event stream, send the user event, process tool confirmations/results, and wait for a documented terminal state.
7. Retrieve deliverables and archive/delete resources according to retention requirements.

## Safety and reliability

- Keep non-MCP secrets host-side or in the documented vault mechanism; never write secrets into prompts or skill files.
- Handle reconnects without dropping or duplicating events.
- Distinguish queued, processing, idle, failed, interrupted, and terminal states using documented fields.
- Bound polling and persist cursors/IDs needed for recovery.
- Verify webhook signatures before processing events.
- Treat environment and session files as untrusted input.

## Documentation rule

Endpoint names, beta headers, SDK namespaces, event types, and environment capabilities change. Fetch the current Managed Agents overview and endpoint documentation from `references/live-sources.md` before emitting code. Do not infer Managed Agents APIs from the Messages SDK.
