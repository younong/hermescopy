# Prompt Caching

Optimize for a byte-stable reusable prefix followed by changing content.

## Placement

Order request content from most stable to most dynamic:

1. Stable system instructions and large shared reference material.
2. Stable tool definitions.
3. Reusable conversation prefix.
4. Current-turn user input and other dynamic suffixes.

Prefer automatic caching first. Add explicit `cache_control` only when the application needs a deliberate boundary, a specific supported TTL, or compatibility with an existing cache design.

## Invariants

- Do not mutate earlier messages between turns.
- Keep tool definition order and serialized schemas stable.
- Keep system prompt bytes stable; whitespace and ordering matter.
- Append new turns rather than rebuilding semantically equivalent prefixes differently.
- Place per-request timestamps, IDs, retrieved snippets, and user-specific state after the cacheable prefix.
- Do not assume concurrent requests can read a cache entry before the request that creates it has begun returning.

## Diagnosis

Inspect response usage fields for cache creation and cache reads. Compare serialized request prefixes, not only logical objects. Common silent invalidators include:

- Reordered tools or JSON properties.
- Dynamic system-prompt text.
- Inserting content before the cache boundary.
- Switching models or providers.
- Changing beta headers or feature modes that affect the prompt representation.
- A cache breakpoint falling outside the provider's active lookback behavior.

## Testing

Build two sequential requests with an identical large prefix and different suffixes. Assert the second request reports a cache read in an explicitly authorized integration test. In unit tests, assert stable message/tool ordering and the intended `cache_control` placement; do not fabricate provider usage as proof of a real cache hit.

Fetch current TTL, pricing, breakpoint, and lookback details from `references/live-sources.md` before making numerical claims.
