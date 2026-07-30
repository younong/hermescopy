"""Bounded Control Plane dispatch for due authenticated-owner cron jobs."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_cli.dashboard_auth.owner_context import owner_context_from_owner_key
from hermes_cli.owner_worker.client import OwnerWorkerClient
from hermes_cli.owner_worker.gateway_client import authority_lease_for_handle


_log = logging.getLogger(__name__)
_OWNER_KEY_PREFIX = "ok1_"


@dataclass(frozen=True)
class _StoredOwner:
    owner_key: str
    owner_home: Path


def _canonical_owner_homes(global_home: str | Path) -> list[_StoredOwner]:
    root = Path(global_home).expanduser().resolve() / "users"
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        _log.warning("owner cron scan skipped unsafe users root")
        return []

    owners: list[_StoredOwner] = []
    try:
        candidates = list(root.iterdir())
    except OSError:
        _log.warning("owner cron scan could not read users root")
        return []
    for candidate in candidates:
        if not candidate.name.startswith(_OWNER_KEY_PREFIX):
            continue
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            continue
        resolved = candidate.resolve()
        if resolved.parent != root:
            continue
        owners.append(_StoredOwner(candidate.name, resolved))
    return owners


def _owner_may_be_due(owner: _StoredOwner) -> bool:
    from hermes_cli.cron_management import cron_home_scope

    with cron_home_scope(owner.owner_home):
        from cron.jobs import list_jobs

        now = datetime.now(timezone.utc)
        for job in list_jobs(include_disabled=False):
            next_run_at = job.get("next_run_at")
            if not next_run_at:
                continue
            try:
                due_at = datetime.fromisoformat(str(next_run_at).replace("Z", "+00:00"))
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if due_at <= now:
                return True
        return False


def _dispatch_owner_request(
    supervisor: Any,
    global_home: Path,
    owner: _StoredOwner,
    path: str,
    *,
    content: bytes | None = None,
) -> dict[str, Any]:
    owner_context = owner_context_from_owner_key(
        owner.owner_key,
        global_home=global_home,
    )
    handle = supervisor.get_or_start(owner_context)
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


def dispatch_owner_job(
    supervisor: Any,
    global_home: str | Path,
    owner_key: str,
    job_id: str,
) -> bool:
    resolved_home = Path(global_home).expanduser().resolve()
    owner = next(
        (item for item in _canonical_owner_homes(resolved_home) if item.owner_key == owner_key),
        None,
    )
    if owner is None:
        return False
    payload = _dispatch_owner_request(
        supervisor,
        resolved_home,
        owner,
        "/internal/cron/fire",
        content=json.dumps({"job_id": job_id}).encode("utf-8"),
    )
    return bool(payload.get("executed"))


def dispatch_owner_due_jobs(supervisor: Any, global_home: str | Path) -> int:
    """Wake due owners and synchronously tick each while holding a use lease."""
    resolved_home = Path(global_home).expanduser().resolve()
    executed = 0
    for owner in _canonical_owner_homes(resolved_home):
        try:
            if not _owner_may_be_due(owner):
                continue
            payload = _dispatch_owner_request(
                supervisor,
                resolved_home,
                owner,
                "/internal/cron/tick",
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
    interval: float | None = None,
) -> None:
    delay = max(
        1.0,
        float(interval or os.environ.get("HERMES_OWNER_CRON_INTERVAL", "15") or 15),
    )
    while not stop.is_set():
        await asyncio.to_thread(dispatch_owner_due_jobs, supervisor, global_home)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
