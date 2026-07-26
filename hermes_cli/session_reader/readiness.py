"""Background lifecycle coordination for authenticated Session Readers."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from hermes_cli.dashboard_auth.owner_context import owner_context_from_session
from .client import SessionReaderHealthError
from .supervisor import SessionReaderStartupError, SessionReaderUnavailableError

_log = logging.getLogger(__name__)
_LIFECYCLE_ATTR = "session_reader_lifecycle"


@dataclass
class _ObservedOwner:
    owner: Any
    last_observed_at: float
    failures: int = 0
    retry_at: float = 0.0


class SessionReaderLifecycle:
    """Own Reader startup and maintenance outside the business request path."""

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
                name="session-reader-lifecycle",
            )

    def observe_verified_owner(self, owner: Any) -> None:
        if not self._accepting:
            return
        owner_key = str(owner.owner_key)
        now = time.monotonic()
        observed = self._owners.get(owner_key)
        if observed is None:
            self._owners[owner_key] = _ObservedOwner(owner=owner, last_observed_at=now)
        else:
            observed.owner = owner
            observed.last_observed_at = now
        self._wake.set()
        self._schedule_start(owner_key)

    def report_request_failure(self, lease: Any, reason: str) -> None:
        """Wake maintenance only when the failed fence is still locally current."""
        if not self._accepting:
            return
        owner_key = str(lease.owner_key)
        if not self.supervisor.report_request_failure(lease):
            return
        _log.warning(
            "session reader request failure owner=%s generation=%s reason=%s",
            owner_key,
            lease.reader_generation,
            reason,
        )
        self._wake.set()

    def _schedule_start(self, owner_key: str) -> None:
        observed = self._owners.get(owner_key)
        if observed is None or observed.retry_at > time.monotonic():
            return
        existing = self._startups.get(owner_key)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._ensure_started(owner_key, observed),
            name=f"session-reader-start:{owner_key}",
        )
        self._startups[owner_key] = task

        def discard(completed: asyncio.Task[None]) -> None:
            if self._startups.get(owner_key) is completed:
                self._startups.pop(owner_key, None)

        task.add_done_callback(discard)

    async def _ensure_started(self, owner_key: str, observed: _ObservedOwner) -> None:
        try:
            handle = await asyncio.to_thread(
                self.supervisor.ensure_started,
                observed.owner,
            )
            if str(handle.owner_key) != owner_key:
                raise SessionReaderHealthError("session reader returned a mismatched handle")
            observed.failures = 0
            observed.retry_at = 0.0
        except asyncio.CancelledError:
            raise
        except (
            TimeoutError,
            SessionReaderUnavailableError,
            SessionReaderStartupError,
            SessionReaderHealthError,
        ):
            observed.failures += 1
            delay = min(
                self.max_backoff,
                self.initial_backoff * (2 ** min(observed.failures - 1, 8)),
            )
            observed.retry_at = time.monotonic() + delay
            _log.warning("session reader background startup failed owner=%s", owner_key)
        except Exception as exc:
            observed.failures += 1
            observed.retry_at = time.monotonic() + self.max_backoff
            _log.warning(
                "session reader background startup failed owner=%s error_type=%s",
                owner_key,
                type(exc).__name__,
            )

    async def _run(self) -> None:
        while self._accepting:
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self.maintenance_interval,
                )
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await asyncio.to_thread(self.supervisor.maintenance_tick)
            except Exception:
                _log.exception("session reader lifecycle maintenance failed")
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
            self._task.cancel()
        for task in tuple(self._startups.values()):
            task.cancel()
        tasks = tuple(self._startups.values())
        if self._task is not None:
            tasks = (self._task, *tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._startups.clear()
        self._owners.clear()


def initialize_session_reader_warmups(app: Any) -> None:
    supervisor = getattr(app.state, "session_reader_supervisor", None)
    lifecycle = SessionReaderLifecycle(supervisor) if supervisor is not None else None
    setattr(app.state, _LIFECYCLE_ATTR, lifecycle)
    if lifecycle is not None:
        lifecycle.start()


def observe_verified_session(app: Any, session: Any) -> None:
    lifecycle = getattr(app.state, _LIFECYCLE_ATTR, None)
    if lifecycle is None:
        return
    try:
        lifecycle.observe_verified_owner(owner_context_from_session(session))
    except Exception:
        _log.exception("session reader owner observation failed")


async def drain_session_reader_warmups(app: Any) -> None:
    lifecycle = getattr(app.state, _LIFECYCLE_ATTR, None)
    if lifecycle is not None:
        await lifecycle.close()
    setattr(app.state, _LIFECYCLE_ATTR, None)
