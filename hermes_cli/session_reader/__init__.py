"""Lightweight owner-scoped Session Reader service."""
from __future__ import annotations

from typing import Any

__all__ = [
    "SessionReaderClient",
    "SessionReaderHandle",
    "SessionReaderHealthError",
    "SessionReaderStartupError",
    "SessionReaderSupervisor",
    "SessionReaderUnavailableError",
]


def __getattr__(name: str) -> Any:
    if name in {"SessionReaderClient", "SessionReaderHealthError"}:
        from .client import SessionReaderClient, SessionReaderHealthError

        return {
            "SessionReaderClient": SessionReaderClient,
            "SessionReaderHealthError": SessionReaderHealthError,
        }[name]
    if name in {
        "SessionReaderHandle",
        "SessionReaderStartupError",
        "SessionReaderSupervisor",
        "SessionReaderUnavailableError",
    }:
        from .supervisor import (
            SessionReaderHandle,
            SessionReaderStartupError,
            SessionReaderSupervisor,
            SessionReaderUnavailableError,
        )

        return {
            "SessionReaderHandle": SessionReaderHandle,
            "SessionReaderStartupError": SessionReaderStartupError,
            "SessionReaderSupervisor": SessionReaderSupervisor,
            "SessionReaderUnavailableError": SessionReaderUnavailableError,
        }[name]
    raise AttributeError(name)
