from __future__ import annotations

import threading
import time
from types import MappingProxyType

import pytest

from hermes_cli.dashboard_auth.authority import (
    AuthorityStore, WorkerGenerationState, WorkerLeaseState,
)
from hermes_cli.owner_worker.cgroup_v2 import (
    CgroupAdmissionRejected,
    CgroupResourceEvents,
    CgroupV2Unavailable,
)
from hermes_cli.owner_worker.executor_identity import ExecutorIdentity
from hermes_cli.owner_worker.resource_broker import (
    DeploymentResourceBroker,
    OwnerResourceBrokerClient,
    ResourceBrokerError,
    ResourceBrokerReason,
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
    def __init__(self, *, admission_error=None):
        self.admissions = []
        self.admission_error = admission_error

    def admit_executor(self, identity, invocation_id):
        if self.admission_error is not None:
            raise self.admission_error
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


def _join_peer_thread(peer):
    peer.thread.join(timeout=5)
    assert not peer.thread.is_alive(), "resource broker peer thread did not stop"


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


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (CgroupAdmissionRejected("capacity secret /owner/private"), ResourceBrokerReason.ADMISSION_REJECTED),
        (CgroupV2Unavailable("cgroup path /sys/fs/cgroup/private"), ResourceBrokerReason.CGROUP_UNAVAILABLE),
        (RuntimeError("unexpected secret /tmp/private"), ResourceBrokerReason.INTERNAL_REJECTION),
    ],
)
def test_resource_broker_redacts_admission_failure_reason(tmp_path, error, reason):
    store, starting = _starting_lease(tmp_path)
    broker = DeploymentResourceBroker(
        manager=_Manager(admission_error=error), authority_store=store,
    )
    client = OwnerResourceBrokerClient(broker.register(starting))
    active = _activate(store, starting)
    broker.activate(active)

    with pytest.raises(ResourceBrokerError) as raised:
        client.reserve_executor(_identity(active), "invocation-secret")

    assert raised.value.reason == reason
    assert "secret" not in str(raised.value)
    assert "/" not in str(raised.value)
    client.close()
    broker.close()


def test_resource_broker_rejects_unknown_response_reason(monkeypatch):
    from hermes_cli.owner_worker import resource_broker

    class _Connection:
        def sendall(self, value):
            del value

    client = object.__new__(OwnerResourceBrokerClient)
    client._connection = _Connection()
    client._lock = threading.Lock()
    client._closed = False
    monkeypatch.setattr(resource_broker, "_recv_frame", lambda connection: {"ok": False, "reason": "raw-secret"})
    monkeypatch.setattr(resource_broker, "_send_frame", lambda connection, request: None)

    with pytest.raises(ResourceBrokerError) as raised:
        client._request({"operation": "test"})

    assert raised.value.reason == ResourceBrokerReason.INTERNAL_REJECTION
    assert "raw-secret" not in str(raised.value)


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

    with pytest.raises(ResourceBrokerError, match="lease is not active") as raised:
        client.reserve_executor(_identity(active), "invocation-a")
    assert raised.value.reason == ResourceBrokerReason.LEASE_INACTIVE

    broker.revoke(draining)
    client.close()
    broker.close()


def test_resource_broker_generation_shutdown_is_idempotent_after_peer_disconnect(tmp_path):
    store, starting = _starting_lease(tmp_path)
    manager = _Manager()
    broker = DeploymentResourceBroker(manager=manager, authority_store=store)
    client = OwnerResourceBrokerClient(broker.register(starting))
    active = _activate(store, starting)
    broker.activate(active)
    client.reserve_executor(_identity(active), "invocation-a")
    scope = manager.admissions[-1][2]

    broker.revoke(active)
    deadline = time.monotonic() + 1
    while not scope.released and time.monotonic() < deadline:
        time.sleep(0.01)

    assert scope.released
    client.shutdown_generation()
    client.shutdown_generation()
    with pytest.raises(ResourceBrokerError, match="unavailable"):
        client.reserve_executor(_identity(active), "invocation-b")
    broker.close()


def test_resource_broker_retries_failed_reservation_cleanup_on_revoke(tmp_path):
    store, starting = _starting_lease(tmp_path)
    manager = _Manager()
    broker = DeploymentResourceBroker(manager=manager, authority_store=store)
    client = OwnerResourceBrokerClient(broker.register(starting))
    active = _activate(store, starting)
    broker.activate(active)
    peer = broker._peers[broker._key(active)]
    client.reserve_executor(_identity(active), "invocation-a")
    scope = manager.admissions[-1][2]
    cleanup_calls = 0
    allow_cleanup = threading.Event()

    def cleanup():
        nonlocal cleanup_calls
        cleanup_calls += 1
        if not allow_cleanup.is_set():
            raise OSError("scope cleanup failed")
        scope.released = True

    scope.cleanup = cleanup

    with pytest.raises(OSError, match="scope cleanup failed"):
        broker.revoke(active)
    _join_peer_thread(peer)

    assert cleanup_calls == 2
    assert scope.released is False
    assert broker._cleanup_peers == {broker._generation_key(active): peer}

    allow_cleanup.set()
    broker.revoke(active)

    assert cleanup_calls == 3
    assert scope.released is True
    assert broker._cleanup_peers == {}
    client.close()
    broker.close()


def test_resource_broker_close_retries_retained_failed_reservation_cleanup(tmp_path):
    store, starting = _starting_lease(tmp_path)
    manager = _Manager()
    broker = DeploymentResourceBroker(manager=manager, authority_store=store)
    client = OwnerResourceBrokerClient(broker.register(starting))
    active = _activate(store, starting)
    broker.activate(active)
    peer = broker._peers[broker._key(active)]
    client.reserve_executor(_identity(active), "invocation-a")
    scope = manager.admissions[-1][2]
    cleanup_calls = 0
    allow_cleanup = threading.Event()

    def cleanup():
        nonlocal cleanup_calls
        cleanup_calls += 1
        if not allow_cleanup.is_set():
            raise OSError("scope cleanup failed")
        scope.released = True

    scope.cleanup = cleanup

    with pytest.raises(ResourceBrokerError, match="resource broker cleanup failed"):
        broker.close()
    _join_peer_thread(peer)

    assert cleanup_calls == 2
    assert scope.released is False
    assert broker._cleanup_peers == {broker._generation_key(active): peer}

    allow_cleanup.set()
    broker.close()

    assert cleanup_calls == 3
    assert scope.released is True
    assert broker._cleanup_peers == {}
    client.close()


def test_resource_broker_generation_shutdown_preserves_protocol_rejection(tmp_path):
    store, starting = _starting_lease(tmp_path)
    broker = DeploymentResourceBroker(manager=_Manager(), authority_store=store)
    client = OwnerResourceBrokerClient(broker.register(starting))
    active = _activate(store, starting)
    broker.activate(active)
    store.transition_worker_lease(
        active,
        state=WorkerLeaseState.DRAINING,
        generation_state=WorkerGenerationState.DRAINING,
    )

    with pytest.raises(ResourceBrokerError, match="lease is not active") as raised:
        client.shutdown_generation()
    assert raised.value.reason == ResourceBrokerReason.LEASE_INACTIVE
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
