# Claude API with TypeScript

Use this file for TypeScript and JavaScript request shapes. Confirm the installed `@anthropic-ai/sdk` version before relying on recent fields.

## Client and basic messages

```ts
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic(); // reads ANTHROPIC_API_KEY
const message = await client.messages.create({
  model: "claude-fable-5",
  max_tokens: 2048,
  messages: [{ role: "user", content: "Explain this diff." }],
});

const text = message.content
  .filter((block) => block.type === "text")
  .map((block) => block.text)
  .join("");
```

Narrow on `block.type` before accessing type-specific fields. Do not cast all content to text.

## Streaming

```ts
const stream = client.messages.stream({
  model: "claude-fable-5",
  max_tokens: 4096,
  messages: [{ role: "user", content: prompt }],
});

stream.on("text", (delta) => render(delta));
const finalMessage = await stream.finalMessage();
```

Use the final-message helper when downstream code needs the complete typed response. Treat raw SSE events as deltas, not complete blocks.

## Thinking

```ts
const message = await client.messages.create({
  model: "claude-fable-5",
  max_tokens: 8192,
  thinking: { type: "adaptive" },
  messages: [{ role: "user", content: prompt }],
});
```

Verify current model compatibility before combining thinking with sampling fields or older budget-based examples.

## Manual tool loop

Supply JSON Schema tools, append the full assistant content to history, execute every `tool_use`, and return matching `tool_result` blocks in the next user message.

```ts
const tools: Anthropic.Tool[] = [{
  name: "get_weather",
  description: "Get current weather for a city.",
  input_schema: {
    type: "object",
    properties: { city: { type: "string" } },
    required: ["city"],
    additionalProperties: false,
  },
}];

const response = await client.messages.create({
  model: "claude-fable-5",
  max_tokens: 4096,
  tools,
  messages: history,
});

history.push({ role: "assistant", content: response.content });
const results = await Promise.all(
  response.content
    .filter((block) => block.type === "tool_use")
    .map(async (block) => ({
      type: "tool_result" as const,
      tool_use_id: block.id,
      content: await dispatch(block.name, block.input),
    })),
);
if (results.length) history.push({ role: "user", content: results });
```

Validate `block.input` at the application boundary even when the schema is strict. Represent execution failures with an error tool result. Verify beta tool-runner and Zod helper names against the installed SDK before using them.

## Files, images, and structured output

Use ordered content blocks and documented source shapes for images/documents. Prefer Files API IDs for reused uploads when the feature is available. Treat Files API beta headers and content-block forms as version-sensitive.

Use documented structured-output helpers or `output_config.format` when supported. Do not force JSON using assistant prefills on models that reject prefills.

## Errors and verification

- Distinguish typed authentication, permission, validation, rate-limit, overloaded, and connection errors.
- Keep retries bounded, idempotent, and respectful of server timing.
- Mock the SDK boundary and assert exact request objects.
- Test mixed blocks, multiple tool calls, tool failures, stream interruption, and final aggregation.
