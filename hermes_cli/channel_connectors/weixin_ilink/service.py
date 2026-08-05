"""Lifecycle container for central iLink enrollment, polling, and sending."""

from __future__ import annotations

import asyncio
import time
import uuid

from hermes_cli.channel_dispatch import (
    ChannelDispatcher,
    recover_stale_outbound,
)
from hermes_cli.channel_identity.store import ChannelIdentityStore

from .dispatch_hooks import WeixinDispatchHooks, encode_weixin_outbound
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
        dispatch_hooks = WeixinDispatchHooks(session, config=config)
        self.dispatcher = ChannelDispatcher(
            store,
            supervisor,
            provider="weixin_ilink",
            turn_timeout=self.claim_timeout,
            media_materializer=dispatch_hooks.materialize,
            outbound_encoder=encode_weixin_outbound,
            media_config=config,
            dispatch_config=config,
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
            retry_max_seconds=float(config.get("outbound_retry_max_seconds", 300)),
            max_attempts=int(config.get("outbound_max_attempts", 8)),
            chunk_delay_seconds=float(config.get("outbound_chunk_delay_seconds", 0.2)),
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
            try:
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
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.idle_seconds)

    async def _dispatch_one(self, claim: dict) -> None:
        try:
            await self.dispatcher.dispatch_claim(claim, holder=self.holder)
        except asyncio.CancelledError:
            raise
        except Exception:
            # dispatch_claim owns claim recovery so decrypt/parse/Worker failures
            # cannot be double-counted or converted into terminal failures here.
            return

    async def _sender_loop(self) -> None:
        while self._running:
            try:
                claim = claim_outbound(self.store, holder=self.holder)
                if claim is not None:
                    await self.sender.send_claim(claim, holder=self.holder)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.idle_seconds)

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
                      AND account_id IN (
                        SELECT account_id FROM connector_accounts
                        WHERE provider='weixin_ilink'
                      )
                """,
                (now, cutoff),
            )
        recover_stale_outbound(
            self.store,
            provider="weixin_ilink",
            claimed_before=cutoff,
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
