"""Owner-scoped persistence and reference loading for deployment image calls."""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
from pathlib import Path
from typing import Any

from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import ExpectedType, RootKind
from hermes_cli.deployment_image import DeploymentImageDescriptor
from hermes_cli.owner_worker.image_relay import OwnerImageRelayClient
from hermes_cli.owner_worker.user_files import migrated_legacy_path, publish_user_bytes

_ALLOWED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


def _legacy_reference_path(candidate: Path, *, owner_home: Path) -> str:
    try:
        relative = candidate.relative_to(owner_home).as_posix()
    except ValueError as exc:
        raise ValueError("reference image is outside the authenticated workspace") from exc
    components = relative.split("/")
    if (
        (len(components) != 2 or components[0] != "images")
        and (len(components) != 3 or components[:2] != ["cache", "images"])
    ) or any(component in {"", ".", ".."} for component in components):
        raise ValueError("reference image is outside the authenticated workspace")
    return relative


def _reference_location(
    raw: str,
    *,
    workspace_context: AuthenticatedWorkspaceContext,
    owner_home: Path,
) -> tuple[str, str]:
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("reference image must be an absolute workspace path")
    try:
        controlled = workspace_context.controlled_api_path(str(candidate))
    except ValueError:
        legacy = _legacy_reference_path(candidate, owner_home=owner_home)
        migrated = migrated_legacy_path(workspace_context, legacy)
        if migrated is None:
            raise ValueError("reference image is outside the authenticated workspace") from None
        controlled = workspace_context.controlled_workspace_path(migrated)
    return controlled, candidate.name


def _read_reference(
    context: AuthenticatedWorkspaceContext,
    relative: str,
    *,
    limit: int,
) -> bytes:
    fd = context.roots.open_relative(
        RootKind.WORKSPACE,
        relative,
        expected_type=ExpectedType.REGULAR_FILE,
    )
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
    workspace_context: AuthenticatedWorkspaceContext,
    owner_home: Path,
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
        relative, name = _reference_location(
            raw,
            workspace_context=workspace_context,
            owner_home=owner_home,
        )
        mime_type = (mimetypes.guess_type(name)[0] or "").lower()
        if mime_type not in _ALLOWED_MIME_TYPES:
            raise ValueError("reference image type is unsupported")
        data = _read_reference(
            workspace_context,
            relative,
            limit=descriptor.max_reference_bytes,
        )
        total += len(data)
        if total > descriptor.max_total_reference_bytes:
            raise ValueError("reference images are too large")
        references.append({"name": name, "mime_type": mime_type, "data": data})
    result = relay_client.generate(
        prompt=arguments["prompt"], aspect_ratio=arguments["aspect_ratio"],
        model=descriptor.model, references=references,
    )
    suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[result["mime_type"]]
    output = publish_user_bytes(
        workspace_context,
        "image",
        f"apiyi_{secrets.token_hex(16)}.{suffix}",
        result["image_bytes"],
    )
    payload = {
        "success": True, "image": str(output.diagnostic_path),
        "provider": result["provider"], "model": result["model"],
        "aspect_ratio": result["aspect_ratio"], "modality": result["modality"],
        "mime_type": result["mime_type"], **dict(result.get("metadata") or {}),
    }
    return json.dumps(payload)
