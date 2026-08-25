"""Control Plane dispatch for authenticated Owner cron schedules."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hermes_cli.dashboard_auth.authority import AuthorityStore
from hermes_cli.dashboard_auth.owner_context import owner_context_from_registry
from hermes_cli.owner_worker.client import OwnerWorkerClient
from hermes_cli.owner_worker.gateway_client import authority_lease_for_handle

_log = logging.getLogger(__name__)

_CRON_STARTUP_TIMEOUT_DEFAULT = 30.0
_CRON_STARTUP_RETRIES = 2
_CRON_STARTUP_BACKOFF = (0.25, 0.5)


def _cron_startup_timeout() -> float:
    raw = os.environ.get("HERMES_OWNER_CRON_STARTUP_TIMEOUT", "")
    try:
        value = float(raw) if raw.strip() else _CRON_STARTUP_TIMEOUT_DEFAULT
    except (TypeError, ValueError):
        value = _CRON_STARTUP_TIMEOUT_DEFAULT
    if not math.isfinite(value) or value < 0:
        return _CRON_STARTUP_TIMEOUT_DEFAULT
    return value


@dataclass(frozen=True)
class _StoredOwner:
    owner_context: Any

    @property
    def owner_key(self) -> str:
        return self.owner_context.owner_key

    @property
    def owner_home(self) -> Path:
        return self.owner_context.owner_home


def _authenticated_owners(
    authority_store: AuthorityStore,
    global_home: str | Path,
) -> tuple[_StoredOwner, ...]:
    """Revalidate every trusted registry row before selecting an Owner home."""
    owners: list[_StoredOwner] = []
    for row in authority_store.list_authenticated_owners():
        try:
            owner = owner_context_from_registry(
                auth_provider=row.auth_provider,
                tenant_id=row.tenant_id,
                canonical_user_id=row.canonical_user_id,
                expected_owner_key=row.owner_key,
                global_home=global_home,
            )
        except (RuntimeError, ValueError):
            _log.error("authenticated Owner registry row failed canonical validation")
            continue
        owners.append(_StoredOwner(owner))
    return tuple(owners)


def _startup_diagnostics(supervisor: Any, owner: _StoredOwner) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {"owner": owner.owner_key}
    try:
        store = getattr(supervisor, "authority_store", None)
        lease = store.read_owner_worker_lease(owner.owner_key) if store is not None else None
        if lease is not None:
            diagnostics.update(
                lease_state=lease.state.value,
                worker_generation=lease.worker_generation,
                worker_id=lease.worker_id,
                lease_version=lease.lease_version,
                recovery_generation=lease.recovery_generation,
            )
            try:
                socket_path = supervisor.socket_path_for(
                    owner.owner_context, lease.worker_generation
                )
                diagnostics.update(socket_path=str(socket_path), socket_exists=socket_path.exists())
            except Exception:
                diagnostics["socket_path"] = "unavailable"
        if store is not None:
            binding = store.read_owner_worker_process_binding(owner.owner_key)
            if binding is not None:
                diagnostics.update(
                    heartbeat_at=binding.heartbeat_at,
                    heartbeat_seq=binding.heartbeat_seq,
                    pid=binding.pid,
                    process_start_time=binding.process_start_time,
                )
    except Exception as exc:
        diagnostics["diagnostics_error"] = type(exc).__name__
    return diagnostics


def _get_cron_handle(supervisor: Any, owner: _StoredOwner):
    started_at = time.monotonic()
    timeout = _cron_startup_timeout()
    for attempt in range(_CRON_STARTUP_RETRIES + 1):
        try:
            return supervisor.get_or_start(owner.owner_context, timeout=timeout)
        except TimeoutError:
            if attempt >= _CRON_STARTUP_RETRIES:
                _log.error(
                    "owner cron startup timed out elapsed=%.3f attempts=%d timeout=%.3f diagnostics=%s",
                    time.monotonic() - started_at,
                    attempt + 1,
                    timeout,
                    _startup_diagnostics(supervisor, owner),
                )
                raise
            time.sleep(_CRON_STARTUP_BACKOFF[attempt])


def _dispatch_owner_request(
    supervisor: Any,
    owner: _StoredOwner,
    path: str,
    *,
    content: bytes | None = None,
    cron_startup: bool = False,
) -> dict[str, Any]:
    handle = _get_cron_handle(supervisor, owner) if cron_startup else supervisor.get_or_start(owner.owner_context)
    with supervisor.acquire_use(handle):
        response = OwnerWorkerClient(
            handle.socket_path,
            control_home=getattr(supervisor, "control_home", None),
            timeout=300.0,
        ).request(
            "POST",
            path,
            lease=authority_lease_for_handle(handle),
            headers={"Content-Type": "application/json"} if content else None,
            content=content,
        )
        response.raise_for_status()
        return response.json()


def _ack_delivery(
    supervisor: Any,
    owner: _StoredOwner,
    fire_id: str,
    *,
    error: str | None,
    cron_startup: bool = False,
) -> None:
    _dispatch_owner_request(
        supervisor,
        owner,
        f"/internal/cron/delivery/{fire_id}/ack",
        cron_startup=cron_startup,
        content=json.dumps({"error": error}).encode("utf-8"),
    )


def _enqueue_deliveries(
    supervisor: Any,
    owner: _StoredOwner,
    deliveries: Any,
    enqueue_delivery: Callable[..., str] | None,
    *,
    cron_startup: bool = False,
) -> None:
    if not isinstance(deliveries, list):
        raise RuntimeError("Owner Worker cron deliveries are invalid")
    if deliveries and enqueue_delivery is None:
        raise RuntimeError("canonical channel outbox is unavailable")
    for delivery in deliveries[:128]:
        if not isinstance(delivery, dict):
            continue
        fire_id = str(delivery.get("fire_id") or "").strip()
        binding_id = str(delivery.get("binding_id") or "").strip()
        payload = str(delivery.get("payload") or "")
        if not fire_id or not binding_id or not payload.strip() or len(payload.encode("utf-8")) > 256_000:
            continue
        try:
            assert enqueue_delivery is not None
            enqueue_delivery(
                owner_key=owner.owner_key,
                binding_id=binding_id,
                fire_id=fire_id,
                payload=payload,
            )
        except Exception as exc:
            try:
                _ack_delivery(
                    supervisor,
                    owner,
                    fire_id,
                    error=f"{type(exc).__name__}: {exc}"[:512],
                    cron_startup=cron_startup,
                )
            except Exception:
                _log.exception("owner cron delivery failure ack failed owner=%s", owner.owner_key)
            continue
        _ack_delivery(supervisor, owner, fire_id, error=None)


def dispatch_owner_job(
    supervisor: Any,
    global_home: str | Path,
    owner_key: str,
    job_id: str,
    fire_id: str,
    *,
    authority_store: AuthorityStore | None = None,
    enqueue_delivery: Callable[..., str] | None = None,
) -> bool:
    store = authority_store or AuthorityStore(getattr(supervisor, "control_home", None))
    owner = next(
        (
            item
            for item in _authenticated_owners(store, global_home)
            if item.owner_key == owner_key
        ),
        None,
    )
    if owner is None:
        return False
    payload = _dispatch_owner_request(
        supervisor,
        owner,
        "/internal/cron/fire",
        cron_startup=True,
        content=json.dumps({"job_id": job_id, "fire_id": fire_id}).encode("utf-8"),
    )
    _enqueue_deliveries(
        supervisor, owner, payload.get("deliveries", []), enqueue_delivery, cron_startup=True
    )
    return bool(payload.get("executed"))


def dispatch_owner_due_jobs(
    supervisor: Any,
    global_home: str | Path,
    *,
    authority_store: AuthorityStore | None = None,
    enqueue_delivery: Callable[..., str] | None = None,
) -> int:
    """Ask each registered Owner Worker to claim and execute its due jobs."""
    store = authority_store or AuthorityStore(getattr(supervisor, "control_home", None))
    executed = 0
    tick_id = f"tick:{uuid.uuid4().hex}"
    for owner in _authenticated_owners(store, global_home):
        try:
            payload = _dispatch_owner_request(
                supervisor,
                owner,
                "/internal/cron/tick",
                cron_startup=True,
                content=json.dumps({"tick_id": tick_id}).encode("utf-8"),
            )
            _enqueue_deliveries(
                supervisor,
                owner,
                payload.get("deliveries", []),
                enqueue_delivery,
                cron_startup=True,
            )
            executed += int(payload.get("executed") or 0)
        except Exception:
            _log.exception("owner cron dispatch failed owner=%s", owner.owner_key)
    return executed


async def run_owner_cron_dispatcher(
    stop: asyncio.Event,
    supervisor: Any,
    global_home: str | Path,
    *,
    authority_store: AuthorityStore | None = None,
    enqueue_delivery: Callable[..., str] | None = None,
    interval: float | None = None,
) -> None:
    delay = max(
        1.0,
        float(interval or os.environ.get("HERMES_OWNER_CRON_INTERVAL", "15") or 15),
    )
    store = authority_store or AuthorityStore(getattr(supervisor, "control_home", None))
    while not stop.is_set():
        await asyncio.to_thread(
            dispatch_owner_due_jobs,
            supervisor,
            global_home,
            authority_store=store,
            enqueue_delivery=enqueue_delivery,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
