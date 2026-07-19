# Official Live Sources

Use official sources to verify time-sensitive SDK signatures, model IDs, feature support, beta headers, and migration requirements.

## Documentation indexes

- Claude Platform documentation index: `https://platform.claude.com/llms.txt`
- Claude API overview: `https://platform.claude.com/docs/en/api/overview`
- Models overview: `https://platform.claude.com/docs/en/about-claude/models/overview`
- Model deprecations: `https://platform.claude.com/docs/en/about-claude/model-deprecations`
- Prompt caching: `https://platform.claude.com/docs/en/build-with-claude/prompt-caching`
- Tool use: `https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview`
- Streaming: `https://platform.claude.com/docs/en/build-with-claude/streaming`
- Token counting: `https://platform.claude.com/docs/en/build-with-claude/token-counting`
- Files API: `https://platform.claude.com/docs/en/build-with-claude/files`

If a path redirects or moves, fetch `llms.txt`, locate the current official page, then fetch that page.

## Official SDK repositories

- Python SDK: `https://github.com/anthropics/anthropic-sdk-python`
- TypeScript SDK: `https://github.com/anthropics/anthropic-sdk-typescript`
- Java SDK: `https://github.com/anthropics/anthropic-sdk-java`
- Go SDK: `https://github.com/anthropics/anthropic-sdk-go`
- Ruby SDK: `https://github.com/anthropics/anthropic-sdk-ruby`
- C# SDK: `https://github.com/anthropics/anthropic-sdk-csharp`
- PHP SDK: `https://github.com/anthropics/anthropic-sdk-php`
- Claude Agent SDK Python: `https://github.com/anthropics/claude-agent-sdk-python`
- Claude Agent SDK TypeScript: `https://github.com/anthropics/claude-agent-sdk-typescript`

Prefer a tag matching the project's installed version. Use repository examples/tests to confirm APIs omitted from prose documentation.

## Fetch discipline

1. Fetch only the page or repository file needed for the current question.
2. Prefer first-party Anthropic sources over blogs, snippets, or generated examples.
3. Compare publication/version information with the installed dependency.
4. State when an API is beta or unavailable on a third-party deployment.
5. Never load the entire documentation index or every reference into context when one page is sufficient.
