#!/usr/bin/env python3
"""Server-side smoke test for the APIYI image generation plugin.

Run this after deploying on the server from the current systemd release runtime:

    set -a
    [ ! -f /opt/hermes/shared/.env ] || . /opt/hermes/shared/.env
    set +a
    cd /opt/hermes/current
    /opt/hermes/shared/venv/bin/python deploy/smoke-apiyi.py

The script intentionally never prints API keys. It only reports model names,
success/failure, and generated image paths/errors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Iterable

from plugins.image_gen.apiyi import ApiyiImageGenProvider

DEFAULT_MODELS = ("gpt-image-2-medium", "nano-banana-2")


def _redact_error(value: Any) -> str:
    text = str(value or "")
    secret = os.environ.get("APIYI_API_KEY", "").strip()
    if secret:
        text = text.replace(secret, "***")
    return text


def _run_model(provider: ApiyiImageGenProvider, model: str, prompt: str, aspect_ratio: str) -> Dict[str, Any]:
    result = provider.generate(prompt, aspect_ratio=aspect_ratio, model=model)
    return {
        "model": model,
        "success": bool(result.get("success")),
        "provider": result.get("provider"),
        "image": result.get("image"),
        "error_type": result.get("error_type"),
        "error": _redact_error(result.get("error")),
    }


def _parse_models(raw: str) -> Iterable[str]:
    for item in raw.split(","):
        model = item.strip()
        if model:
            yield model


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test APIYI image generation models.")
    parser.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="Comma-separated Hermes image model IDs to test.",
    )
    parser.add_argument(
        "--prompt",
        default="A small watercolor robot holding a banana, no text, clean white background",
        help="Prompt used for each smoke-test generation.",
    )
    parser.add_argument(
        "--aspect-ratio",
        default="square",
        choices=("landscape", "square", "portrait"),
        help="Hermes image aspect ratio.",
    )
    args = parser.parse_args()

    if not os.environ.get("APIYI_API_KEY", "").strip():
        print("APIYI_API_KEY is not set in this runtime environment.", file=sys.stderr)
        return 2

    provider = ApiyiImageGenProvider()
    results = [
        _run_model(provider, model, args.prompt, args.aspect_ratio)
        for model in _parse_models(args.models)
    ]
    print(json.dumps(results, ensure_ascii=False, indent=2))

    failed = [item for item in results if not item["success"]]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
