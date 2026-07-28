"""Shared authenticated Owner Worker readiness boundary."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from hermes_cli.dashboard_auth.owner_context import (
    ensure_owner_home,
    owner_context_from_session,
)
from hermes_cli.owner_worker import (
    OwnerWorkerHealthError,
    OwnerWorkerStartupError,
    OwnerWorkerUnavailableError,
)


_log = logging.getLogger(__name__)
_LIFECYCLE_ATTR = "owner_worker_lifecycle"


@dataclass
class _ObservedOwner:
    owner: Any
    last_observed_at: float
    failures: int = 0
    retry_at: float = 0.0


class OwnerWorkerLifecycle:
    """Own Worker startup and retirement outside business request paths."""

    def __init__(
        self,
        supervisor: Any,
        *,
        maintenance_interval: float = 1.0,
        initial_backoff: float = 0.1,
        max_backoff: float = 5.0,
    ) -> None:
        self.supervisor = supervisor
        self.maintenance_interval = max(0.05, float(maintenance_interval))
        self.initial_backoff = max(0.01, float(initial_backoff))
        self.max_backoff = max(self.initial_backoff, float(max_backoff))
        self._owners: dict[str, _ObservedOwner] = {}
        self._startups: dict[str, asyncio.Task[None]] = {}
        self._accepting = True
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(),
                name="owner-worker-lifecycle",
            )

    def observe_verified_owner(
        self,
        owner: Any,
        *,
        schedule_start: bool = True,
    ) -> asyncio.Task[None] | None:
        if not self._accepting:
            return None
        owner_key = str(owner.owner_key)
        now = time.monotonic()
        observed = self._owners.get(owner_key)
        if observed is None:
            self._owners[owner_key] = _ObservedOwner(
                owner=owner,
                last_observed_at=now,
            )
        else:
            observed.owner = owner
            observed.last_observed_at = now
        if not schedule_start:
            return None
        self._wake.set()
        return self._schedule_start(owner_key)

    def report_request_failure(self, handle: Any, reason: str) -> None:
        """Wake retirement only when the failed generation is still current."""
        if not self._accepting or not self.supervisor.report_request_failure(handle):
            return
        _log.warning(
            "owner worker request failure owner=%s generation=%s reason=%s",
            handle.owner_key,
            handle.worker_generation,
            reason,
        )
        self._wake.set()

    def _schedule_start(self, owner_key: str) -> asyncio.Task[None] | None:
        observed = self._owners.get(owner_key)
        if observed is None or observed.retry_at > time.monotonic():
            return None
        existing = self._startups.get(owner_key)
        if existing is not None and not existing.done():
            return existing
        needs_start = getattr(self.supervisor, "needs_start", None)
        if callable(needs_start) and not needs_start(observed.owner):
            observed.failures = 0
            observed.retry_at = 0.0
            return None
        task = asyncio.create_task(
            self._ensure_started(owner_key, observed),
            name=f"owner-worker-start:{owner_key}",
        )
        self._startups[owner_key] = task

        def discard(completed: asyncio.Task[None]) -> None:
            if self._startups.get(owner_key) is completed:
                self._startups.pop(owner_key, None)

        task.add_done_callback(discard)
        return task

    async def _ensure_started(
        self,
        owner_key: str,
        observed: _ObservedOwner,
    ) -> None:
        try:
            handle = await start_owner_worker(self.supervisor, observed.owner)
            if str(handle.owner_key) != owner_key:
                raise OwnerWorkerHealthError(
                    "owner worker returned a mismatched handle"
                )
            observed.failures = 0
            observed.retry_at = 0.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - lifecycle must remain alive
            observed.failures += 1
            expected = isinstance(
                exc,
                (
                    TimeoutError,
                    OwnerWorkerUnavailableError,
                    OwnerWorkerStartupError,
                    OwnerWorkerHealthError,
                ),
            )
            delay = (
                min(
                    self.max_backoff,
                    self.initial_backoff * (2 ** min(observed.failures - 1, 8)),
                )
                if expected
                else self.max_backoff
            )
            observed.retry_at = time.monotonic() + delay
            _log.warning(
                "owner worker background startup failed owner=%s "
                "error_type=%s attempt=%s retry_delay=%.3f",
                owner_key,
                type(exc).__name__,
                observed.failures,
                delay,
            )
        finally:
            self._wake.set()

    def _next_wake_delay(self, now: float) -> float:
        idle_timeout = float(getattr(self.supervisor, "idle_timeout", 1800.0))
        desired_ttl = max(self.maintenance_interval, idle_timeout)
        deadlines = [
            observed.last_observed_at + desired_ttl
            for observed in self._owners.values()
        ]
        deadlines.extend(
            observed.retry_at
            for observed in self._owners.values()
            if observed.retry_at > now
        )
        next_maintenance = getattr(self.supervisor, "next_maintenance_delay", None)
        if callable(next_maintenance):
            deadlines.append(now + max(0.0, float(next_maintenance())))
        elif self._owners:
            deadlines.append(now + self.maintenance_interval)
        if not deadlines:
            return idle_timeout
        return max(0.01, min(deadlines) - now)

    async def _run(self) -> None:
        while self._accepting:
            now = time.monotonic()
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._next_wake_delay(now),
                )
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if not self._accepting:
                break
            try:
                await asyncio.to_thread(self.supervisor.maintenance_tick)
            except Exception:
                _log.exception("owner worker lifecycle maintenance failed")
            now = time.monotonic()
            desired_ttl = max(
                self.maintenance_interval,
                float(getattr(self.supervisor, "idle_timeout", 1800.0)),
            )
            for owner_key, observed in tuple(self._owners.items()):
                if now - observed.last_observed_at >= desired_ttl:
                    self._owners.pop(owner_key, None)
                    continue
                self._schedule_start(owner_key)

    async def close(self) -> None:
        self._accepting = False
        self._wake.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        tasks = tuple(self._startups.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._startups.clear()
        self._owners.clear()


def initialize_owner_worker_warmups(
    app: Any,
    *,
    supervisor: Any | None = None,
) -> None:
    supervisor = supervisor or getattr(app.state, "owner_worker_supervisor", None)
    lifecycle = OwnerWorkerLifecycle(supervisor) if supervisor is not None else None
    setattr(app.state, _LIFECYCLE_ATTR, lifecycle)
    if lifecycle is not None:
        lifecycle.start()


def schedule_owner_worker_warmup(
    app: Any,
    *,
    owner: Any,
    supervisor: Any | None = None,
) -> asyncio.Task[None] | None:
    """Observe one verified owner and start only when no live Worker exists."""
    lifecycle = getattr(app.state, _LIFECYCLE_ATTR, None)
    selected = supervisor or getattr(app.state, "owner_worker_supervisor", None)
    if lifecycle is None and selected is not None:
        lifecycle = OwnerWorkerLifecycle(selected)
        setattr(app.state, _LIFECYCLE_ATTR, lifecycle)
        lifecycle.start()
    if lifecycle is None or lifecycle.supervisor is not selected:
        return None
    return lifecycle.observe_verified_owner(owner)


async def drain_owner_worker_warmups(app: Any) -> None:
    lifecycle = getattr(app.state, _LIFECYCLE_ATTR, None)
    if lifecycle is not None:
        await lifecycle.close()
    setattr(app.state, _LIFECYCLE_ATTR, None)


def _start_owner_worker(supervisor: Any, owner: Any) -> Any:
    ensure_owner_home(owner)
    handle = supervisor.get_or_start(owner)
    if str(handle.owner_key) != str(owner.owner_key):
        raise OwnerWorkerHealthError("owner worker returned a mismatched handle")
    return handle


async def start_owner_worker(supervisor: Any, owner: Any) -> Any:
    """Admit an owner home and return its ready Worker without blocking the loop."""
    return await asyncio.to_thread(_start_owner_worker, supervisor, owner)


async def ensure_owner_worker_ready(request: Request) -> tuple[Any, Any]:
    """Return the verified owner context and its ready Worker handle."""
    session = getattr(request.state, "session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    owner = owner_context_from_session(session)
    supervisor = getattr(request.app.state, "owner_worker_supervisor", None)
    if supervisor is None:
        raise HTTPException(
            status_code=503,
            detail="Owner worker supervisor is unavailable",
        )

    try:
        handle = await start_owner_worker(supervisor, owner)
    except TimeoutError as exc:
        _log.warning("owner worker startup timed out: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Owner worker startup timed out",
        ) from exc
    except (OwnerWorkerUnavailableError, OwnerWorkerStartupError) as exc:
        _log.warning("owner worker unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Owner worker is unavailable",
        ) from exc
    except OwnerWorkerHealthError as exc:
        _log.warning("owner worker health check failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Owner worker request failed",
        ) from exc

    lifecycle = getattr(request.app.state, _LIFECYCLE_ATTR, None)
    if lifecycle is not None:
        lifecycle.observe_verified_owner(owner, schedule_start=False)
    return owner, handle
