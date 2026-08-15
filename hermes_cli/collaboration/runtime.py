"""Owner Worker lifecycle container for internal collaboration."""

from __future__ import annotations

from typing import Any

from hermes_state import SessionDB

from .resolver import CollaborationEmployeeResolver
from .scheduler import CollaborationScheduler
from .service import CollaborationService


class CollaborationRuntime:
    """Own the service, bounded scheduler, and trusted Agent runner."""

    def __init__(
        self,
        db: SessionDB,
        *,
        runtime: Any,
        resolver: CollaborationEmployeeResolver,
        runner: Any,
        emit,
        capacity: int = 4,
        active_budget_seconds: float = 300.0,
        deliver_web_origin=None,
    ) -> None:
        self.db = db
        self.service = CollaborationService(
            db,
            owner_key=runtime.owner_key,
            resolver=resolver,
            emit=emit,
            ensure_member_session=runner.ensure_member_session,
            provision_session_generation=runner.provision_session_generation,
            filesystem_context=runtime.filesystem_context,
            deliver_web_origin=deliver_web_origin,
            worker_id=runtime.worker_id,
            worker_generation=runtime.worker_generation,
            lease_version=runtime.lease_version,
            recovery_generation=runtime.recovery_generation,
        )
        bind_service = getattr(runner, "bind_service", None)
        if callable(bind_service):
            bind_service(self.service)
        self.scheduler = CollaborationScheduler(
            db,
            store=self.service.store,
            resolver=resolver,
            runner=runner,
            runtime=runtime,
            emit=emit,
            capacity=capacity,
            active_budget_seconds=active_budget_seconds,
        )
        self.service.bind_scheduler(self.scheduler)
        self._closed = False

    def start(self) -> None:
        self.scheduler.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.scheduler.close()
