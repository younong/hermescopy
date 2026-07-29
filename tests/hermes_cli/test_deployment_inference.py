from __future__ import annotations

import base64
import logging
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from hermes_cli.dashboard_auth.authority import AuthorityStore, WorkerGenerationState, WorkerLeaseState
from hermes_cli.deployment_inference import (
    DeploymentInferencePolicy,
    DeploymentInferencePolicyInvalid,
    deployment_descriptor_from_environment,
    policy_from_control_plane_environment,
)
from hermes_cli.owner_worker.inference_relay import (
    DeploymentInferenceBroker,
    DeploymentInferenceRelayError,
    OwnerInferenceRelay,
)


def _policy(
    *,
    api_mode: str = "chat_completions",
    supports_vision: bool | None = None,
) -> DeploymentInferencePolicy:
    return DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode=api_mode,
        policy_id="test-deployment-v1",
        allowed_models=("gpt-safe", "gpt-safe-mini"),
        supports_vision=supports_vision,
        runtime_resolver=lambda: {
            "provider": "custom:deployment",
            "api_mode": api_mode,
            "base_url": "https://provider.example.test/v1",
            "api_key": "control-plane-secret",
        },
    )


def test_policy_descriptor_and_worker_environment_are_secret_free(monkeypatch):
    descriptor = _policy(supports_vision=True).descriptor()
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", descriptor.provider)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_MODEL", descriptor.model)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_API_MODE", descriptor.api_mode)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_POLICY_ID", descriptor.policy_id)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", ",".join(descriptor.allowed_models))
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_SUPPORTS_VISION", "true")

    parsed = deployment_descriptor_from_environment()

    assert parsed == descriptor
    assert "secret" not in repr(parsed).lower()
    assert "base_url" not in parsed.__dict__


def test_descriptor_rejects_incomplete_or_unsupported_worker_environment(monkeypatch):
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", "custom:deployment")
    with pytest.raises(DeploymentInferencePolicyInvalid, match="incomplete"):
        deployment_descriptor_from_environment()

    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_MODEL", "gpt-safe")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_API_MODE", "bedrock_converse")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_POLICY_ID", "test")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", "gpt-safe")
    with pytest.raises(DeploymentInferencePolicyInvalid, match="unsupported"):
        deployment_descriptor_from_environment()


def test_control_plane_environment_factory_does_not_resolve_until_broker_use(monkeypatch):
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", "custom:deployment")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_MODEL", "gpt-safe")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_API_MODE", "chat_completions")
    monkeypatch.delenv("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", raising=False)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_SUPPORTS_VISION", "false")

    policy = policy_from_control_plane_environment()

    assert policy.descriptor().allowed_models == ("gpt-safe",)
    assert policy.descriptor().supports_vision is False


def test_control_plane_factory_uses_global_model_vision_capability(monkeypatch):
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", "custom:deployment")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_MODEL", "gpt-safe")
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_API_MODE", "chat_completions")
    monkeypatch.delenv("HERMES_DEPLOYMENT_INFERENCE_SUPPORTS_VISION", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"model": {"supports_vision": True}},
    )

    assert policy_from_control_plane_environment().descriptor().supports_vision is True


def test_descriptor_leaves_absent_vision_capability_unknown(monkeypatch):
    descriptor = _policy().descriptor()
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", descriptor.provider)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_MODEL", descriptor.model)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_API_MODE", descriptor.api_mode)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_POLICY_ID", descriptor.policy_id)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", ",".join(descriptor.allowed_models))
    monkeypatch.delenv("HERMES_DEPLOYMENT_INFERENCE_SUPPORTS_VISION", raising=False)

    assert deployment_descriptor_from_environment().supports_vision is None


def test_descriptor_rejects_invalid_vision_capability(monkeypatch):
    descriptor = _policy().descriptor()
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", descriptor.provider)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_MODEL", descriptor.model)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_API_MODE", descriptor.api_mode)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_POLICY_ID", descriptor.policy_id)
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_ALLOWED_MODELS", ",".join(descriptor.allowed_models))
    monkeypatch.setenv("HERMES_DEPLOYMENT_INFERENCE_SUPPORTS_VISION", "maybe")

    with pytest.raises(DeploymentInferencePolicyInvalid, match="true or false"):
        deployment_descriptor_from_environment()


def test_broker_rejects_requests_for_starting_or_revoked_worker(tmp_path):
    store = AuthorityStore(tmp_path / "control")
    claim = store.claim_worker_start("ok1_owner", worker_id="worker-1")
    broker = DeploymentInferenceBroker(policy=_policy(), authority_store=store)
    worker_fd = broker.register(claim.lease)
    worker = socket.socket(fileno=worker_fd)
    key = broker._key(claim.lease)
    peer = broker._peers[key]
    request = _request_for_model()

    with pytest.raises(DeploymentInferenceRelayError, match="not active"):
        broker._handle_request(peer.lease, request)

    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    broker.activate(active)
    broker.revoke(active)

    assert broker._key(active) not in broker._peers
    worker.close()


@contextmanager
def _upstream_server():
    received: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers["Content-Length"])
            received.append({
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": self.rfile.read(length),
            })
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            self.wfile.write(b"data: second\n\n")
            self.wfile.flush()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request_for_model(model: str = "gpt-safe") -> dict[str, object]:
    import base64
    import json

    return {
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": {},
        "body": base64.b64encode(
            json.dumps({"model": model, "messages": []}).encode()
        ).decode("ascii"),
    }


def _activate_relay(store: AuthorityStore, policy: DeploymentInferencePolicy):
    claim = store.claim_worker_start("ok1_owner", worker_id="worker-1")
    broker = DeploymentInferenceBroker(policy=policy, authority_store=store)
    worker_fd = broker.register(claim.lease)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    broker.activate(active)
    relay = OwnerInferenceRelay(worker_fd)
    relay.start()
    return broker, active, relay


def test_broker_logs_safe_complete_diagnostics(tmp_path, caplog):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("cf-ray", "ray-safe")
            self.send_header("x-request-id", "request-safe")
            self.send_header("x-secret-header", "must-not-log")
            self.end_headers()
            self.wfile.write(b"data: safe-output\n\n")

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    policy = DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode="chat_completions",
        runtime_resolver=lambda: {
            "provider": "custom:deployment",
            "api_mode": "chat_completions",
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "api_key": "sk-super-secret-value-123456789",
        },
    )
    store = AuthorityStore(tmp_path / "control")
    claim = store.claim_worker_start("ok1_owner", worker_id="worker-1")
    broker = DeploymentInferenceBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    try:
        with caplog.at_level(logging.INFO, logger="hermes_cli.owner_worker.inference_relay"):
            frames = []
            broker._stream_request(active, _request_for_model(), frames.append)
        message = caplog.messages[-1]
        assert "outcome=complete" in message
        assert "http_status=200" in message
        assert "bytes=19" in message
        assert "chunks=1" in message
        assert "cf-ray=ray-safe" in message
        assert "x-request-id=request-safe" in message
        assert "must-not-log" not in caplog.text
        assert "safe-output" not in caplog.text
        assert "super-secret" not in caplog.text
    finally:
        broker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_broker_logs_upstream_http_error(tmp_path, caplog):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(503)
            self.send_header("x-vercel-id", "vercel-safe")
            self.end_headers()
            self.wfile.write(b"provider body must stay private")

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    policy = DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode="chat_completions",
        runtime_resolver=lambda: {
            "provider": "custom:deployment",
            "api_mode": "chat_completions",
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "api_key": "control-plane-secret",
        },
    )
    store = AuthorityStore(tmp_path / "control")
    claim = store.claim_worker_start("ok1_owner", worker_id="worker-1")
    broker = DeploymentInferenceBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    try:
        with caplog.at_level(
            logging.WARNING,
            logger="hermes_cli.owner_worker.inference_relay",
        ):
            frames = []
            broker._stream_request(active, _request_for_model(), frames.append)
        assert frames[0]["status"] == 503
        assert "outcome=upstream_http_error" in caplog.text
        assert "http_status=503" in caplog.text
        assert "x-vercel-id=vercel-safe" in caplog.text
        assert "provider body" not in caplog.text
    finally:
        broker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_broker_uses_bounded_cloud_timeout(tmp_path, monkeypatch):
    import httpx

    captured = {}

    class FailingClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("silent upstream")

    monkeypatch.setattr(httpx, "Client", FailingClient)
    policy = DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode="chat_completions",
        runtime_resolver=lambda: {
            "provider": "custom:deployment",
            "api_mode": "chat_completions",
            "base_url": "https://provider.example.test/v1",
            "api_key": "control-plane-secret",
        },
    )
    store = AuthorityStore(tmp_path / "control")
    claim = store.claim_worker_start("ok1_owner", worker_id="worker-1")
    broker = DeploymentInferenceBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    try:
        with pytest.raises(DeploymentInferenceRelayError, match="unavailable"):
            broker._stream_request(active, _request_for_model(), lambda _frame: None)
    finally:
        broker.close()

    timeout = captured["timeout"]
    assert timeout.connect == 30.0
    assert timeout.read == 120.0
    assert timeout.write == 120.0
    assert timeout.pool == 30.0


def test_broker_logs_pre_header_failure_without_secret_or_url(
    tmp_path, caplog, monkeypatch
):
    import httpx

    class FailingClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            raise httpx.ConnectError(
                "connect failed token=sk-super-secret-value-123456789"
            )

    monkeypatch.setattr(httpx, "Client", FailingClient)
    policy = DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode="chat_completions",
        runtime_resolver=lambda: {
            "provider": "custom:deployment",
            "api_mode": "chat_completions",
            "base_url": "https://provider.example.test/private?token=hidden",
            "api_key": "sk-super-secret-value-123456789",
        },
    )
    store = AuthorityStore(tmp_path / "control")
    claim = store.claim_worker_start("ok1_owner", worker_id="worker-1")
    broker = DeploymentInferenceBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    try:
        with caplog.at_level(
            logging.WARNING,
            logger="hermes_cli.owner_worker.inference_relay",
        ):
            with pytest.raises(DeploymentInferenceRelayError, match="unavailable"):
                broker._stream_request(
                    active,
                    _request_for_model(),
                    lambda _frame: None,
                )
        assert "outcome=pre_header_transport_failure" in caplog.text
        assert "ConnectError" in caplog.text
        assert "token=hidden" not in caplog.text
        assert "super-secret" not in caplog.text
        assert "sk-super-secret-value-123456789" not in caplog.text
        assert "«redacted:sk-…»" in caplog.text
    finally:
        broker.close()


def test_broker_logs_midstream_failure_with_counts(tmp_path, caplog):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("x-openrouter-id", "openrouter-safe")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"7\r\npartial\r\n")
            self.wfile.flush()
            # Close before the terminating zero-size chunk. httpx must classify
            # this as an incomplete response rather than a normal EOF.
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    policy = DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode="chat_completions",
        runtime_resolver=lambda: {
            "provider": "custom:deployment",
            "api_mode": "chat_completions",
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "api_key": "control-plane-secret",
        },
    )
    store = AuthorityStore(tmp_path / "control")
    claim = store.claim_worker_start("ok1_owner", worker_id="worker-1")
    broker = DeploymentInferenceBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    try:
        with caplog.at_level(logging.WARNING, logger="hermes_cli.owner_worker.inference_relay"):
            with pytest.raises(DeploymentInferenceRelayError, match="unavailable"):
                broker._stream_request(active, _request_for_model(), lambda _frame: None)
        assert "outcome=midstream_failure" in caplog.text
        assert "http_status=200" in caplog.text
        # Native transport chunks are forwarded before httpcore detects that the
        # terminating chunk is missing, so diagnostics include bytes that already
        # reached the worker.
        assert "bytes=7" in caplog.text
        assert "chunks=1" in caplog.text
        assert "x-openrouter-id=openrouter-safe" in caplog.text
        assert "partial" not in caplog.text
    finally:
        broker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_owner_relay_does_not_send_second_error_after_headers(caplog):
    import hermes_cli.owner_worker.inference_relay as relay_module

    broker_side, worker_side = socket.socketpair()
    broker_errors: list[BaseException] = []

    def fail_midstream() -> None:
        try:
            assert relay_module._recv_frame(broker_side) == {
                "type": "hello",
                "version": relay_module._PROTOCOL_VERSION,
            }
            relay_module._send_frame(broker_side, {
                "type": "hello_ack",
                "version": relay_module._PROTOCOL_VERSION,
            })
            request = relay_module._recv_frame(broker_side)
            request_id = request["request_id"]
            relay_module._send_frame(broker_side, {
                "type": "response_start",
                "request_id": request_id,
                "status": 200,
                "headers": {},
                "sequence": 0,
            })
            relay_module._send_frame(broker_side, {
                "type": "error",
                "request_id": request_id,
                "sequence": 1,
                "message": "upstream stream failed",
            })
        except BaseException as exc:
            broker_errors.append(exc)

    broker_thread = threading.Thread(target=fail_midstream, daemon=True)
    broker_thread.start()
    relay = OwnerInferenceRelay(worker_side.detach())

    class Handler:
        path = "/v1/chat/completions"
        command = "POST"
        headers = {"Content-Length": "0"}
        rfile = type("Reader", (), {"read": staticmethod(lambda _length: b"")})()
        wfile = type("Writer", (), {
            "write": staticmethod(lambda _body: None),
            "flush": staticmethod(lambda: None),
        })()
        close_connection = False

        def __init__(self):
            self.sent_status = None
            self.ended = False
            self.error_calls = []

        def send_response(self, status):
            self.sent_status = status

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            self.ended = True

        def send_error(self, status, message):
            self.error_calls.append((status, message))

    handler = Handler()
    try:
        with caplog.at_level(
            logging.WARNING,
            logger="hermes_cli.owner_worker.inference_relay",
        ):
            relay._handle_http(handler)
        assert handler.sent_status == 200
        assert handler.ended is True
        assert handler.error_calls == []
        assert handler.close_connection is True
        assert "phase=midstream" in caplog.text
        assert "upstream stream failed" not in caplog.text
    finally:
        relay.close()
        broker_side.close()
        broker_thread.join(timeout=2)

    assert broker_errors == []


def test_owner_relay_cancels_abandoned_response_without_blocking_next_request(caplog):
    import hermes_cli.owner_worker.inference_relay as relay_module

    broker_side, worker_side = socket.socketpair()
    broker_errors: list[BaseException] = []

    def serve_two_responses() -> None:
        try:
            assert relay_module._recv_frame(broker_side) == {
                "type": "hello",
                "version": relay_module._PROTOCOL_VERSION,
            }
            relay_module._send_frame(broker_side, {
                "type": "hello_ack",
                "version": relay_module._PROTOCOL_VERSION,
            })
            first = relay_module._recv_frame(broker_side)
            first_id = first["request_id"]
            for sequence, frame in enumerate((
                {"type": "response_start", "status": 200, "headers": {}},
                {
                    "type": "response_chunk",
                    "body": base64.b64encode(b"abandoned-first").decode("ascii"),
                },
            )):
                relay_module._send_frame(
                    broker_side,
                    {**frame, "request_id": first_id, "sequence": sequence},
                )

            cancel = relay_module._recv_frame(broker_side)
            assert cancel == {"type": "cancel", "request_id": first_id}
            second = relay_module._recv_frame(broker_side)
            second_id = second["request_id"]
            for sequence, frame in enumerate((
                {"type": "response_start", "status": 200, "headers": {}},
                {
                    "type": "response_chunk",
                    "body": base64.b64encode(b"fresh-response").decode("ascii"),
                },
                {"type": "response_end"},
            )):
                relay_module._send_frame(
                    broker_side,
                    {**frame, "request_id": second_id, "sequence": sequence},
                )
        except BaseException as exc:
            broker_errors.append(exc)

    broker_thread = threading.Thread(target=serve_two_responses, daemon=True)
    broker_thread.start()
    relay = OwnerInferenceRelay(worker_side.detach())

    class Handler:
        path = "/v1/chat/completions"
        command = "POST"
        headers = {"Content-Length": "0"}
        rfile = type("Reader", (), {"read": staticmethod(lambda _length: b"")})()
        close_connection = False

        def __init__(self, writer):
            self.wfile = writer
            self.error_calls = []

        def send_response(self, _status):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

        def send_error(self, status, message):
            self.error_calls.append((status, message))

    class DisconnectedWriter:
        @staticmethod
        def write(_body):
            raise BrokenPipeError("worker SDK disconnected")

        @staticmethod
        def flush():
            pass

    class RecordingWriter:
        def __init__(self):
            self.body = bytearray()

        def write(self, body):
            self.body.extend(body)

        def flush(self):
            pass

    first_handler = Handler(DisconnectedWriter())
    second_writer = RecordingWriter()
    second_handler = Handler(second_writer)
    try:
        with caplog.at_level(
            logging.WARNING,
            logger="hermes_cli.owner_worker.inference_relay",
        ):
            relay._handle_http(first_handler)
        relay._handle_http(second_handler)

        assert first_handler.close_connection is True
        assert first_handler.error_calls == []
        assert bytes(second_writer.body) == b"fresh-response"
        assert second_handler.error_calls == []
        assert "abandoned-first" not in caplog.text
        assert "abandoned-second" not in caplog.text
    finally:
        relay.close()
        broker_side.close()
        broker_thread.join(timeout=2)

    assert broker_errors == []
    assert broker_thread.is_alive() is False


def test_owner_relay_multiplexes_slow_and_fast_requests(tmp_path):
    slow_started = threading.Event()
    release_slow = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if b'"marker":"slow"' in body:
                slow_started.set()
                release_slow.wait(timeout=10)
                response_body = b"slow-response"
            else:
                response_body = b"fast-response"
            self.send_response(200)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    policy = DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode="chat_completions",
        runtime_resolver=lambda: {
            "provider": "custom:deployment",
            "api_mode": "chat_completions",
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "api_key": "control-plane-secret",
        },
    )
    broker, active, relay = _activate_relay(AuthorityStore(tmp_path / "control"), policy)
    slow_result: list[bytes] = []
    slow_errors: list[BaseException] = []

    def request_slow() -> None:
        try:
            response = httpx.post(
                f"{relay.base_url}/chat/completions",
                json={"model": "gpt-safe", "messages": [], "marker": "slow"},
                timeout=5,
            )
            slow_result.append(response.content)
        except BaseException as exc:
            slow_errors.append(exc)

    slow_thread = threading.Thread(target=request_slow, daemon=True)
    slow_thread.start()
    try:
        assert slow_started.wait(timeout=5)
        fast = httpx.post(
            f"{relay.base_url}/chat/completions",
            json={"model": "gpt-safe", "messages": [], "marker": "fast"},
            timeout=2,
        )
        assert fast.status_code == 200
        assert fast.content == b"fast-response"
        assert slow_thread.is_alive()

        release_slow.set()
        slow_thread.join(timeout=5)
        assert slow_errors == []
        assert slow_result == [b"slow-response"]
    finally:
        release_slow.set()
        broker.revoke(active)
        relay.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_broker_checks_exact_lease_before_resolving_credentials(tmp_path):
    runtime_calls = 0

    def resolve_runtime():
        nonlocal runtime_calls
        runtime_calls += 1
        return {
            "provider": "custom:deployment",
            "api_mode": "chat_completions",
            "base_url": "https://provider.example.test/v1",
            "api_key": "control-plane-secret",
        }

    policy = DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode="chat_completions",
        runtime_resolver=resolve_runtime,
    )
    store = AuthorityStore(tmp_path / "control")
    claim = store.claim_worker_start("ok1_owner", worker_id="worker-1")
    broker = DeploymentInferenceBroker(policy=policy, authority_store=store)
    active = store.transition_worker_lease(
        claim.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    revoked = store.transition_worker_lease(
        active,
        state=WorkerLeaseState.REVOKED,
        generation_state=WorkerGenerationState.FAILED,
    )

    with pytest.raises(DeploymentInferenceRelayError, match="not active"):
        broker._request_parts(active, _request_for_model())
    assert revoked.state is WorkerLeaseState.REVOKED
    assert runtime_calls == 0


def test_owner_relay_forwards_small_sse_event_before_upstream_completes(tmp_path):
    first_event = b"data: first\n\n"
    second_event = b"data: second\n\n"
    first_upstream_event_sent = threading.Event()
    release_second_event = threading.Event()
    upstream_completed = threading.Event()
    first_worker_event_received = threading.Event()
    worker_completed = threading.Event()
    received: list[dict[str, object]] = []
    worker_bytes = bytearray()
    worker_errors: list[BaseException] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers["Content-Length"])
            received.append({
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": self.rfile.read(length),
            })
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(first_event)
            self.wfile.flush()
            first_upstream_event_sent.set()
            if not release_second_event.wait(timeout=10):
                return
            self.wfile.write(second_event)
            self.wfile.flush()
            upstream_completed.set()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    policy = DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode="chat_completions",
        runtime_resolver=lambda: {
            "provider": "custom:deployment",
            "api_mode": "chat_completions",
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "api_key": "control-plane-secret",
        },
    )
    broker, active, relay = _activate_relay(AuthorityStore(tmp_path / "control"), policy)

    def consume_worker_stream() -> None:
        try:
            with httpx.Client(timeout=5.0) as client:
                with client.stream(
                    "POST",
                    f"{relay.base_url}/chat/completions",
                    headers={"Authorization": "Bearer worker-marker"},
                    json={"model": "gpt-safe", "stream": True, "messages": []},
                ) as response:
                    assert response.status_code == 200
                    assert response.headers["content-type"] == "text/event-stream"
                    for chunk in response.iter_raw():
                        worker_bytes.extend(chunk)
                        if first_event in worker_bytes:
                            first_worker_event_received.set()
        except BaseException as exc:
            worker_errors.append(exc)
        finally:
            worker_completed.set()

    worker_thread = threading.Thread(target=consume_worker_stream, daemon=True)
    worker_thread.start()
    try:
        assert first_upstream_event_sent.wait(timeout=5)
        assert first_worker_event_received.wait(timeout=5)
        assert upstream_completed.is_set() is False
        assert bytes(worker_bytes) == first_event

        release_second_event.set()
        assert worker_completed.wait(timeout=5)
        assert worker_errors == []
        assert bytes(worker_bytes) == first_event + second_event
        upstream = received[0]
        assert upstream["path"] == "/v1/chat/completions"
        assert upstream["headers"]["Authorization"] == "Bearer control-plane-secret"
        assert "worker-marker" not in str(upstream["headers"])
    finally:
        release_second_event.set()
        worker_thread.join(timeout=2)
        broker.revoke(active)
        relay.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


@pytest.mark.parametrize("base_path", ["", "/v1"])
def test_owner_relay_streams_sse_and_injects_control_plane_credential(tmp_path, base_path):
    with _upstream_server() as (server, received):
        policy = DeploymentInferencePolicy(
            provider="custom:deployment",
            model="gpt-safe",
            api_mode="chat_completions",
            runtime_resolver=lambda: {
                "provider": "custom:deployment",
                "api_mode": "chat_completions",
                "base_url": f"http://127.0.0.1:{server.server_port}{base_path}",
                "api_key": "control-plane-secret",
            },
        )
        broker, active, relay = _activate_relay(AuthorityStore(tmp_path / "control"), policy)
        try:
            with httpx.Client(timeout=5.0) as client:
                with client.stream(
                    "POST",
                    f"{relay.base_url}/chat/completions",
                    headers={"Authorization": "Bearer worker-marker"},
                    json={"model": "gpt-safe", "stream": True, "messages": []},
                ) as response:
                    assert response.status_code == 200
                    assert response.headers["content-type"] == "text/event-stream"
                    assert b"".join(response.iter_raw()) == b"data: first\n\ndata: second\n\n"

            upstream = received[0]
            assert upstream["path"] == "/v1/chat/completions"
            assert upstream["headers"]["Authorization"] == "Bearer control-plane-secret"
            assert "worker-marker" not in str(upstream["headers"])
        finally:
            broker.revoke(active)
            relay.close()


def test_owner_relay_preserves_upstream_prefix_for_anthropic_messages(tmp_path):
    with _upstream_server() as (server, received):
        policy = DeploymentInferencePolicy(
            provider="custom:deployment",
            model="claude-safe",
            api_mode="anthropic_messages",
            runtime_resolver=lambda: {
                "provider": "custom:deployment",
                "api_mode": "anthropic_messages",
                "base_url": f"http://127.0.0.1:{server.server_port}/prefix/v1",
                "api_key": "control-plane-secret",
            },
        )
        broker, active, relay = _activate_relay(AuthorityStore(tmp_path / "control"), policy)
        try:
            with httpx.Client(timeout=5.0) as client:
                with client.stream(
                    "POST",
                    f"{relay.base_url}/messages",
                    headers={
                        "Authorization": "Bearer worker-marker",
                        "x-api-key": "worker-marker",
                    },
                    json={
                        "model": "claude-safe",
                        "stream": True,
                        "messages": [],
                        "max_tokens": 1,
                    },
                ) as response:
                    assert response.status_code == 200
                    assert response.headers["content-type"] == "text/event-stream"
                    assert b"".join(response.iter_raw()) == b"data: first\n\ndata: second\n\n"

            upstream = received[0]
            upstream_headers = {name.lower(): value for name, value in upstream["headers"].items()}
            assert upstream["path"] == "/prefix/v1/messages"
            assert upstream_headers["x-api-key"] == "control-plane-secret"
            assert "worker-marker" not in str(upstream_headers)
        finally:
            broker.revoke(active)
            relay.close()


def test_owner_relay_rejects_revoked_worker_before_upstream_call(tmp_path):
    with _upstream_server() as (server, received):
        policy = DeploymentInferencePolicy(
            provider="custom:deployment",
            model="claude-safe",
            api_mode="anthropic_messages",
            runtime_resolver=lambda: {
                "provider": "custom:deployment",
                "api_mode": "anthropic_messages",
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "api_key": "control-plane-secret",
            },
        )
        store = AuthorityStore(tmp_path / "control")
        broker, active, relay = _activate_relay(store, policy)
        try:
            store.transition_worker_lease(
                active,
                state=WorkerLeaseState.REVOKED,
                generation_state=WorkerGenerationState.FAILED,
            )
            with httpx.Client(timeout=5.0) as client:
                response = client.post(
                    f"{relay.base_url}/messages",
                    json={"model": "claude-safe", "messages": [], "max_tokens": 1},
                )
            assert response.status_code == 502
            assert received == []
        finally:
            broker.revoke(active)
            relay.close()


def test_policy_runtime_accepts_named_custom_provider_resolved_to_custom():
    policy = DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode="chat_completions",
        runtime_resolver=lambda: {
            "provider": "custom",
            "requested_provider": "custom:deployment",
            "api_mode": "chat_completions",
            "base_url": "https://provider.example.test/v1",
            "api_key": "control-plane-secret",
        },
    )

    assert policy.resolve_runtime()["provider"] == "custom"


def test_policy_runtime_refuses_wrong_upstream_identity_or_credential():
    policy = DeploymentInferencePolicy(
        provider="custom:deployment",
        model="gpt-safe",
        api_mode="chat_completions",
        runtime_resolver=lambda: {
            "provider": "other",
            "api_mode": "chat_completions",
            "base_url": "https://provider.example.test/v1",
            "api_key": "secret",
        },
    )
    with pytest.raises(DeploymentInferencePolicyInvalid, match="does not match"):
        policy.resolve_runtime()
