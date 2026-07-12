"""Owner-side revocable broker for minimum Tool Executor capabilities."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .executor_identity import ExecutorIdentity
from .executor_tokens import ExecutorCapabilityClaims, ExecutorCapabilityInvalid


@dataclass(frozen=True)
class BrokerGrant:
    """Opaque capability returned to an executor; raw broker state stays owner-side."""

    capability: str
    expires_at: int
    jti: str


class CredentialBroker:
    """Issues exact task-bound grants and invalidates them on executor revocation.

    This is intentionally process-local to the Owner Worker. A child runtime
    cannot mint grants because it receives neither signing material nor the
    registry that maps a random capability to its trusted identity.
    """

    def __init__(self, *, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._grants: dict[str, ExecutorCapabilityClaims] = {}
        self._revoked_executor_keys: set[tuple] = set()
        self._revoked_worker_generations: set[tuple[str, str, int]] = set()

    @staticmethod
    def _worker_generation_key(identity: ExecutorIdentity) -> tuple[str, str, int]:
        return (identity.owner_key, identity.worker_id, identity.worker_generation)

    def _is_revoked_locked(self, identity: ExecutorIdentity) -> bool:
        return (
            identity.stable_key in self._revoked_executor_keys
            or self._worker_generation_key(identity) in self._revoked_worker_generations
        )

    def issue(
        self,
        identity: ExecutorIdentity,
        *,
        audience: str,
        operation: str,
        scope: str,
        ttl_seconds: int = 30,
        now: int | None = None,
    ) -> BrokerGrant:
        current = int(self._clock()) if now is None else int(now)
        with self._lock:
            self._cleanup_locked(current)
            if self._is_revoked_locked(identity):
                raise ExecutorCapabilityInvalid("executor_capability_revoked")
            claims = ExecutorCapabilityClaims.issue(
                identity,
                audience=audience,
                operation=operation,
                scope=scope,
                ttl_seconds=ttl_seconds,
                now=current,
            )
            self._grants[claims.capability] = claims
            return BrokerGrant(claims.capability, claims.expires_at, claims.jti)

    def validate(
        self,
        capability: str,
        identity: ExecutorIdentity,
        *,
        audience: str,
        operation: str,
        scope: str,
        now: int | None = None,
    ) -> ExecutorCapabilityClaims:
        current = int(self._clock()) if now is None else int(now)
        with self._lock:
            self._cleanup_locked(current)
            claims = self._grants.get(str(capability or ""))
            if claims is None or self._is_revoked_locked(claims.identity):
                raise ExecutorCapabilityInvalid("executor_capability_revoked_or_unknown")
            claims.validate(
                identity,
                audience=audience,
                operation=operation,
                scope=scope,
                now=current,
            )
            return claims

    def revoke(self, capability: str) -> bool:
        with self._lock:
            return self._grants.pop(str(capability or ""), None) is not None

    def revoke_executor(self, identity: ExecutorIdentity) -> int:
        """Immediately invalidate all grants for this exact executor generation."""
        with self._lock:
            self._revoked_executor_keys.add(identity.stable_key)
            return self._revoke_matching_locked(lambda claims: claims.identity == identity)

    def revoke_worker_generation(
        self,
        *,
        owner_key: str,
        worker_generation: int,
        worker_id: str | None = None,
    ) -> int:
        """Invalidate every executor grant belonging to a stopped worker generation."""
        normalized_owner = str(owner_key or "").strip()
        normalized_generation = int(worker_generation)
        normalized_worker = str(worker_id or "").strip()
        if not normalized_owner or normalized_generation < 1 or not normalized_worker:
            raise ValueError("owner_key, worker_generation, and worker_id are required")
        generation_key = (normalized_owner, normalized_worker, normalized_generation)
        with self._lock:
            self._revoked_worker_generations.add(generation_key)

            def matches(claims: ExecutorCapabilityClaims) -> bool:
                return self._worker_generation_key(claims.identity) == generation_key

            return self._revoke_matching_locked(matches)

    def cleanup(self, *, now: int | None = None) -> int:
        with self._lock:
            return self._cleanup_locked(int(self._clock()) if now is None else int(now))

    def close(self) -> None:
        """Release all owner-local transient grant and revocation state."""
        with self._lock:
            self._grants.clear()
            self._revoked_executor_keys.clear()
            self._revoked_worker_generations.clear()

    @property
    def active_grant_count(self) -> int:
        with self._lock:
            return len(self._grants)

    def _revoke_matching_locked(self, predicate) -> int:
        revoked = [capability for capability, claims in self._grants.items() if predicate(claims)]
        for capability in revoked:
            self._grants.pop(capability)
        return len(revoked)

    def _cleanup_locked(self, now: int) -> int:
        expired = [capability for capability, claims in self._grants.items() if now >= claims.expires_at]
        for capability in expired:
            self._grants.pop(capability)
        return len(expired)
