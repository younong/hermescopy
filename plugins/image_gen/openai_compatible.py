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
