"""Owner-scoped persistence and reference loading for deployment image calls."""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
from pathlib import Path
from typing import Any

from hermes_cli.controlled_roots import ControlledRoots, ExpectedType, RootKind
from hermes_cli.deployment_image import DeploymentImageDescriptor
from hermes_cli.owner_worker.image_relay import OwnerImageRelayClient

_ALLOWED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


def _reference_location(raw: str, *, owner_home: Path, workspace_root: Path) -> tuple[RootKind, str, str]:
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("reference image must be an absolute owner path")
    for kind, root in ((RootKind.WORKSPACE, workspace_root), (RootKind.OWNER_WRITABLE, owner_home)):
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        if relative:
            return kind, relative, candidate.name
    raise ValueError("reference image is outside owner roots")


def _read_reference(roots: ControlledRoots, kind: RootKind, relative: str, *, limit: int) -> bytes:
    fd = roots.open_relative(kind, relative, expected_type=ExpectedType.REGULAR_FILE)
    try:
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
    finally:
        os.close(fd)
    if not data or len(data) > limit:
        raise ValueError("reference image is too large")
    return bytes(data)


def dispatch_deployment_image(
    arguments: dict[str, Any],
    *,
    relay_client: OwnerImageRelayClient,
    descriptor: DeploymentImageDescriptor,
    controlled_roots: ControlledRoots,
    owner_home: Path,
    workspace_root: Path,
) -> str:
    sources = []
    if arguments.get("image_url"):
        sources.append(arguments["image_url"])
    sources.extend(arguments.get("reference_image_urls") or [])
    if len(sources) > descriptor.max_reference_images:
        raise ValueError("too many reference images")
    references = []
    total = 0
    for raw in sources:
        kind, relative, name = _reference_location(raw, owner_home=owner_home, workspace_root=workspace_root)
        mime_type = (mimetypes.guess_type(name)[0] or "").lower()
        if mime_type not in _ALLOWED_MIME_TYPES:
            raise ValueError("reference image type is unsupported")
        data = _read_reference(controlled_roots, kind, relative, limit=descriptor.max_reference_bytes)
        total += len(data)
        if total > descriptor.max_total_reference_bytes:
            raise ValueError("reference images are too large")
        references.append({"name": name, "mime_type": mime_type, "data": data})
    result = relay_client.generate(
        prompt=arguments["prompt"], aspect_ratio=arguments["aspect_ratio"],
        model=descriptor.model, references=references,
    )
    suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[result["mime_type"]]
    relative_output = f"images/apiyi_{secrets.token_hex(16)}.{suffix}"
    controlled_roots.replace_bytes(RootKind.OWNER_WRITABLE, relative_output, result["image_bytes"], overwrite=False)
    payload = {
        "success": True, "image": str(owner_home / relative_output),
        "provider": result["provider"], "model": result["model"],
        "aspect_ratio": result["aspect_ratio"], "modality": result["modality"],
        "mime_type": result["mime_type"], **dict(result.get("metadata") or {}),
    }
    return json.dumps(payload)
