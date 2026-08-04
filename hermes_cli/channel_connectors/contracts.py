"""Provider-neutral contracts at the canonical channel boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class NormalizedInboundEnvelope:
    """One transport-authenticated message for the encrypted canonical inbox."""

    provider_message_id: str
    conversation_id: str
    actor_id: str
    payload_kind: str
    payload: str
    conversation_kind: str = "direct"
    actor_display_name: str | None = None
    thread_id: str | None = None
    parent_conversation_id: str | None = None
    reply_to_message_id: str | None = None
    occurred_at: float | None = None
    context_token: str | None = None
    rejection_reason: str | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class InboundBatch:
    """A provider cursor and the normalized messages committed with it."""

    cursor: str
    messages: tuple[NormalizedInboundEnvelope, ...]


class PollTransport(Protocol):
    """Provider transport used by a fenced account poller."""

    provider: str

    async def poll(
        self,
        account: Any,
        *,
        cursor: str,
        timeout_ms: int,
    ) -> InboundBatch: ...


@dataclass(frozen=True)
class OutboundDelivery:
    provider: str
    account_id: str
    binding_id: str
    conversation_id: str
    outbound_id: str
    client_message_id: str
    payload: str
    credential_version: int
    next_part_index: int
    part_attempts: int
    context_token: str | None = None


@dataclass(frozen=True)
class DeliveryReceipt:
    provider_message_id: str | None = None
    provider_receipt: str | None = None


class OutboundTransport(Protocol):
    """Provider transport used by the canonical outbox sender."""

    provider: str

    async def send(
        self,
        account: Any,
        delivery: OutboundDelivery,
    ) -> DeliveryReceipt: ...


class ChannelConnector(Protocol):
    """Transport-only lifecycle used by the canonical connector supervisor."""

    provider: str

    async def close(self) -> None: ...


@dataclass(frozen=True)
class MediaMaterializationRequest:
    claim: Mapping[str, Any]
    owner: Any
    client: Any
    session_id: str
    payload_kind: str
    text: str
    attachments: Sequence[Mapping[str, Any]]


MediaMaterializer = Callable[[MediaMaterializationRequest], Awaitable[str]]
OutboundEncoder = Callable[[str], str]
