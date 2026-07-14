"""Focused unit tests for the Control Plane owner-worker WS relay."""
from __future__ import annotations

import asyncio

import pytest

from hermes_cli import web_server
from hermes_cli.dashboard_auth.audit import AuthorityAuditReason


class _Lease:
    def __init__(self) -> None:
        self.release_count = 0

    def release(self) -> None:
        self.release_count += 1


class _Peer:
    def __init__(self) -> None:
        self.closed: list[dict[str, object]] = []

    async def close(self, **kwargs: object) -> None:
        self.closed.append(kwargs)


async def _fill_queue_until_timeout() -> None:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    queue.put_nowait("occupied")
    old_timeout = web_server._OWNER_WORKER_WS_RELAY_OPERATION_TIMEOUT
    web_server._OWNER_WORKER_WS_RELAY_OPERATION_TIMEOUT = 0.01
    try:
        with pytest.raises(web_server._OwnerWorkerWsRelayClosed) as exc_info:
            await web_server._relay_queue_put(queue, "blocked")
    finally:
        web_server._OWNER_WORKER_WS_RELAY_OPERATION_TIMEOUT = old_timeout
    assert exc_info.value.code == 1013


def test_owner_worker_relay_queue_timeout_is_generic_backpressure() -> None:
    asyncio.run(_fill_queue_until_timeout())


async def _bridge_close_is_idempotent() -> tuple[_Peer, _Peer, _Lease]:
    browser, worker, lease = _Peer(), _Peer(), _Lease()
    bridge = web_server._OwnerWorkerWsBridge(browser, worker, lease)
    await bridge.close(code=4401, reason="auth: membership revoked")
    await bridge.close(code=1011, reason="ignored")
    return browser, worker, lease


def test_owner_worker_bridge_close_closes_both_halves_and_releases_once() -> None:
    browser, worker, lease = asyncio.run(_bridge_close_is_idempotent())

    assert browser.closed == [{"code": 4401, "reason": "auth: membership revoked"}]
    assert worker.closed == [{"code": 4401, "reason": "auth: membership revoked"}]
    assert lease.release_count == 1


def test_owner_worker_bridge_lifecycle_audit_is_terminal_and_exactly_once(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(web_server, "_report_bridge_lifecycle", lambda lease, reason: events.append((lease, reason)))

    browser, worker, lease = asyncio.run(_bridge_close_is_idempotent())

    assert browser.closed and worker.closed
    assert events == [(lease, AuthorityAuditReason.BRIDGE_CLOSED)]
