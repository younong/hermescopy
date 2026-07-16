"""Control-plane-owned inference defaults for authenticated owner workers.

An authenticated owner worker must not inherit the Dashboard process environment or
copy its auth store.  This module lets an operator explicitly provide one default
inference policy.  The policy and its credentials live in the Control Plane; a
worker receives only a display-safe descriptor and a private relay connection.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import os
from urllib.parse import urlparse


_SUPPORTED_API_MODES = frozenset({"chat_completions", "anthropic_messages"})


class DeploymentInferencePolicyInvalid(RuntimeError):
    """The deployment supplied an unusable inference default policy."""


class DeploymentInferenceSelectionRejected(RuntimeError):
    """An explicit owner/request selection cannot use the deployment default."""


@dataclass(frozen=True)
class DeploymentInferenceDescriptor:
    """Non-secret policy fields safe to pass to one owner worker."""

    provider: str
    model: str
    api_mode: str
    policy_id: str
    allowed_models: tuple[str, ...]

    def allows_model(self, model: str) -> bool:
        return str(model or "").strip() in self.allowed_models

    def relay_runtime(self, *, model: str | None = None) -> dict[str, str]:
        """Return only inert, owner-safe fields for a local relay client.

        The local worker relay swaps this sentinel for a Control-Plane request;
        neither the worker nor any of its children receives an upstream endpoint
        or credential.
        """
        selected_model = str(model or self.model).strip()
        if not self.allows_model(selected_model):
            raise DeploymentInferenceSelectionRejected("deployment inference model is not allowed")
        return {
            "provider": self.provider,
            "api_mode": self.api_mode,
            "api_key": "deployment-inference-relay",
            "source": "deployment-relay",
            "selection_source": "deployment",
            "policy_id": self.policy_id,
            "model": selected_model,
        }


@dataclass(frozen=True)
class DeploymentInferencePolicy:
    """Operator-owned inference default resolved exclusively by the Control Plane.

    ``runtime_resolver`` must return a normal ``resolve_runtime_provider`` shaped
    mapping.  It is deliberately not serializable and never crosses the worker
    boundary, which keeps provider keys and auth-store access in the Control
    Plane process.
    """

    provider: str
    model: str
    api_mode: str
    runtime_resolver: Callable[[], Mapping[str, Any]]
    policy_id: str = "deployment-default-v1"
    allowed_models: tuple[str, ...] = ()

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
        allowed = tuple(dict.fromkeys(str(value or "").strip() for value in self.allowed_models if str(value or "").strip()))
        if not allowed:
            allowed = (model,)
        if model not in allowed:
            allowed = (model, *allowed)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "allowed_models", allowed)

    def descriptor(self) -> DeploymentInferenceDescriptor:
        return DeploymentInferenceDescriptor(
            provider=self.provider,
            model=self.model,
            api_mode=self.api_mode,
            policy_id=self.policy_id,
            allowed_models=self.allowed_models,
        )

    def resolve_runtime(self) -> dict[str, Any]:
        try:
            runtime = dict(self.runtime_resolver())
        except Exception as exc:  # pragma: no cover - operator callback details are private
            raise DeploymentInferencePolicyInvalid("deployment inference runtime is unavailable") from exc
        provider = str(runtime.get("provider") or "").strip()
        requested_provider = str(runtime.get("requested_provider") or "").strip().lower()
        api_key = runtime.get("api_key")
        base_url = str(runtime.get("base_url") or "").strip().rstrip("/")
        api_mode = str(runtime.get("api_mode") or "").strip()
        parsed = urlparse(base_url)
        # Named custom providers resolve to the shared transport class ``custom``
        # while preserving their routable identity in ``requested_provider``.
        # The descriptor needs that named identity so an owner worker cannot
        # select a different configured custom endpoint through the relay.
        matches_provider = provider == self.provider or requested_provider == self.provider
        if not matches_provider or api_mode != self.api_mode or not base_url:
            raise DeploymentInferencePolicyInvalid("deployment inference runtime does not match policy")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise DeploymentInferencePolicyInvalid("deployment inference endpoint is invalid")
        if not api_key:
            raise DeploymentInferencePolicyInvalid("deployment inference credentials are unavailable")
        return runtime


def deployment_descriptor_from_environment(
    source: Mapping[str, str] | None = None,
) -> DeploymentInferenceDescriptor | None:
    """Decode the supervisor-owned, non-secret policy descriptor.

    A descriptor is not an authorization capability: it identifies the only
    provider/model pair that the worker-side relay may ask its inherited broker
    to invoke.  The Control Plane holds the matching policy and credentials.
    """
    env = source if source is not None else os.environ
    provider = str(env.get("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", "")).strip().lower()
    model = str(env.get("HERMES_DEPLOYMENT_INFERENCE_MODEL", "")).strip()
    api_mode = str(env.get("HERMES_DEPLOYMENT_INFERENCE_API_MODE", "")).strip()
    policy_id = str(env.get("HERMES_DEPLOYMENT_INFERENCE_POLICY_ID", "")).strip()
    raw_allowed = str(env.get("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", ""))
    if not any((provider, model, api_mode, policy_id, raw_allowed.strip())):
        return None
    if not all((provider, model, api_mode, policy_id)):
        raise DeploymentInferencePolicyInvalid("deployment inference descriptor is incomplete")
    if api_mode not in _SUPPORTED_API_MODES:
        raise DeploymentInferencePolicyInvalid("deployment inference descriptor api mode is unsupported")
    allowed = tuple(dict.fromkeys(item.strip() for item in raw_allowed.split(",") if item.strip()))
    if not allowed or model not in allowed:
        raise DeploymentInferencePolicyInvalid("deployment inference descriptor models are invalid")
    return DeploymentInferenceDescriptor(
        provider=provider,
        model=model,
        api_mode=api_mode,
        policy_id=policy_id,
        allowed_models=allowed,
    )


def policy_from_control_plane_environment() -> DeploymentInferencePolicy:
    """Build the standard policy from Control-Plane-only operator settings.

    Operators opt in by setting ``HERMES_DEPLOYMENT_INFERENCE_POLICY`` to
    ``hermes_cli.deployment_inference:policy_from_control_plane_environment``.
    The factory itself executes only in the Dashboard process, where the normal
    provider resolver can read the deployment's credential sources.  Owner
    workers receive only :meth:`DeploymentInferencePolicy.descriptor`.
    """
    provider = os.environ.get("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", "").strip().lower()
    model = os.environ.get("HERMES_DEPLOYMENT_INFERENCE_MODEL", "").strip()
    api_mode = os.environ.get("HERMES_DEPLOYMENT_INFERENCE_API_MODE", "").strip().lower()
    policy_id = os.environ.get("HERMES_DEPLOYMENT_INFERENCE_POLICY_ID", "deployment-default-v1").strip()
    allowed_models = tuple(
        item.strip()
        for item in os.environ.get("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", "").split(",")
        if item.strip()
    )
    if not provider or not model or not api_mode:
        raise DeploymentInferencePolicyInvalid("deployment inference environment is incomplete")

    def _resolve_runtime() -> Mapping[str, Any]:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        return resolve_runtime_provider(requested=provider, target_model=model)

    return DeploymentInferencePolicy(
        provider=provider,
        model=model,
        api_mode=api_mode,
        runtime_resolver=_resolve_runtime,
        policy_id=policy_id,
        allowed_models=allowed_models,
    )


def load_deployment_inference_policy(spec: str) -> DeploymentInferencePolicy | None:
    """Load an explicit operator factory, or return ``None`` when disabled.

    The deployment opt-in is intentionally separate from the owner-worker
    allowlist.  An absent policy preserves existing owner-only behavior.
    """
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

