"""APIYI image generation backend.

APIYI exposes different upstream image APIs behind one account token:

* ``gpt-image-2`` uses the OpenAI Images API shape under ``/v1``.
* Nano Banana 2 uses a Gemini-style ``generateContent`` endpoint under
  ``/v1beta``.

This plugin keeps those protocol differences inside the provider while exposing
stable Hermes model IDs via ``image_gen.model``.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

logger = logging.getLogger(__name__)

_DEFAULT_OPENAI_BASE_URL = "https://api.apiyi.com/v1"
_DEFAULT_GEMINI_BASE_URL = "https://api.apiyi.com/v1beta"
_DEFAULT_GPT_UPSTREAM = "gpt-image-2"
_DEFAULT_NANO_UPSTREAM = "gemini-3.1-flash-image-preview"
_DEFAULT_MODEL = "gpt-image-2-medium"
_REQUEST_TIMEOUT = 300.0
_MAX_GPT_REFERENCE_IMAGES = 16
_MAX_NANO_REFERENCE_IMAGES = 3

_GPT_MODELS: Dict[str, Dict[str, Any]] = {
    "gpt-image-2-low": {
        "display": "GPT Image 2 (APIYI, Low)",
        "speed": "fastest",
        "strengths": "Fast iteration via APIYI",
        "quality": "low",
    },
    "gpt-image-2-medium": {
        "display": "GPT Image 2 (APIYI, Medium)",
        "speed": "balanced",
        "strengths": "Balanced quality/cost via APIYI",
        "quality": "medium",
    },
    "gpt-image-2-high": {
        "display": "GPT Image 2 (APIYI, High)",
        "speed": "slowest",
        "strengths": "Highest GPT Image 2 fidelity via APIYI",
        "quality": "high",
    },
}

_NANO_MODEL = "nano-banana-2"
_MODELS: Dict[str, Dict[str, Any]] = {
    **_GPT_MODELS,
    _NANO_MODEL: {
        "display": "Nano Banana 2 (APIYI)",
        "speed": "varies",
        "strengths": "Gemini-style image generation via APIYI",
    },
}

_SIZES = {
    "landscape": "1536x1024",
    "square": "1024x1024",
    "portrait": "1024x1536",
}

_GEMINI_ASPECT_RATIOS = {
    "landscape": "16:9",
    "square": "1:1",
    "portrait": "9:16",
}


def _load_image_gen_config() -> Dict[str, Any]:
    """Read the ``image_gen`` config section, best-effort."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # noqa: BLE001 - config is best-effort
        logger.debug("could not load image_gen config: %s", exc)
        return {}


def _apiyi_config() -> Dict[str, Any]:
    cfg = _load_image_gen_config()
    section = cfg.get("apiyi") if isinstance(cfg.get("apiyi"), dict) else {}
    return section if isinstance(section, dict) else {}


def _clean_base_url(value: Optional[str], default: str) -> str:
    text = str(value or "").strip().rstrip("/")
    return text or default


def _openai_base_url() -> str:
    cfg = _apiyi_config()
    return _clean_base_url(
        os.environ.get("APIYI_OPENAI_BASE_URL")
        or os.environ.get("APIYI_BASE_URL")
        or cfg.get("openai_base_url")
        or cfg.get("base_url"),
        _DEFAULT_OPENAI_BASE_URL,
    )


def _gemini_base_url() -> str:
    cfg = _apiyi_config()
    return _clean_base_url(
        os.environ.get("APIYI_GEMINI_BASE_URL")
        or cfg.get("gemini_base_url"),
        _DEFAULT_GEMINI_BASE_URL,
    )


def _model_map() -> Dict[str, str]:
    mapping = {
        "gpt-image-2-low": _DEFAULT_GPT_UPSTREAM,
        "gpt-image-2-medium": _DEFAULT_GPT_UPSTREAM,
        "gpt-image-2-high": _DEFAULT_GPT_UPSTREAM,
        _NANO_MODEL: _DEFAULT_NANO_UPSTREAM,
    }
    cfg_map = _apiyi_config().get("model_map")
    if isinstance(cfg_map, dict):
        for key, value in cfg_map.items():
            if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip():
                mapping[key.strip()] = value.strip()
    return mapping


def _resolve_model(explicit: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """Resolve the Hermes-visible model id and its metadata."""
    candidates = [
        explicit,
        os.environ.get("APIYI_IMAGE_MODEL"),
        _apiyi_config().get("model"),
        _load_image_gen_config().get("model"),
        _DEFAULT_MODEL,
    ]
    for candidate in candidates:
        if isinstance(candidate, str):
            model_id = candidate.strip()
            if model_id in _MODELS:
                return model_id, _MODELS[model_id]
    return _DEFAULT_MODEL, _MODELS[_DEFAULT_MODEL]


def _upstream_model(model_id: str) -> str:
    return _model_map().get(model_id, model_id)


def _load_image_bytes(ref: str) -> Tuple[bytes, str, str]:
    """Load image bytes from URL, data URI, or local path."""
    ref = ref.strip()
    lower = ref.lower()
    if lower.startswith(("http://", "https://")):
        import requests

        response = requests.get(ref, timeout=60)
        response.raise_for_status()
        name = ref.split("?", 1)[0].rsplit("/", 1)[-1] or "image.png"
        mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip() or mimetypes.guess_type(name)[0] or "image/png"
        return response.content, name, mime
    if lower.startswith("data:"):
        header, _, b64 = ref.partition(",")
        mime = "image/png"
        if header.startswith("data:") and ";" in header:
            mime = header[5:].split(";", 1)[0] or mime
        ext = mime.split("/", 1)[-1] or "png"
        return base64.b64decode(b64), f"image.{ext}", mime

    path = Path(ref)
    data = path.read_bytes()
    name = path.name or "image.png"
    mime = mimetypes.guess_type(name)[0] or "image/png"
    return data, name, mime


def _collect_sources(image_url: Optional[str], reference_image_urls: Optional[List[str]]) -> List[str]:
    sources: List[str] = []
    if isinstance(image_url, str) and image_url.strip():
        sources.append(image_url.strip())
    for ref in normalize_reference_images(reference_image_urls) or []:
        sources.append(ref)
    return sources


def _extract_openai_image(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(b64, url, revised_prompt)`` from an OpenAI Images response."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return None, None, None
    first = data[0]
    if not isinstance(first, dict):
        return None, None, None
    b64 = first.get("b64_json") if isinstance(first.get("b64_json"), str) else None
    url = first.get("url") if isinstance(first.get("url"), str) else None
    revised = first.get("revised_prompt") if isinstance(first.get("revised_prompt"), str) else None
    return b64, url, revised


def _extract_gemini_images(payload: Dict[str, Any]) -> List[str]:
    """Extract image data/URLs from Gemini-style generateContent responses."""
    images: List[str] = []
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list):
        return images
    for candidate in candidates:
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict):
                data = inline.get("data")
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                if isinstance(data, str) and data.strip():
                    images.append(f"data:{mime};base64,{data.strip()}")
                    continue
            file_data = part.get("fileData") or part.get("file_data")
            if isinstance(file_data, dict):
                uri = file_data.get("fileUri") or file_data.get("file_uri")
                if isinstance(uri, str) and uri.strip():
                    images.append(uri.strip())
    return images


def _save_image_ref(image_ref: str, *, prefix: str) -> str:
    if image_ref.startswith("data:"):
        b64 = image_ref.split(",", 1)[1] if "," in image_ref else ""
        return str(save_b64_image(b64, prefix=prefix))
    if image_ref.startswith(("http://", "https://")):
        return str(save_url_image(image_ref, prefix=prefix))
    return str(save_b64_image(image_ref, prefix=prefix))


def _decode_apiyi_image_payload(payload: Dict[str, Any]) -> Tuple[bytes, str]:
    b64, url, _ = _extract_openai_image(payload)
    if b64:
        return base64.b64decode(b64), "image/png"
    if url:
        import requests
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        return response.content, mime if mime.startswith("image/") else "image/png"
    raise ValueError("APIYI response contained no image data")


def generate_apiyi_image_bytes(
    *,
    prompt: str,
    aspect_ratio: str,
    model: str,
    references: List[Dict[str, Any]],
    api_key: str,
    openai_base_url: str,
    gemini_base_url: str,
) -> Dict[str, Any]:
    """Call APIYI using trusted explicit runtime inputs without filesystem writes."""
    import requests

    model_id, meta = _resolve_model(model)
    aspect = resolve_aspect_ratio(aspect_ratio)
    if model_id == _NANO_MODEL:
        if references:
            raise ValueError("nano-banana-2 does not accept reference images")
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE", "TEXT"],
                "imageConfig": {"aspectRatio": _GEMINI_ASPECT_RATIOS.get(aspect, "1:1")},
            },
        }
        response = requests.post(
            f"{gemini_base_url.rstrip('/')}/models/{_upstream_model(model_id)}:generateContent",
            headers={"Authorization": f"Bearer {api_key}", "x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        images = _extract_gemini_images(response.json())
        if not images:
            raise ValueError("APIYI response contained no image data")
        first = images[0]
        if first.startswith("data:"):
            header, encoded = first.split(",", 1)
            mime_type = header[5:].split(";", 1)[0].lower()
            image_bytes = base64.b64decode(encoded)
        else:
            downloaded = requests.get(first, timeout=60)
            downloaded.raise_for_status()
            mime_type = downloaded.headers.get("Content-Type", "image/png").split(";", 1)[0].lower()
            image_bytes = downloaded.content
        return {"image_bytes": image_bytes, "mime_type": mime_type, "metadata": {"upstream_model": _upstream_model(model_id)}}
    size = _SIZES.get(aspect, _SIZES["square"])
    headers = {"Authorization": f"Bearer {api_key}"}
    if references:
        files = [("image", (item["name"], item["data"], item["mime_type"])) for item in references]
        response = requests.post(
            f"{openai_base_url.rstrip('/')}/images/edits", headers=headers,
            data={"model": _upstream_model(model_id), "prompt": prompt, "size": size, "n": "1", "quality": str(meta["quality"])},
            files=files, timeout=_REQUEST_TIMEOUT,
        )
    else:
        response = requests.post(
            f"{openai_base_url.rstrip('/')}/images/generations",
            headers={**headers, "Content-Type": "application/json"},
            json={"model": _upstream_model(model_id), "prompt": prompt, "size": size, "n": 1, "quality": meta["quality"]},
            timeout=_REQUEST_TIMEOUT,
        )
    response.raise_for_status()
    payload = response.json()
    image_bytes, mime_type = _decode_apiyi_image_payload(payload)
    _, _, revised_prompt = _extract_openai_image(payload)
    metadata: Dict[str, Any] = {"size": size, "quality": meta["quality"], "upstream_model": _upstream_model(model_id)}
    if revised_prompt:
        metadata["revised_prompt"] = revised_prompt
    return {"image_bytes": image_bytes, "mime_type": mime_type, "metadata": metadata}


class ApiyiImageGenProvider(ImageGenProvider):
    """APIYI image-generation provider for GPT-Image-2 and Nano Banana 2."""

    @property
    def name(self) -> str:
        return "apiyi"

    @property
    def display_name(self) -> str:
        return "APIYI"

    def is_available(self) -> bool:
        return bool(os.environ.get("APIYI_API_KEY", "").strip())

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta.get("speed", "varies"),
                "strengths": meta.get("strengths", "APIYI image generation"),
                "price": "APIYI billing",
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return _DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "APIYI",
            "badge": "paid",
            "tag": "GPT-Image-2 and Nano Banana 2 via APIYI; uses APIYI_API_KEY",
            "env_vars": [
                {
                    "key": "APIYI_API_KEY",
                    "prompt": "APIYI API key",
                    "url": "https://www.apiyi.com/",
                }
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        model_id, _ = _resolve_model()
        if model_id == _NANO_MODEL:
            return {"modalities": ["text"], "max_reference_images": 0}
        return {"modalities": ["text", "image"], "max_reference_images": _MAX_GPT_REFERENCE_IMAGES}

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
        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider=self.name,
                aspect_ratio=aspect,
            )
        api_key = os.environ.get("APIYI_API_KEY", "").strip()
        if not api_key:
            return error_response(
                error=(
                    "APIYI_API_KEY not set. Put it in your local environment or "
                    "the server-only /opt/hermes/shared/.env file; do not commit it."
                ),
                error_type="auth_required",
                provider=self.name,
                aspect_ratio=aspect,
            )

        model_id, meta = _resolve_model(kwargs.get("model"))
        if model_id == _NANO_MODEL:
            return self._generate_nano(
                prompt,
                aspect,
                api_key=api_key,
                model_id=model_id,
                image_url=image_url,
                reference_image_urls=reference_image_urls,
            )
        return self._generate_gpt(
            prompt,
            aspect,
            api_key=api_key,
            model_id=model_id,
            meta=meta,
            image_url=image_url,
            reference_image_urls=reference_image_urls,
        )

    def _generate_gpt(
        self,
        prompt: str,
        aspect: str,
        *,
        api_key: str,
        model_id: str,
        meta: Dict[str, Any],
        image_url: Optional[str],
        reference_image_urls: Optional[List[str]],
    ) -> Dict[str, Any]:
        import requests

        upstream = _upstream_model(model_id)
        base_url = _openai_base_url()
        size = _SIZES.get(aspect, _SIZES["square"])
        sources = _collect_sources(image_url, reference_image_urls)[:_MAX_GPT_REFERENCE_IMAGES]
        is_edit = bool(sources)
        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            if is_edit:
                files = []
                for ref in sources:
                    data, filename, mime = _load_image_bytes(ref)
                    files.append(("image", (filename, data, mime)))
                form = {
                    "model": upstream,
                    "prompt": prompt,
                    "size": size,
                    "n": "1",
                    "quality": str(meta["quality"]),
                }
                response = requests.post(
                    f"{base_url}/images/edits",
                    headers=headers,
                    data=form,
                    files=files,
                    timeout=_REQUEST_TIMEOUT,
                )
            else:
                payload = {
                    "model": upstream,
                    "prompt": prompt,
                    "size": size,
                    "n": 1,
                    "quality": meta["quality"],
                }
                response = requests.post(
                    f"{base_url}/images/generations",
                    headers={**headers, "Content-Type": "application/json"},
                    json=payload,
                    timeout=_REQUEST_TIMEOUT,
                )
            response.raise_for_status()
        except requests.HTTPError as exc:
            resp = exc.response
            status = resp.status_code if resp is not None else 0
            try:
                err_msg = resp.json().get("error", {}).get("message", resp.text[:300])
            except Exception:  # noqa: BLE001
                err_msg = resp.text[:300] if resp is not None else str(exc)
            return error_response(
                error=f"APIYI GPT-Image-2 request failed ({status}): {err_msg}",
                error_type="api_error",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except Exception as exc:  # noqa: BLE001
            return error_response(
                error=f"APIYI GPT-Image-2 request failed: {exc}",
                error_type="api_error",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            return error_response(
                error=f"APIYI GPT-Image-2 returned invalid JSON: {exc}",
                error_type="invalid_response",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        b64, url, revised_prompt = _extract_openai_image(payload)
        if not b64 and not url:
            return error_response(
                error="APIYI GPT-Image-2 response contained no image data",
                error_type="empty_response",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            if b64:
                image_ref = str(save_b64_image(b64, prefix=f"apiyi_{model_id}"))
            else:
                image_ref = str(save_url_image(url or "", prefix=f"apiyi_{model_id}"))
        except Exception as exc:  # noqa: BLE001
            return error_response(
                error=f"Could not save APIYI GPT-Image-2 image: {exc}",
                error_type="io_error",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        extra: Dict[str, Any] = {
            "upstream_model": upstream,
            "size": size,
            "quality": meta["quality"],
        }
        if revised_prompt:
            extra["revised_prompt"] = revised_prompt
        return success_response(
            image=image_ref,
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=self.name,
            modality="image" if is_edit else "text",
            extra=extra,
        )

    def _generate_nano(
        self,
        prompt: str,
        aspect: str,
        *,
        api_key: str,
        model_id: str,
        image_url: Optional[str],
        reference_image_urls: Optional[List[str]],
    ) -> Dict[str, Any]:
        import requests

        upstream = _upstream_model(model_id)
        base_url = _gemini_base_url()
        parts: List[Dict[str, Any]] = [{"text": prompt}]
        references = _collect_sources(image_url, reference_image_urls)[:_MAX_NANO_REFERENCE_IMAGES]
        for ref in references:
            try:
                data, _, mime = _load_image_bytes(ref)
            except Exception as exc:  # noqa: BLE001
                return error_response(
                    error=f"Could not load source image for APIYI Nano Banana 2: {exc}",
                    error_type="io_error",
                    provider=self.name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}})

        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["IMAGE", "TEXT"],
                "imageConfig": {"aspectRatio": _GEMINI_ASPECT_RATIOS.get(aspect, "1:1")},
            },
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                f"{base_url}/models/{upstream}:generateContent",
                headers=headers,
                json=payload,
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            resp = exc.response
            status = resp.status_code if resp is not None else 0
            try:
                err_msg = resp.json().get("error", {}).get("message", resp.text[:300])
            except Exception:  # noqa: BLE001
                err_msg = resp.text[:300] if resp is not None else str(exc)
            return error_response(
                error=f"APIYI Nano Banana 2 request failed ({status}): {err_msg}",
                error_type="api_error",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except Exception as exc:  # noqa: BLE001
            return error_response(
                error=f"APIYI Nano Banana 2 request failed: {exc}",
                error_type="api_error",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            return error_response(
                error=f"APIYI Nano Banana 2 returned invalid JSON: {exc}",
                error_type="invalid_response",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        images = _extract_gemini_images(payload)
        if not images:
            return error_response(
                error="APIYI Nano Banana 2 response contained no image data",
                error_type="empty_response",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        try:
            image_ref = _save_image_ref(images[0], prefix=f"apiyi_{model_id}")
        except Exception as exc:  # noqa: BLE001
            return error_response(
                error=f"Could not save APIYI Nano Banana 2 image: {exc}",
                error_type="io_error",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        return success_response(
            image=image_ref,
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=self.name,
            modality="image" if references else "text",
            extra={
                "upstream_model": upstream,
                "aspect_ratio_native": _GEMINI_ASPECT_RATIOS.get(aspect, "1:1"),
            },
        )


def register(ctx: Any) -> None:
    """Plugin entry point — register APIYI image generation."""
    ctx.register_image_gen_provider(ApiyiImageGenProvider())
