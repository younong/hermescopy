"""Control-plane-owned media generation routes for authenticated owner workers.

This is the generation-media counterpart of ``deployment_inference.py``: the
operator declares routes for the ``image`` and ``video`` kinds, credentials
stay in the Control Plane, and workers receive only display-safe route
descriptors plus a private relay connection. See ``docs/model-plane.md`` —
this module and the media relay are the only deployment-managed credential
path for generation media.

Routes are declared in the Control Plane environment as
``HERMES_DEPLOYMENT_MEDIA_ROUTES`` — a JSON array of objects::

    {
        "kind": "image",                     # "image" | "video"
        "provider": "apiyi",                 # capability provider name
        "models": ["gpt-image-2-medium"],    # allowed model ids
        "default_model": "gpt-image-2-medium",
        "key_env": "APIYI_API_KEY",          # Control Plane credential env
        "executor": "plugins.image_gen.apiyi:generate_apiyi_image_bytes",
        "base_urls": {"openai_base_url": "https://api.example.com/v1"},
        "executor_params": {},               # extra executor kwargs
        "text_only_models": [],              # models that reject references
        "limits": {"max_reference_images": 16, ...}
    }

When ``HERMES_DEPLOYMENT_MEDIA_ROUTES`` is unset, an ``APIYI_API_KEY`` in the
Control Plane environment activates the legacy default APIYI image route so
existing deployments keep working unchanged.
"""
from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from hermes_cli.model_plane.kinds import GATEWAY_KINDS

DEFAULT_POLICY_ID = "deployment-media-v1"
ROUTES_ENV = "HERMES_DEPLOYMENT_MEDIA_ROUTES"
POLICY_ID_ENV = "HERMES_DEPLOYMENT_MEDIA_POLICY_ID"

OPERATIONS = frozenset({"image_generate", "video_generate"})
_OPERATION_KINDS = {"image_generate": "image", "video_generate": "video"}

MAX_REFERENCE_IMAGES = 16
MAX_REFERENCE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_REFERENCE_BYTES = 48 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
_LIMIT_NAMES = (
    "max_reference_images",
    "max_reference_bytes",
    "max_total_reference_bytes",
    "max_output_bytes",
)

IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
VIDEO_MIME_TYPES = frozenset({"video/mp4", "video/webm", "video/quicktime"})

_DEFAULT_APIYI_MODELS = (
    "gpt-image-2-low",
    "gpt-image-2-medium",
    "gpt-image-2-high",
    "nano-banana-2",
)
_DEFAULT_APIYI_TEXT_ONLY_MODELS = ("nano-banana-2",)


class DeploymentMediaPolicyInvalid(RuntimeError):
    """The deployment supplied an unusable media policy."""


class DeploymentMediaSelectionRejected(RuntimeError):
    """A worker request is outside the deployment media policy."""


def _clean_model_list(values: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise DeploymentMediaPolicyInvalid(f"deployment media {field} is invalid")
    models = tuple(dict.fromkeys(
        str(value or "").strip() for value in values if str(value or "").strip()
    ))
    if not models:
        raise DeploymentMediaPolicyInvalid(f"deployment media {field} is invalid")
    return models


def _validate_https_url(value: str, *, field: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise DeploymentMediaPolicyInvalid(f"deployment media {field} is invalid")
    return value.rstrip("/")


@dataclass(frozen=True)
class DeploymentMediaRouteDescriptor:
    """Non-secret route fields safe to pass to an owner worker."""

    kind: str
    provider: str
    models: tuple[str, ...]
    default_model: str
    text_only_models: tuple[str, ...] = ()
    max_reference_images: int = MAX_REFERENCE_IMAGES
    max_reference_bytes: int = MAX_REFERENCE_BYTES
    max_total_reference_bytes: int = MAX_TOTAL_REFERENCE_BYTES
    max_output_bytes: int = MAX_OUTPUT_BYTES

    def __post_init__(self) -> None:
        kind = str(self.kind or "").strip().lower()
        provider = str(self.provider or "").strip().lower()
        default_model = str(self.default_model or "").strip()
        models = _clean_model_list(self.models, field="route models")
        text_only = tuple(dict.fromkeys(
            str(value or "").strip() for value in self.text_only_models
            if str(value or "").strip()
        ))
        if kind not in GATEWAY_KINDS or not provider:
            raise DeploymentMediaPolicyInvalid("deployment media route identity is invalid")
        if default_model not in models:
            raise DeploymentMediaPolicyInvalid("deployment media route models are invalid")
        if any(value not in models for value in text_only):
            raise DeploymentMediaPolicyInvalid("deployment media route models are invalid")
        for name in _LIMIT_NAMES:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise DeploymentMediaPolicyInvalid("deployment media route limits are invalid")
        if self.max_total_reference_bytes < self.max_reference_bytes:
            raise DeploymentMediaPolicyInvalid("deployment media route limits are invalid")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "models", models)
        object.__setattr__(self, "default_model", default_model)
        object.__setattr__(self, "text_only_models", text_only)

    def allows_model(self, model: str) -> bool:
        return str(model or "").strip() in self.models

    def supports_references(self, model: str) -> bool:
        return str(model or "").strip() not in self.text_only_models

    def capabilities_for(self, model: str | None = None) -> dict[str, Any]:
        selected = str(model or self.default_model).strip()
        if not self.allows_model(selected):
            raise DeploymentMediaSelectionRejected("deployment media model is not allowed")
        supports_references = self.supports_references(selected)
        return {
            "provider": self.provider,
            "model": selected,
            "modalities": ["text", "image"] if supports_references else ["text"],
            "max_reference_images": self.max_reference_images if supports_references else 0,
        }

    def payload(self) -> dict[str, Any]:
        """Return the stable non-secret representation exposed to workers."""
        return {
            "kind": self.kind,
            "provider": self.provider,
            "models": list(self.models),
            "default_model": self.default_model,
            "text_only_models": list(self.text_only_models),
            "max_reference_images": self.max_reference_images,
            "max_reference_bytes": self.max_reference_bytes,
            "max_total_reference_bytes": self.max_total_reference_bytes,
            "max_output_bytes": self.max_output_bytes,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DeploymentMediaRouteDescriptor":
        if not isinstance(payload, Mapping):
            raise DeploymentMediaPolicyInvalid("deployment media route descriptor is invalid")
        try:
            return cls(
                kind=payload["kind"],
                provider=payload["provider"],
                models=tuple(payload["models"]),
                default_model=payload["default_model"],
                text_only_models=tuple(payload.get("text_only_models") or ()),
                **{name: int(payload[name]) for name in _LIMIT_NAMES},
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DeploymentMediaPolicyInvalid(
                "deployment media route descriptor is invalid"
            ) from exc


@dataclass(frozen=True)
class DeploymentMediaDescriptor:
    """Non-secret deployment media policy safe to pass to one owner worker."""

    policy_id: str
    routes: tuple[DeploymentMediaRouteDescriptor, ...]

    def __post_init__(self) -> None:
        policy_id = str(self.policy_id or "").strip()
        routes = tuple(self.routes)
        if not policy_id or not routes:
            raise DeploymentMediaPolicyInvalid("deployment media descriptor is incomplete")
        if any(not isinstance(route, DeploymentMediaRouteDescriptor) for route in routes):
            raise DeploymentMediaPolicyInvalid("deployment media descriptor routes are invalid")
        identities = [(route.kind, route.provider) for route in routes]
        if len(set(identities)) != len(identities):
            raise DeploymentMediaPolicyInvalid("deployment media descriptor routes are invalid")
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "routes", routes)

    def route_for(
        self,
        kind: str,
        provider: str,
        model: str | None = None,
    ) -> DeploymentMediaRouteDescriptor | None:
        selected_kind = str(kind or "").strip().lower()
        selected_provider = str(provider or "").strip().lower()
        selected_model = str(model or "").strip()
        for route in self.routes:
            if route.kind != selected_kind:
                continue
            if selected_provider and route.provider != selected_provider:
                continue
            if selected_model and not route.allows_model(selected_model):
                continue
            return route
        return None

    def payload(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "routes": [route.payload() for route in self.routes],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DeploymentMediaDescriptor":
        if not isinstance(payload, Mapping):
            raise DeploymentMediaPolicyInvalid("deployment media descriptor is invalid")
        try:
            return cls(
                policy_id=payload["policy_id"],
                routes=tuple(
                    DeploymentMediaRouteDescriptor.from_payload(route)
                    for route in payload["routes"]
                ),
            )
        except (KeyError, TypeError) as exc:
            raise DeploymentMediaPolicyInvalid("deployment media descriptor is invalid") from exc


@dataclass(frozen=True)
class DeploymentMediaRoute:
    """Control-Plane-only route paired with its private executor."""

    descriptor: DeploymentMediaRouteDescriptor
    key_env: str
    executor: str
    base_urls: Mapping[str, str] | None = None
    executor_params: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        key_env = str(self.key_env or "").strip()
        executor = str(self.executor or "").strip()
        module_name, separator, attribute = executor.partition(":")
        if not key_env:
            raise DeploymentMediaPolicyInvalid("deployment media route credential is invalid")
        if not separator or not module_name or not attribute or "." in attribute:
            raise DeploymentMediaPolicyInvalid("deployment media route executor is invalid")
        base_urls = dict(self.base_urls or {})
        for name, value in base_urls.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(value, str):
                raise DeploymentMediaPolicyInvalid("deployment media route endpoints are invalid")
            base_urls[name] = _validate_https_url(value, field="route endpoints")
        params = dict(self.executor_params or {})
        try:
            json.dumps(params)
        except (TypeError, ValueError) as exc:
            raise DeploymentMediaPolicyInvalid(
                "deployment media route executor params are invalid"
            ) from exc
        object.__setattr__(self, "key_env", key_env)
        object.__setattr__(self, "executor", executor)
        object.__setattr__(self, "base_urls", base_urls)
        object.__setattr__(self, "executor_params", params)

    def load_executor(self) -> Callable[..., Mapping[str, Any]]:
        module_name, _, attribute = self.executor.partition(":")
        try:
            executor = getattr(importlib.import_module(module_name), attribute)
        except (ImportError, AttributeError) as exc:
            raise DeploymentMediaPolicyInvalid(
                "deployment media route executor is unavailable"
            ) from exc
        if not callable(executor):
            raise DeploymentMediaPolicyInvalid("deployment media route executor is invalid")
        return executor


@dataclass(frozen=True)
class DeploymentMediaPolicy:
    """Operator-owned media routes resolved exclusively by the Control Plane."""

    routes: tuple[DeploymentMediaRoute, ...]
    policy_id: str = DEFAULT_POLICY_ID

    def __post_init__(self) -> None:
        policy_id = str(self.policy_id or "").strip()
        routes = tuple(self.routes)
        if not policy_id or not routes:
            raise DeploymentMediaPolicyInvalid("deployment media policy is incomplete")
        if any(not isinstance(route, DeploymentMediaRoute) for route in routes):
            raise DeploymentMediaPolicyInvalid("deployment media policy routes are invalid")
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "routes", routes)
        # Reuses the aggregate descriptor validation (duplicate route identity).
        self.descriptor()

    def descriptor(self) -> DeploymentMediaDescriptor:
        return DeploymentMediaDescriptor(
            policy_id=self.policy_id,
            routes=tuple(route.descriptor for route in self.routes),
        )

    def route_for(
        self,
        kind: str,
        provider: str,
        model: str | None = None,
    ) -> DeploymentMediaRoute | None:
        selected_kind = str(kind or "").strip().lower()
        selected_provider = str(provider or "").strip().lower()
        selected_model = str(model or "").strip()
        for route in self.routes:
            descriptor = route.descriptor
            if descriptor.kind != selected_kind:
                continue
            if selected_provider and descriptor.provider != selected_provider:
                continue
            if selected_model and not descriptor.allows_model(selected_model):
                continue
            return route
        return None

    def _resolve_api_key(self, route: DeploymentMediaRoute) -> str:
        api_key = os.environ.get(route.key_env, "").strip()
        if not api_key:
            raise DeploymentMediaPolicyInvalid("deployment media credentials are unavailable")
        return api_key

    def execute(
        self,
        operation: str,
        *,
        provider: str,
        model: str,
        prompt: str,
        aspect_ratio: str = "",
        references: tuple[dict[str, Any], ...] = (),
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        kind = _OPERATION_KINDS.get(str(operation or "").strip())
        if kind is None:
            raise DeploymentMediaSelectionRejected("deployment media operation is not allowed")
        route = self.route_for(kind, provider, model)
        if route is None:
            raise DeploymentMediaSelectionRejected("deployment media selection is not allowed")
        descriptor = route.descriptor
        if references and not descriptor.supports_references(model):
            raise DeploymentMediaSelectionRejected(
                "deployment media model does not accept references"
            )
        kwargs: dict[str, Any] = {**route.base_urls, **route.executor_params}
        try:
            result = dict(route.load_executor()(
                api_key=self._resolve_api_key(route),
                model=model,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                references=list(references),
                params=dict(params or {}),
                **kwargs,
            ))
        except DeploymentMediaSelectionRejected:
            raise
        except Exception as exc:
            raise DeploymentMediaPolicyInvalid("deployment media generation failed") from exc
        return self._normalize_result(
            descriptor,
            result,
            model=model,
            aspect_ratio=aspect_ratio,
            has_references=bool(references),
        )

    def _normalize_result(
        self,
        descriptor: DeploymentMediaRouteDescriptor,
        result: Mapping[str, Any],
        *,
        model: str,
        aspect_ratio: str,
        has_references: bool,
    ) -> dict[str, Any]:
        metadata = result.get("metadata")
        normalized: dict[str, Any] = {
            "provider": descriptor.provider,
            "model": model,
            "aspect_ratio": aspect_ratio,
            "modality": "image" if has_references else "text",
            "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
        }
        if descriptor.kind == "image":
            image = result.get("image_bytes")
            mime_type = str(result.get("mime_type") or "").strip().lower()
            if (
                not isinstance(image, bytes)
                or not image
                or len(image) > descriptor.max_output_bytes
                or mime_type not in IMAGE_MIME_TYPES
            ):
                raise DeploymentMediaPolicyInvalid("deployment media response is invalid")
            normalized["image_bytes"] = image
            normalized["mime_type"] = mime_type
            return normalized
        video_url = str(result.get("video_url") or "").strip()
        video = result.get("video_bytes")
        if video_url and video is None:
            parsed = urlparse(video_url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise DeploymentMediaPolicyInvalid("deployment media response is invalid")
            normalized["video_url"] = video_url
            return normalized
        mime_type = str(result.get("mime_type") or "").strip().lower()
        if (
            not isinstance(video, bytes)
            or not video
            or len(video) > descriptor.max_output_bytes
            or mime_type not in VIDEO_MIME_TYPES
        ):
            raise DeploymentMediaPolicyInvalid("deployment media response is invalid")
        normalized["video_bytes"] = video
        normalized["mime_type"] = mime_type
        return normalized


def _route_declaration(payload: Mapping[str, Any]) -> DeploymentMediaRoute:
    if not isinstance(payload, Mapping):
        raise DeploymentMediaPolicyInvalid("deployment media route declaration is invalid")
    try:
        limits = payload.get("limits") or {}
        descriptor = DeploymentMediaRouteDescriptor(
            kind=payload["kind"],
            provider=payload["provider"],
            models=tuple(payload["models"]),
            default_model=str(payload.get("default_model") or "").strip()
            or str(tuple(payload["models"])[0]),
            text_only_models=tuple(payload.get("text_only_models") or ()),
            **{name: int(limits[name]) for name in _LIMIT_NAMES if name in limits},
        )
        return DeploymentMediaRoute(
            descriptor=descriptor,
            key_env=payload["key_env"],
            executor=payload["executor"],
            base_urls=payload.get("base_urls") or {},
            executor_params=payload.get("executor_params") or {},
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DeploymentMediaPolicyInvalid(
            "deployment media route declaration is invalid"
        ) from exc


def _default_apiyi_route() -> DeploymentMediaRoute | None:
    """Legacy auto-activation: an APIYI key implies the default image route."""
    if not os.environ.get("APIYI_API_KEY", "").strip():
        return None
    openai_base_url = (
        os.environ.get("APIYI_OPENAI_BASE_URL")
        or os.environ.get("APIYI_BASE_URL")
        or "https://api.apiyi.com/v1"
    ).strip()
    gemini_base_url = (
        os.environ.get("APIYI_GEMINI_BASE_URL") or "https://api.apiyi.com/v1beta"
    ).strip()
    default_model = (
        os.environ.get("APIYI_IMAGE_MODEL", "").strip() or "gpt-image-2-medium"
    )
    return _route_declaration({
        "kind": "image",
        "provider": "apiyi",
        "models": list(_DEFAULT_APIYI_MODELS),
        "default_model": default_model,
        "key_env": "APIYI_API_KEY",
        "executor": "plugins.image_gen.apiyi:generate_apiyi_image_bytes",
        "base_urls": {
            "openai_base_url": openai_base_url,
            "gemini_base_url": gemini_base_url,
        },
        "text_only_models": list(_DEFAULT_APIYI_TEXT_ONLY_MODELS),
    })


def policy_from_control_plane_environment() -> DeploymentMediaPolicy | None:
    """Build the deployment media policy from Control Plane settings."""
    policy_id = os.environ.get(POLICY_ID_ENV, "").strip() or DEFAULT_POLICY_ID
    raw = os.environ.get(ROUTES_ENV, "").strip()
    if raw:
        try:
            declarations = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeploymentMediaPolicyInvalid(
                "deployment media routes environment is invalid"
            ) from exc
        if not isinstance(declarations, list):
            raise DeploymentMediaPolicyInvalid(
                "deployment media routes environment is invalid"
            )
        routes = tuple(_route_declaration(item) for item in declarations)
        if not routes:
            raise DeploymentMediaPolicyInvalid(
                "deployment media routes environment is invalid"
            )
        return DeploymentMediaPolicy(routes=routes, policy_id=policy_id)
    default_route = _default_apiyi_route()
    if default_route is None:
        return None
    return DeploymentMediaPolicy(routes=(default_route,), policy_id=policy_id)


def deployment_media_descriptor_from_environment(
    source: Mapping[str, str] | None = None,
) -> DeploymentMediaDescriptor | None:
    """Decode the supervisor-owned, non-secret worker descriptor."""
    env = source if source is not None else os.environ
    policy_id = str(env.get(POLICY_ID_ENV, "")).strip()
    raw = str(env.get(ROUTES_ENV, "")).strip()
    if not policy_id and not raw:
        return None
    if not policy_id or not raw:
        raise DeploymentMediaPolicyInvalid("deployment media descriptor is incomplete")
    try:
        payloads = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeploymentMediaPolicyInvalid(
            "deployment media descriptor routes are invalid"
        ) from exc
    if not isinstance(payloads, list):
        raise DeploymentMediaPolicyInvalid("deployment media descriptor routes are invalid")
    return DeploymentMediaDescriptor(
        policy_id=policy_id,
        routes=tuple(DeploymentMediaRouteDescriptor.from_payload(item) for item in payloads),
    )


def deployment_media_route_from_environment(
    kind: str,
    *,
    provider: str = "",
    model: str = "",
    source: Mapping[str, str] | None = None,
) -> DeploymentMediaRouteDescriptor | None:
    """Return the deployment route matching one selection, if this is a worker.

    Tool-layer availability checks and dynamic schemas mirror the worker
    dispatcher's routing: an explicit (provider, model) selection matches
    only a route for that pair; an empty provider matches the first route
    of the kind (unconfigured users ride the deployment route).
    """
    from hermes_cli.owner_runtime import is_owner_worker_env

    if not is_owner_worker_env(source=source):
        return None
    descriptor = deployment_media_descriptor_from_environment(source=source)
    if descriptor is None:
        return None
    return descriptor.route_for(kind, provider, model)
