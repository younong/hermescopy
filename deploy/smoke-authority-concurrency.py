#!/usr/bin/env python3
"""Gate a release on isolated Dashboard authority concurrency contracts."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import stat
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Barrier
from typing import Any, Callable, Sequence

from hermes_cli.dashboard_auth.authority import (
    AuthorizationRejected,
    AuthorizationScope,
    AuthorityStore,
    WorkerGenerationState,
    WorkerLeaseState,
)

SCHEMA_VERSION = 1
KIND = "hermes.authority-concurrency-smoke"
_AUTHORITY_SCHEMA_VERSION = 10
_WORKERS = 8
_WAIT_SECONDS = 15
_REQUIRED_TABLES = frozenset({
    "authority_meta",
    "authorization_scopes",
    "consumed_credentials",
    "authority_changes",
    "owner_worker_generations",
    "owner_worker_leases",
    "owner_worker_bootstrap_consumptions",
    "owner_worker_changes",
    "session_reader_generations",
    "session_reader_leases",
    "authenticated_owners",
    "machine_credentials",
})


class SmokeFailure(RuntimeError):
    def __init__(self, code: str, check: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.check = check


def _bounded(value: object, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _elapsed_ms(started_ns: int, clock_ns: Callable[[], int]) -> float:
    return (clock_ns() - started_ns) / 1_000_000


def _record(checks: list[dict[str, Any]], name: str, **details: Any) -> None:
    checks.append({"name": name, "status": "passed", **details})


def _path_absent(path: Path) -> bool:
    try:
        path.stat()
    except (FileNotFoundError, PermissionError):
        return True
    return False


def _scope() -> AuthorizationScope:
    return AuthorizationScope(
        "release-smoke",
        "isolated-tenant",
        "isolated-user",
        "isolated-session",
        "revision-1",
    )


def _require_isolated_environment(root: Path) -> None:
    parent = root.parent.resolve()
    if root.exists() or root.is_symlink():
        raise SmokeFailure(
            "unsafe_smoke_root",
            "environment_isolation",
            "Authority smoke root must not already exist",
        )
    try:
        parent_status = parent.stat()
    except OSError as exc:
        raise SmokeFailure(
            "unsafe_smoke_root",
            "environment_isolation",
            "Authority smoke parent is unavailable",
        ) from exc
    if not stat.S_ISDIR(parent_status.st_mode):
        raise SmokeFailure(
            "unsafe_smoke_root",
            "environment_isolation",
            "Authority smoke parent must be a directory",
        )
    if os.name != "nt" and parent_status.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SmokeFailure(
            "unsafe_smoke_root",
            "environment_isolation",
            "Authority smoke parent permissions are too broad",
        )
    for name in ("HOME", "TMPDIR", "HERMES_HOME"):
        value = os.environ.get(name)
        if not value or Path(value).resolve() != parent:
            raise SmokeFailure(
                "environment_not_isolated",
                "environment_isolation",
                f"{name} is not bound to the isolated smoke directory",
            )


def _wait_for(futures: Sequence[Future[Any]], *, check: str) -> list[Any]:
    done, pending = wait(futures, timeout=_WAIT_SECONDS)
    if pending:
        for future in pending:
            future.cancel()
        raise SmokeFailure(
            "concurrency_timeout",
            check,
            f"{check} did not finish within the bounded wait",
        )
    return [future.result() for future in futures]


def _initialize_worker(
    control_home: Path,
    barrier: Barrier,
    scope: AuthorizationScope,
) -> tuple[int, int]:
    store = AuthorityStore(control_home)
    barrier.wait(timeout=_WAIT_SECONDS)
    store.ensure_ready()
    state = store.read_state(scope)
    return state.epoch, state.recovery_generation


def _consume_browser(
    control_home: Path,
    barrier: Barrier,
    scope: AuthorizationScope,
    epoch: int,
    recovery_generation: int,
) -> str:
    store = AuthorityStore(control_home)
    barrier.wait(timeout=_WAIT_SECONDS)
    try:
        store.check_and_consume(
            scope,
            token_class="browser-ws",
            issuer_key_version="release-smoke-key",
            jti="release-smoke-browser-credential",
            audience="browser-ws:/api/ws",
            expires_at=1_000,
            claim_epoch=epoch,
            claim_recovery_generation=recovery_generation,
            now=999,
        )
    except AuthorizationRejected as exc:
        if exc.code == "credential_replayed":
            return "replayed"
        raise
    return "accepted"


def _consume_bootstrap(control_home: Path, barrier: Barrier, lease: Any) -> str:
    store = AuthorityStore(control_home)
    barrier.wait(timeout=_WAIT_SECONDS)
    try:
        store.check_and_consume_owner_worker_bootstrap(
            lease,
            issuer_key_version="release-smoke-worker-key",
            jti="release-smoke-worker-bootstrap",
            audience="owner-worker-uds-bootstrap",
            expires_at=1_000,
            now=999,
        )
    except AuthorizationRejected as exc:
        if exc.code == "credential_replayed":
            return "replayed"
        raise
    return "accepted"


def _race_two(
    executor: ThreadPoolExecutor,
    function: Callable[..., str],
    *args: Any,
    check: str,
) -> list[str]:
    barrier = Barrier(3)
    futures = [executor.submit(function, *args, barrier) for _ in range(2)]
    barrier.wait(timeout=_WAIT_SECONDS)
    return _wait_for(futures, check=check)


def _validate_database(
    store: AuthorityStore,
    checks: list[dict[str, Any]],
    observations: dict[str, Any],
) -> None:
    with sqlite3.connect(store.path, timeout=5, isolation_level=None) as conn:
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or int(checkpoint[0]) != 0:
            raise SmokeFailure(
                "checkpoint_busy",
                "authority_checkpoint",
                "Authority WAL checkpoint did not complete",
            )
        checkpoint_observation = {
            "busy": int(checkpoint[0]),
            "logFrames": int(checkpoint[1]),
            "checkpointedFrames": int(checkpoint[2]),
        }
        observations["checkpoint"] = checkpoint_observation
        _record(checks, "authority_checkpoint", **checkpoint_observation)

        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise SmokeFailure(
                "integrity_check_failed",
                "authority_integrity",
                "Authority integrity check did not return ok",
            )
        observations["integrity"] = "ok"
        _record(checks, "authority_integrity", result="ok")

        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        values = dict(
            conn.execute(
                "SELECT key, value FROM authority_meta WHERE key IN "
                "('schema_version', 'recovery_required')"
            ).fetchall()
        )
        try:
            schema_version = int(values["schema_version"])
            recovery_required = int(values["recovery_required"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SmokeFailure(
                "authority_metadata_invalid",
                "authority_schema",
                "Authority metadata is incomplete",
            ) from exc
        if schema_version != _AUTHORITY_SCHEMA_VERSION or not _REQUIRED_TABLES.issubset(tables):
            raise SmokeFailure(
                "authority_schema_invalid",
                "authority_schema",
                "Authority schema is incomplete or unsupported",
            )
        observations["schemaVersion"] = schema_version
        observations["requiredTableCount"] = len(_REQUIRED_TABLES)
        _record(
            checks,
            "authority_schema",
            schemaVersion=schema_version,
            requiredTableCount=len(_REQUIRED_TABLES),
        )
        if recovery_required != 0:
            raise SmokeFailure(
                "recovery_required",
                "authority_recovery_state",
                "Authority unexpectedly requires recovery",
            )
        observations["recoveryRequired"] = recovery_required
        _record(checks, "authority_recovery_state", recoveryRequired=0)


def run_smoke(
    *,
    root: Path,
    fault: str | None = None,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> tuple[dict[str, Any], int]:
    started_ns = clock_ns()
    temporary_root = Path(root).resolve()
    control_home = temporary_root / "control-plane"
    checks: list[dict[str, Any]] = []
    observations: dict[str, Any] = {}
    cleanup = {
        "executorStopped": False,
        "temporaryRootRemoved": False,
    }
    failure: SmokeFailure | None = None
    executor: ThreadPoolExecutor | None = None
    created_root = False
    try:
        _require_isolated_environment(temporary_root)
        temporary_root.mkdir(mode=0o700)
        created_root = True
        control_home.mkdir(mode=0o700)
        _record(checks, "environment_isolation")

        scope = _scope()
        executor = ThreadPoolExecutor(
            max_workers=_WORKERS,
            thread_name_prefix="authority-smoke",
        )
        initialization_barrier = Barrier(_WORKERS + 1)
        initialization = [
            executor.submit(
                _initialize_worker,
                control_home,
                initialization_barrier,
                scope,
            )
            for _ in range(_WORKERS)
        ]
        initialization_barrier.wait(timeout=_WAIT_SECONDS)
        states = _wait_for(initialization, check="concurrent_initialization")
        if len(set(states)) != 1:
            raise SmokeFailure(
                "authority_state_diverged",
                "concurrent_initialization",
                "Concurrent stores observed divergent authority state",
            )
        epoch, recovery_generation = states[0]
        observations["initializationWorkers"] = len(states)
        _record(
            checks,
            "concurrent_initialization",
            workers=len(states),
        )

        visible = AuthorityStore(control_home).read_state(scope)
        if (visible.epoch, visible.recovery_generation) != states[0]:
            raise SmokeFailure(
                "authority_state_not_visible",
                "scope_visibility",
                "A fresh store did not observe the activated scope",
            )
        _record(checks, "scope_visibility")

        browser_results = _race_two(
            executor,
            lambda home, scope_value, epoch_value, generation_value, barrier:
                _consume_browser(
                    home,
                    barrier,
                    scope_value,
                    epoch_value,
                    generation_value,
                ),
            control_home,
            scope,
            epoch,
            recovery_generation,
            check="browser_exact_once",
        )
        if sorted(browser_results) != ["accepted", "replayed"]:
            raise SmokeFailure(
                "browser_exact_once_failed",
                "browser_exact_once",
                "Browser credential did not produce one admission and one replay",
            )
        observations["browserAccepted"] = 1
        observations["browserReplayed"] = 1
        _record(checks, "browser_exact_once", accepted=1, replayed=1)

        owner_key = "release-smoke-owner"
        claim = AuthorityStore(control_home).claim_worker_start(
            owner_key,
            worker_id="release-smoke-worker",
        )
        active = AuthorityStore(control_home).transition_worker_lease(
            claim.lease,
            state=WorkerLeaseState.ACTIVE,
            generation_state=WorkerGenerationState.ACTIVE,
        )
        if AuthorityStore(control_home).read_owner_worker_lease(owner_key) != active:
            raise SmokeFailure(
                "worker_lease_not_visible",
                "worker_lifecycle",
                "A fresh store did not observe the active Worker lease",
            )

        bootstrap_results = _race_two(
            executor,
            lambda home, lease, barrier: _consume_bootstrap(home, barrier, lease),
            control_home,
            active,
            check="worker_bootstrap_exact_once",
        )
        if sorted(bootstrap_results) != ["accepted", "replayed"]:
            raise SmokeFailure(
                "worker_bootstrap_exact_once_failed",
                "worker_bootstrap_exact_once",
                "Worker bootstrap did not produce one admission and one replay",
            )
        observations["bootstrapAccepted"] = 1
        observations["bootstrapReplayed"] = 1
        _record(checks, "worker_bootstrap_exact_once", accepted=1, replayed=1)

        draining = AuthorityStore(control_home).transition_worker_lease(
            active,
            state=WorkerLeaseState.DRAINING,
            generation_state=WorkerGenerationState.DRAINING,
        )
        revoked = AuthorityStore(control_home).transition_worker_lease(
            draining,
            state=WorkerLeaseState.REVOKED,
            generation_state=WorkerGenerationState.TERMINATED,
        )
        observed_lease = AuthorityStore(control_home).read_owner_worker_lease(owner_key)
        changes = AuthorityStore(control_home).worker_changes_since(0)
        if observed_lease != revoked or len(changes) != 3:
            raise SmokeFailure(
                "worker_lifecycle_invalid",
                "worker_lifecycle",
                "Worker lease lifecycle or shared change feed is incomplete",
            )
        observations["workerTransitions"] = len(changes)
        _record(checks, "worker_lifecycle", transitions=len(changes))

        if fault == "after_concurrency":
            raise SmokeFailure(
                "injected_failure",
                "fault_injection",
                "Injected authority smoke failure",
            )

        store = AuthorityStore(control_home)
        _validate_database(store, checks, observations)
        recovery_backups = list(control_home.glob("authority.sqlite3.corrupt.*.bak"))
        if store.recovery_marker_path.exists() or recovery_backups:
            raise SmokeFailure(
                "recovery_artifact_created",
                "recovery_artifacts",
                "Authority smoke created a recovery marker or forensic backup",
            )
        observations["recoveryArtifacts"] = 0
        _record(checks, "recovery_artifacts", observed=0)
    except SmokeFailure as exc:
        failure = exc
    except Exception as exc:
        failure = SmokeFailure(
            "unexpected_error",
            "runner",
            f"Unexpected {type(exc).__name__} in authority smoke",
        )
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        cleanup["executorStopped"] = True
        if created_root:
            shutil.rmtree(temporary_root, ignore_errors=True)
        cleanup["temporaryRootRemoved"] = _path_absent(temporary_root)

    if not all(cleanup.values()) and failure is None:
        failure = SmokeFailure(
            "artifact_cleanup_failed",
            "artifact_cleanup",
            "Authority concurrency smoke cleanup was incomplete",
        )
    if all(cleanup.values()):
        _record(checks, "artifact_cleanup")
    result: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": KIND,
        "status": "failed" if failure else "passed",
        "observations": observations,
        "checks": checks,
        "cleanup": cleanup,
        "durationMs": round(_elapsed_ms(started_ns, clock_ns), 3),
    }
    if failure:
        result["failure"] = {
            "code": failure.code,
            "check": failure.check,
            "message": _bounded(failure),
        }
    return result, 1 if failure else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help=argparse.SUPPRESS)
    parser.add_argument(
        "--test-fault",
        choices=("after_concurrency",),
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result, status = run_smoke(root=args.root, fault=args.test_fault)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
