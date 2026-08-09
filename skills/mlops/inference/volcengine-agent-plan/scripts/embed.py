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


def _embed_via_deployment(args) -> dict | None:
    """Return the deployment-relay embedding when a vector route matches.

    Deployment-managed embeddings keep the credential in the Control Plane;
    a worker without its own key reaches it through the media relay. Returns
    None outside a worker process (or without a matching route) so the
    caller falls back to the local capability path.
    """
    if not args.text or args.image_url:
        return None
    try:
        from hermes_cli.deployment_media import (
            deployment_media_route_from_environment,
        )
        from hermes_cli.owner_worker.media_dispatch import worker_media_relay

        route = deployment_media_route_from_environment(
            "vector",
            provider="volcengine-agent-plan",
            model="doubao-embedding-vision",
        )
    except Exception:
        return None
    if route is None:
        return None
    relay = worker_media_relay()
    if relay is None:
        return None
    result = relay.execute(
        "embed",
        provider=route.provider,
        model="doubao-embedding-vision",
        prompt=args.text,
        params={"dimensions": args.dimensions, "instructions": args.instructions},
    )
    return {
        "provider": result["provider"],
        "model": result["model"],
        "embedding": result["embedding"],
        "dimensions": result["dimensions"],
        "usage": {},
    }


def main() -> int:
    args = _parser().parse_args()
    if not args.text and not args.image_url:
        raise SystemExit("at least one of --text or --image-url is required")

    result = _embed_via_deployment(args)
    if result is None:
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
