"""Owner-scoped persistence and reference loading for deployment media calls."""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
from pathlib import Path
from typing import Any

from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import ExpectedType, RootKind
from hermes_cli.deployment_media import (
    IMAGE_MIME_TYPES,
    DeploymentMediaRouteDescriptor,
)
from hermes_cli.owner_worker.media_relay import OwnerMediaRelayClient
from hermes_cli.owner_worker.user_files import migrated_legacy_path, publish_user_bytes

_IMAGE_SUFFIXES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_VIDEO_SUFFIXES = {"video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"}
_VIDEO_PARAM_KEYS = ("duration", "resolution", "negative_prompt", "audio", "seed")

# The worker-process media relay client, registered by the owner-worker
# entrypoint at startup. Tool-layer deployment checks (TTS synthesis,
# transcription, embeddings) run inside the worker process but outside the
# FastAPI app-state scope, so they reach the lease-bound relay through this
# module-level handle. The fd itself stays private to the client.
_WORKER_MEDIA_RELAY: OwnerMediaRelayClient | None = None


def set_worker_media_relay(client: OwnerMediaRelayClient | None) -> None:
    """Register (or clear) the worker-process deployment media relay client."""
    global _WORKER_MEDIA_RELAY
    _WORKER_MEDIA_RELAY = client


def worker_media_relay() -> OwnerMediaRelayClient | None:
    """Return the worker-process deployment media relay client, if started."""
    return _WORKER_MEDIA_RELAY


def active_media_selection(kind: str) -> tuple[str, str]:
    """Return the owner's active ``(provider, model)`` selection for one media kind."""
    from hermes_cli.config import load_config

    try:
        config = load_config()
    except Exception:  # noqa: BLE001 - selection is best-effort; local path decides
        return "", ""
    section = config.get(f"{kind}_gen") if isinstance(config, dict) else None
    if not isinstance(section, dict):
        return "", ""
    provider = section.get("provider")
    model = section.get("model")
    return (
        provider.strip() if isinstance(provider, str) else "",
        model.strip() if isinstance(model, str) else "",
    )


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


def _load_references(
    arguments: dict[str, Any],
    *,
    descriptor: DeploymentMediaRouteDescriptor,
    workspace_context: AuthenticatedWorkspaceContext,
    owner_home: Path,
) -> list[dict[str, Any]]:
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
        if mime_type not in IMAGE_MIME_TYPES:
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
    return references


def dispatch_deployment_media(
    arguments: dict[str, Any],
    *,
    kind: str,
    model: str,
    relay_client: OwnerMediaRelayClient,
    descriptor: DeploymentMediaRouteDescriptor,
    workspace_context: AuthenticatedWorkspaceContext,
    owner_home: Path,
) -> str:
    """Execute one deployment-routed media call and publish the artifact."""
    references = _load_references(
        arguments,
        descriptor=descriptor,
        workspace_context=workspace_context,
        owner_home=owner_home,
    )
    if kind == "image":
        params = {}
        resolution = arguments.get("resolution")
        if isinstance(resolution, str) and resolution.strip():
            params["resolution"] = resolution.strip()
        result = relay_client.execute(
            "image_generate",
            provider=descriptor.provider,
            model=model,
            prompt=arguments["prompt"],
            aspect_ratio=arguments["aspect_ratio"],
            references=references,
            params=params,
        )
        suffix = _IMAGE_SUFFIXES[result["mime_type"]]
        output = publish_user_bytes(
            workspace_context,
            "image",
            f"{descriptor.provider}_{secrets.token_hex(16)}.{suffix}",
            result["image_bytes"],
        )
        payload = {
            "success": True, "image": output.visible_path,
            "provider": result["provider"], "model": result["model"],
            "aspect_ratio": result["aspect_ratio"], "modality": result["modality"],
            "mime_type": result["mime_type"], **dict(result.get("metadata") or {}),
        }
        return json.dumps(payload)

    params = {
        key: arguments[key] for key in _VIDEO_PARAM_KEYS if key in arguments
    }
    result = relay_client.execute(
        "video_generate",
        provider=descriptor.provider,
        model=model,
        prompt=arguments["prompt"],
        aspect_ratio=str(arguments.get("aspect_ratio") or ""),
        references=references,
        params=params,
    )
    payload = {
        "success": True,
        "provider": result["provider"], "model": result["model"],
        "aspect_ratio": result["aspect_ratio"], "modality": result["modality"],
        **dict(result.get("metadata") or {}),
    }
    if "video_url" in result:
        payload["video"] = result["video_url"]
        return json.dumps(payload)
    suffix = _VIDEO_SUFFIXES[result["mime_type"]]
    output = publish_user_bytes(
        workspace_context,
        "video",
        f"{descriptor.provider}_{secrets.token_hex(16)}.{suffix}",
        result["video_bytes"],
    )
    payload["video"] = output.visible_path
    payload["mime_type"] = result["mime_type"]
    return json.dumps(payload)
