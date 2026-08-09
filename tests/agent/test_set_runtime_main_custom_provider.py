"""Regression test: set_runtime_main() must pass base_url/api_key/api_mode
so that _resolve_auto() can route custom: providers in Step 1.

Fixes https://github.com/NousResearch/hermes-agent/issues/34777
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch, MagicMock

import pytest

from hermes_cli.deployment_inference import DeploymentInferenceRouteDescriptor


def _get_globals(mod):
    """Read runtime globals without triggering redaction."""
    return {
        "provider": mod._RUNTIME_MAIN_PROVIDER,
        "model": mod._RUNTIME_MAIN_MODEL,
        "base_url": mod._RUNTIME_MAIN_BASE_URL,
        "cred": mod._RUNTIME_MAIN_API_KEY,  # renamed to avoid redaction
        "api_mode": mod._RUNTIME_MAIN_API_MODE,
    }


class TestSetRuntimeMainCustomProvider:
    """set_runtime_main must propagate base_url/api_key/api_mode for custom providers."""

    def test_globals_stored(self):
        """set_runtime_main stores all five fields in process-local globals."""
        import agent.auxiliary_client as mod

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main(
                "custom:my-router",
                "glm-5.1",
                base_url="https://my-server.example.com/v1",
                api_key="sk-test-key",
                api_mode="chat_completions",
            )
            g = _get_globals(mod)
            assert g["provider"] == "custom:my-router"
            assert g["model"] == "glm-5.1"
            assert g["base_url"] == "https://my-server.example.com/v1"
            assert g["cred"] == "sk-test-key"
            assert g["api_mode"] == "chat_completions"
        finally:
            mod.clear_runtime_main()

    def test_clear_resets_all_globals(self):
        """clear_runtime_main resets all five globals to empty."""
        import agent.auxiliary_client as mod

        mod.set_runtime_main(
            "custom:x", "m",
            base_url="https://x.example.com",
            api_key="sk-abc",
            api_mode="chat_completions",
        )
        mod.clear_runtime_main()
        g = _get_globals(mod)
        for v in g.values():
            assert v == "", f"Expected empty, got {v!r}"

    def test_resolve_auto_uses_globals_for_custom_provider(self):
        """_resolve_auto reads base_url/api_key from globals when main_runtime is None."""
        import agent.auxiliary_client as mod

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main(
                "custom:test-router",
                "test-model",
                base_url="https://custom-endpoint.example.com/v1",
                api_key="sk-test-123",
            )

            with patch.object(mod, "resolve_provider_client") as mock_resolve:
                mock_resolve.return_value = (MagicMock(), "test-model")
                client, resolved = mod._resolve_auto(main_runtime=None)

                mock_resolve.assert_called_once()
                call_args = mock_resolve.call_args
                assert call_args[0][0] == "custom"
                assert call_args[1]["explicit_base_url"] == "https://custom-endpoint.example.com/v1"
                assert call_args[1]["explicit_api_key"] == "sk-test-123"
        finally:
            mod.clear_runtime_main()

    def test_explicit_main_runtime_takes_precedence(self):
        """When main_runtime dict has values, globals are NOT used."""
        import agent.auxiliary_client as mod

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main(
                "custom:router-a",
                "model-a",
                base_url="https://from-global.example.com",
                api_key="sk-global",
            )

            with patch.object(mod, "resolve_provider_client") as mock_resolve:
                mock_resolve.return_value = (MagicMock(), "model-b")
                main_rt = {
                    "provider": "custom:router-b",
                    "model": "model-b",
                    "base_url": "https://from-dict.example.com",
                    "api_key": "sk-dict",
                }
                mod._resolve_auto(main_runtime=main_rt)

                call_args = mock_resolve.call_args[1]
                assert call_args["explicit_base_url"] == "https://from-dict.example.com"
                assert call_args["explicit_api_key"] == "sk-dict"
        finally:
            mod.clear_runtime_main()

    def test_backward_compatible_defaults(self):
        """Calling set_runtime_main with only positional args still works."""
        import agent.auxiliary_client as mod

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main("openrouter", "gpt-4o")
            g = _get_globals(mod)
            assert g["provider"] == "openrouter"
            assert g["model"] == "gpt-4o"
            assert g["base_url"] == ""
            assert g["cred"] == ""
            assert g["api_mode"] == ""
        finally:
            mod.clear_runtime_main()


class TestResolveAutoCustomEndToEnd:
    """End-to-end routing assertions — build a *real* client (no mock on
    resolve_provider_client) and verify the auxiliary auto-detect chain lands
    on the user's custom endpoint instead of falling through to the aggregator
    chain.  These guard the actual user-visible symptom in #34777 (aux tasks
    silently routed to a fallback provider) rather than just the wiring.
    """

    @staticmethod
    def _client_base_url(client):
        for chain in (("base_url",), ("_client", "base_url")):
            obj = client
            try:
                for attr in chain:
                    obj = getattr(obj, attr)
                return str(obj)
            except AttributeError:
                continue
        return None

    def test_relay_header_follows_selected_model_route(self, monkeypatch):
        import agent.auxiliary_client as mod
        from hermes_cli.deployment_inference import DEPLOYMENT_INFERENCE_RELAY_MARKER

        monkeypatch.setattr(
            "hermes_cli.deployment_inference.route_descriptors_from_control_plane",
            lambda: (
                DeploymentInferenceRouteDescriptor(
                    provider="custom:volcengine-ark",
                    model="deepseek-v4-pro",
                    api_mode="chat_completions",
                ),
            ),
        )

        headers = mod._deployment_relay_headers(
            DEPLOYMENT_INFERENCE_RELAY_MARKER,
            {"provider": "custom:kimi-code"},
            model="deepseek-v4-pro",
        )

        assert headers == {"x-hermes-deployment-provider": "custom:volcengine-ark"}

    def test_config_less_custom_endpoint_routes_via_global(self, tmp_path, monkeypatch):
        """custom:<name> with NO config entry: the live base_url carried by
        set_runtime_main() must build a real client at that endpoint — not
        fall through to Step 2 (the regression in #34777)."""
        import agent.auxiliary_client as mod

        # Hermetic: no aggregator creds, no stale OPENAI_BASE_URL.
        for var in ("OPENROUTER_API_KEY", "NOUS_API_KEY", "OPENAI_API_KEY",
                    "OPENAI_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: glm-5.1\n"
            "  provider: 'custom:ephemeral'\n"
            "  base_url: ''\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main(
                "custom:ephemeral",
                "glm-5.1",
                base_url="https://ephemeral.live/v1",
                api_key="sk-live",
            )
            client, resolved = mod.resolve_provider_client("auto", None)
            assert client is not None, (
                "config-less custom endpoint fell through to Step 2 — "
                "the #34777 bug is back"
            )
            assert resolved == "glm-5.1"
            base = self._client_base_url(client)
            assert base and base.rstrip("/") == "https://ephemeral.live/v1"
        finally:
            mod.clear_runtime_main()

    def test_compression_call_reaches_relay_chat_endpoint(self, tmp_path, monkeypatch):
        """Real OpenAI transport must send the deployment route selector."""
        import agent.auxiliary_client as mod
        from hermes_cli.deployment_inference import DEPLOYMENT_INFERENCE_RELAY_MARKER

        captured = {}

        class RelayHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured["path"] = self.path
                captured["provider"] = self.headers.get(
                    "x-hermes-deployment-provider"
                )
                length = int(self.headers.get("Content-Length", "0"))
                captured["body"] = json.loads(self.rfile.read(length))
                payload = json.dumps({
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "gpt-5.6-sol",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "summary"},
                        "finish_reason": "stop",
                    }],
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        relay_url = f"http://127.0.0.1:{server.server_port}/v1"

        for var in ("OPENROUTER_API_KEY", "NOUS_API_KEY", "OPENAI_API_KEY",
                    "OPENAI_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: gpt-5.6-sol\n"
            "  provider: 'custom:codex'\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        mod.clear_runtime_main()
        mod.shutdown_cached_clients()
        try:
            mod.set_runtime_main(
                "custom:codex",
                "gpt-5.6-sol",
                base_url=relay_url,
                api_key=DEPLOYMENT_INFERENCE_RELAY_MARKER,
                api_mode="chat_completions",
            )

            response = mod.call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarize"}],
                max_tokens=32,
            )

            assert response.choices[0].message.content == "summary"
            assert captured["path"] == "/v1/chat/completions"
            assert captured["provider"] == "custom:codex"
            assert captured["body"]["model"] == "gpt-5.6-sol"
        finally:
            mod.shutdown_cached_clients()
            mod.clear_runtime_main()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_named_custom_with_config_entry_still_routes(self, tmp_path, monkeypatch):
        """Regression guard: custom:<name> WITH a custom_providers entry must
        still resolve to that entry's endpoint.  An earlier competing fix
        collapsed the provider to bare ``custom`` before resolution, which
        broke the named-custom branch and returned None here."""
        import agent.auxiliary_client as mod

        for var in ("OPENROUTER_API_KEY", "NOUS_API_KEY", "OPENAI_API_KEY",
                    "OPENAI_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: glm-5.1\n"
            "  provider: 'custom:openclaw'\n"
            "  base_url: ''\n"
            "custom_providers:\n"
            "  - name: openclaw\n"
            "    base_url: 'https://withcfg.example/v1'\n"
            "    model: glm-5.1\n"
            "    api_key: cfg-key\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        # No live base_url carried — resolution must come from config alone,
        # via the named-custom branch in resolve_provider_client.
        mod.clear_runtime_main()
        try:
            mod.set_runtime_main("custom:openclaw", "glm-5.1")
            client, resolved = mod.resolve_provider_client("auto", None)
            assert client is not None
            base = self._client_base_url(client)
            assert base and base.rstrip("/") == "https://withcfg.example/v1"
        finally:
            mod.clear_runtime_main()
