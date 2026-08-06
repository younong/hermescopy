"""Async lifecycle adapter for the pinned Feishu WebSocket SDK."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_log = logging.getLogger(__name__)


class FeishuWebSocketSession:
    """Run one SDK client on the caller's loop with deterministic shutdown.

    ``lark-oapi==1.5.3`` exposes only a blocking ``Client.start()`` backed by a
    module-global event loop. Its internal coroutines are otherwise per-client,
    so Hermes owns their tasks directly instead of entering that shared loop.
    """

    def __init__(self, client: Any) -> None:
        self.client = client
        self._running = False
        self._connect_task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            await self._connect()
        except BaseException:
            self._running = False
            await self._disconnect()
            raise
        self._ping_task = asyncio.create_task(
            self.client._ping_loop(),
            name="feishu-ws-ping",
        )

    async def _connect(self) -> None:
        while self._running:
            try:
                self._connect_task = asyncio.create_task(
                    self.client._connect(),
                    name="feishu-ws-connect",
                )
                await self._connect_task
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self._running or not getattr(self.client, "_auto_reconnect", True):
                    raise
                await self.client._reconnect()
            finally:
                self._connect_task = None

    async def close(self) -> None:
        if not self._running and self._connect_task is None and self._ping_task is None:
            return
        self._running = False
        self.client._auto_reconnect = False
        tasks = [
            task
            for task in (self._connect_task, self._ping_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connect_task = None
        self._ping_task = None
        try:
            await self.client._disconnect()
        except Exception as exc:
            _log.warning(
                "Feishu WebSocket shutdown failed error_type=%s",
                type(exc).__name__,
            )
