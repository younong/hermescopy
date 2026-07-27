#!/usr/bin/env python3
"""Gate a release on deterministic Session Reader performance contracts."""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from hermes_cli import session_api
from hermes_cli.session_reader import client as reader_client
from hermes_cli.session_reader import entrypoint
from hermes_cli.session_reader.db import ReadOnlySessionDB
from hermes_cli.session_reader.performance_contract import (
    SEARCH_MARKER,
    STANDARDS,
    expected_latest_session_id,
    populate_large_session_history,
)
from hermes_cli.session_reader.supervisor import SessionReaderSupervisor
from hermes_state import SessionDB

SCHEMA_VERSION = 1
KIND = "hermes.session-reader-performance-smoke"


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


def _record(
    checks: list[dict[str, Any]],
    name: str,
    *,
    observed: int | None = None,
    observed_ms: float | None = None,
    **details: Any,
) -> None:
    item: dict[str, Any] = {"name": name, "status": "passed", **details}
    if observed is not None:
        item["observed"] = observed
    if observed_ms is not None:
        item["observedMs"] = round(observed_ms, 3)
    checks.append(item)


def _require_latency(check: str, code: str, elapsed_ms: float, maximum_ms: float) -> None:
    if elapsed_ms >= maximum_ms:
        raise SmokeFailure(
            code,
            check,
            f"{check} took {elapsed_ms:.3f} ms; standard is < {maximum_ms:g} ms",
        )


def _resource_observations() -> dict[str, int]:
    return {
        "dbPoolSize": entrypoint._DB_POOL_SIZE,
        "serverWorkers": entrypoint._MAX_WORKERS,
        "serverInFlight": entrypoint._MAX_IN_FLIGHT,
        "clientConnections": reader_client._MAX_CONNECTIONS,
        "clientKeepaliveConnections": reader_client._MAX_KEEPALIVE_CONNECTIONS,
    }


def _require_resource_contract() -> dict[str, int]:
    observed = _resource_observations()
    expected = {
        "dbPoolSize": STANDARDS.db_pool_size,
        "serverWorkers": STANDARDS.server_workers,
        "serverInFlight": STANDARDS.server_in_flight,
        "clientConnections": STANDARDS.client_connections,
        "clientKeepaliveConnections": STANDARDS.client_keepalive_connections,
    }
    if observed != expected:
        raise SmokeFailure(
            "resource_contract_mismatch",
            "reader_resource_contract",
            f"Reader resource settings differ: observed={observed} expected={expected}",
        )
    return observed


def _query_plan_uses_parent_index(db: SessionDB) -> bool:
    details = [
        row[3]
        for row in db._conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT child.id FROM sessions parent "
            "JOIN sessions child INDEXED BY idx_sessions_parent "
            "ON child.parent_session_id = parent.id "
            "WHERE parent.end_reason = 'compression'"
        ).fetchall()
    ]
    return any("idx_sessions_parent" in detail for detail in details)


def _top_level_statements(statements: list[str]) -> int:
    return sum(not sql.lstrip().startswith("--") for sql in statements)


def _trace_call(db: ReadOnlySessionDB, call: Callable[[], Any]) -> tuple[Any, int]:
    statements: list[str] = []
    db._conn.set_trace_callback(statements.append)
    try:
        return call(), _top_level_statements(statements)
    finally:
        db._conn.set_trace_callback(None)


def _validate_page(payload: dict[str, Any]) -> None:
    sessions = payload.get("sessions")
    if (
        payload.get("total") != STANDARDS.visible_sessions
        or not isinstance(sessions, list)
        or len(sessions) != STANDARDS.page_size
        or sessions[0].get("id") != expected_latest_session_id()
    ):
        raise SmokeFailure(
            "reader_payload_invalid",
            "reader_payload",
            "Reader did not return the expected 3,000-history recent page",
        )


async def _reader_requests(
    supervisor: SessionReaderSupervisor,
    owner: dict[str, Any],
    checks: list[dict[str, Any]],
    cleanup: dict[str, bool],
    clock_ns: Callable[[], int],
) -> None:
    startup_started = clock_ns()
    try:
        handle = await asyncio.to_thread(supervisor.ensure_started, owner)
    except Exception as exc:
        raise SmokeFailure("reader_startup_failed", "reader_startup", str(exc)) from exc
    _record(checks, "reader_startup", observed_ms=_elapsed_ms(startup_started, clock_ns))
    use = None
    client = None
    try:
        cold_started = clock_ns()
        use = supervisor.acquire_active(owner)
        client = supervisor.client_for(handle)
        response = await client.request(
            "GET",
            f"/api/sessions?limit={STANDARDS.page_size}&offset=0&order=recent&compact=true",
            lease=use.lease,
        )
        cold_ms = _elapsed_ms(cold_started, clock_ns)
        if response.status_code != 200:
            raise SmokeFailure(
                "reader_payload_invalid",
                "reader_uds_cold",
                f"Cold Reader request returned HTTP {response.status_code}",
            )
        _validate_page(response.json())
        _require_latency(
            "reader_uds_cold",
            "reader_cold_latency_exceeded",
            cold_ms,
            STANDARDS.reader_cold_max_ms,
        )
        _record(
            checks,
            "reader_uds_cold",
            observed_ms=cold_ms,
            maximumMsExclusive=STANDARDS.reader_cold_max_ms,
        )
        use.release()
        use = None
        cleanup["activeUseReleased"] = True

        warm_started = clock_ns()
        response = await client.request(
            "GET",
            f"/api/sessions?limit={STANDARDS.page_size}&offset=0&order=recent&compact=true",
            lease=supervisor._lease_for_handle(handle),
        )
        warm_ms = _elapsed_ms(warm_started, clock_ns)
        if response.status_code != 200:
            raise SmokeFailure(
                "reader_payload_invalid",
                "reader_uds_warm",
                f"Warm Reader request returned HTTP {response.status_code}",
            )
        _validate_page(response.json())
        _require_latency(
            "reader_uds_warm",
            "reader_warm_latency_exceeded",
            warm_ms,
            STANDARDS.reader_warm_max_ms,
        )
        _record(
            checks,
            "reader_uds_warm",
            observed_ms=warm_ms,
            maximumMsExclusive=STANDARDS.reader_warm_max_ms,
        )
    finally:
        if use is not None:
            use.release()
            cleanup["activeUseReleased"] = True
        if client is not None:
            await supervisor.close_client(handle)
            cleanup["clientClosed"] = True


def run_smoke(
    *,
    root: Path | None = None,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> tuple[dict[str, Any], int]:
    started_ns = clock_ns()
    temporary_root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="hermes-reader-smoke-", dir="/tmp"))
    owner_home = temporary_root / "owner"
    control_home = temporary_root / "control"
    owner = {"owner_key": "ok1_release_reader_smoke", "owner_home": owner_home}
    checks: list[dict[str, Any]] = []
    cleanup = {
        "activeUseReleased": False,
        "clientClosed": False,
        "readerStopped": False,
        "databasesClosed": False,
        "socketRemoved": False,
        "temporaryRootRemoved": False,
    }
    failure: SmokeFailure | None = None
    writer = None
    reader = None
    supervisor = None
    observations: dict[str, Any] = {}
    try:
        temporary_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        owner_home.mkdir(parents=True, mode=0o700)
        observations["resources"] = _require_resource_contract()
        _record(checks, "reader_resource_contract", **observations["resources"])

        writer = SessionDB(owner_home / "state.db")
        fixture = populate_large_session_history(
            writer,
            owner_key=owner["owner_key"],
            owner_home=owner_home,
        )
        observations["fixture"] = fixture
        if fixture["visibleSessions"] != STANDARDS.visible_sessions:
            raise SmokeFailure(
                "fixture_contract_failed",
                "reader_fixture",
                "Synthetic Reader fixture has the wrong visible history count",
            )
        if not _query_plan_uses_parent_index(writer):
            raise SmokeFailure(
                "fixture_contract_failed",
                "reader_query_plan",
                "Compression child lookup does not use idx_sessions_parent",
            )
        _record(checks, "reader_fixture", **fixture)
        _record(checks, "reader_query_plan", index="idx_sessions_parent")

        scope = {
            "owner_key": owner["owner_key"],
            "workspace_root": str((owner_home / "workspaces").resolve()),
            "historical_resume": True,
        }
        reader = ReadOnlySessionDB(owner_home / "state.db")
        list_payload, list_sql = _trace_call(
            reader,
            lambda: session_api.list_sessions_payload(
                reader,
                limit=STANDARDS.page_size,
                order="recent",
                compact=False,
                recovery_scope=scope,
            ),
        )
        _validate_page(list_payload)
        if list_sql > STANDARDS.list_sql_max:
            raise SmokeFailure(
                "list_sql_budget_exceeded",
                "list_sql_budget",
                f"List used {list_sql} statements; maximum is {STANDARDS.list_sql_max}",
            )
        _record(checks, "list_sql_budget", observed=list_sql, maximum=STANDARDS.list_sql_max)

        _stats_payload, stats_sql = _trace_call(
            reader,
            lambda: session_api.stats_payload(reader, recovery_scope=scope),
        )
        if stats_sql != STANDARDS.stats_sql_exact:
            raise SmokeFailure(
                "stats_sql_budget_mismatch",
                "stats_sql_budget",
                f"Stats used {stats_sql} statements; required is {STANDARDS.stats_sql_exact}",
            )
        _record(checks, "stats_sql_budget", observed=stats_sql, exact=STANDARDS.stats_sql_exact)

        search_payload, search_sql = _trace_call(
            reader,
            lambda: session_api.search_sessions_payload(
                reader,
                q=SEARCH_MARKER,
                limit=10,
                recovery_scope=scope,
            ),
        )
        if not search_payload.get("results"):
            raise SmokeFailure(
                "reader_payload_invalid",
                "search_sql_budget",
                "Search did not return the fixture marker",
            )
        if search_sql > STANDARDS.search_sql_max:
            raise SmokeFailure(
                "search_sql_budget_exceeded",
                "search_sql_budget",
                f"Search used {search_sql} statements; maximum is {STANDARDS.search_sql_max}",
            )
        _record(checks, "search_sql_budget", observed=search_sql, maximum=STANDARDS.search_sql_max)
        observations["sql"] = {"list": list_sql, "stats": stats_sql, "search": search_sql}

        local_started = clock_ns()
        local_payload = session_api.list_sessions_payload(
            writer,
            limit=STANDARDS.page_size,
            order="recent",
            compact=True,
            recovery_scope=scope,
        )
        local_ms = _elapsed_ms(local_started, clock_ns)
        _validate_page(local_payload)
        _require_latency(
            "local_compact_listing",
            "local_listing_latency_exceeded",
            local_ms,
            STANDARDS.local_list_max_ms,
        )
        _record(
            checks,
            "local_compact_listing",
            observed_ms=local_ms,
            maximumMsExclusive=STANDARDS.local_list_max_ms,
        )
        observations["localListMs"] = round(local_ms, 3)

        reader.close()
        reader = None
        writer.close()
        writer = None
        cleanup["databasesClosed"] = True
        supervisor = SessionReaderSupervisor(
            control_home=control_home,
            global_home=temporary_root,
            startup_timeout=3,
            poll_interval=0.001,
        )
        asyncio.run(_reader_requests(supervisor, owner, checks, cleanup, clock_ns))
    except SmokeFailure as exc:
        failure = exc
    except Exception as exc:
        failure = SmokeFailure("unexpected_error", "runner", f"{type(exc).__name__}: {exc}")
    finally:
        if reader is not None:
            reader.close()
        if writer is not None:
            writer.close()
        cleanup["databasesClosed"] = True
        if supervisor is not None:
            supervisor.shutdown()
            cleanup["readerStopped"] = True
        cleanup["socketRemoved"] = not any(temporary_root.glob("**/*.sock"))
        shutil.rmtree(temporary_root, ignore_errors=True)
        cleanup["temporaryRootRemoved"] = not temporary_root.exists()

    required_cleanup = (
        cleanup["readerStopped"]
        and cleanup["databasesClosed"]
        and cleanup["socketRemoved"]
        and cleanup["temporaryRootRemoved"]
    )
    if not required_cleanup and failure is None:
        failure = SmokeFailure(
            "artifact_cleanup_failed",
            "artifact_cleanup",
            "Session Reader performance smoke cleanup was incomplete",
        )
    result: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": KIND,
        "status": "failed" if failure else "passed",
        "standards": STANDARDS.payload(),
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
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result, status = run_smoke(root=args.root)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
