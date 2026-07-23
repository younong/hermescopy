"""Narrow de-identified authority audit reporter for Owner Worker decisions."""
from __future__ import annotations

import hashlib
from typing import Callable

from hermes_cli.dashboard_auth.audit import (
    AuthorityAuditEvent,
    AuthorityAuditReason,
    audit_authority,
    new_authority_correlation_id,
)

from .cgroup_v2 import CgroupResourceEvents
from .executor_identity import ExecutorIdentity


ExecutorAuditReporter = Callable[
    [AuthorityAuditEvent, AuthorityAuditReason, ExecutorIdentity, str | None, CgroupResourceEvents | None],
    None,
]


def report_worker_lifecycle(
    event: AuthorityAuditEvent,
    reason: AuthorityAuditReason,
    *,
    worker_generation: int,
) -> None:
    """Best-effort de-identified lifecycle audit for trusted worker boundaries."""
    try:
        audit_authority(
            event,
            correlation_id=new_authority_correlation_id(),
            reason=reason,
            audience_class="browser-ws",
            worker_generation=worker_generation,
        )
    except Exception:
        return


def report_executor_authority_decision(
    event: AuthorityAuditEvent,
    reason: AuthorityAuditReason,
    identity: ExecutorIdentity,
    policy_id: str | None = None,
    resource_events: CgroupResourceEvents | None = None,
) -> None:
    """Best-effort Control Plane audit without executor input or owner identity."""
    try:
        policy_digest = None
        if policy_id:
            policy_digest = hashlib.sha256(policy_id.encode("utf-8")).hexdigest()
        cpu = resource_events.cpu if resource_events is not None else {}
        memory = resource_events.memory if resource_events is not None else {}
        pids = resource_events.pids if resource_events is not None else {}
        audit_authority(
            event,
            correlation_id=new_authority_correlation_id(),
            reason=reason,
            audience_class="none",
            worker_generation=identity.worker_generation,
            executor_generation=identity.executor_generation,
            policy_digest=policy_digest,
            cpu_nr_throttled=cpu.get("nr_throttled"),
            cpu_throttled_usec=cpu.get("throttled_usec"),
            memory_oom=memory.get("oom"),
            memory_oom_kill=memory.get("oom_kill"),
            pids_max=pids.get("max"),
        )
    except Exception:
        # Observability cannot weaken an otherwise fail-closed executor decision.
        return
