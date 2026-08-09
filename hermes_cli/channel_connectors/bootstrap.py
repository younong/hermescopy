"""Authenticated startup for the retained canonical messaging connectors."""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import certifi

from gateway.weixin_ilink.media import PublicAddressResolver
from hermes_cli.channel_identity import ChannelCrypto, ChannelIdentityStore

from .feishu import FeishuConnector
from .supervisor import ConnectorSupervisor
from .webhook import WebhookService
from .weixin_ilink.bootstrap import WeixinILinkStatus
from .weixin_ilink.service import WeixinILinkService

_log = logging.getLogger(__name__)

_RETAINED_PROVIDERS = frozenset({"weixin_ilink", "feishu", "webhook"})


def build_channel_identity_store(
    config: dict[str, Any],
    *,
    control_home: str | Path,
    global_home: str | Path,
) -> ChannelIdentityStore:
    """Construct the authoritative channel store for one Control Plane home."""
    resolved_control_home = Path(control_home).expanduser().resolve()
    resolved_global_home = Path(global_home).expanduser().resolve()
    expected_control_home = resolved_global_home / "control-plane"
    if resolved_control_home != expected_control_home:
        raise RuntimeError("Owner Worker supervisor homes are incompatible")
    retained = {
        provider: settings
        for provider, settings in config.items()
        if provider in _RETAINED_PROVIDERS and isinstance(settings, dict)
    }
    key_versions = {
        (
            int(settings.get("active_lookup_key_version", 1)),
            int(settings.get("active_encryption_key_version", 1)),
        )
        for settings in retained.values()
    }
    if len(key_versions) > 1:
        raise RuntimeError("connector keyring versions disagree")
    lookup_version, encryption_version = (
        next(iter(key_versions)) if key_versions else (1, 1)
    )
    crypto = ChannelCrypto.from_env(
        lookup_version=lookup_version,
        encryption_version=encryption_version,
    )
    return ChannelIdentityStore(
        crypto,
        resolved_control_home,
        global_home=resolved_global_home,
    )


@dataclass(frozen=True)
class ConnectorRuntimeStatus:
    states: dict[str, str]

    @property
    def ready(self) -> bool:
        return all(
            state == "ready"
            for connector, state in self.states.items()
            if ":" not in connector
        )


class CanonicalConnectorRuntime:
    def __init__(self, status: ConnectorRuntimeStatus) -> None:
        self.status = status
        self.connectors = ConnectorSupervisor()
        self.store: ChannelIdentityStore | None = None
        self.session: aiohttp.ClientSession | None = None
        self.feishu_config: dict[str, Any] | None = None
        self.owner_worker_supervisor: Any = None
        self._closed = False

    def get(self, provider: str, account_id: str | None = None) -> object | None:
        return self.connectors.get(provider, account_id)

    def register_feishu_account(self, account_id: str) -> None:
        if self.store is None or self.owner_worker_supervisor is None or self.feishu_config is None:
            raise RuntimeError("Feishu connector runtime is unavailable")
        store = self.store
        supervisor = self.owner_worker_supervisor
        config = dict(self.feishu_config)

        async def start_feishu() -> FeishuConnector:
            service = FeishuConnector(
                store,
                supervisor,
                account_id=account_id,
                config=config,
            )
            await service.start()
            return service

        self.connectors.register("feishu", start_feishu, account_id=account_id)

    async def start_account(self, provider: str, account_id: str) -> object:
        service = await self.connectors.start_provider(provider, account_id=account_id)
        self.status.states[f"{provider}:{account_id}"] = "ready"
        if provider == "feishu":
            self.status.states[provider] = "ready"
        return service

    async def stop_account(self, provider: str, account_id: str) -> bool:
        stopped = await self.connectors.stop_provider(provider, account_id=account_id)
        self.status.states[f"{provider}:{account_id}"] = "stopped"
        if provider == "feishu" and not any(
            self.connectors.get(provider, candidate) is not None
            for candidate in self.connectors.accounts(provider)
        ):
            self.status.states[provider] = "account_unavailable"
        return stopped

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.connectors.close()
        if self.session is not None and not self.session.closed:
            await self.session.close()


def _active_accounts(
    store: ChannelIdentityStore,
    provider: str,
) -> tuple[str, ...]:
    with store.read() as conn:
        rows = conn.execute(
            """
            SELECT account_id FROM connector_accounts
            WHERE provider=? AND status='active'
            ORDER BY account_id
            """,
            (provider,),
        ).fetchall()
    return tuple(str(row["account_id"]) for row in rows)


def _active_managed_feishu_accounts(
    store: ChannelIdentityStore,
) -> tuple[str, ...]:
    with store.read() as conn:
        rows = conn.execute(
            """
            SELECT a.account_id FROM connector_accounts a
            JOIN managed_feishu_accounts m ON m.account_id=a.account_id
                                             AND m.lifecycle_status='active'
            JOIN feishu_employee_profiles p ON p.account_id=a.account_id
                                            AND p.lifecycle_status='active'
            WHERE a.provider='feishu' AND a.status='active'
            ORDER BY a.account_id
            """
        ).fetchall()
    return tuple(str(row["account_id"]) for row in rows)


async def bootstrap_channel_connectors(
    config: dict[str, Any],
    *,
    auth_required: bool,
    supervisor: Any,
) -> CanonicalConnectorRuntime:
    """Start retained connectors behind the authenticated Owner boundary."""
    retained = {
        provider: settings
        for provider, settings in config.items()
        if provider in _RETAINED_PROVIDERS and isinstance(settings, dict)
    }
    configured = {
        provider: settings
        for provider, settings in config.items()
        if isinstance(settings, dict) and settings.get("enabled", True) is True
    }
    enabled = {
        provider: settings
        for provider, settings in configured.items()
        if provider in _RETAINED_PROVIDERS
    }
    unsupported = {
        provider: "unsupported"
        for provider in configured
        if provider not in _RETAINED_PROVIDERS
    }
    if not auth_required or supervisor is None:
        if not enabled:
            return CanonicalConnectorRuntime(
                ConnectorRuntimeStatus(unsupported)
            )
        states = {
            provider: "authenticated_dashboard_required" for provider in enabled
        }
        states.update(unsupported)
        return CanonicalConnectorRuntime(ConnectorRuntimeStatus(states))
    if not retained:
        return CanonicalConnectorRuntime(
            ConnectorRuntimeStatus(unsupported)
        )

    blocked_states: dict[str, str] = {}
    if "weixin_ilink" in enabled and (
        getattr(supervisor, "deployment_inference_policy", None) is None
        or getattr(supervisor, "deployment_media_policy", None) is None
    ):
        blocked_states["weixin_ilink"] = "deployment_policy_unavailable"
    elif "weixin_ilink" in enabled and getattr(supervisor, "resource_manager", None) is None:
        blocked_states["weixin_ilink"] = "resource_governance_unavailable"
    if enabled and enabled.keys() <= blocked_states.keys():
        states = dict(blocked_states)
        states.update(unsupported)
        return CanonicalConnectorRuntime(ConnectorRuntimeStatus(states))

    try:
        store = build_channel_identity_store(
            retained,
            control_home=supervisor.control_home,
            global_home=supervisor.global_home,
        )
    except Exception as exc:
        _log.warning("connector startup unavailable error_type=%s", type(exc).__name__)
        states = {provider: "control_plane_unavailable" for provider in enabled}
        states.update(unsupported)
        return CanonicalConnectorRuntime(ConnectorRuntimeStatus(states))

    runtime = CanonicalConnectorRuntime(
        ConnectorRuntimeStatus({provider: "startup_failed" for provider in configured})
    )
    runtime.store = store
    runtime.owner_worker_supervisor = supervisor
    if not enabled:
        runtime.status = ConnectorRuntimeStatus(unsupported)
        return runtime

    ssl_context = ssl.create_default_context(cafile=certifi.where())
    runtime.session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            ssl=ssl_context,
            resolver=PublicAddressResolver(),
            use_dns_cache=False,
        ),
        trust_env=False,
    )

    if "weixin_ilink" in enabled and "weixin_ilink" not in blocked_states:
        ilink_config = enabled["weixin_ilink"]

        async def start_ilink() -> WeixinILinkService:
            service = WeixinILinkService(
                store,
                runtime.session,
                supervisor,
                config=ilink_config,
            )
            await service.start()
            return service

        runtime.connectors.register("weixin_ilink", start_ilink)

    feishu_accounts: tuple[str, ...] = ()
    if "feishu" in enabled:
        feishu_config = enabled["feishu"]
        runtime.feishu_config = dict(feishu_config)
        feishu_accounts = _active_managed_feishu_accounts(store)
        for feishu_account_id in feishu_accounts:
            runtime.register_feishu_account(feishu_account_id)

    if "webhook" in enabled:
        webhook_config = enabled["webhook"]
        if _active_accounts(store, "webhook"):

            async def start_webhook() -> WebhookService:
                service = WebhookService(
                    store,
                    supervisor,
                    config=webhook_config,
                )
                await service.start()
                return service

            runtime.connectors.register("webhook", start_webhook)

    states = dict(unsupported)
    registered = set(runtime.connectors.providers)
    for provider in enabled:
        if provider in blocked_states:
            states[provider] = blocked_states[provider]
            continue
        if provider not in registered:
            states[provider] = (
                "account_unavailable"
                if provider in {"feishu", "webhook"}
                else "unsupported"
            )
            continue
        if provider == "feishu":
            started = 0
            for account_id in feishu_accounts:
                try:
                    await runtime.connectors.start_provider(
                        provider,
                        account_id=account_id,
                    )
                except Exception as exc:
                    _log.warning(
                        "connector startup failed provider=%s account_id=%s error_type=%s",
                        provider,
                        account_id,
                        type(exc).__name__,
                    )
                    states[f"{provider}:{account_id}"] = "startup_failed"
                else:
                    started += 1
                    states[f"{provider}:{account_id}"] = "ready"
            states[provider] = "ready" if started else "startup_failed"
            continue
        try:
            await runtime.connectors.start_provider(provider)
        except Exception as exc:
            _log.warning(
                "connector startup failed provider=%s error_type=%s",
                provider,
                type(exc).__name__,
            )
            states[provider] = "startup_failed"
        else:
            states[provider] = "ready"
    runtime.status = ConnectorRuntimeStatus(states)
    return runtime


def ilink_status(runtime: CanonicalConnectorRuntime) -> WeixinILinkStatus:
    state = runtime.status.states.get("weixin_ilink", "disabled")
    if state == "ready":
        return WeixinILinkStatus.create("ready")
    if state == "disabled":
        return WeixinILinkStatus.create("disabled", enabled=False)
    if state == "authenticated_dashboard_required":
        return WeixinILinkStatus.create("authenticated_dashboard_required")
    if state == "deployment_policy_unavailable":
        return WeixinILinkStatus.create("deployment_policy_unavailable")
    if state == "resource_governance_unavailable":
        return WeixinILinkStatus.create("resource_governance_unavailable")
    return WeixinILinkStatus.create("startup_failed")
