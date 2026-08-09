#!/usr/bin/env python3
"""Write one Agent Plan multimodal embedding to a JSON file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hermes_cli.model_plane.capability import (
    ensure_capability_providers,
    resolve_embedding_capability,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text")
    parser.add_argument("--image-url")
    parser.add_argument("--dimensions", type=int, choices=(1024, 2048), default=1024)
    parser.add_argument("--instructions")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.text and not args.image_url:
        raise SystemExit("at least one of --text or --image-url is required")

    ensure_capability_providers()
    result = resolve_embedding_capability("volcengine-agent-plan").embed(
        text=args.text,
        image_url=args.image_url,
        dimensions=args.dimensions,
        instructions=args.instructions,
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "provider": result["provider"],
        "model": result["model"],
        "dimensions": result["dimensions"],
        "usage": result["usage"],
        "output": str(output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
