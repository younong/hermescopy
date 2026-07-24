from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_cli.channel_connectors.weixin_ilink import bootstrap


def _supervisor(**overrides):
    values = {
        "deployment_inference_policy": object(),
        "deployment_image_policy": object(),
        "resource_manager": object(),
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
        {"enabled": True}, auth_required=True, supervisor=_supervisor()
    )

    assert runtime.status.state == "startup_failed"
    assert runtime.service is None
    assert runtime.session is None
    service.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_ready_runtime_stops_once(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ILINK_LOOKUP_KEYS_JSON", _keys(1))
    monkeypatch.setenv("HERMES_ILINK_ENCRYPTION_KEYS_JSON", _keys(2))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    service = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(bootstrap, "WeixinILinkService", lambda *args, **kwargs: service)

    runtime = await bootstrap.bootstrap_weixin_ilink(
        {"enabled": True}, auth_required=True, supervisor=_supervisor()
    )

    assert runtime.status.state == "ready"
    assert runtime.status.ready is True
    assert runtime.service is service
    await runtime.close()
    await runtime.close()
    service.stop.assert_awaited_once()
