"""Regression tests for #45006: typed `/model <name>` resolution must route a
model declared in user/custom provider config to that provider instead of
leaving it on the current provider and soft-accepting it.

Repro: with the current provider set to ``openai-codex``, typing
``/model qwen3.5-4b`` (a model the user declares under ``providers.<slug>`` or
``custom_providers``) showed ``Provider: OpenAI Codex`` — because typed
detection only consulted static catalogs / OpenRouter, never the user's
configured provider model lists, so the name stayed on Codex and was
soft-accepted as an unknown hidden Codex model.

The fix adds an exact-match configured-provider detection step in
``switch_model`` that runs before ``detect_provider_for_model`` and before
common-path validation.  These tests pin its precedence rules and prove the
deliberately-supported Codex hidden-model soft-accept (#16172 / #19729) is left
intact when nothing in config matches.

Hermetic: the model-resolution chain is fully mocked (no network), mirroring
``tests/hermes_cli/test_user_providers_model_switch.py``.
"""

from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli.model_switch import switch_model

_ACCEPTED = {"accepted": True, "persist": True, "recognized": True, "message": None}
_REJECTED = {"accepted": False, "persist": False, "recognized": False, "message": "not found"}
# What validate_requested_model returns for an unknown id on openai-codex: it
# soft-accepts with a "may be a hidden model" note (#16172 / #19729).
_CODEX_SOFT_ACCEPT = {
    "accepted": True,
    "persist": True,
    "recognized": False,
    "message": (
        "Note: `gpt-5.9-codex-hidden` was not found in the OpenAI Codex model "
        "listing. It may still work if your account has access to a newer or "
        "hidden model ID."
    ),
}


def _run_switch(
    *,
    raw_input,
    current_provider,
    user_providers=None,
    custom_providers=None,
    validation=_ACCEPTED,
    current_model="old-model",
    current_base_url="",
):
    """Drive ``switch_model`` with the resolution chain mocked out.

    Every external lookup that would otherwise hit catalogs/network is patched:
    alias resolution, aggregator catalog, ``detect_provider_for_model`` (so step
    e is a no-op and cannot accidentally reroute), validation, credential
    resolution, normalization, and model metadata.  This isolates the new
    configured-provider detection step.
    """
    with patch("hermes_cli.model_switch.resolve_alias", return_value=None), \
         patch("hermes_cli.model_switch.list_provider_models", return_value=[]), \
         patch("hermes_cli.model_switch.normalize_model_for_provider", side_effect=lambda model, provider: model), \
         patch("hermes_cli.models.validate_requested_model", return_value=validation), \
         patch("hermes_cli.models.detect_provider_for_model", return_value=None), \
         patch("hermes_cli.model_switch.get_model_info", return_value=None), \
         patch("hermes_cli.model_switch.get_model_capabilities", return_value=None), \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             return_value={
                 "api_key": "***",
                 "base_url": current_base_url or "http://resolved/v1",
                 "api_mode": "",
             },
         ):
        return switch_model(
            raw_input=raw_input,
            current_provider=current_provider,
            current_model=current_model,
            current_base_url=current_base_url,
            user_providers=user_providers or {},
            custom_providers=custom_providers or [],
        )


def test_trusted_selection_skips_remote_validation_and_metadata_refresh():
    with patch("hermes_cli.models.validate_requested_model") as validate, \
         patch("hermes_cli.model_switch.get_model_info", return_value=None) as get_info, \
         patch("hermes_cli.model_switch.get_model_capabilities", return_value=None) as get_caps, \
         patch("agent.models_dev.fetch_models_dev", return_value={}), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value={
             "api_key": "new-key",
             "base_url": "https://api.example/v1",
             "api_mode": "chat_completions",
         }):
        result = switch_model(
            raw_input="registered-model",
            current_provider="old-provider",
            current_model="old-model",
            explicit_provider="openrouter",
            trusted_selection=True,
        )

    assert result.success is True
    validate.assert_not_called()
    get_info.assert_called_once_with(
        result.target_provider,
        result.new_model,
        allow_network=False,
    )
    get_caps.assert_called_once_with(
        result.target_provider,
        result.new_model,
        allow_network=False,
    )


def test_trusted_deployment_route_never_consults_catalog_or_provider_chain():
    """A trusted (registered) selection backed by a control-plane deployment
    route resolves entirely from the route descriptor: neither the models.dev
    catalog nor the generic provider-resolution chain is consulted, so a
    blocked catalog endpoint cannot stall send-time preflight.  Regression: on
    networks where models.dev is unreachable, the provider-resolution fallback
    hung ~75s on a TCP connect and killed the chat WebSocket before the
    submitted route was ever applied."""
    route = SimpleNamespace(
        provider="custom:volcengine-ark",
        model="kimi-k3",
        name="Kimi K3",
        api_mode="chat_completions",
    )
    with patch(
         "hermes_cli.deployment_inference.route_descriptors_from_control_plane",
         return_value=[route],
         ), \
         patch("hermes_cli.model_switch.resolve_provider_full") as resolve_full, \
         patch("agent.models_dev.fetch_models_dev") as fetch_registry, \
         patch("hermes_cli.model_switch.get_model_info", return_value=None), \
         patch("hermes_cli.model_switch.get_model_capabilities", return_value=None), \
         patch(
             "hermes_cli.model_switch.normalize_model_for_provider",
             side_effect=lambda model, provider: model,
         ), \
         patch(
             "hermes_cli.runtime_provider.resolve_deployment_inference_runtime",
             return_value={
                 "api_key": "relay-key",
                 "base_url": "https://relay.internal/v1",
                 "api_mode": "chat_completions",
                 "relay_provider": "custom:volcengine-ark",
                 "model": "kimi-k3",
             },
         ):
        result = switch_model(
            raw_input="kimi-k3",
            current_provider="custom:codex",
            current_model="gpt-5.6-sol",
            explicit_provider="custom:volcengine-ark",
            trusted_selection=True,
        )

    assert result.success is True, result.error_message
    assert result.target_provider == "custom:volcengine-ark"
    assert result.new_model == "kimi-k3"
    # Label comes from the control-plane descriptor, not from a catalog lookup.
    assert result.provider_label == "Kimi K3"
    fetch_registry.assert_not_called()
    resolve_full.assert_not_called()


def test_typed_configured_model_routes_away_from_openai_codex():
    """The core repro: a model declared under ``providers.<slug>`` typed while
    on ``openai-codex`` routes to the configured provider, not Codex."""
    user_providers = {
        "local-ollama": {
            "name": "Local Ollama",
            "base_url": "http://localhost:11434/v1",
            "models": ["qwen3.5-4b", "kimi-k2.5"],
        }
    }
    result = _run_switch(
        raw_input="qwen3.5-4b",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        user_providers=user_providers,
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "local-ollama"
    assert result.new_model == "qwen3.5-4b"


def test_typed_configured_model_routes_to_custom_provider():
    """``custom_providers`` entries route to their ``custom:<name>`` slug."""
    custom_providers = [
        {
            "name": "mylocal",
            "base_url": "http://localhost:1234/v1",
            "model": "qwen3.5-4b",
            "models": {"qwen3.5-4b": {}},
        }
    ]
    result = _run_switch(
        raw_input="qwen3.5-4b",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        custom_providers=custom_providers,
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "custom:mylocal"
    assert result.new_model == "qwen3.5-4b"


def test_current_provider_declaring_model_is_not_rerouted():
    """Precedence rule 4: if the current provider declares the model, keep it —
    even when another configured provider also declares the same id (so this
    must NOT trip the ambiguity guard)."""
    user_providers = {
        "local-ollama": {
            "name": "Local Ollama",
            "base_url": "http://localhost:11434/v1",
            "models": ["qwen3.5-4b"],
        },
        "other-relay": {
            "name": "Other Relay",
            "base_url": "http://other/v1",
            "models": ["qwen3.5-4b"],
        },
    }
    result = _run_switch(
        raw_input="qwen3.5-4b",
        current_provider="local-ollama",
        current_model="kimi-k2.5",
        current_base_url="http://localhost:11434/v1",
        user_providers=user_providers,
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "local-ollama"


def test_ambiguous_configured_model_fails_with_provider_hint():
    """Precedence rule 6: when two non-current providers declare the same id and
    neither is current, fail clearly and point at ``--provider`` — never
    silently pick the first match."""
    user_providers = {
        "relay-a": {
            "name": "Relay A",
            "base_url": "http://a/v1",
            "models": ["qwen3.5-4b"],
        },
        "relay-b": {
            "name": "Relay B",
            "base_url": "http://b/v1",
            "models": ["qwen3.5-4b"],
        },
    }
    result = _run_switch(
        raw_input="qwen3.5-4b",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        user_providers=user_providers,
    )
    assert result.success is False
    assert "--provider" in result.error_message
    assert "relay-a" in result.error_message
    assert "relay-b" in result.error_message


def test_configured_model_absent_from_live_models_accepted_after_reroute():
    """End-to-end synergy: after rerouting to the configured provider, a live
    ``/v1/models`` probe that does NOT list the model is still accepted via the
    existing user-config override — proving the reroute lands on the right
    provider for that override to match."""
    user_providers = {
        "local-ollama": {
            "name": "Local Ollama",
            "base_url": "http://localhost:11434/v1",
            "models": {"qwen3.5-4b": {"context_length": 32768}},
        }
    }
    result = _run_switch(
        raw_input="qwen3.5-4b",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        user_providers=user_providers,
        validation=_REJECTED,
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "local-ollama"
    assert result.new_model == "qwen3.5-4b"


def test_no_configured_match_leaves_current_provider_for_soft_accept():
    """The Codex hidden-model soft-accept (#16172 / #19729) is untouched: an
    unknown id with no config match stays on the current provider and is
    soft-accepted exactly as before."""
    result = _run_switch(
        raw_input="gpt-5.9-codex-hidden",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        # Config is present but declares an unrelated model — detection is a no-op.
        user_providers={
            "local-ollama": {
                "base_url": "http://localhost:11434/v1",
                "models": ["qwen3.5-4b"],
            }
        },
        validation=_CODEX_SOFT_ACCEPT,
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "openai-codex"
    assert result.new_model == "gpt-5.9-codex-hidden"


def test_configured_match_is_case_insensitive_and_returns_canonical_spelling():
    """Matching is case-insensitive but the configured spelling wins, so the
    downstream validation/override path sees the canonical id."""
    user_providers = {
        "local-ollama": {
            "base_url": "http://localhost:11434/v1",
            "models": ["Qwen3.5-4B"],
        }
    }
    result = _run_switch(
        raw_input="qwen3.5-4b",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        user_providers=user_providers,
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "local-ollama"
    assert result.new_model == "Qwen3.5-4B"


def test_default_model_only_declaration_routes():
    """A model declared ONLY via `default_model` (not in `models`) still routes
    to that configured provider (#45006 — default_model is a declaring field)."""
    user_providers = {
        "local-ollama": {
            "name": "Local Ollama",
            "base_url": "http://localhost:11434/v1",
            "default_model": "qwen3.5-4b",
        }
    }
    result = _run_switch(
        raw_input="qwen3.5-4b",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        user_providers=user_providers,
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "local-ollama"
    assert result.new_model == "qwen3.5-4b"


def test_malformed_provider_config_does_not_raise():
    """Garbage shapes in provider config must not crash detection — they're
    skipped and the typed name falls through to the soft-accept no-op."""
    user_providers = {
        "bad1": "not-a-dict",            # non-dict cfg
        "bad2": {"models": 12345},        # models as int
        "bad3": {"models": [None, 7, {"noname": "x"}]},  # junk list items
        "bad4": {"model": {"k": object()}},  # dict with non-target keys
    }
    custom_providers = [
        "not-a-dict",                     # non-dict entry
        {"name": ""},                     # empty name
        {"models": ["unrelated-model"]},  # no name key
    ]
    result = _run_switch(
        raw_input="gpt-5.9-codex-hidden",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        user_providers=user_providers,
        custom_providers=custom_providers,
        validation=_CODEX_SOFT_ACCEPT,
    )
    # No match anywhere -> stays on codex, soft-accepted, no exception.
    assert result.success is True, result.error_message
    assert result.target_provider == "openai-codex"


def test_xai_oauth_soft_accept_preserved_when_no_match():
    """The xai-oauth hidden-model soft-accept (sibling of openai-codex) is also
    a no-op when config declares no matching model."""
    user_providers = {
        "local-ollama": {"base_url": "http://x/v1", "models": ["some-other-model"]},
    }
    result = _run_switch(
        raw_input="grok-hidden-preview",
        current_provider="xai-oauth",
        current_model="grok-4",
        user_providers=user_providers,
        validation=_CODEX_SOFT_ACCEPT,
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "xai-oauth"
