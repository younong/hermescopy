"""Control-plane-owned inference routes for authenticated owner workers.

An authenticated owner worker must not inherit the Dashboard process environment or
copy its auth store.  This module lets an operator explicitly provide deployment
inference routes.  Policies and credentials live in the Control Plane; workers
receive only display-safe route descriptors and a private relay connection.
"""
from __future__ import annotations

import importlib
import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlparse


_SUPPORTED_API_MODES = frozenset({"chat_completions", "anthropic_messages"})
_SUPPORTS_VISION_ENV = "HERMES_DEPLOYMENT_INFERENCE_SUPPORTS_VISION"
_ROUTES_ENV = "HERMES_DEPLOYMENT_INFERENCE_ROUTES"
DEPLOYMENT_INFERENCE_RELAY_MARKER = "deployment-inference-relay"


def is_deployment_inference_relay(api_key: object) -> bool:
    """Return whether runtime credentials target the owner-local cloud relay."""
    return api_key == DEPLOYMENT_INFERENCE_RELAY_MARKER


def _parse_optional_bool(raw: object, *, field: str) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in {0, 1}:
        return bool(raw)
    if isinstance(raw, str):
        value = raw.strip().lower()
        if not value:
            return None
        if value in {"true", "yes", "on", "1"}:
            return True
        if value in {"false", "no", "off", "0"}:
            return False
    raise DeploymentInferencePolicyInvalid(f"{field} must be true or false")


class DeploymentInferencePolicyInvalid(RuntimeError):
    """The deployment supplied an unusable inference policy."""


class DeploymentInferenceSelectionRejected(RuntimeError):
    """An explicit owner/request selection cannot use deployment inference."""


@dataclass(frozen=True)
class DeploymentInferenceRouteDescriptor:
    """One non-secret provider/model route safe to pass to an owner worker."""

    provider: str
    model: str
    api_mode: str
    name: str = ""
    supports_vision: bool | None = None

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        model = str(self.model or "").strip()
        if not provider or not model:
            raise DeploymentInferencePolicyInvalid("deployment inference route identity is required")
        if self.api_mode not in _SUPPORTED_API_MODES:
            raise DeploymentInferencePolicyInvalid("deployment inference route api mode is unsupported")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "name", str(self.name or "").strip())
        object.__setattr__(
            self,
            "supports_vision",
            _parse_optional_bool(
                self.supports_vision,
                field="deployment inference route supports_vision",
            ),
        )


@dataclass(frozen=True)
class DeploymentInferenceDescriptor:
    """Non-secret deployment policy fields safe to pass to one owner worker."""

    provider: str
    model: str
    api_mode: str
    policy_id: str
    allowed_models: tuple[str, ...]
    supports_vision: bool | None = None
    routes: tuple[DeploymentInferenceRouteDescriptor, ...] = ()

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        model = str(self.model or "").strip()
        policy_id = str(self.policy_id or "").strip()
        if not provider or not model or not policy_id:
            raise DeploymentInferencePolicyInvalid("deployment inference identity is required")
        if self.api_mode not in _SUPPORTED_API_MODES:
            raise DeploymentInferencePolicyInvalid("deployment inference api mode is unsupported")
        allowed = tuple(dict.fromkeys(
            str(value or "").strip()
            for value in self.allowed_models
            if str(value or "").strip()
        ))
        if not allowed:
            allowed = (model,)
        if model not in allowed:
            raise DeploymentInferencePolicyInvalid("deployment inference descriptor models are invalid")

        routes = tuple(self.routes) or tuple(
            DeploymentInferenceRouteDescriptor(
                provider=provider,
                model=allowed_model,
                api_mode=self.api_mode,
                supports_vision=self.supports_vision if allowed_model == model else None,
            )
            for allowed_model in allowed
        )
        route_models = tuple(route.model for route in routes)
        if len(set(route_models)) != len(route_models) or set(route_models) != set(allowed):
            raise DeploymentInferencePolicyInvalid("deployment inference descriptor routes are invalid")
        default_route = next((route for route in routes if route.model == model), None)
        if (
            default_route is None
            or default_route.provider != provider
            or default_route.api_mode != self.api_mode
        ):
            raise DeploymentInferencePolicyInvalid("deployment inference default route is invalid")

        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "allowed_models", allowed)
        object.__setattr__(self, "supports_vision", default_route.supports_vision)
        object.__setattr__(self, "routes", routes)

    def route_for(
        self,
        model: str,
        *,
        provider: str | None = None,
    ) -> DeploymentInferenceRouteDescriptor | None:
        selected_model = str(model or "").strip()
        selected_provider = str(provider or "").strip().lower()
        for route in self.routes:
            if route.model != selected_model:
                continue
            if selected_provider and route.provider != selected_provider:
                continue
            return route
        return None

    def allows_model(self, model: str) -> bool:
        return self.route_for(model) is not None

    def routes_json(self) -> str:
        return json.dumps(
            [
                {key: value for key, value in asdict(route).items() if value not in {"", None}}
                for route in self.routes
            ],
            separators=(",", ":"),
            sort_keys=True,
        )

    def relay_runtime(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
    ) -> dict[str, str]:
        """Return only inert, owner-safe fields for a local relay client."""
        selected_model = str(model or self.model).strip()
        route = self.route_for(selected_model, provider=provider)
        if route is None:
            raise DeploymentInferenceSelectionRejected("deployment inference route is not allowed")
        return {
            "provider": route.provider,
            "api_mode": route.api_mode,
            "api_key": DEPLOYMENT_INFERENCE_RELAY_MARKER,
            "source": "deployment-relay",
            "selection_source": "deployment",
            "policy_id": self.policy_id,
            "model": route.model,
        }


@dataclass(frozen=True)
class DeploymentInferenceRoute:
    """Control-Plane-only route paired with its private runtime resolver."""

    provider: str
    model: str
    api_mode: str
    runtime_resolver: Callable[[], Mapping[str, Any]]
    name: str = ""
    supports_vision: bool | None = None

    def __post_init__(self) -> None:
        descriptor = self.descriptor()
        if not callable(self.runtime_resolver):
            raise DeploymentInferencePolicyInvalid("deployment inference runtime resolver is required")
        object.__setattr__(self, "provider", descriptor.provider)
        object.__setattr__(self, "model", descriptor.model)
        object.__setattr__(self, "api_mode", descriptor.api_mode)
        object.__setattr__(self, "name", descriptor.name)
        object.__setattr__(self, "supports_vision", descriptor.supports_vision)

    def descriptor(self) -> DeploymentInferenceRouteDescriptor:
        return DeploymentInferenceRouteDescriptor(
            provider=self.provider,
            model=self.model,
            api_mode=self.api_mode,
            name=self.name,
            supports_vision=self.supports_vision,
        )


@dataclass(frozen=True)
class DeploymentInferencePolicy:
    """Operator-owned inference routes resolved exclusively by the Control Plane.

    Runtime resolvers return normal ``resolve_runtime_provider`` shaped mappings.
    They are deliberately not serializable and never cross the worker boundary,
    which keeps provider keys and auth-store access in the Control Plane process.
    """

    provider: str
    model: str
    api_mode: str
    runtime_resolver: Callable[[], Mapping[str, Any]]
    policy_id: str = "deployment-default-v1"
    allowed_models: tuple[str, ...] = ()
    supports_vision: bool | None = None
    routes: tuple[DeploymentInferenceRoute, ...] = ()

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        model = str(self.model or "").strip()
        policy_id = str(self.policy_id or "").strip()
        if not provider or not model or not policy_id:
            raise DeploymentInferencePolicyInvalid("deployment inference identity is required")
        if not callable(self.runtime_resolver):
            raise DeploymentInferencePolicyInvalid("deployment inference runtime resolver is required")
        if self.api_mode not in _SUPPORTED_API_MODES:
            raise DeploymentInferencePolicyInvalid("deployment inference api mode is unsupported")
        allowed = tuple(dict.fromkeys(
            str(value or "").strip()
            for value in self.allowed_models
            if str(value or "").strip()
        ))
        if not allowed:
            allowed = (model,)
        if model not in allowed:
            allowed = (model, *allowed)

        default_route = DeploymentInferenceRoute(
            provider=provider,
            model=model,
            api_mode=self.api_mode,
            runtime_resolver=self.runtime_resolver,
            supports_vision=self.supports_vision,
        )
        extra_routes = tuple(self.routes)
        explicit_models = {route.model for route in extra_routes}
        if model in explicit_models or len(explicit_models) != len(extra_routes):
            raise DeploymentInferencePolicyInvalid("deployment inference routes are invalid")
        routes = (default_route, *extra_routes)
        for allowed_model in allowed:
            if allowed_model not in {route.model for route in routes}:
                routes += (
                    DeploymentInferenceRoute(
                        provider=provider,
                        model=allowed_model,
                        api_mode=self.api_mode,
                        runtime_resolver=self.runtime_resolver,
                    ),
                )
        if {route.model for route in routes} != set(allowed):
            raise DeploymentInferencePolicyInvalid("deployment inference routes are invalid")

        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "allowed_models", allowed)
        object.__setattr__(self, "supports_vision", default_route.supports_vision)
        object.__setattr__(self, "routes", routes[1:])
        object.__setattr__(self, "_all_routes", routes)

    def descriptor(self) -> DeploymentInferenceDescriptor:
        return DeploymentInferenceDescriptor(
            provider=self.provider,
            model=self.model,
            api_mode=self.api_mode,
            policy_id=self.policy_id,
            allowed_models=self.allowed_models,
            supports_vision=self.supports_vision,
            routes=tuple(route.descriptor() for route in self._all_routes),
        )

    def route_for(self, model: str) -> DeploymentInferenceRoute | None:
        selected_model = str(model or "").strip()
        return next(
            (route for route in self._all_routes if route.model == selected_model),
            None,
        )

    def resolve_route_runtime(self, route: DeploymentInferenceRoute) -> dict[str, Any]:
        if route not in self._all_routes:
            raise DeploymentInferenceSelectionRejected("deployment inference route is not allowed")
        try:
            runtime = dict(route.runtime_resolver())
        except Exception as exc:  # pragma: no cover - operator callback details are private
            raise DeploymentInferencePolicyInvalid("deployment inference runtime is unavailable") from exc
        provider = str(runtime.get("provider") or "").strip()
        requested_provider = str(runtime.get("requested_provider") or "").strip().lower()
        api_key = runtime.get("api_key")
        base_url = str(runtime.get("base_url") or "").strip().rstrip("/")
        api_mode = str(runtime.get("api_mode") or "").strip()
        parsed = urlparse(base_url)
        matches_provider = provider == route.provider or requested_provider == route.provider
        if not matches_provider or api_mode != route.api_mode or not base_url:
            raise DeploymentInferencePolicyInvalid("deployment inference runtime does not match policy")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise DeploymentInferencePolicyInvalid("deployment inference endpoint is invalid")
        if not api_key:
            raise DeploymentInferencePolicyInvalid("deployment inference credentials are unavailable")
        return runtime

    def resolve_runtime(self, *, model: str | None = None) -> dict[str, Any]:
        selected_model = str(model or self.model).strip()
        route = self.route_for(selected_model)
        if route is None:
            raise DeploymentInferenceSelectionRejected("deployment inference model is not allowed")
        return self.resolve_route_runtime(route)


def _routes_from_json(raw: str) -> tuple[DeploymentInferenceRouteDescriptor, ...]:
    try:
        values = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DeploymentInferencePolicyInvalid("deployment inference descriptor routes are invalid") from exc
    if not isinstance(values, list):
        raise DeploymentInferencePolicyInvalid("deployment inference descriptor routes are invalid")
    routes: list[DeploymentInferenceRouteDescriptor] = []
    for value in values:
        if not isinstance(value, dict):
            raise DeploymentInferencePolicyInvalid("deployment inference descriptor routes are invalid")
        unexpected = set(value) - {"provider", "model", "api_mode", "name", "supports_vision"}
        if unexpected:
            raise DeploymentInferencePolicyInvalid("deployment inference descriptor routes are invalid")
        routes.append(DeploymentInferenceRouteDescriptor(**value))
    return tuple(routes)


def deployment_descriptor_from_environment(
    source: Mapping[str, str] | None = None,
) -> DeploymentInferenceDescriptor | None:
    """Decode the supervisor-owned, non-secret policy descriptor."""
    env = source if source is not None else os.environ
    provider = str(env.get("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", "")).strip().lower()
    model = str(env.get("HERMES_DEPLOYMENT_INFERENCE_MODEL", "")).strip()
    api_mode = str(env.get("HERMES_DEPLOYMENT_INFERENCE_API_MODE", "")).strip()
    policy_id = str(env.get("HERMES_DEPLOYMENT_INFERENCE_POLICY_ID", "")).strip()
    raw_allowed = str(env.get("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", ""))
    raw_supports_vision = env.get(_SUPPORTS_VISION_ENV)
    raw_routes = str(env.get(_ROUTES_ENV, "")).strip()
    if not any((provider, model, api_mode, policy_id, raw_allowed.strip(), raw_supports_vision, raw_routes)):
        return None
    if not all((provider, model, api_mode, policy_id)):
        raise DeploymentInferencePolicyInvalid("deployment inference descriptor is incomplete")
    allowed = tuple(dict.fromkeys(item.strip() for item in raw_allowed.split(",") if item.strip()))
    if not allowed or model not in allowed:
        raise DeploymentInferencePolicyInvalid("deployment inference descriptor models are invalid")
    return DeploymentInferenceDescriptor(
        provider=provider,
        model=model,
        api_mode=api_mode,
        policy_id=policy_id,
        allowed_models=allowed,
        supports_vision=_parse_optional_bool(
            raw_supports_vision,
            field=_SUPPORTS_VISION_ENV,
        ),
        routes=_routes_from_json(raw_routes) if raw_routes else (),
    )


def _declared_models(entry: Mapping[str, Any]) -> tuple[str, ...]:
    values = [str(entry.get("model") or "").strip()]
    models = entry.get("models")
    if isinstance(models, dict):
        values.extend(str(candidate or "").strip() for candidate in models)
    return tuple(dict.fromkeys(value for value in values if value))


def _configured_route_index() -> dict[str, list[tuple[str, str, str, bool | None]]]:
    from hermes_cli.config import get_compatible_custom_providers, load_config_readonly
    from hermes_cli.providers import custom_provider_slug, determine_api_mode

    index: dict[str, list[tuple[str, str, str, bool | None]]] = {}
    for entry in get_compatible_custom_providers(load_config_readonly()):
        provider_key = str(entry.get("provider_key") or "").strip()
        provider = custom_provider_slug(provider_key or str(entry.get("name") or ""))
        name = str(entry.get("name") or provider).strip()
        base_url = str(entry.get("base_url") or "").strip()
        api_mode = str(entry.get("api_mode") or "").strip() or determine_api_mode(provider, base_url)
        models = entry.get("models")
        for model in _declared_models(entry):
            metadata = models.get(model) if isinstance(models, dict) else None
            supports_vision = (
                _parse_optional_bool(metadata.get("supports_vision"), field="supports_vision")
                if isinstance(metadata, dict) and "supports_vision" in metadata
                else None
            )
            index.setdefault(model, []).append((provider, name, api_mode, supports_vision))
    return index


def policy_from_control_plane_environment() -> DeploymentInferencePolicy:
    """Build deployment routes from operator settings and global provider config."""
    provider = os.environ.get("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", "").strip().lower()
    model = os.environ.get("HERMES_DEPLOYMENT_INFERENCE_MODEL", "").strip()
    api_mode = os.environ.get("HERMES_DEPLOYMENT_INFERENCE_API_MODE", "").strip().lower()
    policy_id = os.environ.get("HERMES_DEPLOYMENT_INFERENCE_POLICY_ID", "deployment-default-v1").strip()
    allowed_models = tuple(dict.fromkeys(
        item.strip()
        for item in os.environ.get("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", "").split(",")
        if item.strip()
    ))
    if not provider or not model or not api_mode:
        raise DeploymentInferencePolicyInvalid("deployment inference environment is incomplete")
    if not allowed_models:
        allowed_models = (model,)
    if model not in allowed_models:
        allowed_models = (model, *allowed_models)

    raw_supports_vision = os.environ.get(_SUPPORTS_VISION_ENV)
    if raw_supports_vision is not None:
        supports_vision = _parse_optional_bool(
            raw_supports_vision,
            field=_SUPPORTS_VISION_ENV,
        )
    else:
        supports_vision = None
        try:
            from agent.image_routing import _supports_vision_override
            from hermes_cli.config import load_config_readonly

            supports_vision = _supports_vision_override(
                load_config_readonly(),
                provider,
                model,
            )
        except Exception:
            pass

    def _runtime_resolver(route_provider: str, route_model: str) -> Callable[[], Mapping[str, Any]]:
        def resolve() -> Mapping[str, Any]:
            from hermes_cli.runtime_provider import resolve_runtime_provider

            return resolve_runtime_provider(requested=route_provider, target_model=route_model)

        return resolve

    route_index = _configured_route_index()
    extra_routes: list[DeploymentInferenceRoute] = []
    for allowed_model in allowed_models:
        if allowed_model == model:
            continue
        matches = route_index.get(allowed_model, [])
        if len(matches) > 1:
            raise DeploymentInferencePolicyInvalid(
                f"deployment inference model {allowed_model!r} has multiple configured routes"
            )
        if not matches:
            raise DeploymentInferencePolicyInvalid(
                f"deployment inference model {allowed_model!r} has no configured route"
            )
        route_provider, name, route_api_mode, route_supports_vision = matches[0]
        extra_routes.append(DeploymentInferenceRoute(
            provider=route_provider,
            model=allowed_model,
            api_mode=route_api_mode,
            runtime_resolver=_runtime_resolver(route_provider, allowed_model),
            name=name,
            supports_vision=route_supports_vision,
        ))

    return DeploymentInferencePolicy(
        provider=provider,
        model=model,
        api_mode=api_mode,
        runtime_resolver=_runtime_resolver(provider, model),
        policy_id=policy_id,
        allowed_models=allowed_models,
        supports_vision=supports_vision,
        routes=tuple(extra_routes),
    )


def load_deployment_inference_policy(spec: str) -> DeploymentInferencePolicy | None:
    """Load an explicit operator factory, or return ``None`` when disabled."""
    value = str(spec or "").strip()
    if not value:
        return None
    module_name, separator, attribute = value.partition(":")
    if not separator or not module_name or not attribute or "." in attribute:
        raise DeploymentInferencePolicyInvalid("deployment inference policy factory is invalid")
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as exc:
        raise DeploymentInferencePolicyInvalid("deployment inference policy factory is unavailable") from exc
    if not callable(factory):
        raise DeploymentInferencePolicyInvalid("deployment inference policy factory is invalid")
    try:
        policy = factory()
    except Exception as exc:
        raise DeploymentInferencePolicyInvalid("deployment inference policy factory failed") from exc
    if not isinstance(policy, DeploymentInferencePolicy):
        raise DeploymentInferencePolicyInvalid("deployment inference policy factory returned invalid policy")
    return policy
