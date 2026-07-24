"""Fail-closed, failure-isolated startup for the central iLink connector."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from hermes_cli.channel_identity.crypto import ChannelCrypto
from hermes_cli.channel_identity.store import ChannelIdentityStore

from .service import WeixinILinkService

_log = logging.getLogger(__name__)

_MESSAGES = {
    "disabled": "WeChat connection is disabled on this server.",
    "configuration_invalid": "WeChat connection is not available on this server yet.",
    "authenticated_dashboard_required": "WeChat connection is not available on this server yet.",
    "deployment_policy_unavailable": "WeChat connection is not available on this server yet.",
    "resource_governance_unavailable": "WeChat connection is not available on this server yet.",
    "keyrings_unavailable": "WeChat connection is not available on this server yet.",
    "startup_failed": "WeChat connection is temporarily unavailable.",
    "ready": "WeChat connection is ready.",
}


@dataclass(frozen=True)
class WeixinILinkStatus:
    enabled: bool
    ready: bool
    state: str
    message: str

    @classmethod
    def create(cls, state: str, *, enabled: bool = True) -> "WeixinILinkStatus":
        return cls(
            enabled=enabled,
            ready=state == "ready",
            state=state,
            message=_MESSAGES[state],
        )


class WeixinILinkRuntime:
    """Owns any partially or fully initialized connector resources."""

    def __init__(self, status: WeixinILinkStatus) -> None:
        self.status = status
        self.service: WeixinILinkService | None = None
        self.session: aiohttp.ClientSession | None = None
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        service, self.service = self.service, None
        session, self.session = self.session, None
        if service is not None:
            try:
                await service.stop()
            except Exception:
                _log.exception("iLink connector shutdown failed")
        if session is not None and not session.closed:
            await session.close()


async def bootstrap_weixin_ilink(
    config: dict[str, Any],
    *,
    auth_required: bool,
    supervisor: Any,
) -> WeixinILinkRuntime:
    """Start iLink only when every trusted execution prerequisite is available."""
    configured_enabled = config.get("enabled", True)
    if not isinstance(configured_enabled, bool):
        return WeixinILinkRuntime(WeixinILinkStatus.create("configuration_invalid"))
    if not configured_enabled:
        return WeixinILinkRuntime(WeixinILinkStatus.create("disabled", enabled=False))
    if not auth_required or supervisor is None:
        return WeixinILinkRuntime(
            WeixinILinkStatus.create("authenticated_dashboard_required")
        )
    if (
        getattr(supervisor, "deployment_inference_policy", None) is None
        or getattr(supervisor, "deployment_image_policy", None) is None
    ):
        return WeixinILinkRuntime(
            WeixinILinkStatus.create("deployment_policy_unavailable")
        )
    if getattr(supervisor, "resource_manager", None) is None:
        return WeixinILinkRuntime(
            WeixinILinkStatus.create("resource_governance_unavailable")
        )

    try:
        crypto = ChannelCrypto.from_env(
            lookup_version=int(config.get("active_lookup_key_version", 1)),
            encryption_version=int(config.get("active_encryption_key_version", 1)),
        )
    except Exception as exc:
        _log.warning("iLink connector unavailable state=keyrings_unavailable error_type=%s", type(exc).__name__)
        return WeixinILinkRuntime(WeixinILinkStatus.create("keyrings_unavailable"))

    runtime = WeixinILinkRuntime(WeixinILinkStatus.create("startup_failed"))
    runtime.session = aiohttp.ClientSession(trust_env=True)
    try:
        store = ChannelIdentityStore(crypto)
        runtime.service = WeixinILinkService(
            store,
            runtime.session,
            supervisor,
            config=config,
        )
        await runtime.service.start()
    except Exception as exc:
        _log.warning("iLink connector unavailable state=startup_failed error_type=%s", type(exc).__name__)
        await runtime.close()
        return runtime

    runtime.status = WeixinILinkStatus.create("ready")
    return runtime
