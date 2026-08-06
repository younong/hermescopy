"""Provider-neutral lifecycle for canonical channel connector services."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class ConnectorSupervisor:
    """Starts and closes account-keyed services without owning Agent execution."""

    def __init__(self) -> None:
        self._factories: dict[tuple[str, str | None], Callable[[], Awaitable[object]]] = {}
        self._services: dict[tuple[str, str | None], object] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _key(provider: str, account_id: str | None = None) -> tuple[str, str | None]:
        exact_provider = str(provider or "").strip()
        exact_account = str(account_id or "").strip() or None
        if not exact_provider:
            raise ValueError("connector provider is required")
        return exact_provider, exact_account

    def register(
        self,
        provider: str,
        factory: Callable[[], Awaitable[object]],
        *,
        account_id: str | None = None,
    ) -> None:
        key = self._key(provider, account_id)
        if self._closed:
            raise RuntimeError("connector supervisor is closed")
        if key in self._factories:
            label = f"{key[0]}:{key[1]}" if key[1] is not None else key[0]
            raise ValueError(f"connector already registered: {label}")
        self._factories[key] = factory

    async def start(self) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("connector supervisor is closed")
            started: list[object] = []
            try:
                for key, factory in self._factories.items():
                    if key in self._services:
                        continue
                    service = await factory()
                    self._services[key] = service
                    started.append(service)
            except BaseException:
                for service in reversed(started):
                    await self._close_service(service)
                self._services.clear()
                raise

    async def start_provider(
        self,
        provider: str,
        *,
        account_id: str | None = None,
    ) -> object:
        """Start one provider account without changing another lifecycle."""
        key = self._key(provider, account_id)
        async with self._lock:
            if self._closed:
                raise RuntimeError("connector supervisor is closed")
            if key in self._services:
                return self._services[key]
            factory = self._factories.get(key)
            if factory is None:
                raise KeyError(key)
            service = await factory()
            self._services[key] = service
            return service

    async def stop_provider(
        self,
        provider: str,
        *,
        account_id: str | None = None,
    ) -> bool:
        """Stop one provider account without closing the supervisor."""
        key = self._key(provider, account_id)
        async with self._lock:
            service = self._services.pop(key, None)
            if service is None:
                return False
            await self._close_service(service)
            return True

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(provider for provider, _ in self._factories))

    def accounts(self, provider: str) -> tuple[str, ...]:
        exact_provider = self._key(provider)[0]
        return tuple(
            account_id
            for candidate_provider, account_id in self._factories
            if candidate_provider == exact_provider and account_id is not None
        )

    def get(self, provider: str, account_id: str | None = None) -> object | None:
        exact_provider, exact_account = self._key(provider, account_id)
        if exact_account is not None:
            return self._services.get((exact_provider, exact_account))
        singleton = self._services.get((exact_provider, None))
        if singleton is not None:
            return singleton
        matches = [
            service
            for (candidate_provider, _), service in self._services.items()
            if candidate_provider == exact_provider
        ]
        return matches[0] if len(matches) == 1 else None

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
