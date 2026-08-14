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
from pathlib import Path
from typing import Any, Dict, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.image_gen_provider import (  # noqa: E402
    DEFAULT_RESOLUTION,
    VALID_RESOLUTIONS,
    resolve_aspect_ratio,
)
from agent.image_size import (  # noqa: E402
    GPT_IMAGE_2_SIZE_PROFILE,
    inspect_image_path,
    resolve_image_size,
    validate_image_output,
)
from plugins.image_gen.apiyi import ApiyiImageGenProvider  # noqa: E402

DEFAULT_MODELS = ("gpt-image-2-medium", "nano-banana-2")


def _redact_error(value: Any) -> str:
    text = str(value or "")
    secret = os.environ.get("APIYI_API_KEY", "").strip()
    if secret:
        text = text.replace(secret, "***")
    return text


def _run_model(
    provider: ApiyiImageGenProvider,
    model: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str,
) -> Dict[str, Any]:
    result = provider.generate(
        prompt,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        model=model,
    )
    item = {
        "model": model,
        "success": bool(result.get("success")),
        "provider": result.get("provider"),
        "image": result.get("image"),
        "requested_aspect_ratio": result.get("requested_aspect_ratio"),
        "effective_aspect_ratio": result.get("effective_aspect_ratio"),
        "requested_resolution": result.get("requested_resolution"),
        "effective_resolution": result.get("effective_resolution"),
        "quality": result.get("quality"),
        "size": result.get("size"),
        "error_type": result.get("error_type"),
        "error": _redact_error(result.get("error")),
    }
    expected_aspect_ratio = resolve_aspect_ratio(aspect_ratio)
    plan = (
        resolve_image_size(
            expected_aspect_ratio,
            resolution,
            profile=GPT_IMAGE_2_SIZE_PROFILE,
        )
        if model.startswith("gpt-image-2") else None
    )
    artifact_valid = False
    if item["success"] and isinstance(item["image"], str):
        try:
            actual = inspect_image_path(item["image"])
            validate_image_output(
                actual,
                plan=plan,
                effective_aspect_ratio=(
                    None if plan is not None else item["effective_aspect_ratio"]
                ),
            )
            artifact_valid = True
        except (OSError, ValueError):
            pass
    if (
        artifact_valid
        and item["requested_aspect_ratio"] == expected_aspect_ratio
        and item["effective_aspect_ratio"] == expected_aspect_ratio
        and item["requested_resolution"] == resolution
        and item["effective_resolution"] == resolution
        and (plan is None or item["size"] == plan.size)
    ):
        return item

    item["success"] = False
    item["error_type"] = "image_contract_validation"
    item["error"] = "APIYI aspect ratio or resolution validation failed"
    return item


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
        help="Hermes image width:height ratio or legacy directional alias.",
    )
    parser.add_argument(
        "--resolution",
        default=DEFAULT_RESOLUTION,
        choices=VALID_RESOLUTIONS,
        help="Hermes image resolution tier.",
    )
    args = parser.parse_args()

    if not os.environ.get("APIYI_API_KEY", "").strip():
        print("APIYI_API_KEY is not set in this runtime environment.", file=sys.stderr)
        return 2

    provider = ApiyiImageGenProvider()
    results = [
        _run_model(
            provider,
            model,
            args.prompt,
            args.aspect_ratio,
            args.resolution,
        )
        for model in _parse_models(args.models)
    ]
    print(json.dumps(results, ensure_ascii=False, indent=2))

    failed = [item for item in results if not item["success"]]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
