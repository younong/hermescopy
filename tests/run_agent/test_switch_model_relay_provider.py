from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _agent(*, api_mode: str = "chat_completions") -> AIAgent:
    agent = AIAgent.__new__(AIAgent)
    agent.provider = "custom:deployment"
    agent.model = "gpt-safe"
    agent.base_url = "http://127.0.0.1:39123/v1"
    agent.api_key = "deployment-inference-relay"
    agent.api_mode = api_mode
    agent.relay_provider = "custom:deployment"
    agent.client = MagicMock()
    agent._client_kwargs = {}
    agent.context_compressor = None
    agent._anthropic_api_key = (
        "deployment-inference-relay" if api_mode == "anthropic_messages" else ""
    )
    agent._anthropic_base_url = (
        agent.base_url if api_mode == "anthropic_messages" else None
    )
    agent._anthropic_client = (
        MagicMock() if api_mode == "anthropic_messages" else None
    )
    agent._is_anthropic_oauth = False
    agent._cached_system_prompt = "cached"
    agent._primary_runtime = {}
    agent._fallback_activated = False
    agent._fallback_index = 0
    agent._fallback_chain = []
    agent._fallback_model = None
    agent._config_context_length = None
    agent._credential_pool = None
    agent._create_openai_client = MagicMock(return_value=MagicMock())
    agent._anthropic_prompt_cache_policy = MagicMock(return_value=(False, False))
    agent._ensure_lmstudio_runtime_loaded = MagicMock()
    return agent


def test_switch_model_installs_private_provider_header_on_anthropic_client():
    agent = _agent()

    with (
        patch("agent.anthropic_adapter.build_anthropic_client") as build_client,
        patch("hermes_cli.timeouts.get_provider_request_timeout", return_value=None),
    ):
        build_client.return_value = MagicMock()
        agent.switch_model(
            new_model="k3-256k",
            new_provider="custom:kimi-code",
            api_key="deployment-inference-relay",
            base_url="http://127.0.0.1:39123/v1",
            api_mode="anthropic_messages",
            relay_provider="custom:kimi-code",
        )

    assert agent.relay_provider == "custom:kimi-code"
    assert build_client.call_args.kwargs["default_headers"] == {
        "x-hermes-deployment-provider": "custom:kimi-code",
    }
    assert agent._primary_runtime["relay_provider"] == "custom:kimi-code"


def test_switch_model_installs_private_provider_header_on_openai_client():
    agent = _agent()

    with patch("hermes_cli.timeouts.get_provider_request_timeout", return_value=None):
        agent.switch_model(
            new_model="gpt-safe-next",
            new_provider="custom:deployment-next",
            api_key="deployment-inference-relay",
            base_url="http://127.0.0.1:39123/v1",
            api_mode="chat_completions",
            relay_provider="custom:deployment-next",
        )

    kwargs = agent._create_openai_client.call_args.args[0]
    assert kwargs["default_headers"] == {
        "x-hermes-deployment-provider": "custom:deployment-next",
    }
    assert agent._primary_runtime["relay_provider"] == "custom:deployment-next"


def test_request_local_anthropic_client_keeps_private_provider_header():
    agent = _agent(api_mode="anthropic_messages")

    with (
        patch("agent.anthropic_adapter.build_anthropic_client") as build_client,
        patch("hermes_cli.timeouts.get_provider_request_timeout", return_value=None),
    ):
        request_client = MagicMock()
        build_client.return_value = request_client
        assert agent._create_request_anthropic_client() is request_client

    assert build_client.call_args.kwargs["default_headers"] == {
        "x-hermes-deployment-provider": "custom:deployment",
    }


def test_restore_primary_runtime_keeps_private_provider_header():
    agent = _agent(api_mode="anthropic_messages")
    agent._primary_runtime = {
        "model": agent.model,
        "provider": agent.provider,
        "base_url": agent.base_url,
        "api_mode": agent.api_mode,
        "api_key": agent.api_key,
        "relay_provider": agent.relay_provider,
        "client_kwargs": {},
        "use_prompt_caching": False,
        "use_native_cache_layout": False,
        "anthropic_api_key": agent._anthropic_api_key,
        "anthropic_base_url": agent._anthropic_base_url,
        "is_anthropic_oauth": False,
        "compressor_model": agent.model,
        "compressor_context_length": 262144,
        "compressor_base_url": agent.base_url,
        "compressor_api_key": agent.api_key,
        "compressor_provider": agent.provider,
        "compressor_api_mode": agent.api_mode,
    }
    agent._fallback_activated = True
    agent.context_compressor = MagicMock()

    with (
        patch("agent.anthropic_adapter.build_anthropic_client") as build_client,
        patch("hermes_cli.timeouts.get_provider_request_timeout", return_value=None),
        patch("agent.chat_completion_helpers.rewrite_prompt_model_identity"),
    ):
        build_client.return_value = MagicMock()
        assert agent._restore_primary_runtime() is True

    assert build_client.call_args.kwargs["default_headers"] == {
        "x-hermes-deployment-provider": "custom:deployment",
    }


def test_recover_primary_transport_keeps_private_provider_header():
    agent = _agent(api_mode="anthropic_messages")
    agent._primary_runtime = {
        "model": agent.model,
        "provider": agent.provider,
        "base_url": agent.base_url,
        "api_mode": agent.api_mode,
        "api_key": agent.api_key,
        "relay_provider": agent.relay_provider,
        "client_kwargs": {},
        "anthropic_api_key": agent._anthropic_api_key,
        "anthropic_base_url": agent._anthropic_base_url,
        "is_anthropic_oauth": False,
    }
    agent._is_openrouter_url = MagicMock(return_value=False)
    agent._close_openai_client = MagicMock()
    agent._vprint = MagicMock()
    agent.log_prefix = ""
    timeout = type("ReadTimeout", (Exception,), {})("timeout")

    with (
        patch("agent.anthropic_adapter.build_anthropic_client") as build_client,
        patch("hermes_cli.timeouts.get_provider_request_timeout", return_value=None),
        patch("agent.agent_runtime_helpers.time.sleep"),
    ):
        build_client.return_value = MagicMock()
        assert agent._try_recover_primary_transport(
            timeout,
            retry_count=3,
            max_retries=3,
        ) is True

    assert build_client.call_args.kwargs["default_headers"] == {
        "x-hermes-deployment-provider": "custom:deployment",
    }
