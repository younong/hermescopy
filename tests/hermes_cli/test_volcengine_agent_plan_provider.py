"""Volcengine Ark Agent Plan provider integration contracts."""

from unittest.mock import patch


PROVIDER = "volcengine-agent-plan"
BASE_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"


def test_profile_is_first_class_and_uses_plan_only_key():
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.models import CANONICAL_PROVIDERS
    from hermes_cli.provider_catalog import provider_catalog_by_slug
    from providers import get_provider_profile

    profile = get_provider_profile(PROVIDER)
    assert profile is not None
    assert profile.api_mode == "codex_responses"
    assert profile.base_url == BASE_URL
    assert profile.env_vars == ("VOLCENGINE_AGENT_PLAN_API_KEY",)
    assert profile.supports_model_listing is False
    assert profile.default_aux_model in profile.fallback_models
    assert "ARK_API_KEY" not in profile.env_vars

    assert PROVIDER in PROVIDER_REGISTRY
    assert PROVIDER_REGISTRY[PROVIDER].api_key_env_vars == profile.env_vars
    assert PROVIDER in {entry.slug for entry in CANONICAL_PROVIDERS}
    assert provider_catalog_by_slug()[PROVIDER].tab == "keys"


def test_shared_api_mode_resolver_uses_profile_metadata():
    from hermes_cli.providers import determine_api_mode

    assert determine_api_mode(PROVIDER, BASE_URL) == "codex_responses"
    assert determine_api_mode("ark-agent-plan", BASE_URL) == "codex_responses"


def test_catalog_preserves_plan_model_ids_without_parallel_static_list():
    from hermes_cli.models import _PROVIDER_MODELS, provider_model_ids
    from providers import get_provider_profile

    profile = get_provider_profile(PROVIDER)
    assert PROVIDER not in _PROVIDER_MODELS
    assert provider_model_ids(PROVIDER) == list(profile.fallback_models)
    assert len(profile.fallback_models) == len(set(profile.fallback_models))
    assert "ark-code-latest" in profile.fallback_models
    assert "deepseek-v4-flash" in profile.fallback_models
    assert "kimi-k2.7-code" in profile.fallback_models


def test_catalog_does_not_probe_unsupported_models_endpoint():
    from hermes_cli.models import provider_model_ids
    from providers import get_provider_profile

    profile = get_provider_profile(PROVIDER)
    with (
        patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": "fake", "base_url": BASE_URL},
        ),
        patch.object(profile, "fetch_models") as fetch_models,
    ):
        result = provider_model_ids(PROVIDER)

    assert result == list(profile.fallback_models)
    fetch_models.assert_not_called()
