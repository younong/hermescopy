from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx


def test_session_reader_client_classifies_transport_errors(monkeypatch, tmp_path):
    from hermes_cli.session_reader.client import (
        SessionReaderClient,
        SessionReaderHealthError,
    )

    class _Client:
        async def request(self, *args, **kwargs):
            raise httpx.ReadTimeout("private sentinel")

    client = SessionReaderClient(tmp_path / "reader.sock")
    client._client = _Client()

    async def run():
        try:
            await client.request("GET", "/api/sessions", lease=SimpleNamespace())
        except SessionReaderHealthError as exc:
            assert exc.failure_code == "timeout"
            assert "private sentinel" not in str(exc)
        else:
            raise AssertionError("expected health error")

    asyncio.run(run())


def test_session_reader_lifecycle_backs_off_request_failures():
    from hermes_cli.session_reader.readiness import SessionReaderLifecycle

    class _Supervisor:
        idle_timeout = 1800

        def __init__(self):
            self.failed = []
            self.successful = []

        def report_request_failure(self, lease, reason="other"):
            self.failed.append((lease, reason))
            return len(self.failed) == 1

        def report_request_success(self, lease):
            self.successful.append(lease)
            return True

    async def run():
        supervisor = _Supervisor()
        lifecycle = SessionReaderLifecycle(supervisor, initial_backoff=1, max_backoff=4)
        owner = SimpleNamespace(owner_key="ok1_test")
        lifecycle.observe_verified_owner(owner)
        lifecycle._startups.clear()
        observed = lifecycle._owners[owner.owner_key]
        lease = SimpleNamespace(owner_key=owner.owner_key, reader_generation=1)
        lifecycle.report_request_failure(lease, "timeout")
        assert observed.request_failures == 1
        assert observed.request_retry_at > 0
        lifecycle.report_request_failure(lease, "timeout")
        assert observed.request_failures == 1
        lifecycle.report_request_success(lease)
        assert observed.request_failures == 0
        assert observed.request_retry_at == 0
        await lifecycle.close()

    asyncio.run(run())
