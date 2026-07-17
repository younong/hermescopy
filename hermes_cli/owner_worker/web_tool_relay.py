"""One-shot authenticated relay for isolated web tool executors.

The Tool Executor keeps its private network namespace and receives no owner
credentials. For the two built-in web research operations it receives one end
of a socketpair that is bound to an exact executor invocation. The owner worker
validates that binding and performs the existing provider dispatch in its
owner-scoped runtime.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
import threading
from dataclasses import dataclass
from typing import Any, Callable

from hermes_cli.owner_worker.executor_identity import (
    EgressProfile,
    ExecutorIdentity,
    ExecutorInvocation,
)

logger = logging.getLogger(__name__)

WEB_RELAY_TOOL_NAMES = frozenset({"web_search", "web_extract"})
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class WebToolRelayError(RuntimeError):
    """The private web tool relay rejected or lost an invocation."""


def _send_frame(connection: socket.socket, value: dict[str, Any], *, limit: int) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not encoded or len(encoded) > limit:
        raise WebToolRelayError("web tool relay frame is invalid")
    connection.sendall(struct.pack("!I", len(encoded)) + encoded)


def _recv_exact(connection: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise WebToolRelayError("web tool relay peer closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_frame(connection: socket.socket, *, limit: int) -> dict[str, Any]:
    size = struct.unpack("!I", _recv_exact(connection, 4))[0]
    if not size or size > limit:
        raise WebToolRelayError("web tool relay frame is invalid")
    try:
        value = json.loads(_recv_exact(connection, size))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebToolRelayError("web tool relay frame is malformed") from exc
    if not isinstance(value, dict):
        raise WebToolRelayError("web tool relay frame is malformed")
    return value


def _validated_arguments(tool_name: str, arguments: object) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise WebToolRelayError("web tool relay arguments are invalid")
    if tool_name == "web_search":
        if set(arguments) - {"query", "limit"}:
            raise WebToolRelayError("web tool relay arguments are invalid")
        query = arguments.get("query")
        limit = arguments.get("limit", 5)
        if not isinstance(query, str) or not query.strip() or len(query) > 16_384:
            raise WebToolRelayError("web tool relay arguments are invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise WebToolRelayError("web tool relay arguments are invalid")
        return {"query": query, "limit": limit}
    if tool_name == "web_extract":
        if set(arguments) - {"urls", "char_limit"}:
            raise WebToolRelayError("web tool relay arguments are invalid")
        urls = arguments.get("urls")
        char_limit = arguments.get("char_limit")
        if (
            not isinstance(urls, list)
            or not 1 <= len(urls) <= 5
            or any(not isinstance(url, str) or not url.strip() or len(url) > 16_384 for url in urls)
        ):
            raise WebToolRelayError("web tool relay arguments are invalid")
        if char_limit is not None and (
            not isinstance(char_limit, int)
            or isinstance(char_limit, bool)
            or char_limit < 2_000
            or char_limit > 500_000
        ):
            raise WebToolRelayError("web tool relay arguments are invalid")
        result: dict[str, Any] = {"urls": list(urls)}
        if char_limit is not None:
            result["char_limit"] = char_limit
        return result
    raise WebToolRelayError("web tool relay operation is not allowed")


def _dispatch_web_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    from hermes_cli.config import reload_env
    from tools.web_tools import web_extract_tool, web_search_tool

    # Provider implementations read their API keys from os.environ. Refresh
    # only from this worker's owner-scoped HERMES_HOME; the executor never sees
    # the resulting credentials.
    reload_env()

    if tool_name == "web_search":
        return str(web_search_tool(arguments["query"], limit=arguments["limit"]))
    if tool_name == "web_extract":
        return str(asyncio.run(web_extract_tool(
            arguments["urls"],
            "markdown",
            char_limit=arguments.get("char_limit"),
        )))
    raise WebToolRelayError("web tool relay operation is not allowed")


@dataclass
class _RelayEndpoint:
    invocation: ExecutorInvocation
    connection: socket.socket
    thread: threading.Thread


class WebToolRelayBroker:
    """Owner-worker broker for exact one-shot web tool invocations."""

    def __init__(
        self,
        *,
        identity_validator: Callable[[ExecutorIdentity], None],
        dispatcher: Callable[[str, dict[str, Any]], str] = _dispatch_web_tool,
    ) -> None:
        self._identity_validator = identity_validator
        self._dispatcher = dispatcher
        self._endpoints: dict[tuple[tuple[Any, ...], str], _RelayEndpoint] = {}
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _key(invocation: ExecutorInvocation) -> tuple[tuple[Any, ...], str]:
        return invocation.identity.stable_key, invocation.invocation_id

    def register(self, invocation: ExecutorInvocation) -> int:
        """Return a child descriptor bound to this exact web invocation."""
        if invocation.tool_name not in WEB_RELAY_TOOL_NAMES:
            raise WebToolRelayError("web tool relay operation is not allowed")
        if invocation.egress_profile is not EgressProfile.TOOL_NONE:
            raise WebToolRelayError("web tool relay requires isolated network egress")
        self._identity_validator(invocation.identity)
        _validated_arguments(invocation.tool_name, invocation.arguments)

        parent, child = socket.socketpair()
        parent.set_inheritable(False)
        child.set_inheritable(False)
        key = self._key(invocation)
        thread = threading.Thread(
            target=self._serve,
            args=(key,),
            daemon=True,
            name=f"web-tool-relay-{invocation.invocation_id[:12]}",
        )
        endpoint = _RelayEndpoint(invocation, parent, thread)
        with self._lock:
            if self._closed or key in self._endpoints:
                parent.close()
                child.close()
                raise WebToolRelayError("web tool relay is unavailable")
            self._endpoints[key] = endpoint
        thread.start()
        return child.detach()

    def _serve(self, key: tuple[tuple[Any, ...], str]) -> None:
        with self._lock:
            endpoint = self._endpoints.get(key)
        if endpoint is None:
            return
        try:
            request = _recv_frame(endpoint.connection, limit=_MAX_REQUEST_BYTES)
            result = self._handle_request(endpoint.invocation, request)
            _send_frame(
                endpoint.connection,
                {"ok": True, "result": result},
                limit=_MAX_RESPONSE_BYTES,
            )
        except (OSError, WebToolRelayError):
            try:
                _send_frame(
                    endpoint.connection,
                    {"ok": False, "error": "authenticated web tool relay rejected the request"},
                    limit=_MAX_RESPONSE_BYTES,
                )
            except (OSError, WebToolRelayError):
                pass
        except Exception:
            try:
                _send_frame(
                    endpoint.connection,
                    {"ok": False, "error": "authenticated web tool execution failed"},
                    limit=_MAX_RESPONSE_BYTES,
                )
            except (OSError, WebToolRelayError):
                pass
        finally:
            with self._lock:
                if self._endpoints.get(key) is endpoint:
                    self._endpoints.pop(key, None)
            endpoint.connection.close()

    def _handle_request(self, expected: ExecutorInvocation, request: dict[str, Any]) -> str:
        if set(request) != {"identity", "invocation_id", "tool_name", "arguments"}:
            raise WebToolRelayError("web tool relay request is invalid")
        try:
            identity = ExecutorIdentity.from_payload(request["identity"])
        except Exception as exc:
            raise WebToolRelayError("web tool relay identity is invalid") from exc
        if identity != expected.identity:
            raise WebToolRelayError("web tool relay identity does not match invocation")
        self._identity_validator(identity)
        if request["invocation_id"] != expected.invocation_id:
            raise WebToolRelayError("web tool relay invocation does not match")
        if request["tool_name"] != expected.tool_name:
            raise WebToolRelayError("web tool relay operation does not match")
        arguments = _validated_arguments(expected.tool_name, request["arguments"])
        if arguments != _validated_arguments(expected.tool_name, expected.arguments):
            raise WebToolRelayError("web tool relay arguments do not match")
        correlation = {
            "tool_name": expected.tool_name,
            "invocation_id": expected.invocation_id,
            "tool_call_id": expected.tool_call_id,
            "api_request_id": expected.api_request_id,
        }
        logger.info("Authenticated web relay dispatch started", extra=correlation)
        try:
            result = self._dispatcher(expected.tool_name, arguments)
            if not isinstance(result, str):
                raise WebToolRelayError("web tool relay result is invalid")
        except Exception:
            logger.warning("Authenticated web relay dispatch failed", extra=correlation)
            raise
        logger.info("Authenticated web relay dispatch completed", extra=correlation)
        return result

    def revoke_invocation(self, invocation: ExecutorInvocation) -> int:
        return self._revoke(lambda endpoint: self._key(endpoint.invocation) == self._key(invocation))

    def revoke_executor(self, identity: ExecutorIdentity) -> int:
        return self._revoke(lambda endpoint: endpoint.invocation.identity == identity)

    def revoke_worker_generation(self, *, owner_key: str, worker_id: str, worker_generation: int) -> int:
        return self._revoke(
            lambda endpoint: (
                endpoint.invocation.identity.owner_key == owner_key
                and endpoint.invocation.identity.worker_id == worker_id
                and endpoint.invocation.identity.worker_generation == worker_generation
            )
        )

    def close(self) -> int:
        with self._lock:
            self._closed = True
        return self._revoke(lambda _endpoint: True)

    def _revoke(self, predicate: Callable[[_RelayEndpoint], bool]) -> int:
        with self._lock:
            selected = [
                (key, endpoint)
                for key, endpoint in self._endpoints.items()
                if predicate(endpoint)
            ]
            for key, _endpoint in selected:
                self._endpoints.pop(key, None)
        for _key, endpoint in selected:
            try:
                endpoint.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            endpoint.connection.close()
        return len(selected)


def dispatch_web_tool_over_relay(inherited_fd: int, invocation: ExecutorInvocation) -> str:
    """Execute one admitted web invocation through its inherited relay FD."""
    if invocation.tool_name not in WEB_RELAY_TOOL_NAMES:
        raise WebToolRelayError("web tool relay operation is not allowed")
    if inherited_fd < 0:
        raise WebToolRelayError("web tool relay descriptor is invalid")
    connection = socket.socket(fileno=inherited_fd)
    connection.set_inheritable(False)
    try:
        _send_frame(
            connection,
            {
                "identity": invocation.identity.to_payload(),
                "invocation_id": invocation.invocation_id,
                "tool_name": invocation.tool_name,
                "arguments": dict(invocation.arguments),
            },
            limit=_MAX_REQUEST_BYTES,
        )
        response = _recv_frame(connection, limit=_MAX_RESPONSE_BYTES)
        if set(response) == {"ok", "result"} and response["ok"] is True and isinstance(response["result"], str):
            return response["result"]
        raise WebToolRelayError("authenticated web tool relay rejected the request")
    except OSError as exc:
        raise WebToolRelayError("authenticated web tool relay is unavailable") from exc
    finally:
        connection.close()
