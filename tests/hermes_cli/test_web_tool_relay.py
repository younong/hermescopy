from __future__ import annotations

import os
import socket
import struct
import sys

import pytest

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.owner_worker.executor_identity import ExecutorIdentity, ExecutorInvocation
from hermes_cli.owner_worker.web_tool_relay import (
    WebToolRelayBroker,
    WebToolRelayError,
    dispatch_web_tool_over_relay,
)


def _identity(owner_key: str = "ok1_owner") -> ExecutorIdentity:
    lease = OwnerWorkerAuthorityLease(owner_key, 3, "worker-3", WorkerLeaseState.ACTIVE, 2, 1)
    return ExecutorIdentity.for_task(
        lease,
        workspace_prefix="default",
        task_id="task-a",
        session_id="session-a",
        executor_id="executor-a",
    )


def _invocation(tool_name="web_search", arguments=None, *, identity=None, invocation_id="invoke-a"):
    return ExecutorInvocation(
        identity or _identity(),
        tool_name,
        arguments or {"query": "Hermes", "limit": 5},
        "call-a",
        "turn-a",
        "request-a",
        invocation_id,
        "tool-none",
    )


def test_web_relay_logs_only_safe_correlation_fields(caplog):
    secret_query = "private-query-sentinel"
    invocation = _invocation(arguments={"query": secret_query, "limit": 5})
    broker = WebToolRelayBroker(
        identity_validator=lambda _identity: None,
        dispatcher=lambda _name, _args: '{"success":true}',
    )
    with caplog.at_level("INFO", logger="hermes_cli.owner_worker.web_tool_relay"):
        relay_fd = broker.register(invocation)
        assert dispatch_web_tool_over_relay(relay_fd, invocation) == '{"success":true}'
    broker.close()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "dispatch started" in messages
    assert "dispatch completed" in messages
    assert secret_query not in messages
    for record in caplog.records:
        assert record.tool_name == "web_search"
        assert record.invocation_id == invocation.invocation_id
        assert record.tool_call_id == invocation.tool_call_id
        assert record.api_request_id == invocation.api_request_id


def test_web_relay_dispatches_exact_search_and_extract_invocations():
    seen = []
    broker = WebToolRelayBroker(
        identity_validator=lambda identity: seen.append(("identity", identity)),
        dispatcher=lambda name, args: seen.append((name, args)) or '{"success":true}',
    )
    search = _invocation()
    search_fd = broker.register(search)
    assert dispatch_web_tool_over_relay(search_fd, search) == '{"success":true}'

    extract = _invocation(
        "web_extract",
        {"urls": ["https://example.com/page"], "char_limit": 4000},
        invocation_id="invoke-b",
    )
    extract_fd = broker.register(extract)
    assert dispatch_web_tool_over_relay(extract_fd, extract) == '{"success":true}'
    assert ("web_search", {"query": "Hermes", "limit": 5}) in seen
    assert ("web_extract", {"urls": ["https://example.com/page"], "char_limit": 4000}) in seen
    broker.close()


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("read_file", {"path": "README.md"}),
        ("web_search", {"query": "", "limit": 5}),
        ("web_search", {"query": "Hermes", "provider": "forged"}),
        ("web_extract", {"urls": ["https://example.com"] * 6}),
        ("web_extract", {"urls": ["https://example.com"], "headers": {"Authorization": "secret"}}),
    ],
)
def test_web_relay_rejects_noncanonical_operations_and_arguments(tool_name, arguments):
    broker = WebToolRelayBroker(identity_validator=lambda _identity: None)
    with pytest.raises(WebToolRelayError):
        broker.register(_invocation(tool_name, arguments))
    broker.close()


def test_web_relay_rejects_forged_identity_and_invocation():
    expected = _invocation()
    broker = WebToolRelayBroker(
        identity_validator=lambda _identity: None,
        dispatcher=lambda _name, _args: "should-not-run",
    )
    child_fd = broker.register(expected)
    connection = socket.socket(fileno=child_fd)
    try:
        from hermes_cli.owner_worker.web_tool_relay import _recv_frame, _send_frame

        _send_frame(
            connection,
            {
                "identity": _identity("ok1_other").to_payload(),
                "invocation_id": "forged",
                "tool_name": "web_search",
                "arguments": {"query": "Hermes", "limit": 5},
            },
            limit=256 * 1024,
        )
        response = _recv_frame(connection, limit=2 * 1024 * 1024)
        assert response["ok"] is False
        assert "owner" not in response["error"]
    finally:
        connection.close()
        broker.close()


def test_web_relay_revocation_closes_only_matching_executor():
    first = _invocation(invocation_id="invoke-a")
    second_identity = ExecutorIdentity.for_task(
        OwnerWorkerAuthorityLease("ok1_owner", 3, "worker-3", WorkerLeaseState.ACTIVE, 2, 1),
        workspace_prefix="default",
        task_id="task-b",
        session_id="session-b",
        executor_id="executor-b",
    )
    second = _invocation(identity=second_identity, invocation_id="invoke-b")
    broker = WebToolRelayBroker(identity_validator=lambda _identity: None)
    first_fd = broker.register(first)
    second_fd = broker.register(second)

    assert broker.revoke_executor(first.identity) == 1
    with pytest.raises(WebToolRelayError):
        dispatch_web_tool_over_relay(first_fd, first)

    broker.revoke_executor(second.identity)
    with pytest.raises(WebToolRelayError):
        dispatch_web_tool_over_relay(second_fd, second)
    broker.close()


def test_web_relay_rejects_oversized_frame_before_dispatch():
    broker = WebToolRelayBroker(identity_validator=lambda _identity: None)
    invocation = _invocation()
    child_fd = broker.register(invocation)
    connection = socket.socket(fileno=child_fd)
    try:
        connection.sendall(struct.pack("!I", 256 * 1024 + 1))
        response_size = struct.unpack("!I", connection.recv(4))[0]
        response = connection.recv(response_size)
        assert b'"ok":false' in response
    finally:
        connection.close()
        broker.close()


def test_web_relay_child_descriptor_is_not_inheritable():
    broker = WebToolRelayBroker(identity_validator=lambda _identity: None)
    fd = broker.register(_invocation())
    try:
        assert os.get_inheritable(fd) is False
    finally:
        os.close(fd)
        broker.close()


def test_executor_runtime_dispatches_web_tool_through_real_socketpair(tmp_path, monkeypatch):
    import json

    from hermes_cli.tool_executor_runtime import entrypoint
    from hermes_cli.tool_executor_runtime.env import build_executor_environment

    invocation = _invocation()
    broker = WebToolRelayBroker(
        identity_validator=lambda identity: identity == invocation.identity or None,
        dispatcher=lambda name, arguments: json.dumps({
            "tool": name,
            "query": arguments["query"],
        }),
    )
    relay_fd = broker.register(invocation)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_fd = os.open(workspace, os.O_RDONLY)
    bootstrap_read, bootstrap_write = os.pipe()
    response_read, response_write = os.pipe()
    gate_read, gate_write = os.pipe()
    environment = build_executor_environment(
        invocation.identity,
        runtime_home=tmp_path,
        workspace_fd=workspace_fd,
        bootstrap_fd=bootstrap_read,
        response_fd=response_write,
        start_gate_fd=gate_read,
        web_relay_fd=relay_fd,
        egress_profile="tool-none",
    )
    os.write(gate_write, b"1")
    os.close(gate_write)
    os.write(bootstrap_write, json.dumps(invocation.to_payload()).encode())
    os.close(bootstrap_write)
    monkeypatch.setattr(entrypoint, "_workspace_mount_status", workspace.stat)
    monkeypatch.setattr(entrypoint.os, "chdir", lambda _path: None)

    try:
        assert entrypoint.run_once(environment) == 0
        response = json.loads(os.read(response_read, 1 << 20))
        assert json.loads(response["result"]) == {"tool": "web_search", "query": "Hermes"}
    finally:
        os.close(response_read)
        broker.close()


def test_owner_side_dispatch_uses_current_owner_home_credentials(tmp_path, monkeypatch):
    from hermes_cli import config
    from tools import web_tools

    homes = [tmp_path / "owner-a", tmp_path / "owner-b"]
    sentinels = ["owner-a-secret", "owner-b-secret"]
    for home, sentinel in zip(homes, sentinels, strict=True):
        home.mkdir()
        (home / ".env").write_text(f"TAVILY_API_KEY={sentinel}\n")

    monkeypatch.setattr(
        web_tools,
        "web_search_tool",
        lambda query, *, limit: f"{query}:{limit}:{os.environ['TAVILY_API_KEY']}",
    )

    results = []
    for index, (home, sentinel) in enumerate(zip(homes, sentinels, strict=True)):
        monkeypatch.setenv("HERMES_HOME", str(home))
        config.invalidate_env_cache()
        invocation = _invocation(
            identity=_identity(f"ok1_owner_{index}"),
            invocation_id=f"invoke-{index}",
        )
        broker = WebToolRelayBroker(identity_validator=lambda _identity: None)
        try:
            relay_fd = broker.register(invocation)
            results.append(dispatch_web_tool_over_relay(relay_fd, invocation))
            assert sentinel not in str(invocation.to_payload())
        finally:
            broker.close()

    assert results == [
        "Hermes:5:owner-a-secret",
        "Hermes:5:owner-b-secret",
    ]


def test_owner_side_dispatch_uses_owner_scoped_ddgs_backend(tmp_path, monkeypatch):
    import json

    from hermes_cli import config
    from plugins.web.ddgs import provider as ddgs_provider
    from tools import web_tools

    homes = [tmp_path / "owner-a", tmp_path / "owner-b"]
    backends = ["yandex", "bing"]
    for home, backend in zip(homes, backends, strict=True):
        home.mkdir()
        (home / "config.yaml").write_text(
            f"web:\n  backend: ddgs\n  ddgs_backend: {backend}\n",
            encoding="utf-8",
        )

    seen_backends = []

    def _search(query, safe_limit, backend):
        seen_backends.append(backend)
        return []

    monkeypatch.setattr(ddgs_provider, "_run_ddgs_search", _search)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_search_backend", lambda: "ddgs")
    monkeypatch.setattr(
        "agent.web_search_registry.get_provider",
        lambda _name: ddgs_provider.DDGSWebSearchProvider(),
    )

    import types

    fake_ddgs = types.ModuleType("ddgs")
    monkeypatch.setitem(sys.modules, "ddgs", fake_ddgs)

    results = []
    for index, home in enumerate(homes):
        monkeypatch.setenv("HERMES_HOME", str(home))
        config._LOAD_CONFIG_CACHE.clear()
        invocation = _invocation(
            identity=_identity(f"ok1_owner_{index}"),
            invocation_id=f"invoke-{index}",
        )
        assert backends[index] not in str(invocation.to_payload())
        broker = WebToolRelayBroker(identity_validator=lambda _identity: None)
        try:
            relay_fd = broker.register(invocation)
            results.append(json.loads(dispatch_web_tool_over_relay(relay_fd, invocation)))
        finally:
            broker.close()

    assert seen_backends == backends
    assert all(result["success"] is True for result in results)


def test_owner_side_web_extract_keeps_private_url_guard(monkeypatch):
    import json

    from hermes_cli.owner_worker import web_tool_relay
    from tools import web_tools

    provider_calls = []

    class Provider:
        display_name = "test"

        def supports_extract(self):
            return True

        def extract(self, urls, **_kwargs):
            provider_calls.append(urls)
            return {"success": True, "results": []}

    monkeypatch.setattr(web_tool_relay, "_dispatch_web_tool", web_tool_relay._dispatch_web_tool)
    monkeypatch.setattr("hermes_cli.config.reload_env", lambda: 0)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "test")
    monkeypatch.setattr("agent.web_search_registry.get_provider", lambda _name: Provider())

    result = json.loads(web_tool_relay._dispatch_web_tool(
        "web_extract",
        {"urls": ["http://127.0.0.1/private"], "char_limit": 4000},
    ))

    assert provider_calls == []
    assert "private or internal network" in result["results"][0]["error"]
