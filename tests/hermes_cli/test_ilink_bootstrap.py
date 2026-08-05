from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hermes_cli.channel_connectors.weixin_ilink import bootstrap


def _supervisor(*, control_home=None, **overrides):
    values = {
        "deployment_inference_policy": object(),
        "deployment_image_policy": object(),
        "resource_manager": object(),
        "control_home": control_home or Path("/tmp/hermes-test") / "control-plane",
        "global_home": (control_home.parent if control_home else Path("/tmp/hermes-test")),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _keys(seed: int) -> str:
    return json.dumps({"1": base64.b64encode(bytes([seed]) * 32).decode()})


@pytest.mark.asyncio
async def test_disabled_connector_does_not_read_keyrings(monkeypatch):
    crypto = AsyncMock()
    monkeypatch.setattr(bootstrap.ChannelCrypto, "from_env", crypto)

    runtime = await bootstrap.bootstrap_weixin_ilink(
        {"enabled": False}, auth_required=True, supervisor=_supervisor()
    )

    assert runtime.status.state == "disabled"
    assert runtime.status.enabled is False
    crypto.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_enablement_fails_closed(monkeypatch):
    crypto = AsyncMock()
    monkeypatch.setattr(bootstrap.ChannelCrypto, "from_env", crypto)

    runtime = await bootstrap.bootstrap_weixin_ilink(
        {"enabled": "false"}, auth_required=True, supervisor=_supervisor()
    )

    assert runtime.status.state == "configuration_invalid"
    assert runtime.status.enabled is True
    crypto.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auth_required", "supervisor", "state"),
    [
        (False, _supervisor(), "authenticated_dashboard_required"),
        (True, None, "authenticated_dashboard_required"),
        (
            True,
            _supervisor(deployment_inference_policy=None),
            "deployment_policy_unavailable",
        ),
        (
            True,
            _supervisor(deployment_image_policy=None),
            "deployment_policy_unavailable",
        ),
        (True, _supervisor(resource_manager=None), "resource_governance_unavailable"),
    ],
)
async def test_prerequisites_fail_closed_without_reading_keys(
    monkeypatch, auth_required, supervisor, state
):
    crypto = AsyncMock()
    monkeypatch.setattr(bootstrap.ChannelCrypto, "from_env", crypto)

    runtime = await bootstrap.bootstrap_weixin_ilink(
        {"enabled": True}, auth_required=auth_required, supervisor=supervisor
    )

    assert runtime.status.state == state
    assert runtime.status.enabled is True
    assert runtime.service is None
    crypto.assert_not_called()


@pytest.mark.asyncio
async def test_incompatible_supervisor_homes_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ILINK_LOOKUP_KEYS_JSON", _keys(1))
    monkeypatch.setenv("HERMES_ILINK_ENCRYPTION_KEYS_JSON", _keys(2))
    supervisor = _supervisor(control_home=tmp_path / "other-control")
    supervisor.global_home = tmp_path

    runtime = await bootstrap.bootstrap_weixin_ilink(
        {"enabled": True}, auth_required=True, supervisor=supervisor
    )

    assert runtime.status.state == "startup_failed"
    assert runtime.service is None


@pytest.mark.asyncio
async def test_missing_keyrings_leave_connector_unavailable(monkeypatch):
    monkeypatch.delenv("HERMES_ILINK_LOOKUP_KEYS_JSON", raising=False)
    monkeypatch.delenv("HERMES_ILINK_ENCRYPTION_KEYS_JSON", raising=False)

    runtime = await bootstrap.bootstrap_weixin_ilink(
        {"enabled": True}, auth_required=True, supervisor=_supervisor()
    )

    assert runtime.status.state == "keyrings_unavailable"
    assert runtime.service is None
    assert runtime.session is None


@pytest.mark.asyncio
async def test_startup_failure_closes_partial_resources(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ILINK_LOOKUP_KEYS_JSON", _keys(1))
    monkeypatch.setenv("HERMES_ILINK_ENCRYPTION_KEYS_JSON", _keys(2))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    service = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("secret detail")), stop=AsyncMock())
    monkeypatch.setattr(bootstrap, "WeixinILinkService", lambda *args, **kwargs: service)

    runtime = await bootstrap.bootstrap_weixin_ilink(
        {"enabled": True}, auth_required=True, supervisor=_supervisor(control_home=tmp_path / "control-plane")
    )

    assert runtime.status.state == "startup_failed"
    assert runtime.service is None
    assert runtime.session is None
    service.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_connector_uses_certifi_tls_context(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ILINK_LOOKUP_KEYS_JSON", _keys(1))
    monkeypatch.setenv("HERMES_ILINK_ENCRYPTION_KEYS_JSON", _keys(2))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ssl_context = object()
    connector = object()
    resolver = object()
    monkeypatch.setattr(bootstrap.certifi, "where", lambda: "/trusted/cacert.pem")
    create_context = Mock(return_value=ssl_context)
    monkeypatch.setattr(bootstrap.ssl, "create_default_context", create_context)
    tcp_connector = Mock(return_value=connector)
    monkeypatch.setattr(bootstrap.aiohttp, "TCPConnector", tcp_connector)
    public_resolver = Mock(return_value=resolver)
    monkeypatch.setattr(bootstrap, "PublicAddressResolver", public_resolver)
    session = SimpleNamespace(closed=False, close=AsyncMock())
    client_session = Mock(return_value=session)
    monkeypatch.setattr(bootstrap.aiohttp, "ClientSession", client_session)
    service = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(bootstrap, "WeixinILinkService", lambda *args, **kwargs: service)

    runtime = await bootstrap.bootstrap_weixin_ilink(
        {"enabled": True}, auth_required=True, supervisor=_supervisor(control_home=tmp_path / "control-plane")
    )

    assert runtime.status.state == "ready"
    create_context.assert_called_once_with(cafile="/trusted/cacert.pem")
    public_resolver.assert_called_once_with()
    tcp_connector.assert_called_once_with(
        ssl=ssl_context,
        resolver=resolver,
        use_dns_cache=False,
    )
    client_session.assert_called_once_with(connector=connector, trust_env=False)
    await runtime.close()


@pytest.mark.asyncio
async def test_ready_runtime_stops_once(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ILINK_LOOKUP_KEYS_JSON", _keys(1))
    monkeypatch.setenv("HERMES_ILINK_ENCRYPTION_KEYS_JSON", _keys(2))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    service = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(bootstrap, "WeixinILinkService", lambda *args, **kwargs: service)

    runtime = await bootstrap.bootstrap_weixin_ilink(
        {"enabled": True}, auth_required=True, supervisor=_supervisor(control_home=tmp_path / "control-plane")
    )

    assert runtime.status.state == "ready"
    assert runtime.status.ready is True
    assert runtime.service is service
    await runtime.close()
    await runtime.close()
    service.stop.assert_awaited_once()
