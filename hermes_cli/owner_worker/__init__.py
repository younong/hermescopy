"""Per-owner worker process scaffolding for authenticated dashboard mode."""
from __future__ import annotations

from typing import Any

__all__ = [
    "OwnerWorkerClient",
    "OwnerWorkerHandle",
    "OwnerWorkerHealthError",
    "OwnerWorkerSupervisor",
    "OwnerWorkerStartupError",
    "OwnerWorkerUnavailableError",
    "OwnerWorkerCapabilityClaims",
    "OwnerWorkerCapabilityInvalid",
    "mint_owner_worker_capability",
    "verify_owner_worker_capability",
]


def __getattr__(name: str) -> Any:
    if name in {"OwnerWorkerClient", "OwnerWorkerHealthError"}:
        from .client import OwnerWorkerClient, OwnerWorkerHealthError

        return {
            "OwnerWorkerClient": OwnerWorkerClient,
            "OwnerWorkerHealthError": OwnerWorkerHealthError,
        }[name]
    if name in {
        "OwnerWorkerHandle",
        "OwnerWorkerSupervisor",
        "OwnerWorkerStartupError",
        "OwnerWorkerUnavailableError",
    }:
        from .supervisor import (
            OwnerWorkerHandle,
            OwnerWorkerStartupError,
            OwnerWorkerSupervisor,
            OwnerWorkerUnavailableError,
        )

        return {
            "OwnerWorkerHandle": OwnerWorkerHandle,
            "OwnerWorkerSupervisor": OwnerWorkerSupervisor,
            "OwnerWorkerStartupError": OwnerWorkerStartupError,
            "OwnerWorkerUnavailableError": OwnerWorkerUnavailableError,
        }[name]
    if name in {
        "OwnerWorkerCapabilityClaims",
        "OwnerWorkerCapabilityInvalid",
        "mint_owner_worker_capability",
        "verify_owner_worker_capability",
    }:
        from .tokens import (
            OwnerWorkerCapabilityClaims,
            OwnerWorkerCapabilityInvalid,
            mint_owner_worker_capability,
            verify_owner_worker_capability,
        )

        return {
            "OwnerWorkerCapabilityClaims": OwnerWorkerCapabilityClaims,
            "OwnerWorkerCapabilityInvalid": OwnerWorkerCapabilityInvalid,
            "mint_owner_worker_capability": mint_owner_worker_capability,
            "verify_owner_worker_capability": verify_owner_worker_capability,
        }[name]
    raise AttributeError(name)
