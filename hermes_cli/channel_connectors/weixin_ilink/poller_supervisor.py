"""Single-process supervisor for active iLink account pollers."""

from __future__ import annotations

import asyncio
import uuid

from hermes_cli.channel_identity.store import ChannelIdentityStore

from .poller import AccountPoller, StalePollLeaseError, acquire_poll_lease


class PollerSupervisor:
    def __init__(
        self,
        store: ChannelIdentityStore,
        session,
        *,
        timeout_ms: int = 35_000,
        retry_seconds: float = 2.0,
    ) -> None:
        self.store = store
        self.session = session
        self.timeout_ms = timeout_ms
        self.retry_seconds = retry_seconds
        self.holder = f"service-{uuid.uuid4().hex}"
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True
        await self.reconcile()

    async def reconcile(self) -> None:
        with self.store.read() as conn:
            account_ids = {
                row["account_id"]
                for row in conn.execute("SELECT account_id FROM ilink_accounts WHERE status='active'")
            }
        for account_id in account_ids - self._tasks.keys():
            lease = acquire_poll_lease(self.store, account_id=account_id, holder=self.holder)
            self._tasks[account_id] = asyncio.create_task(
                self._run(AccountPoller(self.store, self.session, lease)),
                name=f"ilink-poller-{account_id}",
            )

    async def _run(self, poller: AccountPoller) -> None:
        try:
            while self._running:
                try:
                    await poller.poll_once(timeout_ms=self.timeout_ms)
                except asyncio.CancelledError:
                    raise
                except StalePollLeaseError:
                    return
                except Exception:
                    await asyncio.sleep(self.retry_seconds)
        finally:
            self._tasks.pop(poller.lease.account_id, None)

    async def stop(self) -> None:
        self._running = False
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
