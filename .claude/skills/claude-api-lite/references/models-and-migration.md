# Models and Migration

Model information is time-sensitive. Verify availability for the user's provider and account before changing production code.

## Current preference

At this skill revision:

- Prefer `claude-fable-5` as the latest capable default when available.
- Use `claude-opus-4-8` when Fable 5 is unavailable or the target deployment does not support it.
- Use current Sonnet for balanced throughput/cost and Haiku only for simple latency-sensitive workloads when the user or application requirements justify it.

Do not replace a deliberate model tier solely because a newer model exists.

## Migration workflow

1. Confirm the exact files/directories and destination model.
2. Inventory every source model ID and provider surface: first-party API, Claude Platform on AWS, Amazon Bedrock, Google Vertex AI, or Microsoft Foundry.
3. Record the installed SDK version, API endpoint, beta headers, thinking configuration, sampling parameters, prefills, structured outputs, caching, tool definitions, and stop-reason handling.
4. Fetch current official migration guidance from `references/live-sources.md` for both source and destination models.
5. Apply only documented changes. Provider-specific model IDs are not interchangeable.
6. Explain each behavior-changing edit.
7. Test request construction and response handling, including refusals, max-token stops, tool calls, and mixed content blocks.

## Common migration risks

- Replacing older fixed thinking budgets with adaptive thinking without checking model support.
- Keeping sampling parameters that are incompatible with the selected thinking mode.
- Using assistant prefills to force JSON when the destination model requires structured outputs.
- Leaving obsolete beta headers enabled.
- Assuming first-party model strings work on Bedrock, Vertex, Foundry, or another gateway.
- Reading only text blocks and missing refusal/fallback/structured stop information.
- Changing the model but not updating output limits, latency expectations, tests, and cost controls.

## Verification sources

Fetch the current model overview, model deprecations, migration pages, and SDK repository from `references/live-sources.md`. If official sources conflict with cached guidance, follow the current official source and state the discrepancy.
