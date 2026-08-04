"""Provider-neutral channel connector contracts and shared queue services."""

from .contracts import (
    ChannelConnector,
    DeliveryReceipt,
    InboundBatch,
    OutboundTransport,
    PollTransport,
    MediaMaterializationRequest,
    MediaMaterializer,
    NormalizedInboundEnvelope,
    OutboundDelivery,
    OutboundEncoder,
)
from .inbox import CanonicalInbox, InboundCommitResult
from .polling import (
    PollLease,
    ResolvedPollAccount,
    StalePollLeaseError,
    acquire_poll_lease,
    commit_inbound_batch,
    load_poll_account,
)
from .supervisor import ConnectorSupervisor

__all__ = [
    "CanonicalInbox",
    "ChannelConnector",
    "ConnectorSupervisor",
    "DeliveryReceipt",
    "InboundBatch",
    "InboundCommitResult",
    "MediaMaterializationRequest",
    "OutboundTransport",
    "PollLease",
    "PollTransport",
    "ResolvedPollAccount",
    "StalePollLeaseError",
    "acquire_poll_lease",
    "commit_inbound_batch",
    "load_poll_account",
    "MediaMaterializer",
    "NormalizedInboundEnvelope",
    "OutboundDelivery",
    "OutboundEncoder",
]
