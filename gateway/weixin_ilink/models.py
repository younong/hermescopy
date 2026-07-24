"""Typed values returned by the Tencent WeChat iLink transport."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class QRCodeStatus(str, Enum):
    WAITING = "waiting"
    SCANNED = "scanned"
    REDIRECT = "redirect"
    EXPIRED = "expired"
    CONFIRMED = "confirmed"


@dataclass(frozen=True)
class QRCode:
    token: str
    content: str


@dataclass(frozen=True)
class ILinkCredentials:
    bot_id: str
    bot_token: str
    base_url: str
    user_id: str


@dataclass(frozen=True)
class QRStatus:
    status: QRCodeStatus
    redirect_host: str = ""
    credentials: ILinkCredentials | None = None


@dataclass(frozen=True)
class UpdateBatch:
    messages: tuple[Mapping[str, Any], ...]
    cursor: str
    long_poll_timeout_ms: int | None = None


class ILinkTransportError(RuntimeError):
    """A sanitized transport/protocol error safe to log.

    Request credentials, QR/context tokens, and provider response bodies are
    deliberately never attached to this exception.
    """

    def __init__(
        self,
        operation: str,
        reason: str,
        *,
        http_status: int | None = None,
    ) -> None:
        self.operation = operation
        self.reason = reason
        self.http_status = http_status
        status = f" HTTP {http_status}" if http_status is not None else ""
        super().__init__(f"iLink {operation}{status}: {reason}")
