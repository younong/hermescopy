"""Shared Owner Worker readiness performance contract."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class OwnerWorkerPerformanceStandards:
    schema_version: int = 3
    ready_max_ms: float = 1000.0

    def payload(self) -> dict[str, int | float]:
        return asdict(self)


STANDARDS = OwnerWorkerPerformanceStandards()
READINESS_PATHS = frozenset({
    "hot_active",
    "hot_health_probe",
    "wait_existing_start",
    "cold_start",
    "replace_unhealthy",
})


class OwnerWorkerPerformanceError(RuntimeError):
    """Raised when a successful readiness sample misses its latency budget."""


def require_ready_latency(
    scenario: str,
    path: str,
    elapsed_ms: float,
    *,
    maximum_ms: float = STANDARDS.ready_max_ms,
) -> None:
    """Require one successful readiness sample to be strictly below its budget."""
    if path not in READINESS_PATHS:
        raise ValueError(f"{scenario} is not an owner-worker readiness path: {path}")
    if elapsed_ms >= maximum_ms:
        raise OwnerWorkerPerformanceError(
            f"{scenario} path={path} took {elapsed_ms:.1f} ms; "
            f"required < {maximum_ms:.1f} ms"
        )
