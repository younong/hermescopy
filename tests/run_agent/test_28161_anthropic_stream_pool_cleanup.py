"""Anthropic request cleanup preserves client ownership across retries.

Native Anthropic requests use request-local SDK clients. Polling threads abort
in-flight sockets without closing descriptors, while the request-owning worker
closes each attempt exactly once. The long-lived shared client is never closed
by stale/interrupt handling.
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest


def _make_anthropic_agent(**kwargs):
    from run_agent import AIAgent

    defaults = dict(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="claude-opus-4-7",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    defaults.update(kwargs)
    agent = AIAgent(**defaults)
    agent.api_mode = "anthropic_messages"
    agent._anthropic_client = MagicMock()
    agent._anthropic_api_key = "test-anthropic-key"
    return agent


def _good_stream_cm():
    cm = MagicMock()
    stream = MagicMock()
    stream.__iter__ = MagicMock(return_value=iter([]))
    msg = MagicMock()
    msg.content = []
    msg.stop_reason = "end_turn"
    msg.usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    stream.get_final_message = MagicMock(return_value=msg)
    cm.__enter__ = MagicMock(return_value=stream)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _request_client(stream_cm):
    client = MagicMock()
    client.messages.stream.return_value = stream_cm
    return client


class TestAnthropicRequestClientCleanup:
    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_stream_retry_closes_each_request_client_on_owner_thread(self, monkeypatch):
        agent = _make_anthropic_agent()
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")

        failing = MagicMock()
        failing.__enter__.side_effect = httpx.ConnectError("connection reset by peer")
        first = _request_client(failing)
        second = _request_client(_good_stream_cm())
        owner_threads = []

        def _close(client, *, reason):
            owner_threads.append((client, reason, threading.get_ident()))
            client.close()

        with (
            patch.object(agent, "_create_request_anthropic_client", side_effect=[first, second]),
            patch.object(agent, "_close_request_api_client", side_effect=_close),
            patch.object(agent, "_replace_primary_openai_client") as mock_replace,
            patch.object(agent, "_rebuild_anthropic_client") as mock_rebuild,
        ):
            agent._interruptible_streaming_api_call({})

        assert [entry[0] for entry in owner_threads] == [first, second]
        assert len({entry[2] for entry in owner_threads}) == 1
        assert owner_threads[0][2] != threading.get_ident()
        first.close.assert_called_once()
        second.close.assert_called_once()
        agent._anthropic_client.close.assert_not_called()
        mock_replace.assert_not_called()
        mock_rebuild.assert_not_called()

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_stale_stream_aborts_then_owner_closes_request_client(self, monkeypatch):
        monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "0.1")
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")
        agent = _make_anthropic_agent()
        unblock = threading.Event()

        blocking_cm = MagicMock()
        blocking_stream = MagicMock()

        def _blocking_gen():
            unblock.wait(timeout=5.0)
            raise httpx.ConnectError("connection dropped after shutdown")
            yield

        blocking_stream.__iter__ = MagicMock(return_value=_blocking_gen())
        blocking_cm.__enter__ = MagicMock(return_value=blocking_stream)
        blocking_cm.__exit__ = MagicMock(return_value=False)
        first = _request_client(blocking_cm)
        second = _request_client(_good_stream_cm())
        aborts = []
        closes = []

        def _abort(client, *, reason):
            aborts.append((client, reason, threading.get_ident()))
            unblock.set()

        def _close(client, *, reason):
            closes.append((client, reason, threading.get_ident()))
            client.close()

        with (
            patch.object(agent, "_create_request_anthropic_client", side_effect=[first, second]),
            patch.object(agent, "_abort_request_api_client", side_effect=_abort),
            patch.object(agent, "_close_request_api_client", side_effect=_close),
            patch.object(agent, "_replace_primary_openai_client") as mock_replace,
            patch.object(agent, "_rebuild_anthropic_client") as mock_rebuild,
        ):
            agent._interruptible_streaming_api_call({})

        assert aborts == [(first, "stale_stream_kill", threading.get_ident())]
        assert [entry[0] for entry in closes] == [first, second]
        assert all(entry[2] != threading.get_ident() for entry in closes)
        first.close.assert_called_once()
        second.close.assert_called_once()
        agent._anthropic_client.close.assert_not_called()
        mock_replace.assert_not_called()
        mock_rebuild.assert_not_called()
