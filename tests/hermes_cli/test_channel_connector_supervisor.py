"""Tests for provider-neutral connector contracts and lifecycle."""

from __future__ import annotations

import pytest

from hermes_cli.channel_connectors import ConnectorSupervisor, NormalizedInboundEnvelope


class _Service:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_normalized_envelope_carries_conversation_and_actor_identity():
    envelope = NormalizedInboundEnvelope(
        provider_message_id="message-1",
        conversation_id="channel-1",
        conversation_kind="channel",
        actor_id="user-1",
        actor_display_name="Example User",
        thread_id="thread-1",
        parent_conversation_id="workspace-1",
        reply_to_message_id="message-0",
        occurred_at=1.5,
        payload_kind="text",
        payload="hello",
    )

    assert envelope.conversation_id == "channel-1"
    assert envelope.actor_id == "user-1"
    assert envelope.thread_id == "thread-1"


@pytest.mark.asyncio
async def test_connector_supervisor_starts_once_and_closes_services():
    supervisor = ConnectorSupervisor()
    service = _Service()
    starts = 0

    async def factory():
        nonlocal starts
        starts += 1
        return service

    supervisor.register("example", factory)
    await supervisor.start()
    await supervisor.start()

    assert starts == 1
    assert supervisor.get("example") is service
    await supervisor.close()
    assert service.closed is True


@pytest.mark.asyncio
async def test_connector_supervisor_stops_one_account_without_affecting_another():
    supervisor = ConnectorSupervisor()
    first = _Service()
    second = _Service()

    async def first_factory():
        return first

    async def second_factory():
        return second

    supervisor.register("feishu", first_factory, account_id="first")
    supervisor.register("feishu", second_factory, account_id="second")
    await supervisor.start_provider("feishu", account_id="first")
    await supervisor.start_provider("feishu", account_id="second")

    assert await supervisor.stop_provider("feishu", account_id="first") is True
    assert first.closed is True
    assert second.closed is False
    assert supervisor.get("feishu", "first") is None
    assert supervisor.get("feishu", "second") is second


@pytest.mark.asyncio
async def test_connector_supervisor_rolls_back_partial_startup():
    supervisor = ConnectorSupervisor()
    service = _Service()

    async def first():
        return service

    async def fail():
        raise RuntimeError("start failed")

    supervisor.register("first", first)
    supervisor.register("second", fail)

    with pytest.raises(RuntimeError, match="start failed"):
        await supervisor.start()

    assert service.closed is True
    assert supervisor.get("first") is None


def test_connector_supervisor_rejects_duplicate_provider():
    supervisor = ConnectorSupervisor()

    async def factory():
        return _Service()

    supervisor.register("example", factory)
    with pytest.raises(ValueError, match="already registered"):
        supervisor.register("example", factory)
