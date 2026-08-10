"""Authenticated Control Plane handoff for collaboration origin delivery."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from hermes_cli.dashboard_auth.authority import AuthorityStore
from hermes_cli.owner_worker.cron_dispatcher import (
    _authenticated_owners,
    _dispatch_owner_request,
)

_log = logging.getLogger(__name__)


def _ack(
    supervisor: Any,
    owner: Any,
    delivery_key: str,
    *,
    outbound_id: str | None = None,
    status: str | None = None,
    error: str | None = None,
) -> None:
    body = {
        "outbound_id": outbound_id,
        "status": status,
        "error": error,
    }
    _dispatch_owner_request(
        supervisor,
        owner,
        f"/internal/collaboration/delivery/{delivery_key}/ack",
        content=json.dumps(body).encode("utf-8"),
    )


def dispatch_owner_collaboration_deliveries(
    supervisor: Any,
    global_home: str | Path,
    *,
    authority_store: AuthorityStore | None = None,
    enqueue_delivery: Callable[..., str] | None = None,
    delivery_status: Callable[[str], dict[str, str | None] | None] | None = None,
) -> int:
    """Poll exact Workers and enqueue each stable intent into the canonical outbox."""
    store = authority_store or AuthorityStore(getattr(supervisor, "control_home", None))
    enqueued = 0
    for owner in _authenticated_owners(store, global_home):
        try:
            payload = _dispatch_owner_request(
                supervisor,
                owner,
                "/internal/collaboration/deliveries",
                content=b"{}",
            )
        except Exception:
            _log.exception("owner collaboration delivery poll failed owner=%s", owner.owner_key)
            continue
        deliveries = payload.get("deliveries", [])
        if not isinstance(deliveries, list):
            _log.error("Owner Worker collaboration deliveries are invalid owner=%s", owner.owner_key)
            continue
        handle = supervisor.get_or_start(owner.owner_context)
        for delivery in deliveries[:128]:
            if not isinstance(delivery, dict):
                continue
            key = str(delivery.get("delivery_key") or "").strip()
            try:
                if (
                    str(delivery.get("provider") or "") != "feishu"
                    or str(delivery.get("worker_owner_key") or "") != owner.owner_key
                    or str(delivery.get("worker_id") or "") != str(handle.worker_id)
                    or int(delivery.get("worker_generation") or 0) != int(handle.worker_generation)
                    or int(delivery.get("lease_version") or 0) != int(handle.lease_version)
                    or int(delivery.get("recovery_generation") or -1)
                    != int(handle.recovery_generation)
                ):
                    raise RuntimeError("collaboration delivery worker fence failed")
                outbound_id = str(delivery.get("outbound_id") or "").strip()
                if outbound_id:
                    if delivery_status is None:
                        raise RuntimeError("canonical channel outbox status is unavailable")
                    result = delivery_status(outbound_id)
                    if result is None:
                        raise RuntimeError("collaboration outbox receipt is unavailable")
                    status = str(result.get("status") or "")
                    if status in {"queued", "sending"}:
                        continue
                    if status not in {"delivered", "failed", "ambiguous"}:
                        raise RuntimeError("collaboration outbox status is invalid")
                    _ack(
                        supervisor,
                        owner,
                        key,
                        outbound_id=outbound_id,
                        status=status,
                        error=result.get("error"),
                    )
                    enqueued += 1
                    continue
                if enqueue_delivery is None:
                    raise RuntimeError("canonical channel outbox is unavailable")
                outbound_id = enqueue_delivery(
                    owner_key=owner.owner_key,
                    account_id=str(delivery.get("account_id") or ""),
                    binding_id=str(delivery.get("binding_id") or ""),
                    conversation_id=str(delivery.get("conversation_id") or ""),
                    thread_id=str(delivery.get("thread_id") or ""),
                    delivery_key=key,
                    payload=str(delivery.get("payload_text") or ""),
                )
            except Exception:
                # Validation and local enqueue failures have no provider side
                # effect. Keep the durable intent pending so a later poll can
                # safely retry the deterministic handoff.
                _log.exception(
                    "owner collaboration delivery handoff failed owner=%s key=%s",
                    owner.owner_key,
                    key,
                )
                continue
            # Enqueue is deterministic and transactionally idempotent. If this
            # acknowledgement fails, the next poll obtains the same outbound row.
            try:
                _ack(supervisor, owner, key, outbound_id=outbound_id)
                enqueued += 1
            except Exception:
                _log.exception(
                    "owner collaboration delivery success ack failed owner=%s",
                    owner.owner_key,
                )
    return enqueued


async def run_owner_collaboration_dispatcher(
    stop: asyncio.Event,
    supervisor: Any,
    global_home: str | Path,
    *,
    authority_store: AuthorityStore | None = None,
    enqueue_delivery: Callable[..., str] | None = None,
    delivery_status: Callable[[str], dict[str, str | None] | None] | None = None,
    interval: float | None = None,
) -> None:
    delay = max(
        1.0,
        float(interval or os.environ.get("HERMES_OWNER_COLLABORATION_INTERVAL", "5") or 5),
    )
    store = authority_store or AuthorityStore(getattr(supervisor, "control_home", None))
    while not stop.is_set():
        await asyncio.to_thread(
            dispatch_owner_collaboration_deliveries,
            supervisor,
            global_home,
            authority_store=store,
            enqueue_delivery=enqueue_delivery,
            delivery_status=delivery_status,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
