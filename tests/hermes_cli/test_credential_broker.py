from __future__ import annotations

from dataclasses import replace

import pytest

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState
from hermes_cli.owner_worker.credential_broker import CredentialBroker
from hermes_cli.owner_worker.executor_identity import ExecutorIdentity, ExecutorIdentityInvalid, ExecutorInvocation
from hermes_cli.owner_worker.executor_tokens import AUD_PROCESS_REGISTRY, ExecutorCapabilityInvalid


def _identity(
    *,
    owner_key="ok1_owner_a",
    task_id="task_a",
    generation=1,
    worker_id="worker-a",
    executor_id="executor-a",
    executor_generation=1,
):
    lease = OwnerWorkerAuthorityLease(owner_key, generation, worker_id, WorkerLeaseState.ACTIVE, 1, 0)
    return ExecutorIdentity.for_task(
        lease,
        workspace_prefix="default",
        task_id=task_id,
        session_id="session-a",
        executor_id=executor_id,
        executor_generation=executor_generation,
    )


def test_executor_identity_is_exact_and_serializable():
    identity = _identity()

    assert ExecutorIdentity.from_payload(identity.to_payload()) == identity
    assert identity.owner_digest != identity.owner_key
    with pytest.raises(ExecutorIdentityInvalid):
        ExecutorIdentity.from_payload({"owner_key": "missing-fields"})


def test_invocation_requires_explicit_nonambient_metadata():
    invocation = ExecutorInvocation(
        _identity(), "read_file", {"path": "README.md"}, "call-a", "turn-a", "request-a", "invoke-a"
    )

    assert invocation.to_payload()["tool_call_id"] == "call-a"
    with pytest.raises(ExecutorIdentityInvalid):
        ExecutorInvocation(_identity(), "read_file", {}, "", "turn-a", "request-a", "invoke-a")


def test_broker_enforces_exact_task_identity_generation_audience_operation_and_scope():
    broker = CredentialBroker(clock=lambda: 100)
    identity = _identity()
    grant = broker.issue(identity, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_a", ttl_seconds=20)

    claims = broker.validate(
        grant.capability, identity, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_a"
    )
    assert claims.jti == grant.jti
    assert claims.task_id == identity.task_id
    assert claims.worker_generation == identity.worker_generation
    assert claims.executor_generation == identity.executor_generation

    with pytest.raises(ExecutorCapabilityInvalid, match="scope_mismatch"):
        broker.validate(grant.capability, identity, audience=AUD_PROCESS_REGISTRY, operation="process.kill", scope="proc_a")
    for foreign in (
        _identity(owner_key="ok1_owner_b"),
        _identity(task_id="task_b"),
        _identity(generation=2),
        _identity(executor_generation=2),
    ):
        with pytest.raises(ExecutorCapabilityInvalid, match="identity_mismatch"):
            broker.validate(
                grant.capability, foreign,
                audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_a",
            )

    with pytest.raises(ExecutorCapabilityInvalid, match="identity_mismatch"):
        replace(claims, task_id="foreign-task").validate(
            identity, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_a", now=100
        )


@pytest.mark.parametrize(
    ("audience", "operation", "scope"),
    [
        ("", "process.read", "proc_a"),
        (AUD_PROCESS_REGISTRY, "", "proc_a"),
        (AUD_PROCESS_REGISTRY, "process.read", ""),
        (AUD_PROCESS_REGISTRY, "process.*", "proc_a"),
        (AUD_PROCESS_REGISTRY, "process.read", "*"),
        (AUD_PROCESS_REGISTRY, "process.read", "all"),
        (AUD_PROCESS_REGISTRY, "process.read", "proc/*"),
        (AUD_PROCESS_REGISTRY, "process.read", "proc\x00a"),
    ],
)
def test_broker_rejects_broad_or_malformed_capability_metadata(audience, operation, scope):
    broker = CredentialBroker(clock=lambda: 100)

    with pytest.raises(ExecutorCapabilityInvalid, match="metadata"):
        broker.issue(_identity(), audience=audience, operation=operation, scope=scope)


def test_revoke_executor_removes_only_exact_grants_and_blocks_reminting():
    broker = CredentialBroker(clock=lambda: 100)
    identity = _identity()
    other = _identity(task_id="task_b", executor_id="executor-b")
    first = broker.issue(identity, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_a")
    second = broker.issue(identity, audience=AUD_PROCESS_REGISTRY, operation="process.kill", scope="proc_a")
    surviving = broker.issue(other, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_b")

    assert broker.revoke_executor(identity) == 2
    assert broker.active_grant_count == 1
    assert broker.revoke_executor(identity) == 0
    for grant, operation in ((first, "process.read"), (second, "process.kill")):
        with pytest.raises(ExecutorCapabilityInvalid, match="revoked_or_unknown"):
            broker.validate(grant.capability, identity, audience=AUD_PROCESS_REGISTRY, operation=operation, scope="proc_a")
    with pytest.raises(ExecutorCapabilityInvalid, match="revoked"):
        broker.issue(identity, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_a")
    assert broker.validate(
        surviving.capability, other, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_b"
    ).capability == surviving.capability


def test_revoking_owner_a_executor_leaves_owner_b_grant_admissible():
    broker = CredentialBroker(clock=lambda: 100)
    owner_a = _identity(owner_key="ok1_owner_a", task_id="task-a", executor_id="executor-a")
    owner_b = _identity(owner_key="ok1_owner_b", task_id="task-b", executor_id="executor-b")
    grant_a = broker.issue(
        owner_a, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc-a"
    )
    grant_b = broker.issue(
        owner_b, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc-b"
    )

    assert broker.revoke_executor(owner_a) == 1
    with pytest.raises(ExecutorCapabilityInvalid, match="revoked_or_unknown"):
        broker.validate(
            grant_a.capability, owner_a, audience=AUD_PROCESS_REGISTRY,
            operation="process.read", scope="proc-a",
        )
    assert broker.validate(
        grant_b.capability, owner_b, audience=AUD_PROCESS_REGISTRY,
        operation="process.read", scope="proc-b",
    ).capability == grant_b.capability


def test_generation_stop_invalidates_existing_and_future_exact_generation_grants():
    broker = CredentialBroker(clock=lambda: 100)
    stopped = _identity()
    fresh_stopped = _identity(task_id="task_fresh", executor_id="executor-fresh")
    different_worker = _identity(task_id="task_worker", worker_id="worker-b", executor_id="executor-worker")
    different_generation = _identity(task_id="task_generation", generation=2, executor_id="executor-generation")
    stopped_grant = broker.issue(stopped, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_a")
    worker_grant = broker.issue(different_worker, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_worker")
    generation_grant = broker.issue(different_generation, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_generation")

    assert broker.revoke_worker_generation(
        owner_key=stopped.owner_key,
        worker_generation=stopped.worker_generation,
        worker_id=stopped.worker_id,
    ) == 1
    with pytest.raises(ExecutorCapabilityInvalid, match="revoked_or_unknown"):
        broker.validate(stopped_grant.capability, stopped, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_a")
    with pytest.raises(ExecutorCapabilityInvalid, match="revoked"):
        broker.issue(fresh_stopped, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_fresh")
    assert broker.validate(
        worker_grant.capability, different_worker, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_worker"
    ).capability == worker_grant.capability
    assert broker.validate(
        generation_grant.capability, different_generation,
        audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_generation",
    ).capability == generation_grant.capability


def test_expired_grants_are_unusable_at_expiry_boundary_and_cleaned_up():
    now = [100]
    broker = CredentialBroker(clock=lambda: now[0])
    identity = _identity()
    grant = broker.issue(identity, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_a", ttl_seconds=1)
    now[0] = 101

    with pytest.raises(ExecutorCapabilityInvalid, match="revoked_or_unknown"):
        broker.validate(grant.capability, identity, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_a")
    assert broker.active_grant_count == 0


def test_close_clears_broker_grants_and_revocation_fences():
    broker = CredentialBroker(clock=lambda: 100)
    identity = _identity()
    broker.issue(identity, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_a")
    broker.revoke_executor(identity)

    broker.close()
    broker.close()

    assert broker.active_grant_count == 0
    assert broker.issue(identity, audience=AUD_PROCESS_REGISTRY, operation="process.read", scope="proc_a")
