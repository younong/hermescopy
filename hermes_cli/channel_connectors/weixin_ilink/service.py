"""Lifecycle container for central iLink enrollment, polling, and sending."""

from __future__ import annotations

import asyncio
import time
import uuid

from hermes_cli.channel_dispatch import ChannelDispatcher
from hermes_cli.channel_identity.store import ChannelIdentityStore

from .enrollment import EnrollmentManager
from .poller_supervisor import PollerSupervisor
from .sender import OutboundSender, claim_outbound


class WeixinILinkService:
    def __init__(self, store: ChannelIdentityStore, session, supervisor, *, config: dict) -> None:
        self.store = store
        self.session = session
        self.holder = f"connector-{uuid.uuid4().hex}"
        self.claim_timeout = float(config.get("dispatch_claim_timeout_seconds", 1800))
        self.idle_seconds = min(float(config.get("outbound_retry_seconds", 2)), 1.0)
        self.dispatcher = ChannelDispatcher(
            store,
            supervisor,
            turn_timeout=self.claim_timeout,
        )
        self.dispatch_concurrency = max(1, int(config.get("dispatch_concurrency", 4)))
        self.enrollments = EnrollmentManager(
            store,
            session,
            bot_type=str(config.get("bot_type", "3")),
            ttl_seconds=int(config.get("enrollment_ttl_seconds", 480)),
            poll_interval_seconds=float(config.get("enrollment_poll_interval_seconds", 1)),
            max_pending_global=int(config.get("max_pending_enrollments", 100)),
            max_events_per_source=int(config.get("rate_limit_per_source", 5)),
            rate_window_seconds=int(config.get("rate_limit_window_seconds", 300)),
            on_account_activated=self.account_activated,
        )
        self.pollers = PollerSupervisor(
            store,
            session,
            timeout_ms=int(config.get("provider_poll_timeout_ms", 35_000)),
            retry_seconds=float(config.get("provider_retry_seconds", 2)),
        )
        self.sender = OutboundSender(
            store,
            session,
            retry_seconds=float(config.get("outbound_retry_seconds", 2)),
        )
        self._running = False
        self._tasks: set[asyncio.Task] = set()
        self._dispatch_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._recover_stale_claims()
        self._running = True
        self._start_task(self._dispatch_loop(), "ilink-dispatch-loop")
        self._start_task(self._sender_loop(), "ilink-sender-loop")
        self._start_task(self._reconcile_loop(), "ilink-reconcile-loop")
        await self.pollers.start()

    async def account_activated(self) -> None:
        await self.pollers.reconcile()

    async def _dispatch_loop(self) -> None:
        while self._running:
            while len(self._dispatch_tasks) < self.dispatch_concurrency:
                claim = self.dispatcher.claim_next(holder=self.holder)
                if claim is None:
                    break
                task = asyncio.create_task(
                    self._dispatch_one(claim),
                    name=f"ilink-dispatch-{claim['inbound_id']}",
                )
                self._dispatch_tasks.add(task)
                task.add_done_callback(self._dispatch_tasks.discard)
            await asyncio.sleep(self.idle_seconds)

    async def _dispatch_one(self, claim: dict) -> None:
        try:
            await self.dispatcher.dispatch_claim(claim, holder=self.holder)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.dispatcher.fail_claim(claim["inbound_id"], self.holder, "dispatch_failed")

    async def _sender_loop(self) -> None:
        while self._running:
            claim = claim_outbound(self.store, holder=self.holder)
            if claim is None:
                await asyncio.sleep(self.idle_seconds)
                continue
            await self.sender.send_claim(claim, holder=self.holder)

    async def _reconcile_loop(self) -> None:
        while self._running:
            await asyncio.sleep(30)
            await self.pollers.reconcile()

    def _recover_stale_claims(self) -> None:
        cutoff = time.time() - self.claim_timeout
        now = time.time()
        with self.store.write() as conn:
            conn.execute(
                """
                UPDATE inbound_messages SET status='queued', claimed_by=NULL, claimed_at=NULL,
                    updated_at=? WHERE status='processing' AND claimed_at<?
                """,
                (now, cutoff),
            )
            conn.execute(
                """
                UPDATE outbound_messages SET status='queued', claimed_by=NULL, claimed_at=NULL,
                    next_attempt_at=?, updated_at=? WHERE status='sending' AND claimed_at<?
                """,
                (now, now, cutoff),
            )

    def _start_task(self, coroutine, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        self._running = False
        await self.enrollments.stop()
        await self.pollers.stop()
        tasks = [*self._tasks, *self._dispatch_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._dispatch_tasks.clear()
