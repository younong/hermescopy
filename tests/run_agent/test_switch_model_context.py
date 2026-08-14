"""Tests that switch_model does not inherit stale context_length overrides."""

from unittest.mock import MagicMock, patch

from run_agent import AIAgent
from agent.context_compressor import ContextCompressor


def _make_agent_with_compressor(config_context_length=None) -> AIAgent:
    """Build a minimal AIAgent with a context_compressor, skipping __init__."""
    agent = AIAgent.__new__(AIAgent)

    # Primary model settings
    agent.model = "primary-model"
    agent.provider = "openrouter"
    agent.base_url = "https://openrouter.ai/api/v1"
    agent.api_key = "sk-primary"
    agent.api_mode = "chat_completions"
    agent.client = MagicMock()
    agent.quiet_mode = True

    # Store the initial config_context_length override used at agent construction.
    agent._config_context_length = config_context_length

    # Context compressor with primary model values
    compressor = ContextCompressor(
        model="primary-model",
        threshold_percent=0.50,
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-primary",
        provider="openrouter",
        quiet_mode=True,
        config_context_length=config_context_length,
    )
    agent.context_compressor = compressor

    # For switch_model
    agent._primary_runtime = {}

    return agent


@patch("agent.model_metadata.get_model_context_length", return_value=131_072)
def test_switch_model_clears_previous_config_context_length(mock_ctx_len):
    """Switching models must not reuse the previous model.context_length override."""
    agent = _make_agent_with_compressor(config_context_length=32_768)

    assert agent.context_compressor.model == "primary-model"
    assert agent.context_compressor.context_length == 32_768  # From config override

    agent._compression_feasibility_checked = True

    agent.switch_model(
        "new-model",
        "openrouter",
        api_key="sk-new",
        base_url="https://openrouter.ai/api/v1",
    )
    # Verify the old config override is not passed to the new model.
    mock_ctx_len.assert_called_once()
    call_kwargs = mock_ctx_len.call_args.kwargs
    assert call_kwargs.get("config_context_length") is None

    # Verify compressor was updated from the newly resolved model metadata.
    assert agent.context_compressor.model == "new-model"
    assert agent.context_compressor.context_length == 131_072
    assert agent._compression_feasibility_checked is False


def test_switch_model_without_config_context_length():
    """When switching models without config override, config_context_length should be None."""
    agent = _make_agent_with_compressor(config_context_length=None)

    with patch("agent.model_metadata.get_model_context_length", return_value=128_000) as mock_ctx_len:
        # Switch model
        agent.switch_model("new-model", "openrouter", api_key="sk-new", base_url="https://openrouter.ai/api/v1")

        # Verify get_model_context_length was called with None
        mock_ctx_len.assert_called_once()
        call_kwargs = mock_ctx_len.call_args.kwargs
        assert call_kwargs.get("config_context_length") is None


def test_switch_model_can_preserve_context_and_system_prompt():
    agent = _make_agent_with_compressor(config_context_length=32_768)
    agent._cached_system_prompt = "existing prompt"
    agent._credential_pool = MagicMock(name="pool")
    agent._ensure_lmstudio_runtime_loaded = MagicMock()
    agent._close_openai_client = MagicMock()
    original_client = agent.client
    original_pool = agent._credential_pool

    with (
        patch("agent.model_metadata.get_model_context_length") as mock_ctx_len,
        patch("agent.credential_pool.load_pool") as load_pool,
    ):
        agent.switch_model(
            "new-model",
            "openrouter",
            api_key="sk-primary",
            base_url="https://openrouter.ai/api/v1",
            route_only=True,
        )

    mock_ctx_len.assert_not_called()
    load_pool.assert_not_called()
    agent._ensure_lmstudio_runtime_loaded.assert_not_called()
    assert agent.client is original_client
    assert agent._credential_pool is original_pool
    assert agent.context_compressor.model == "new-model"
    assert agent.context_compressor.context_length == 32_768
    assert agent._cached_system_prompt == "existing prompt"


def test_route_only_switch_defers_new_client_and_reuses_cached_route():
    agent = _make_agent_with_compressor(config_context_length=32_768)
    agent._credential_pool = MagicMock(name="openrouter-pool")
    agent._ensure_lmstudio_runtime_loaded = MagicMock()
    agent._close_openai_client = MagicMock()
    original_client = agent.client
    original_pool = agent._credential_pool

    agent.switch_model(
        "other-model",
        "custom",
        api_key="custom-key",
        base_url="https://custom.example/v1",
        route_only=True,
    )

    assert agent.client is None
    assert agent._credential_pool is None
    assert agent._route_activation_pending is True

    agent.switch_model(
        "primary-model-2",
        "openrouter",
        api_key="sk-primary",
        base_url="https://openrouter.ai/api/v1",
        route_only=True,
    )

    assert agent.client is original_client
    assert agent._credential_pool is original_pool
    assert agent._route_activation_pending is False
