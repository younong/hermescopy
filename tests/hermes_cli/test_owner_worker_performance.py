from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_cli.latency_trace import latency_trace_scope
from hermes_cli.owner_worker.performance_contract import (
    OwnerWorkerPerformanceError,
    require_ready_latency,
)
from hermes_cli.owner_worker.supervisor import OwnerWorkerSupervisor


@dataclass(frozen=True)
class _Owner:
    owner_key: str
    owner_home: Path
    tenant_id: str = "tenant-1"
    owner_user_id: str = "user-1"
    auth_provider: str = "test"


class _FakeProcess:
    returncode = None

    def __init__(self) -> None:
        self.pid = 4321

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 143

    def kill(self) -> None:
        self.returncode = 137

    def wait(self, timeout=None):
        del timeout
        return self.returncode


class _FakeClient:
    def __init__(self, socket_path, *, control_home=None, timeout=2.0) -> None:
        del control_home, timeout
        self.socket_path = Path(socket_path)

    def verify_health(
        self,
        *,
        owner_key: str,
        owner_home,
        worker_generation=None,
        worker_id=None,
        **_kwargs,
    ):
        return {
            "ready": True,
            "owner_key": owner_key,
            "owner_home": str(Path(owner_home).resolve()),
            "worker_generation": worker_generation,
            "worker_id": worker_id,
            "pid": 4321,
            "hermes_home": str(Path(owner_home).resolve()),
        }


@pytest.fixture(autouse=True)
def _simulate_linux_controlled_roots(monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots
    import hermes_cli.owner_worker.supervisor as supervisor_module

    monkeypatch.setattr(controlled_roots.ControlledRoots, "_require_linux", lambda _self: None)
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)
    monkeypatch.setattr(
        supervisor_module,
        "_seed_owner_worker_skills",
        lambda _owner_home: {"copied": [], "updated": []},
    )


_READY_RE = re.compile(
    r"stage=owner_worker\.ready elapsed_ms=(?P<elapsed>[0-9.]+) "
    r"outcome=ok path=(?P<path>[a-z_]+)$"
)


def _ready_sample(caplog) -> tuple[str, float]:
    matches = [match for message in caplog.messages if (match := _READY_RE.search(message))]
    assert len(matches) == 1
    return matches[0].group("path"), float(matches[0].group("elapsed"))


def _supervisor(tmp_path: Path) -> tuple[OwnerWorkerSupervisor, _Owner]:
    owner = _Owner("ok1_performance", tmp_path / "owner")

    def process_factory(*args, **_kwargs):
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    return (
        OwnerWorkerSupervisor(
            control_home=tmp_path / "control",
            client_cls=_FakeClient,
            process_factory=process_factory,
            startup_timeout=1,
            startup_cooldown=0,
        ),
        owner,
    )


def _trace_ready(supervisor, owner, caplog, trace_id: str):
    logger = logging.getLogger("tests.owner-worker-performance")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=logger.name):
        with latency_trace_scope(logger, trace_id=trace_id, surface="owner-ws-bridge"):
            return supervisor.get_or_start(owner)


def test_ready_latency_contract_is_strictly_below_one_second():
    for path in (
        "hot_active",
        "hot_health_probe",
        "wait_existing_start",
        "cold_start",
        "replace_unhealthy",
    ):
        require_ready_latency(path, path, 999.999)
        with pytest.raises(OwnerWorkerPerformanceError, match="required < 1000.0 ms"):
            require_ready_latency(path, path, 1000.0)


def test_ready_latency_contract_rejects_unknown_path():
    with pytest.raises(ValueError, match="not an owner-worker readiness path"):
        require_ready_latency("gui", "unknown", 1.0)


def test_hot_active_and_health_probe_meet_request_budget(tmp_path, caplog):
    supervisor, owner = _supervisor(tmp_path)
    handle = supervisor.get_or_start(owner)

    lease = supervisor.acquire_use(handle)
    assert _trace_ready(supervisor, owner, caplog, "trace-hot-active-123") is handle
    path, elapsed_ms = _ready_sample(caplog)
    assert path == "hot_active"
    require_ready_latency("hot active", path, elapsed_ms)
    lease.release()

    assert _trace_ready(supervisor, owner, caplog, "trace-hot-probe-123") is handle
    path, elapsed_ms = _ready_sample(caplog)
    assert path == "hot_health_probe"
    require_ready_latency("hot health probe", path, elapsed_ms)

    supervisor.shutdown()


def test_warmup_follower_meets_request_budget(tmp_path, caplog):
    owner = _Owner("ok1_warmup_performance", tmp_path / "owner")
    startup_entered = threading.Event()
    release_startup = threading.Event()

    def process_factory(*args, **_kwargs):
        startup_entered.set()
        assert release_startup.wait(timeout=2)
        argv = args[0]
        Path(argv[argv.index("--socket") + 1]).touch()
        return _FakeProcess()

    supervisor = OwnerWorkerSupervisor(
        control_home=tmp_path / "control",
        client_cls=_FakeClient,
        process_factory=process_factory,
        startup_timeout=1,
        startup_cooldown=0,
    )
    warmup = threading.Thread(target=supervisor.get_or_start, args=(owner,))
    warmup.start()
    assert startup_entered.wait(timeout=2)

    result = []
    logger = logging.getLogger("tests.owner-worker-performance")
    follower_waiting = threading.Event()
    condition_wait = supervisor._start_finished.wait

    def observed_condition_wait(timeout=None):
        follower_waiting.set()
        return condition_wait(timeout=timeout)

    supervisor._start_finished.wait = observed_condition_wait

    def wait_for_warmup() -> None:
        with latency_trace_scope(
            logger,
            trace_id="trace-warmup-wait-123",
            surface="owner-ws-bridge",
        ):
            result.append(supervisor.get_or_start(owner))

    follower = threading.Thread(target=wait_for_warmup)
    with caplog.at_level(logging.INFO, logger=logger.name):
        follower.start()
        assert follower_waiting.wait(timeout=1)
        release_startup.set()
        warmup.join(timeout=2)
        follower.join(timeout=2)

    assert not warmup.is_alive()
    assert not follower.is_alive()
    assert len(result) == 1
    path, elapsed_ms = _ready_sample(caplog)
    assert path == "wait_existing_start"
    require_ready_latency("warmup follower", path, elapsed_ms)

    supervisor.shutdown()
