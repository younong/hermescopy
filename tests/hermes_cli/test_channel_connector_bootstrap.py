"""Tests for authenticated startup of retained messaging connectors."""

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_cli.channel_connectors import bootstrap as connector_bootstrap
from hermes_cli.channel_connectors.supervisor import ConnectorSupervisor


class _Supervisor:
    def __init__(self, tmp_path):
        self.global_home = tmp_path
        self.control_home = tmp_path / "control-plane"
        self.deployment_inference_policy = object()
        self.deployment_media_policy = object()
        self.resource_manager = object()


def _mock_identity_store(monkeypatch, store):
    monkeypatch.setattr(
        connector_bootstrap,
        "ChannelIdentityStore",
        lambda *args, **kwargs: store,
    )
    monkeypatch.setattr(
        connector_bootstrap,
        "reconcile_employee_workspaces",
        lambda candidate: 0 if candidate is store else pytest.fail("unexpected store"),
    )


@pytest.mark.anyio
async def test_empty_configuration_initializes_control_plane_store(
    monkeypatch,
    tmp_path,
):
    crypto = object()
    store = MagicMock()
    monkeypatch.setattr(
        connector_bootstrap.ChannelCrypto,
        "from_env",
        lambda **kwargs: crypto,
    )
    create_store = MagicMock(return_value=store)
    monkeypatch.setattr(connector_bootstrap, "ChannelIdentityStore", create_store)
    reconcile = MagicMock(return_value=0)
    monkeypatch.setattr(
        connector_bootstrap,
        "reconcile_employee_workspaces",
        reconcile,
    )

    runtime = await connector_bootstrap.bootstrap_channel_connectors(
        {},
        auth_required=True,
        supervisor=_Supervisor(tmp_path),
    )

    assert runtime.status.ready is True
    assert runtime.status.states == {}
    assert runtime.store is store
    assert runtime.session is None
    create_store.assert_called_once_with(
        crypto,
        tmp_path / "control-plane",
        global_home=tmp_path,
    )
    reconcile.assert_called_once_with(store)
    await runtime.close()


@pytest.mark.anyio
async def test_disabled_webhook_real_path_creates_provisioning_store(
    monkeypatch,
    tmp_path,
):
    encoded_lookup = base64.b64encode(b"l" * 32).decode("ascii")
    encoded_encryption = base64.b64encode(b"e" * 32).decode("ascii")
    monkeypatch.setenv(
        "HERMES_ILINK_LOOKUP_KEYS_JSON",
        json.dumps({"1": encoded_lookup}),
    )
    monkeypatch.setenv(
        "HERMES_ILINK_ENCRYPTION_KEYS_JSON",
        json.dumps({"1": encoded_encryption}),
    )

    runtime = await connector_bootstrap.bootstrap_channel_connectors(
        {"webhook": {"enabled": False}},
        auth_required=True,
        supervisor=_Supervisor(tmp_path),
    )

    assert runtime.store is not None
    assert runtime.session is None
    assert (tmp_path / "control-plane" / "channel_identities.sqlite3").exists()
    await runtime.close()


@pytest.mark.anyio
async def test_enabled_connector_requires_authenticated_owner_supervisor():
    runtime = await connector_bootstrap.bootstrap_channel_connectors(
        {"feishu": {"enabled": True}},
        auth_required=False,
        supervisor=None,
    )
    assert runtime.status.ready is False
    assert runtime.status.states == {"feishu": "authenticated_dashboard_required"}


@pytest.mark.anyio
async def test_removed_connector_is_rejected_without_loading_keyrings(monkeypatch):
    monkeypatch.setattr(
        connector_bootstrap.ChannelCrypto,
        "from_env",
        lambda **kwargs: pytest.fail("removed connectors must not read keyrings"),
    )
    runtime = await connector_bootstrap.bootstrap_channel_connectors(
        {"telegram": {"enabled": True}},
        auth_required=True,
        supervisor=object(),
    )
    assert runtime.status.ready is False
    assert runtime.status.states == {"telegram": "unsupported"}


@pytest.mark.anyio
async def test_feishu_requires_exactly_one_active_account(monkeypatch, tmp_path):
    monkeypatch.setattr(
        connector_bootstrap.ChannelCrypto,
        "from_env",
        lambda **kwargs: object(),
    )
    store = MagicMock()
    read_context = MagicMock()
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = []
    read_context.__enter__.return_value = connection
    read_context.__exit__.return_value = False
    store.read.return_value = read_context
    _mock_identity_store(monkeypatch, store)

    runtime = await connector_bootstrap.bootstrap_channel_connectors(
        {"feishu": {"enabled": True}},
        auth_required=True,
        supervisor=_Supervisor(tmp_path),
    )

    assert runtime.status.ready is False
    assert runtime.status.states == {"feishu": "account_unavailable"}
    await runtime.close()


@pytest.mark.anyio
async def test_feishu_registers_with_shared_supervisor(monkeypatch, tmp_path):
    service = AsyncMock()
    service.start = AsyncMock()
    service.close = AsyncMock()
    monkeypatch.setattr(
        connector_bootstrap.ChannelCrypto,
        "from_env",
        lambda **kwargs: object(),
    )
    store = MagicMock()
    read_context = MagicMock()
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = [{"account_id": "ca_feishu"}]
    read_context.__enter__.return_value = connection
    read_context.__exit__.return_value = False
    store.read.return_value = read_context
    _mock_identity_store(monkeypatch, store)
    constructor = MagicMock(return_value=service)
    monkeypatch.setattr(
        connector_bootstrap,
        "FeishuConnector",
        constructor,
    )
    supervisor = _Supervisor(tmp_path)

    runtime = await connector_bootstrap.bootstrap_channel_connectors(
        {"feishu": {"enabled": True}},
        auth_required=True,
        supervisor=supervisor,
    )

    assert isinstance(runtime.connectors, ConnectorSupervisor)
    assert runtime.get("feishu") is service
    assert runtime.status.ready is True
    assert runtime.status.states == {
        "feishu": "ready",
        "feishu:ca_feishu": "ready",
    }
    assert constructor.call_args.args == (store, supervisor)
    assert constructor.call_args.kwargs["account_id"] == "ca_feishu"
    service.start.assert_awaited_once()
    await runtime.close()
    service.close.assert_awaited_once()


@pytest.mark.anyio
async def test_feishu_starts_every_managed_account_with_partial_failure(
    monkeypatch, tmp_path
):
    services = {"ca_one": AsyncMock(), "ca_two": AsyncMock()}
    services["ca_one"].start = AsyncMock()
    services["ca_one"].close = AsyncMock()
    services["ca_two"].start = AsyncMock(side_effect=RuntimeError("unavailable"))
    services["ca_two"].close = AsyncMock()
    monkeypatch.setattr(
        connector_bootstrap.ChannelCrypto,
        "from_env",
        lambda **kwargs: object(),
    )
    store = MagicMock()
    read_context = MagicMock()
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = [
        {"account_id": "ca_one"},
        {"account_id": "ca_two"},
    ]
    read_context.__enter__.return_value = connection
    read_context.__exit__.return_value = False
    store.read.return_value = read_context
    _mock_identity_store(monkeypatch, store)
    monkeypatch.setattr(
        connector_bootstrap,
        "FeishuConnector",
        lambda *args, account_id, **kwargs: services[account_id],
    )

    runtime = await connector_bootstrap.bootstrap_channel_connectors(
        {"feishu": {"enabled": True}},
        auth_required=True,
        supervisor=_Supervisor(tmp_path),
    )

    assert runtime.get("feishu") is services["ca_one"]
    assert runtime.get("feishu", "ca_one") is services["ca_one"]
    assert runtime.get("feishu", "ca_two") is None
    assert runtime.status.states == {
        "feishu": "ready",
        "feishu:ca_one": "ready",
        "feishu:ca_two": "startup_failed",
    }
    await runtime.close()
    services["ca_one"].close.assert_awaited_once()


@pytest.mark.anyio
async def test_webhook_registers_only_with_active_account(monkeypatch, tmp_path):
    service = AsyncMock()
    service.start = AsyncMock()
    service.close = AsyncMock()
    monkeypatch.setattr(
        connector_bootstrap.ChannelCrypto,
        "from_env",
        lambda **kwargs: object(),
    )
    store = MagicMock()
    read_context = MagicMock()
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = [
        {"account_id": "ca_webhook"}
    ]
    read_context.__enter__.return_value = connection
    read_context.__exit__.return_value = False
    store.read.return_value = read_context
    _mock_identity_store(monkeypatch, store)
    monkeypatch.setattr(
        connector_bootstrap,
        "WebhookService",
        lambda *args, **kwargs: service,
    )

    runtime = await connector_bootstrap.bootstrap_channel_connectors(
        {"webhook": {"enabled": True}},
        auth_required=True,
        supervisor=_Supervisor(tmp_path),
    )

    assert runtime.get("webhook") is service
    assert runtime.status.ready is True
    assert runtime.status.states == {"webhook": "ready"}
    service.start.assert_awaited_once()
    await runtime.close()
    service.close.assert_awaited_once()


def test_connector_supervisor_starts_one_provider_without_rollback():
    supervisor = ConnectorSupervisor()
    first = object()
    second = object()

    async def start_first():
        return first

    async def start_second():
        return second

    supervisor.register("first", start_first)
    supervisor.register("second", start_second)

    async def run():
        assert await supervisor.start_provider("first") is first
        assert supervisor.get("second") is None
        await supervisor.close()

    import asyncio

    asyncio.run(run())
