"""Shared model-info response builder for dashboard and owner workers."""
from __future__ import annotations

from typing import Any, Mapping

EMPTY_MODEL_INFO: dict[str, Any] = {
    "model": "",
    "provider": "",
    "auto_context_length": 0,
    "config_context_length": 0,
    "effective_context_length": 0,
    "capabilities": {},
}


def model_info_payload_from_config(
    cfg: Mapping[str, Any],
    *,
    deployment_descriptor: Any | None = None,
) -> dict[str, Any]:
    """Return the /api/model/info response shape for a loaded config mapping."""
    model_cfg = cfg.get("model", "")

    if isinstance(model_cfg, dict):
        model_name = model_cfg.get("default", model_cfg.get("name", ""))
        provider = model_cfg.get("provider", "")
        base_url = model_cfg.get("base_url", "")
        config_ctx = model_cfg.get("context_length")
    else:
        model_name = str(model_cfg) if model_cfg else ""
        provider = ""
        base_url = ""
        config_ctx = None

    selection_source = ""
    if not model_name and deployment_descriptor is not None:
        model_name = str(getattr(deployment_descriptor, "model", "") or "")
        provider = str(getattr(deployment_descriptor, "provider", "") or "")
        selection_source = "deployment"
    if not model_name:
        return dict(EMPTY_MODEL_INFO, provider=provider)

    try:
        from agent.model_metadata import get_model_context_length

        auto_ctx = get_model_context_length(
            model=model_name,
            base_url=base_url,
            provider=provider,
            config_context_length=None,
        )
    except Exception:
        auto_ctx = 0

    config_ctx_int = 0
    if isinstance(config_ctx, int) and config_ctx > 0:
        config_ctx_int = config_ctx

    effective_ctx = config_ctx_int if config_ctx_int > 0 else auto_ctx

    caps: dict[str, Any] = {}
    try:
        from agent.models_dev import get_model_capabilities

        mc = get_model_capabilities(provider=provider, model=model_name)
        if mc is not None:
            caps = {
                "supports_tools": mc.supports_tools,
                "supports_vision": mc.supports_vision,
                "supports_reasoning": mc.supports_reasoning,
                "context_window": mc.context_window,
                "max_output_tokens": mc.max_output_tokens,
                "model_family": mc.model_family,
            }
    except Exception:
        pass

    payload = {
        "model": model_name,
        "provider": provider,
        "auto_context_length": auto_ctx,
        "config_context_length": config_ctx_int,
        "effective_context_length": effective_ctx,
        "capabilities": caps,
    }
    if selection_source:
        payload["selection_source"] = selection_source
    return payload
