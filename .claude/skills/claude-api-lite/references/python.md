# Claude API with Python

Use this file for Python-specific request shapes. Confirm the installed `anthropic` version before relying on recently added fields.

## Client and basic messages

```python
import anthropic

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
message = client.messages.create(
    model="claude-fable-5",
    max_tokens=2048,
    messages=[{"role": "user", "content": "Explain this diff."}],
)

text = "".join(
    block.text for block in message.content if block.type == "text"
)
```

Use `anthropic.AsyncAnthropic` in async applications. Configure timeouts/retries on the SDK client or request options rather than wrapping SDK calls in unbounded retry loops.

## Streaming

Prefer the high-level stream manager when the installed SDK supports it:

```python
with client.messages.stream(
    model="claude-fable-5",
    max_tokens=4096,
    messages=[{"role": "user", "content": prompt}],
) as stream:
    for text in stream.text_stream:
        render(text)
    final_message = stream.get_final_message()
```

Do not treat each network chunk as a complete content block. Aggregate with the SDK helper when a final typed message is needed.

## Thinking

For supported current models, adaptive thinking is the default for nontrivial reasoning:

```python
message = client.messages.create(
    model="claude-fable-5",
    max_tokens=8192,
    thinking={"type": "adaptive"},
    messages=[{"role": "user", "content": prompt}],
)
```

Re-check official model documentation before combining thinking with sampling parameters or older `budget_tokens` examples.

## Manual tool loop

Define tools with JSON Schema, send them in `tools`, execute every returned `tool_use`, and return corresponding `tool_result` blocks in the next user message. Preserve all assistant content blocks exactly in history.

```python
history = [{"role": "user", "content": request}]
response = client.messages.create(
    model="claude-fable-5",
    max_tokens=4096,
    tools=[{
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    }],
    messages=history,
)
history.append({"role": "assistant", "content": response.content})

results = []
for block in response.content:
    if block.type == "tool_use":
        output = dispatch(block.name, block.input)
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output,
        })
if results:
    history.append({"role": "user", "content": results})
```

Handle multiple tool calls in one response. Validate tool inputs and return `is_error: true` for execution failures instead of inventing a successful result.

The SDK also provides beta tool-runner helpers in supported releases. Verify decorator names and runner signatures against the installed SDK before using them.

## Images and documents

Message content is an ordered list of blocks. For images, use a supported `source` (`base64` or URL where documented) and the correct media type. For reusable files, prefer the Files API when available, then reference the returned file ID using the documented content block. Verify beta headers and exact block shapes against current docs.

## Structured output

Use the SDK's documented structured-output helper for the installed version, or pass `output_config.format` with a valid JSON Schema when supported. Do not force JSON with an assistant prefill on models where prefills are unsupported. Treat parsed output helpers as version-sensitive and confirm their exact names.

## Errors

Catch typed SDK exceptions at the boundary where recovery is possible. Distinguish authentication/permission/validation failures from retryable rate-limit, overloaded, and transient network failures. Respect `retry-after`; keep retries bounded and idempotent.

## Verification

- Assert the request kwargs sent to `client.messages.create` or `.stream`.
- Test mixed content blocks, multiple tool calls, tool errors, stream interruption, and final aggregation.
- Never assert only a text happy path when the application enables thinking or tools.
