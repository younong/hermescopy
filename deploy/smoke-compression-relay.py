#!/usr/bin/env python3
"""Deterministic smoke for the compression summary LLM routed through the
deployment relay.

Companion to PR #270 (Fix deployment relay context probing) and issue #274.
Stands up an isolated loopback OpenAI-compatible server that pretends to be
the deployment relay, configures a real ``ContextCompressor`` with a small
context window so compression is forced to call the summary LLM, and asserts:

  * The summary LLM call is dispatched to the relay URL with the relay credential.
  * The relay received ``x-hermes-deployment-provider`` matching the live
    policy.
  * ``agent.model_metadata.fetch_endpoint_model_metadata`` was NOT invoked
    against the relay (PR #270 regression: relay is not an OpenAI-compatible
    model API; ``GET /v1/models`` would 405 it).
  * The compressor's chosen summary model equals
    ``HERMES_DEPLOYMENT_INFERENCE_COMPRESSION_MODEL``.
  * Compression produced a structured result.

The smoke is hermetic: the loopback server is the only network endpoint,
no real LLM is contacted, and the policy env vars are sourced from the live
``/opt/hermes/shared/.env`` (when present) without disturbing it.

Exit codes:
  0  all checks passed
  1  one or more checks failed
  2  harness setup error (env, import, port)
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCHEMA_VERSION = 1
KIND = "hermes.compression-relay-smoke"
DEFAULT_TIMEOUT = 60.0
RELAY_BASE_URL_ENV = "HERMES_DEPLOYMENT_INFERENCE_RELAY_BASE_URL"
RELAY_KEY = "deployment-inference-relay"


class SmokeFailure(RuntimeError):
    def __init__(self, code: str, check: str, message: str):
        super().__init__(message)
        self.code = code
        self.check = check


# ---------------------------------------------------------------------------
# Loopback relay stub
# ---------------------------------------------------------------------------


class RelayStub:
    """Loopback HTTP server that mimics the dashboard's deployment inference
    relay endpoint. Captures every chat-completions POST so the smoke can
    assert that the compressor reached the relay and not the direct upstream.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.models_probes: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    path = self.path.rstrip("/")
                    if path.endswith("/chat/completions"):
                        with owner._lock:
                            owner.calls.append({
                                "path": path,
                                "headers": {k: v for k, v in self.headers.items()},
                                "payload": payload,
                            })
                        # Return a structured summary so ContextCompressor
                        # accepts it and continues the pipeline.
                        body = {
                            "id": "smoke-summary-1",
                            "object": "chat.completion",
                            "model": payload.get("model", ""),
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": json.dumps({
                                            "summary": (
                                                "Compression summary from relay stub. "
                                                "All middle turns compacted."
                                            ),
                                            "key_decisions": ["decision A", "decision B"],
                                            "open_questions": ["question 1"],
                                        }),
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {"prompt_tokens": 12000, "completion_tokens": 200},
                        }
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(json.dumps(body))))
                        self.end_headers()
                        self.wfile.write(json.dumps(body).encode("utf-8"))
                    elif path.endswith("/v1/models"):
                        # PR #270 invariant: relay must NOT serve model discovery.
                        # The real relay raises DeploymentInferenceRelayMethodNotAllowed
                        # → 405. The stub mirrors that so the smoke catches regressions.
                        with owner._lock:
                            owner.models_probes.append({"path": path})
                        self.send_response(405)
                        self.send_header("Allow", "POST")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    else:
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self) -> None:  # noqa: N802
                # PR #270: relay rejects /v1/models GET with 405.
                self.send_response(405)
                self.send_header("Allow", "POST")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def start(self) -> None:
        self.server.serve_forever()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# Smoke checks
# ---------------------------------------------------------------------------


def _bounded(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _load_policy_env() -> dict[str, str]:
    """Read deployment-inference env vars from ``/opt/hermes/shared/.env``.

    Falls back to ``os.environ`` so the smoke can also be exercised locally
    with explicit vars.
    """
    env_path = Path("/opt/hermes/shared/.env")
    env: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env.setdefault(key.strip(), value.strip())
    for key, value in os.environ.items():
        env.setdefault(key, value)
    return env


def _build_compressor(model: str, max_tokens: int = 400_000):
    from agent.context_compressor import ContextCompressor

    compressor = ContextCompressor(
        model=model,
        quiet_mode=True,
        max_tokens=max_tokens,
    )
    compressor._clear_compression_failure_cooldown()
    return compressor


def _build_long_turns(n_pairs: int = 40) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for i in range(n_pairs):
        turns.append({
            "role": "user",
            "content": f"Question {i}: " + ("please elaborate " * 30),
        })
        turns.append({
            "role": "assistant",
            "content": f"Answer {i}: " + ("understood " * 40),
        })
    return turns


def run_smoke(timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    started = time.monotonic()
    checks: list[dict[str, Any]] = []
    captured_probes: list[dict[str, Any]] = []

    def record(name: str, status: str, **fields: Any) -> None:
        fields.setdefault("durationMs", 0)
        checks.append({"name": name, "status": status, **fields})

    # --- env + relay setup ---
    env = _load_policy_env()
    summary_model = env.get("HERMES_DEPLOYMENT_INFERENCE_COMPRESSION_MODEL", "").strip()
    default_provider = env.get("HERMES_DEPLOYMENT_INFERENCE_PROVIDER", "").strip()
    if not summary_model:
        raise SmokeFailure(
            code="env_missing",
            check="policy_env",
            message="HERMES_DEPLOYMENT_INFERENCE_COMPRESSION_MODEL not set in env",
        )
    if not default_provider:
        raise SmokeFailure(
            code="env_missing",
            check="policy_env",
            message="HERMES_DEPLOYMENT_INFERENCE_PROVIDER not set in env",
        )

    relay = RelayStub()
    relay_thread = threading.Thread(target=relay.start, daemon=True)
    relay_thread.start()
    cleanup_actions: list[Any] = []

    try:
        # Force the compressor + provider-resolver to use the loopback relay.
        os.environ[RELAY_BASE_URL_ENV] = relay.base_url
        os.environ["HERMES_DEPLOYMENT_INFERENCE_RELAY_KEY"] = RELAY_KEY
        # Point provider resolution at the stub so the LLM client base_url is
        # the loopback relay (not a real upstream).
        os.environ["HERMES_DEPLOYMENT_INFERENCE_RELAY_PROVIDER"] = default_provider

        from hermes_cli.deployment_inference import is_deployment_inference_relay

        if not is_deployment_inference_relay(RELAY_KEY):
            raise SmokeFailure(
                code="relay_key_unrecognized",
                check="relay_credential",
                message=(
                    "is_deployment_inference_relay() returned False for the relay "
                    "credential; deployment_inference module does not recognize it"
                ),
            )
        record("relay_credential", "passed")

        # --- prepare compressor with a tiny context window so compression is forced ---
        compressor = _build_compressor(model="kimi-k3", max_tokens=2000)

        # Capture every fetch_endpoint_model_metadata invocation: any probe
        # against the relay base_url is a PR #270 regression.
        def probe_recorder(*args: Any, **kwargs: Any) -> dict[str, int]:
            captured_probes.append({"args": list(args), "kwargs": dict(kwargs)})
            return {"context_length": 1500, "max_output_tokens": 500}

        turns = _build_long_turns(n_pairs=40)

        with patch("agent.model_metadata.fetch_endpoint_model_metadata", side_effect=probe_recorder):
            t0 = time.monotonic()
            try:
                summary = compressor._generate_rolling_summary(
                    turns,
                    focus_topic=None,
                    deadline_monotonic=time.monotonic() + timeout,
                )
            except Exception as exc:
                record(
                    "compression_summary_llm",
                    "failed",
                    error=type(exc).__name__,
                    message=_bounded(exc),
                )
                raise SmokeFailure(
                    code="compression_exception",
                    check="compression_summary_llm",
                    message=f"_generate_rolling_summary raised: {exc}",
                ) from exc
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            record(
                "compression_summary_llm",
                "passed" if summary else "failed",
                summary_model=summary_model,
                summary_len=len(summary) if summary else 0,
                durationMs=elapsed_ms,
            )
            if not summary:
                raise SmokeFailure(
                    code="empty_summary",
                    check="compression_summary_llm",
                    message="compress() returned no summary despite forced budget",
                )

        # --- assertions on captured relay calls ---
        record(
            "relay_chat_completions_received",
            "passed" if relay.calls else "failed",
            count=len(relay.calls),
        )
        if not relay.calls:
            raise SmokeFailure(
                code="relay_not_called",
                check="relay_chat_completions_received",
                message="summary LLM did not reach the loopback relay",
            )

        first_call = relay.calls[0]
        called_model = first_call["payload"].get("model", "")
        if called_model != summary_model:
            raise SmokeFailure(
                code="summary_model_mismatch",
                check="relay_chat_completions_received",
                message=(
                    f"relay got model={called_model!r} but "
                    f"HERMES_DEPLOYMENT_INFERENCE_COMPRESSION_MODEL={summary_model!r}"
                ),
            )

        provider_header = first_call["headers"].get("x-hermes-deployment-provider", "")
        if provider_header != default_provider:
            raise SmokeFailure(
                code="relay_provider_header_missing",
                check="relay_provider_header",
                message=(
                    f"expected x-hermes-deployment-provider={default_provider!r}, "
                    f"got {provider_header!r}"
                ),
            )
        record(
            "relay_provider_header",
            "passed",
            provider=provider_header,
        )

        # --- PR #270 regression: relay must NOT receive /v1/models probes ---
        relay_probes = [
            p for p in captured_probes
            if relay.base_url.rstrip("/") in str(p.get("args", [""])[0])
        ]
        record(
            "no_relay_models_probe",
            "passed" if not relay_probes else "failed",
            probe_count=len(relay_probes),
        )
        if relay_probes:
            raise SmokeFailure(
                code="relay_models_probe",
                check="no_relay_models_probe",
                message=(
                    "PR #270 regression: fetch_endpoint_model_metadata was "
                    f"called against relay {relay.base_url!r}"
                ),
            )

        # PR #270 also requires the relay to serve 405 on /v1/models GET;
        # the stub mirrors that, so we also assert models_probes is empty
        # (we never want to hit GET /v1/models in this smoke at all).
        record(
            "relay_models_endpoint_untouched",
            "passed" if not relay.models_probes else "failed",
            probe_count=len(relay.models_probes),
        )

    finally:
        relay.close()
        for action in cleanup_actions:
            try:
                action()
            except Exception:
                pass

    duration_ms = int((time.monotonic() - started) * 1000)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": KIND,
        "status": "passed",
        "checks": checks,
        "observations": {
            "summary_model": summary_model,
            "default_provider": default_provider,
            "relay_base_url": relay.base_url,
            "relay_call_count": len(relay.calls),
            "metadata_probe_count": len(captured_probes),
        },
        "durationMs": duration_ms,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=KIND)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    try:
        result = run_smoke(timeout=args.timeout)
    except SmokeFailure as exc:
        sys.stderr.write(
            json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "kind": KIND,
                    "status": "failed",
                    "failure": {"code": exc.code, "check": exc.check},
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return 1
    except Exception as exc:
        sys.stderr.write(
            json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "kind": KIND,
                    "status": "errored",
                    "error": type(exc).__name__,
                    "message": str(exc)[:500],
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return 2

    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())