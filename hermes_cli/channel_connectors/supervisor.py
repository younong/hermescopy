"""Provider-neutral lifecycle for canonical channel connector services."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class ConnectorSupervisor:
    """Starts and closes connector services without owning Agent execution."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], Awaitable[object]]] = {}
        self._services: dict[str, object] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def register(self, provider: str, factory: Callable[[], Awaitable[object]]) -> None:
        provider = provider.strip()
        if not provider:
            raise ValueError("connector provider is required")
        if self._closed:
            raise RuntimeError("connector supervisor is closed")
        if provider in self._factories:
            raise ValueError(f"connector provider already registered: {provider}")
        self._factories[provider] = factory

    async def start(self) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("connector supervisor is closed")
            started: list[object] = []
            try:
                for provider, factory in self._factories.items():
                    if provider in self._services:
                        continue
                    service = await factory()
                    self._services[provider] = service
                    started.append(service)
            except BaseException:
                for service in reversed(started):
                    await self._close_service(service)
                self._services.clear()
                raise

    async def start_provider(self, provider: str) -> object:
        """Start one provider without changing another provider's lifecycle."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("connector supervisor is closed")
            if provider in self._services:
                return self._services[provider]
            factory = self._factories.get(provider)
            if factory is None:
                raise KeyError(provider)
            service = await factory()
            self._services[provider] = service
            return service

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(self._factories)

    def get(self, provider: str) -> object | None:
        return self._services.get(provider)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            services = list(reversed(self._services.values()))
            self._services.clear()
        await asyncio.gather(
            *(self._close_service(service) for service in services),
            return_exceptions=True,
        )

    @staticmethod
    async def _close_service(service: object) -> None:
        close = getattr(service, "close", None) or getattr(service, "stop", None)
        if close is not None:
            await close()
