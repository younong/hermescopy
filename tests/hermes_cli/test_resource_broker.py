from __future__ import annotations

import time
from types import MappingProxyType

import pytest

from hermes_cli.dashboard_auth.authority import (
    AuthorityStore, WorkerGenerationState, WorkerLeaseState,
)
from hermes_cli.owner_worker.cgroup_v2 import CgroupResourceEvents
from hermes_cli.owner_worker.executor_identity import ExecutorIdentity
from hermes_cli.owner_worker.resource_broker import (
    DeploymentResourceBroker, OwnerResourceBrokerClient, ResourceBrokerError,
)


class _Scope:
    def __init__(self):
        self.attached = []
        self.released = False

    def attach(self, pid):
        self.attached.append(pid)

    def verify_membership(self, pid):
        return pid in self.attached

    def read_events(self):
        return CgroupResourceEvents(
            populated=bool(self.attached), frozen=False,
            cpu=MappingProxyType({"usage_usec": 7}),
            memory=MappingProxyType({"oom_kill": 0}),
            pids=MappingProxyType({"max": 0}),
        )

    def cleanup(self):
        self.released = True


class _Manager:
    def __init__(self):
        self.admissions = []

    def admit_executor(self, identity, invocation_id):
        scope = _Scope()
        self.admissions.append((identity, invocation_id, scope))
        return scope


def _starting_lease(tmp_path):
    store = AuthorityStore(tmp_path / "control")
    claim = store.claim_worker_start("ok1_resource_owner", worker_id="worker-a")
    return store, claim.lease


def _activate(store, starting):
    return store.transition_worker_lease(
        starting, state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )


def _identity(lease):
    return ExecutorIdentity.for_task(
        lease, workspace_prefix="default", task_id="task-a",
        session_id="session-a", executor_id="executor-a",
    )


def test_private_resource_broker_round_trip_is_lease_bound_and_deidentified(tmp_path):
    store, starting = _starting_lease(tmp_path)
    manager = _Manager()
    broker = DeploymentResourceBroker(manager=manager, authority_store=store)
    child_fd = broker.register(starting)
    active = _activate(store, starting)
    broker.activate(active)
    client = OwnerResourceBrokerClient(child_fd)

    reservation = client.reserve_executor(_identity(active), "invocation-a")
    reservation.attach_pids([101, 102])

    assert reservation.verify_pids([101, 102])
    events = reservation.read_events()
    assert dict(events.cpu) == {"usage_usec": 7}
    identity, invocation_id, scope = manager.admissions[0]
    assert identity.owner_key == active.owner_key
    assert identity.worker_id == active.worker_id
    assert invocation_id == "invocation-a"
    assert scope.attached == [101, 102]

    reservation.release()
    assert scope.released
    client.close()
    broker.close()


def test_resource_broker_rejects_requests_after_durable_lease_revocation(tmp_path):
    store, starting = _starting_lease(tmp_path)
    manager = _Manager()
    broker = DeploymentResourceBroker(manager=manager, authority_store=store)
    child_fd = broker.register(starting)
    active = _activate(store, starting)
    broker.activate(active)
    client = OwnerResourceBrokerClient(child_fd)
    draining = store.transition_worker_lease(
        active, state=WorkerLeaseState.DRAINING,
        generation_state=WorkerGenerationState.DRAINING,
    )

    with pytest.raises(ResourceBrokerError, match="rejected"):
        client.reserve_executor(_identity(active), "invocation-a")

    broker.revoke(draining)
    client.close()
    broker.close()


def test_resource_broker_generation_shutdown_and_disconnect_cleanup_reservations(tmp_path):
    store, starting = _starting_lease(tmp_path)
    manager = _Manager()
    broker = DeploymentResourceBroker(manager=manager, authority_store=store)
    child_fd = broker.register(starting)
    active = _activate(store, starting)
    broker.activate(active)
    client = OwnerResourceBrokerClient(child_fd)
    client.reserve_executor(_identity(active), "invocation-a")
    first_scope = manager.admissions[-1][2]

    client.shutdown_generation()
    assert first_scope.released

    store2, starting2 = _starting_lease(tmp_path / "second")
    manager2 = _Manager()
    broker2 = DeploymentResourceBroker(manager=manager2, authority_store=store2)
    client2 = OwnerResourceBrokerClient(broker2.register(starting2))
    active2 = _activate(store2, starting2)
    broker2.activate(active2)
    client2.reserve_executor(_identity(active2), "invocation-b")
    second_scope = manager2.admissions[-1][2]
    client2.close()
    deadline = time.monotonic() + 1
    while not second_scope.released and time.monotonic() < deadline:
        time.sleep(0.01)
    assert second_scope.released
    broker2.close()
