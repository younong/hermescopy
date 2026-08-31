"""OpenAI Images-compatible transport with explicit native-size semantics."""
from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional, Tuple

from agent.image_gen_provider import DEFAULT_RESOLUTION
from agent.image_size import (
    image_prompt_with_size_requirements,
    image_size_profile,
    inspect_image_bytes,
    resolve_image_size,
    validate_image_output,
)
from plugins.image_gen.codex_responses import (
    build_responses_payload,
    extract_image_b64,
    iter_sse_json,
)

_REQUEST_TIMEOUT = 300.0


class OpenAICompatibleImageEmpty(ValueError):
    """The upstream response succeeded but contained no image artifact."""


def _extract_openai_image(
    payload: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None, None, None
    first = data[0]
    b64 = first.get("b64_json") if isinstance(first.get("b64_json"), str) else None
    url = first.get("url") if isinstance(first.get("url"), str) else None
    revised = (
        first.get("revised_prompt")
        if isinstance(first.get("revised_prompt"), str)
        else None
    )
    return b64, url, revised


def _decode_openai_image_payload(
    payload: Dict[str, Any],
) -> Tuple[bytes, str, Optional[str]]:
    b64, url, revised_prompt = _extract_openai_image(payload)
    if b64:
        return base64.b64decode(b64), "image/png", revised_prompt
    if url:
        import requests

        response = requests.get(url, timeout=60)
        response.raise_for_status()
        mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        return (
            response.content,
            mime if mime.startswith("image/") else "image/png",
            revised_prompt,
        )
    raise OpenAICompatibleImageEmpty(
        "OpenAI-compatible response contained no image data"
    )


def generate_openai_compatible_image_bytes(
    *,
    prompt: str,
    aspect_ratio: str,
    model: str,
    references: List[Dict[str, Any]],
    api_key: str,
    openai_base_url: str,
    size_profile: str,
    params: Optional[Dict[str, Any]] = None,
    quality: str = "medium",
    edit_protocol: str = "multipart",
) -> Dict[str, Any]:
    """Call an OpenAI Images-compatible endpoint without filesystem writes."""
    import requests

    plan = resolve_image_size(
        aspect_ratio,
        (params or {}).get("resolution", DEFAULT_RESOLUTION),
        profile=image_size_profile(size_profile),
    )
    quality = str((params or {}).get("quality") or quality).strip().lower()
    if quality not in {"low", "medium", "high", "auto"}:
        raise ValueError("quality must be low, medium, high, or auto")
    if edit_protocol not in {"multipart", "json_images"}:
        raise ValueError("edit protocol is invalid")
    constrained_prompt = image_prompt_with_size_requirements(prompt, plan)

    headers = {"Authorization": f"Bearer {api_key}"}
    if references and edit_protocol == "json_images":
        response = requests.post(
            f"{openai_base_url.rstrip('/')}/images/edits",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "model": model,
                "prompt": constrained_prompt,
                "size": plan.size,
                "n": 1,
                "quality": quality,
                "images": [{
                    "image_url": (
                        f"data:{item['mime_type']};base64,"
                        f"{base64.b64encode(item['data']).decode('ascii')}"
                    ),
                } for item in references],
            },
            timeout=_REQUEST_TIMEOUT,
        )
    elif references:
        response = requests.post(
            f"{openai_base_url.rstrip('/')}/images/edits",
            headers=headers,
            data={
                "model": model,
                "prompt": constrained_prompt,
                "size": plan.size,
                "n": "1",
                "quality": quality,
            },
            files=[
                ("image", (item["name"], item["data"], item["mime_type"]))
                for item in references
            ],
            timeout=_REQUEST_TIMEOUT,
        )
    else:
        response = requests.post(
            f"{openai_base_url.rstrip('/')}/images/generations",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "model": model,
                "prompt": constrained_prompt,
                "size": plan.size,
                "n": 1,
                "quality": quality,
            },
            timeout=_REQUEST_TIMEOUT,
        )
    response.raise_for_status()
    payload = response.json()
    image_bytes, mime_type, revised_prompt = _decode_openai_image_payload(payload)
    actual = validate_image_output(
        inspect_image_bytes(image_bytes, declared_mime_type=mime_type),
        plan=plan,
        require_exact_dimensions=False,
    )
    metadata: Dict[str, Any] = {
        **plan.metadata(),
        **actual.metadata(),
        "quality": quality,
        "upstream_model": model,
    }
    if revised_prompt:
        metadata["revised_prompt"] = revised_prompt
    return {
        "image_bytes": image_bytes,
        "mime_type": mime_type,
        "metadata": metadata,
        "size_plan": plan,
    }


_CODEX_IMAGE_MODEL = "gpt-image-2"
_CODEX_INSTRUCTIONS = (
    "You are an assistant that must fulfill image generation and image editing "
    "requests by using the image_generation tool when provided."
)
_CODEX_INPUT_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_CODEX_MAX_REFERENCE_IMAGES = 16
_CODEX_MAX_REFERENCE_BYTES = 25 * 1024 * 1024
_CODEX_MAX_TOTAL_REFERENCE_BYTES = 48 * 1024 * 1024


def _codex_reference_parts(references: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Convert relay-owned reference bytes into validated Responses inputs."""
    from agent.image_routing import _sniff_mime_from_bytes

    if len(references) > _CODEX_MAX_REFERENCE_IMAGES:
        raise ValueError("too many reference images")
    parts: List[Dict[str, str]] = []
    total_bytes = 0
    for reference in references:
        if not isinstance(reference, dict):
            raise ValueError("reference image is invalid")
        raw = reference.get("data")
        mime_type = str(reference.get("mime_type") or "").strip().lower()
        if (
            not isinstance(raw, bytes)
            or not raw
            or len(raw) > _CODEX_MAX_REFERENCE_BYTES
            or mime_type not in _CODEX_INPUT_MIME_TYPES
        ):
            raise ValueError("reference image is invalid")
        total_bytes += len(raw)
        if total_bytes > _CODEX_MAX_TOTAL_REFERENCE_BYTES:
            raise ValueError("reference images are too large")
        if _sniff_mime_from_bytes(raw) != mime_type:
            raise ValueError("reference image MIME type does not match its bytes")
        parts.append({
            "type": "input_image",
            "image_url": (
                f"data:{mime_type};base64,"
                f"{base64.b64encode(raw).decode('ascii')}"
            ),
        })
    return parts


def generate_codex_responses_image_bytes(
    *,
    prompt: str,
    aspect_ratio: str,
    model: str,
    references: List[Dict[str, Any]],
    api_key: str,
    openai_base_url: str,
    chat_model: str = "",
    size_profile: str = "gpt-image-2",
    params: Optional[Dict[str, Any]] = None,
    quality: str = "medium",
) -> Dict[str, Any]:
    """Call a Codex Responses image tool without OAuth or filesystem access."""
    import requests

    if model != _CODEX_IMAGE_MODEL:
        raise ValueError("Codex Responses image model must be gpt-image-2")
    if not str(api_key or "").strip():
        raise ValueError("Codex Responses API key is required")
    chat_model = str(chat_model or "").strip()
    if not chat_model:
        try:
            from hermes_cli.config import (
                get_compatible_custom_providers,
                load_config_readonly,
            )

            config = load_config_readonly()
            codex_block = (config.get("providers") or {}).get("codex") or {}
            chat_model = str(
                codex_block.get("default_model") or codex_block.get("model") or ""
            ).strip()
            if not chat_model:
                for provider in get_compatible_custom_providers(config):
                    provider_name = str(provider.get("name") or "").strip().lower()
                    provider_key = str(provider.get("provider_key") or "").strip().lower()
                    if provider_name not in {"codex", "custom:codex"} and provider_key != "codex":
                        continue
                    chat_model = str(
                        provider.get("model") or provider.get("default_model") or ""
                    ).strip()
                    if chat_model:
                        break
        except Exception:
            chat_model = ""
    if not chat_model:
        raise ValueError(
            "Codex Responses chat_model is required "
            "(set executor_params.chat_model or configure custom:codex in config.yaml)"
        )
    base_url = str(openai_base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("Codex Responses base URL is required")

    params = dict(params or {})
    plan = resolve_image_size(
        aspect_ratio,
        params.get("resolution", DEFAULT_RESOLUTION),
        profile=image_size_profile(size_profile),
    )
    quality = str(params.get("quality") or quality).strip().lower()
    if quality not in {"low", "medium", "high", "auto"}:
        raise ValueError("quality must be low, medium, high, or auto")
    constrained_prompt = image_prompt_with_size_requirements(prompt, plan)
    payload = build_responses_payload(
        prompt=constrained_prompt,
        size=plan.size,
        quality=quality,
        chat_model=chat_model,
        image_model=_CODEX_IMAGE_MODEL,
        instructions=_CODEX_INSTRUCTIONS,
        input_images=_codex_reference_parts(references),
    )

    image_b64: Optional[str] = None
    response = requests.post(
        f"{base_url}/responses",
        headers={
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        stream=True,
        timeout=_REQUEST_TIMEOUT,
    )
    try:
        response.raise_for_status()
        for event in iter_sse_json(response):
            found = extract_image_b64(event)
            if found:
                image_b64 = found
    finally:
        response.close()

    if not image_b64:
        raise OpenAICompatibleImageEmpty(
            "Codex Responses response contained no image data"
        )
    try:
        image_bytes = base64.b64decode(image_b64, validate=True)
        actual = validate_image_output(
            inspect_image_bytes(image_bytes, declared_mime_type="image/png"),
            plan=plan,
            require_exact_dimensions=False,
        )
    except Exception as exc:
        raise ValueError("Codex Responses returned an invalid image artifact") from exc
    return {
        "image_bytes": image_bytes,
        "mime_type": actual.mime_type,
        "metadata": {
            **plan.metadata(),
            **actual.metadata(),
            "quality": quality,
            "upstream_model": _CODEX_IMAGE_MODEL,
            "responses_model": chat_model,
        },
        "size_plan": plan,
    }
