"""Shared Owner Worker readiness performance contract."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class OwnerWorkerPerformanceStandards:
    schema_version: int = 1
    request_ready_max_ms: float = 500.0

    def payload(self) -> dict[str, int | float]:
        return asdict(self)


STANDARDS = OwnerWorkerPerformanceStandards()
REQUEST_READY_PATHS = frozenset({"hot_active", "hot_health_probe", "wait_existing_start"})


class OwnerWorkerPerformanceError(RuntimeError):
    """Raised when a request-facing readiness sample misses its latency budget."""


def require_request_ready_latency(
    scenario: str,
    path: str,
    elapsed_ms: float,
    *,
    maximum_ms: float = STANDARDS.request_ready_max_ms,
) -> None:
    """Require one successful request-facing sample to be strictly below its budget."""
    if path not in REQUEST_READY_PATHS:
        raise ValueError(f"{scenario} is not a request-facing readiness path: {path}")
    if elapsed_ms >= maximum_ms:
        raise OwnerWorkerPerformanceError(
            f"{scenario} path={path} took {elapsed_ms:.1f} ms; "
            f"required < {maximum_ms:.1f} ms"
        )
