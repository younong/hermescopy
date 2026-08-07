"""Multimodal embedding client backed by a model-provider profile."""

from __future__ import annotations

from typing import Any, Optional

import requests

from agent.profile_provider_credentials import resolve_profile_api_key
from providers import get_provider_profile


class ProfileEmbeddingError(RuntimeError):
    """A profile-backed embedding request failed without exposing credentials."""


def _profile(provider: str):
    profile = get_provider_profile(provider)
    if not profile or not profile.embedding_model or not profile.embedding_path:
        raise ValueError(f"Provider {provider!r} has no embedding capability")
    return profile


def embed(
    provider: str,
    *,
    text: Optional[str] = None,
    image_url: Optional[str] = None,
    dimensions: Optional[int] = None,
    instructions: Optional[str] = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Return one dense embedding for a text, image, or combined sample."""
    profile = _profile(provider)
    api_key = resolve_profile_api_key(profile)
    if not api_key:
        env_hint = profile.env_vars[0] if profile.env_vars else "provider API key"
        raise ProfileEmbeddingError(f"{env_hint} is not configured")

    inputs: list[dict[str, Any]] = []
    if isinstance(text, str) and text.strip():
        inputs.append({"type": "text", "text": text.strip()})
    if isinstance(image_url, str) and image_url.strip():
        inputs.append({
            "type": "image_url",
            "image_url": {"url": image_url.strip()},
        })
    if not inputs:
        raise ValueError("At least one of text or image_url is required")

    supported_dimensions = tuple(profile.embedding_dimensions or ())
    if dimensions is None:
        dimensions = supported_dimensions[0] if supported_dimensions else None
    elif supported_dimensions and dimensions not in supported_dimensions:
        supported = ", ".join(str(item) for item in supported_dimensions)
        raise ValueError(f"dimensions must be one of: {supported}")

    payload: dict[str, Any] = {
        "model": profile.embedding_model,
        "input": inputs,
        "encoding_format": "float",
    }
    if dimensions is not None:
        payload["dimensions"] = dimensions
    if isinstance(instructions, str) and instructions.strip():
        payload["instructions"] = instructions.strip()

    endpoint = f"{profile.base_url.rstrip('/')}/{profile.embedding_path.lstrip('/')}"
    try:
        response = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" (HTTP {status})" if status else ""
        raise ProfileEmbeddingError(f"Embedding request failed{suffix}") from exc

    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, list):
        data = data[0] if data else None
    embedding = data.get("embedding") if isinstance(data, dict) else None
    if not isinstance(embedding, list):
        raise ProfileEmbeddingError("Embedding endpoint returned no dense vector")

    usage = body.get("usage") if isinstance(body, dict) else None
    return {
        "provider": profile.name,
        "model": body.get("model") or profile.embedding_model,
        "embedding": embedding,
        "dimensions": len(embedding),
        "usage": usage if isinstance(usage, dict) else {},
    }
