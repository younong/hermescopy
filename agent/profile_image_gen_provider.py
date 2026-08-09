"""Generic image generation adapter backed by a model-provider profile."""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional

import requests

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
)
from agent.profile_provider_credentials import resolve_profile_api_key
from agent.redact import redact_sensitive_text
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

_SIZES = {
    "landscape": "3072x2048",
    "square": "2048x2048",
    "portrait": "2048x3072",
}

# ``resolve_aspect_ratio`` returns canonical ratios ("16:9", "1:1", ...);
# the seedream endpoint accepts only the three legacy tiers.
_CANONICAL_ASPECT_TIERS = {
    "16:9": "landscape", "4:3": "landscape", "3:2": "landscape",
    "1:1": "square",
    "9:16": "portrait", "3:4": "portrait", "2:3": "portrait",
}


def _size_for_aspect(aspect: str) -> str:
    return _SIZES[_CANONICAL_ASPECT_TIERS.get(aspect, "landscape")]


def _safe_error(exc: BaseException, api_key: str = "") -> str:
    message = str(exc)
    if api_key:
        message = message.replace(api_key, "«redacted-secret»")
    return redact_sensitive_text(message, force=True)


def _call_profile_image_endpoint(
    profile: ProviderProfile,
    *,
    api_key: str,
    model: str,
    prompt: str,
    aspect: str,
    sources: List[str],
) -> Dict[str, Any]:
    """POST one OpenAI-compatible image request and return the first data entry."""
    payload: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": _size_for_aspect(aspect),
        "response_format": "url",
        "output_format": "png",
        "watermark": False,
    }
    if sources:
        payload["image"] = sources

    endpoint = (
        f"{profile.base_url.rstrip('/')}"
        f"/{profile.image_generation_path.lstrip('/')}"
    )
    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=300,
    )
    response.raise_for_status()
    body = response.json()
    data = body.get("data") if isinstance(body, dict) else None
    first = data[0] if isinstance(data, list) and data else None
    if not isinstance(first, dict):
        raise ValueError("Image endpoint returned no image data")
    first.setdefault("size", payload["size"])
    first.setdefault("output_format", payload["output_format"])
    return first


def generate_profile_image_bytes(
    *,
    profile: str,
    api_key: str,
    model: str,
    prompt: str,
    aspect_ratio: str,
    references: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Deployment-media executor: profile image generation without filesystem writes.

    ``profile`` names the registered ``ProviderProfile``; references arrive as
    relay frame entries (``name``/``mime_type``/``data`` bytes) and are sent as
    data URIs. Returns ``image_bytes``/``mime_type``/``metadata``.
    """
    from providers import get_provider_profile

    del params  # profile image generation has no relay extras
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    profile_obj = get_provider_profile(str(profile or "").strip())
    if profile_obj is None or not profile_obj.image_generation_model:
        raise ValueError("profile image generation is unavailable")
    aspect = resolve_aspect_ratio(aspect_ratio)
    model_id = str(model or "").strip() or profile_obj.image_generation_model
    sources = [
        f"data:{item['mime_type']};base64,{base64.b64encode(item['data']).decode('ascii')}"
        for item in references[:14]
    ]
    first = _call_profile_image_endpoint(
        profile_obj,
        api_key=api_key,
        model=model_id,
        prompt=prompt,
        aspect=aspect,
        sources=sources,
    )
    b64 = first.get("b64_json")
    url = first.get("url")
    if isinstance(b64, str) and b64:
        image_bytes = base64.b64decode(b64)
        mime_type = "image/png"
    elif isinstance(url, str) and url:
        downloaded = requests.get(url, timeout=60)
        downloaded.raise_for_status()
        mime_type = (
            downloaded.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if not mime_type.startswith("image/"):
            mime_type = "image/png"
        image_bytes = downloaded.content
    else:
        raise ValueError("response contained neither b64_json nor URL")
    if not image_bytes:
        raise ValueError("response contained no image bytes")
    return {
        "image_bytes": image_bytes,
        "mime_type": mime_type,
        "metadata": {
            "size": first.get("size"),
            "output_format": first.get("output_format"),
        },
    }


class ProfileImageGenProvider(ImageGenProvider):
    """Expose a profile's OpenAI-compatible image generation endpoint."""

    def __init__(self, profile: ProviderProfile):
        if not profile.image_generation_model or not profile.image_generation_path:
            raise ValueError(f"Provider profile {profile.name!r} has no image capability")
        self.profile = profile

    @property
    def name(self) -> str:
        return self.profile.name

    @property
    def display_name(self) -> str:
        return self.profile.display_name or self.profile.name

    def is_available(self) -> bool:
        return bool(resolve_profile_api_key(self.profile))

    def list_models(self) -> List[Dict[str, Any]]:
        return [{
            "id": self.profile.image_generation_model,
            "display": self.profile.image_generation_model,
            "strengths": "Text-to-image and reference-image generation",
            "price": "Agent Plan allowance",
        }]

    def get_setup_schema(self) -> Dict[str, Any]:
        env_vars = [
            {
                "key": key,
                "prompt": f"{self.display_name} API key",
                "url": self.profile.signup_url,
            }
            for key in self.profile.env_vars
        ]
        return {
            "name": self.display_name,
            "badge": "plan",
            "tag": f"{self.profile.image_generation_model} via the provider subscription",
            "env_vars": env_vars,
        }

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text", "image"], "max_reference_images": 14}

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        model = self.profile.image_generation_model
        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider=self.name,
                model=model,
                aspect_ratio=aspect,
            )

        api_key = resolve_profile_api_key(self.profile)
        if not api_key:
            env_hint = self.profile.env_vars[0] if self.profile.env_vars else "provider API key"
            return error_response(
                error=f"{env_hint} is not configured",
                error_type="auth_required",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        sources: List[str] = []
        if isinstance(image_url, str) and image_url.strip():
            sources.append(image_url.strip())
        sources.extend(normalize_reference_images(reference_image_urls) or [])
        sources = sources[:14]
        modality = "image" if sources else "text"

        try:
            first = _call_profile_image_endpoint(
                self.profile,
                api_key=api_key,
                model=model,
                prompt=prompt,
                aspect=aspect,
                sources=sources,
            )
        except Exception as exc:
            logger.debug("Profile image generation failed", exc_info=True)
            return error_response(
                error=f"Image generation failed: {_safe_error(exc, api_key)}",
                error_type="api_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        b64 = first.get("b64_json")
        url = first.get("url")
        try:
            if isinstance(b64, str) and b64:
                image = str(save_b64_image(b64, prefix=f"{self.name}_{model}"))
            elif isinstance(url, str) and url:
                image = str(save_url_image(url, prefix=f"{self.name}_{model}"))
            else:
                raise ValueError("response contained neither b64_json nor URL")
        except Exception as exc:
            return error_response(
                error=f"Could not save generated image: {_safe_error(exc)}",
                error_type="io_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        return success_response(
            image=image,
            model=model,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=self.name,
            modality=modality,
            extra={
                "size": first.get("size"),
                "output_format": first.get("output_format"),
            },
        )
