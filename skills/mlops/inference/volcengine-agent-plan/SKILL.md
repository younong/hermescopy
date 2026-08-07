---
name: volcengine-agent-plan
description: Generate multimodal embeddings with Agent Plan.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [volcengine, embedding, multimodal, agent-plan]
    category: mlops
---

# Volcengine Agent Plan

Create one dense vector for text, an image, or a combined text-image sample
with `doubao-embedding-vision`. This capability uses the profile-scoped
`VOLCENGINE_AGENT_PLAN_API_KEY` configured for the first-class
`volcengine-agent-plan` provider.

## When to Use

- The user needs an embedding for semantic retrieval or vector indexing.
- The user needs text and image content represented in one vector space.
- The user wants vector dimensions and usage metadata without dumping a large
  vector into the conversation.

## How to Run

Use the `terminal` tool to call the bundled script. Save vectors to a file by
default so only dimensions and usage are printed.

```bash
python skills/mlops/inference/volcengine-agent-plan/scripts/embed.py \
  --text "A short document" --output /tmp/document-embedding.json
```

Image input may be a public URL or a Base64 data URI:

```bash
python skills/mlops/inference/volcengine-agent-plan/scripts/embed.py \
  --image-url "https://example.com/image.png" \
  --output /tmp/image-embedding.json
```

Combine both inputs for one unified multimodal vector:

```bash
python skills/mlops/inference/volcengine-agent-plan/scripts/embed.py \
  --text "Find this landmark" \
  --image-url "https://example.com/landmark.png" \
  --dimensions 1024 \
  --output /tmp/landmark-embedding.json
```

## Inputs

- `--text`: Optional UTF-8 text.
- `--image-url`: Optional public image URL or image data URI.
- `--dimensions`: `1024` or `2048`; defaults to `1024`.
- `--instructions`: Optional retrieval instruction.
- `--output`: Required JSON output path. The file contains the full vector.

At least one of `--text` and `--image-url` is required. The script reports only
the provider, resolved model, vector dimensions, usage, and output path. Read
the vector file only when a downstream local program needs it; do not inject
the full vector into chat context.

## Verification

Run a low-cost text request and check that the reported dimensions match the
requested value and the output JSON contains an `embedding` array. Never print
or pass the API key as a command-line argument.
