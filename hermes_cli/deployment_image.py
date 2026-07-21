"""Control-plane-owned image generation for authenticated owner workers."""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

DEFAULT_MODEL = "gpt-image-2-medium"
SUPPORTED_MODELS = ("gpt-image-2-low", "gpt-image-2-medium", "gpt-image-2-high", "nano-banana-2")


class DeploymentImagePolicyInvalid(RuntimeError):
    """The deployment supplied an unusable image policy."""


class DeploymentImageSelectionRejected(RuntimeError):
    """A worker request is outside the deployment image policy."""


@dataclass(frozen=True)
class DeploymentImageDescriptor:
    """Non-secret image capability fields safe to pass to an owner worker."""

    provider: str
    model: str
    policy_id: str
    allowed_models: tuple[str, ...]
    max_reference_images: int = 16
    max_reference_bytes: int = 16 * 1024 * 1024
    max_total_reference_bytes: int = 48 * 1024 * 1024
    max_output_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        model = str(self.model or "").strip()
        policy_id = str(self.policy_id or "").strip()
        allowed = tuple(dict.fromkeys(str(v or "").strip() for v in self.allowed_models if str(v or "").strip()))
        if provider != "apiyi" or not model or not policy_id:
            raise DeploymentImagePolicyInvalid("deployment image identity is invalid")
        if model not in allowed or any(value not in SUPPORTED_MODELS for value in allowed):
            raise DeploymentImagePolicyInvalid("deployment image models are invalid")
        for name in ("max_reference_images", "max_reference_bytes", "max_total_reference_bytes", "max_output_bytes"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise DeploymentImagePolicyInvalid("deployment image limits are invalid")
        if self.max_total_reference_bytes < self.max_reference_bytes:
            raise DeploymentImagePolicyInvalid("deployment image limits are invalid")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "allowed_models", allowed)

    def allows_model(self, model: str) -> bool:
        return str(model or "").strip() in self.allowed_models

    def capabilities_for(self, model: str | None = None) -> dict[str, Any]:
        selected = str(model or self.model).strip()
        if not self.allows_model(selected):
            raise DeploymentImageSelectionRejected("deployment image model is not allowed")
        supports_references = selected != "nano-banana-2"
        return {
            "provider": "APIYI",
            "model": selected,
            "modalities": ["text", "image"] if supports_references else ["text"],
            "max_reference_images": self.max_reference_images if supports_references else 0,
        }


@dataclass(frozen=True)
class DeploymentImagePolicy:
    """Credential-bearing policy retained exclusively by the Control Plane."""

    runtime_resolver: Callable[[], Mapping[str, Any]]
    image_generator: Callable[..., Mapping[str, Any]]
    model: str = DEFAULT_MODEL
    policy_id: str = "deployment-apiyi-image-v1"
    allowed_models: tuple[str, ...] = SUPPORTED_MODELS
    max_reference_images: int = 16
    max_reference_bytes: int = 16 * 1024 * 1024
    max_total_reference_bytes: int = 48 * 1024 * 1024
    max_output_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        if not callable(self.runtime_resolver) or not callable(self.image_generator):
            raise DeploymentImagePolicyInvalid("deployment image runtime is invalid")
        descriptor = self.descriptor()
        object.__setattr__(self, "model", descriptor.model)
        object.__setattr__(self, "policy_id", descriptor.policy_id)
        object.__setattr__(self, "allowed_models", descriptor.allowed_models)

    def descriptor(self) -> DeploymentImageDescriptor:
        return DeploymentImageDescriptor(
            provider="apiyi", model=self.model, policy_id=self.policy_id,
            allowed_models=self.allowed_models, max_reference_images=self.max_reference_images,
            max_reference_bytes=self.max_reference_bytes,
            max_total_reference_bytes=self.max_total_reference_bytes, max_output_bytes=self.max_output_bytes,
        )

    def resolve_runtime(self) -> dict[str, Any]:
        try:
            runtime = dict(self.runtime_resolver())
        except Exception as exc:
            raise DeploymentImagePolicyInvalid("deployment image runtime is unavailable") from exc
        api_key = str(runtime.get("api_key") or "").strip()
        openai_base_url = str(runtime.get("openai_base_url") or "").strip().rstrip("/")
        gemini_base_url = str(runtime.get("gemini_base_url") or "").strip().rstrip("/")
        if not api_key:
            raise DeploymentImagePolicyInvalid("deployment image credentials are unavailable")
        for value in (openai_base_url, gemini_base_url):
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise DeploymentImagePolicyInvalid("deployment image endpoint is invalid")
        return {"api_key": api_key, "openai_base_url": openai_base_url, "gemini_base_url": gemini_base_url}

    def generate(self, *, prompt: str, aspect_ratio: str, model: str, references: list[dict[str, Any]]) -> dict[str, Any]:
        descriptor = self.descriptor()
        if not descriptor.allows_model(model):
            raise DeploymentImageSelectionRejected("deployment image model is not allowed")
        if references and not descriptor.capabilities_for(model)["max_reference_images"]:
            raise DeploymentImageSelectionRejected("deployment image model does not accept references")
        try:
            result = dict(self.image_generator(prompt=prompt, aspect_ratio=aspect_ratio, model=model, references=references, **self.resolve_runtime()))
        except DeploymentImageSelectionRejected:
            raise
        except Exception as exc:
            raise DeploymentImagePolicyInvalid("deployment image generation failed") from exc
        image = result.get("image_bytes")
        mime_type = str(result.get("mime_type") or "").strip().lower()
        if not isinstance(image, bytes) or not image or len(image) > descriptor.max_output_bytes:
            raise DeploymentImagePolicyInvalid("deployment image response is invalid")
        if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise DeploymentImagePolicyInvalid("deployment image response type is invalid")
        return {"image_bytes": image, "mime_type": mime_type, "model": model, "provider": "apiyi",
                "aspect_ratio": aspect_ratio, "modality": "image" if references else "text",
                "metadata": dict(result.get("metadata") or {})}


def deployment_image_descriptor_from_environment(source: Mapping[str, str] | None = None) -> DeploymentImageDescriptor | None:
    env = source if source is not None else os.environ
    provider = str(env.get("HERMES_DEPLOYMENT_IMAGE_PROVIDER", "")).strip().lower()
    model = str(env.get("HERMES_DEPLOYMENT_IMAGE_MODEL", "")).strip()
    policy_id = str(env.get("HERMES_DEPLOYMENT_IMAGE_POLICY_ID", "")).strip()
    raw_allowed = str(env.get("HERMES_DEPLOYMENT_IMAGE_ALLOWED_MODELS", "")).strip()
    raw_limits = {
        "max_reference_images": env.get("HERMES_DEPLOYMENT_IMAGE_MAX_REFERENCES"),
        "max_reference_bytes": env.get("HERMES_DEPLOYMENT_IMAGE_MAX_REFERENCE_BYTES"),
        "max_total_reference_bytes": env.get("HERMES_DEPLOYMENT_IMAGE_MAX_TOTAL_REFERENCE_BYTES"),
        "max_output_bytes": env.get("HERMES_DEPLOYMENT_IMAGE_MAX_OUTPUT_BYTES"),
    }
    if not any((provider, model, policy_id, raw_allowed, *(value for value in raw_limits.values() if value))):
        return None
    if not all((provider, model, policy_id, raw_allowed)):
        raise DeploymentImagePolicyInvalid("deployment image descriptor is incomplete")
    try:
        limits = {name: int(value) for name, value in raw_limits.items()}
    except (TypeError, ValueError) as exc:
        raise DeploymentImagePolicyInvalid("deployment image descriptor limits are invalid") from exc
    return DeploymentImageDescriptor(provider=provider, model=model, policy_id=policy_id,
        allowed_models=tuple(item.strip() for item in raw_allowed.split(",") if item.strip()), **limits)


def policy_from_control_plane_environment() -> DeploymentImagePolicy | None:
    api_key = os.environ.get("APIYI_API_KEY", "").strip()
    if not api_key:
        return None
    openai_base_url = (os.environ.get("APIYI_OPENAI_BASE_URL") or os.environ.get("APIYI_BASE_URL") or "https://api.apiyi.com/v1").strip().rstrip("/")
    gemini_base_url = (os.environ.get("APIYI_GEMINI_BASE_URL") or "https://api.apiyi.com/v1beta").strip().rstrip("/")
    model = os.environ.get("APIYI_IMAGE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    def _resolve_runtime() -> Mapping[str, Any]:
        return {"api_key": api_key, "openai_base_url": openai_base_url, "gemini_base_url": gemini_base_url}

    def _generate(**kwargs: Any) -> Mapping[str, Any]:
        from plugins.image_gen.apiyi import generate_apiyi_image_bytes
        return generate_apiyi_image_bytes(**kwargs)

    return DeploymentImagePolicy(runtime_resolver=_resolve_runtime, image_generator=_generate, model=model)


def load_deployment_image_policy(spec: str | None = None) -> DeploymentImagePolicy | None:
    value = str(spec or "").strip()
    if not value:
        return policy_from_control_plane_environment()
    module_name, separator, attribute = value.partition(":")
    if not separator or not module_name or not attribute or "." in attribute:
        raise DeploymentImagePolicyInvalid("deployment image policy factory is invalid")
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as exc:
        raise DeploymentImagePolicyInvalid("deployment image policy factory is unavailable") from exc
    if not callable(factory):
        raise DeploymentImagePolicyInvalid("deployment image policy factory is invalid")
    try:
        policy = factory()
    except Exception as exc:
        raise DeploymentImagePolicyInvalid("deployment image policy factory failed") from exc
    if policy is not None and not isinstance(policy, DeploymentImagePolicy):
        raise DeploymentImagePolicyInvalid("deployment image policy factory returned invalid policy")
    return policy
