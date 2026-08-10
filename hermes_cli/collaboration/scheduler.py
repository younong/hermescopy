"""Capacity-bounded persistent scheduler for internal collaboration turns."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import ExpectedType, RootKind
from hermes_state import SessionDB

from .resolver import CollaborationEmployeeResolver, collaboration_member_policy
from .store import CollaborationStore, aggregate_collaboration_turn

_log = logging.getLogger(__name__)
_TERMINAL_TARGET_STATUSES = frozenset(
    {"completed", "failed", "timed_out", "ambiguous", "cancelled"}
)


class CollaborationPromptRunner(Protocol):
    def run(
        self,
        *,
        stored_session_id: str,
        hidden_session_id: str,
        employee_policy: dict[str, Any],
        prompt: str,
        target_id: str,
        external_receipt_key: str,
        on_delta: Callable[[str], None],
        on_approval: Callable[[dict[str, Any]], None],
        collaboration_context: Any = None,
    ) -> dict[str, Any]: ...

    def ensure_coordinator_session(
        self, *, task_id: str, employee_policy: dict[str, Any]
    ) -> tuple[str, str]: ...

    def interrupt(self, hidden_session_id: str) -> bool: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class WorkerFence:
    owner_key: str
    worker_generation: int
    worker_id: str
    lease_version: int
    recovery_generation: int

    @classmethod
    def from_runtime(cls, runtime: Any) -> "WorkerFence":
        return cls(
            owner_key=str(runtime.owner_key),
            worker_generation=int(runtime.worker_generation),
            worker_id=str(runtime.worker_id),
            lease_version=int(runtime.lease_version),
            recovery_generation=int(runtime.recovery_generation),
        )

    def values(self) -> tuple[Any, ...]:
        return (
            self.owner_key,
            self.worker_id,
            self.worker_generation,
            self.lease_version,
            self.recovery_generation,
        )


class CollaborationScheduler:
    """Claim durable targets onto a fixed worker pool, never one thread per target."""

    def __init__(
        self,
        db: SessionDB,
        *,
        store: CollaborationStore,
        resolver: CollaborationEmployeeResolver,
        runner: CollaborationPromptRunner,
        runtime: Any,
        emit: Callable[[str, dict[str, Any]], None],
        capacity: int = 4,
        active_budget_seconds: float = 300.0,
        poll_seconds: float = 0.1,
    ) -> None:
        self.db = db
        self.store = store
        self.resolver = resolver
        self.runner = runner
        self.runtime = runtime
        self.fence = WorkerFence.from_runtime(runtime)
        if self.fence.owner_key != store.owner_key:
            raise RuntimeError("collaboration scheduler owner mismatch")
        self.emit = emit
        self.capacity = max(1, int(capacity))
        self.active_budget_seconds = max(1.0, float(active_budget_seconds))
        self.poll_seconds = max(0.02, float(poll_seconds))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._pool = ThreadPoolExecutor(
            max_workers=self.capacity,
            thread_name_prefix="collaboration-target",
        )
        self._active_lock = threading.Lock()
        self._active: dict[str, str] = {}
        self._coordinating: set[str] = set()
        self._budget_conditions: dict[str, threading.Condition] = {}
        self._thread: threading.Thread | None = None
        self._closed = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._recover_uncertain_targets()
        self._thread = threading.Thread(
            target=self._loop,
            name="collaboration-scheduler",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()
        with self._active_lock:
            hidden_ids = tuple(self._active.values())
        for hidden_id in hidden_ids:
            try:
                self.runner.interrupt(hidden_id)
            except Exception:
                pass
        self._pool.shutdown(wait=True, cancel_futures=True)
        self.runner.close()

    def request_approval(
        self,
        *,
        target_id: str,
        tool_call_id: str,
        tool_name: str,
        request: dict[str, Any],
    ) -> str:
        from agent.redact import redact_sensitive_text

        approval_id = f"cap_{__import__('uuid').uuid4().hex}"
        now = time.time()
        summary = redact_sensitive_text(str(request.get("description") or "Approval required"))
        safe_request = {
            "summary": summary,
            "description": summary,
            "tool_name": str(tool_name),
            "allow_permanent": bool(request.get("allow_permanent", False)),
        }

        def _write(conn):
            row = self._owned_target(conn, target_id)
            if row is None or row["status"] != "running":
                raise RuntimeError("collaboration approval target is unavailable")
            elapsed = max(0.0, now - float(row["active_started_at"] or now))
            changed = conn.execute(
                "UPDATE collaboration_turn_targets SET status='waiting_approval', "
                "active_seconds=active_seconds+?, active_started_at=NULL, updated_at=? "
                "WHERE target_id=? AND status='running' AND worker_owner_key=? "
                "AND worker_id=? AND worker_generation=? AND lease_version=? "
                "AND recovery_generation=?",
                (elapsed, now, target_id, *self.fence.values()),
            ).rowcount
            if changed != 1:
                raise RuntimeError("collaboration approval fence is no longer valid")
            conn.execute(
                "INSERT INTO collaboration_approvals "
                "(approval_id, target_id, tool_call_id, tool_name, request_json, status, "
                "worker_owner_key, worker_id, worker_generation, lease_version, "
                "recovery_generation, created_at, updated_at) VALUES "
                "(?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    target_id,
                    str(tool_call_id),
                    str(tool_name),
                    json.dumps(safe_request, ensure_ascii=False, separators=(",", ":")),
                    *self.fence.values(),
                    now,
                    now,
                ),
            )
            return dict(self._owned_target(conn, target_id))

        target = self.db._execute_write(_write)
        self._notify_budget_state(target_id)
        payload = {
            "approval_id": approval_id,
            "group_id": str(target["group_id"]),
            "turn_id": str(target["turn_id"]),
            "target_id": target_id,
            "execution_id": str(target["execution_id"]),
            "status": "pending",
            "request": safe_request,
        }
        self.emit("collaboration.approval.changed", payload)
        self.emit("collaboration.target.changed", self._public_target(target))
        return approval_id

    def respond_approval(self, approval_id: str, choice: str) -> dict[str, Any]:
        if choice not in {"once", "session", "always", "deny"}:
            raise ValueError("approval choice is invalid")
        now = time.time()

        def _write(conn):
            approval = conn.execute(
                "SELECT a.*, tt.execution_id, tt.turn_id, t.group_id "
                "FROM collaboration_approvals a "
                "JOIN collaboration_turn_targets tt ON tt.target_id=a.target_id "
                "JOIN collaboration_turns t ON t.turn_id=tt.turn_id "
                "JOIN collaboration_groups g ON g.group_id=t.group_id "
                "WHERE a.approval_id=? AND g.owner_key=?",
                (str(approval_id), self.fence.owner_key),
            ).fetchone()
            if approval is None:
                raise RuntimeError("collaboration approval is unavailable")
            if approval["status"] != "pending":
                raise RuntimeError("collaboration approval is no longer pending")
            if tuple(approval[key] for key in ("worker_owner_key", "worker_id", "worker_generation", "lease_version", "recovery_generation")) != self.fence.values():
                raise RuntimeError("collaboration approval callback is no longer live")
            target = self._owned_target(conn, str(approval["target_id"]))
            if target is None or target["status"] != "waiting_approval":
                raise RuntimeError("collaboration approval callback is no longer live")
            status = "denied" if choice == "deny" else "approved"
            conn.execute(
                "UPDATE collaboration_approvals SET status=?, decided_at=?, updated_at=? WHERE approval_id=? AND status='pending'",
                (status, now, now, approval_id),
            )
            conn.execute(
                "UPDATE collaboration_turn_targets SET status='running', active_started_at=?, updated_at=? WHERE target_id=? AND status='waiting_approval'",
                (now, now, approval["target_id"]),
            )
            return dict(approval), dict(self._owned_target(conn, str(approval["target_id"])))

        approval, target = self.db._execute_write(_write)
        self._notify_budget_state(str(target["target_id"]))
        from tools.approval import resolve_gateway_approval

        with self.db._lock:
            membership = self.db._conn.execute(
                "SELECT stored_session_id FROM collaboration_memberships WHERE membership_id=?",
                (target["membership_id"],),
            ).fetchone()
        resolved = (
            resolve_gateway_approval(
                str(membership["stored_session_id"]),
                choice,
                tool_call_id=str(approval["tool_call_id"]),
            )
            if membership
            else 0
        )
        if resolved == 0:
            approval, target = self._mark_approval_ambiguous(
                approval_id,
                str(target["target_id"]),
            )
            self._notify_budget_state(str(target["target_id"]))
            self.emit("collaboration.approval.changed", approval)
            self.emit("collaboration.target.changed", self._public_target(target))
            raise RuntimeError("collaboration approval callback is no longer live")
        payload = {
            "approval_id": str(approval["approval_id"]),
            "group_id": str(approval["group_id"]),
            "turn_id": str(approval["turn_id"]),
            "target_id": str(approval["target_id"]),
            "execution_id": str(approval["execution_id"]),
            "status": "denied" if choice == "deny" else "approved",
        }
        self.emit("collaboration.approval.changed", payload)
        self.emit("collaboration.target.changed", self._public_target(target))
        return payload

    def _mark_approval_ambiguous(
        self,
        approval_id: str,
        target_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        now = time.time()

        def _write(conn):
            approval = conn.execute(
                "SELECT a.approval_id, tt.execution_id, tt.turn_id, t.group_id "
                "FROM collaboration_approvals a "
                "JOIN collaboration_turn_targets tt ON tt.target_id=a.target_id "
                "JOIN collaboration_turns t ON t.turn_id=tt.turn_id "
                "JOIN collaboration_groups g ON g.group_id=t.group_id "
                "WHERE a.approval_id=? AND a.target_id=? AND g.owner_key=?",
                (approval_id, target_id, self.fence.owner_key),
            ).fetchone()
            if approval is None:
                raise RuntimeError("collaboration approval is unavailable")
            conn.execute(
                "UPDATE collaboration_approvals SET status='ambiguous', updated_at=? "
                "WHERE approval_id=?",
                (now, approval_id),
            )
            conn.execute(
                "UPDATE collaboration_turn_targets SET status='ambiguous', "
                "error='approval callback outcome is uncertain', active_started_at=NULL, "
                "completed_at=?, updated_at=? WHERE target_id=? "
                "AND status IN ('running','waiting_approval')",
                (now, now, target_id),
            )
            aggregate_collaboration_turn(conn, str(approval["turn_id"]), now)
            target = self._owned_target(conn, target_id)
            if target is None:
                raise RuntimeError("collaboration approval target is unavailable")
            return (
                {
                    "approval_id": str(approval["approval_id"]),
                    "group_id": str(approval["group_id"]),
                    "turn_id": str(approval["turn_id"]),
                    "target_id": target_id,
                    "execution_id": str(approval["execution_id"]),
                    "status": "ambiguous",
                },
                dict(target),
            )

        return self.db._execute_write(_write)

    def interrupt_session(self, hidden_session_id: str) -> bool:
        """Best-effort interrupt an already-cancelled live member session."""
        return self.runner.interrupt(hidden_session_id)

    def interrupt(self, target_id: str) -> dict[str, Any]:
        target_id = str(target_id or "").strip()
        if not target_id:
            raise ValueError("target ID is required")
        now = time.time()

        def _write(conn):
            row = self._owned_target(conn, target_id)
            if row is None:
                raise RuntimeError("collaboration target is unavailable")
            if row["status"] in _TERMINAL_TARGET_STATUSES:
                return dict(row), None
            changed = conn.execute(
                "UPDATE collaboration_turn_targets SET status='cancelled', error=?, "
                "active_seconds=active_seconds + CASE WHEN active_started_at IS NULL "
                "THEN 0 ELSE MAX(0, ? - active_started_at) END, active_started_at=NULL, "
                "completed_at=?, updated_at=? WHERE target_id=? AND status IN "
                "('queued','running','waiting_approval')",
                ("interrupted", now, now, now, target_id),
            ).rowcount
            if changed != 1:
                return dict(self._owned_target(conn, target_id)), None
            updated = dict(self._owned_target(conn, target_id))
            aggregate_collaboration_turn(conn, str(updated["turn_id"]), now)
            membership = conn.execute(
                "SELECT hidden_session_id FROM collaboration_memberships WHERE membership_id=?",
                (updated["membership_id"],),
            ).fetchone()
            return updated, str(membership["hidden_session_id"])

        row, hidden_id = self.db._execute_write(_write)
        self._notify_budget_state(target_id)
        if hidden_id:
            self.runner.interrupt(hidden_id)
        with self._active_lock:
            active_hidden_id = self._active.get(target_id)
        if active_hidden_id and active_hidden_id != hidden_id:
            self.runner.interrupt(active_hidden_id)
        payload = self._public_target(row)
        self.emit("collaboration.target.changed", payload)
        return payload

    def _notify_budget_state(self, target_id: str) -> None:
        with self._active_lock:
            condition = self._budget_conditions.get(target_id)
        if condition is not None:
            with condition:
                condition.notify_all()

    def turn_status(self, turn_id: str) -> dict[str, Any]:
        with self.db._lock:
            turn = self.db._conn.execute(
                "SELECT t.* FROM collaboration_turns t JOIN collaboration_groups g "
                "ON g.group_id=t.group_id WHERE t.turn_id=? AND g.owner_key=?",
                (str(turn_id), self.fence.owner_key),
            ).fetchone()
            if turn is None:
                raise RuntimeError("collaboration turn is unavailable")
            targets = self.db._conn.execute(
                "SELECT tt.*, t.group_id FROM collaboration_turn_targets tt "
                "JOIN collaboration_turns t ON t.turn_id=tt.turn_id "
                "WHERE tt.turn_id=? ORDER BY tt.created_at, tt.account_id",
                (turn_id,),
            ).fetchall()
        return {
            "turn_id": str(turn["turn_id"]),
            "group_id": str(turn["group_id"]),
            "snapshot_sequence": int(turn["snapshot_sequence"]),
            "status": str(turn["status"]),
            "targets": [self._public_target(dict(row)) for row in targets],
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._active_lock:
                available = self.capacity - len(self._active)
            for _ in range(max(0, available)):
                claimed = self._claim_next()
                if claimed is None:
                    break
                hidden_id = str(claimed["hidden_session_id"])
                with self._active_lock:
                    self._active[str(claimed["target_id"])] = hidden_id
                self._pool.submit(self._execute_claimed, claimed)
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def _claim_next(self) -> dict[str, Any] | None:
        with self.db._lock:
            candidate = self.db._conn.execute(
                "SELECT tt.target_id, tt.account_id, m.profile_revision, "
                "m.profile_fingerprint FROM collaboration_turn_targets tt "
                "JOIN collaboration_turns t ON t.turn_id=tt.turn_id "
                "JOIN collaboration_groups g ON g.group_id=t.group_id "
                "JOIN collaboration_memberships m ON m.membership_id=tt.membership_id "
                "WHERE g.owner_key=? AND g.status='active' AND tt.status='queued' "
                "AND NOT EXISTS (SELECT 1 FROM collaboration_turn_targets active "
                "WHERE active.membership_id=tt.membership_id "
                "AND active.status IN ('running','waiting_approval')) "
                "ORDER BY tt.created_at, tt.target_id LIMIT 1",
                (self.fence.owner_key,),
            ).fetchone()
        if candidate is None:
            return None
        try:
            resolved = self.resolver.resolve_pinned(
                account_id=str(candidate["account_id"]),
                profile_revision=int(candidate["profile_revision"]),
                profile_fingerprint=str(candidate["profile_fingerprint"]),
            )
        except Exception as exc:
            self._reject_candidate(str(candidate["target_id"]), str(exc))
            return None
        now = time.time()

        def _write(conn):
            row = self._owned_target(conn, str(candidate["target_id"]))
            if row is None or row["status"] != "queued":
                return None
            membership = conn.execute(
                "SELECT * FROM collaboration_memberships WHERE membership_id=?",
                (row["membership_id"],),
            ).fetchone()
            group = conn.execute(
                "SELECT status FROM collaboration_groups WHERE group_id=? AND owner_key=?",
                (row["group_id"], self.fence.owner_key),
            ).fetchone()
            if (
                membership is None
                or membership["leave_sequence"] is not None
                or int(membership["profile_revision"]) != resolved.member.profile_revision
                or str(membership["profile_fingerprint"])
                != resolved.member.profile_fingerprint
                or group is None
                or group["status"] != "active"
            ):
                conn.execute(
                    "UPDATE collaboration_turn_targets SET status='failed', error=?, "
                    "completed_at=?, updated_at=? WHERE target_id=? AND status='queued'",
                    ("collaboration membership is no longer eligible", now, now, row["target_id"]),
                )
                aggregate_collaboration_turn(conn, str(row["turn_id"]), now)
                return None
            changed = conn.execute(
                "UPDATE collaboration_turn_targets SET status='running', "
                "active_started_at=?, attempt=attempt+1, worker_owner_key=?, "
                "worker_id=?, worker_generation=?, lease_version=?, "
                "recovery_generation=?, updated_at=? WHERE target_id=? AND status='queued'",
                (
                    now,
                    *self.fence.values(),
                    now,
                    row["target_id"],
                ),
            ).rowcount
            if changed != 1:
                return None
            conn.execute(
                "UPDATE collaboration_turns SET status='running', worker_owner_key=?, "
                "worker_id=?, worker_generation=?, lease_version=?, recovery_generation=?, "
                "updated_at=? WHERE turn_id=? AND status='queued'",
                (*self.fence.values(), now, row["turn_id"]),
            )
            claimed = dict(self._owned_target(conn, str(row["target_id"])))
            claimed["hidden_session_id"] = str(membership["hidden_session_id"])
            claimed["stored_session_id"] = str(membership["stored_session_id"])
            claimed["employee_policy"] = collaboration_member_policy(
                resolved.employee_policy,
                str(membership["membership_id"]),
            )
            claimed["may_create_groups"] = bool(resolved.may_create_groups)
            return claimed

        claimed = self.db._execute_write(_write)
        if claimed is not None:
            self.emit("collaboration.target.changed", self._public_target(claimed))
        return claimed

    def _execute_claimed(self, claimed: dict[str, Any]) -> None:
        hidden_id = str(claimed["hidden_session_id"])
        target_id = str(claimed["target_id"])
        receipt_key = f"collaboration:{claimed['execution_id']}"
        try:
            prompt = self._context_prompt(claimed)
            receipt = self.db.begin_external_turn(
                turn_key=receipt_key,
                stored_session_id=str(claimed["stored_session_id"]),
                worker_id=self.fence.worker_id,
                worker_generation=self.fence.worker_generation,
            )
            if receipt.get("status") == "ambiguous":
                self._finish(target_id, status="ambiguous", error="external turn outcome is ambiguous")
                return
            if receipt.get("status") == "completed":
                self._commit_completed(
                    target_id,
                    receipt_key=receipt_key,
                    text=str(receipt.get("result_text") or ""),
                    result_status=str(receipt.get("result_status") or "complete"),
                )
                return
            budget_condition = threading.Condition()
            budget_stop = threading.Event()
            budget_race_lock = threading.Lock()
            budget_winner: list[str | None] = [None]
            with self._active_lock:
                self._budget_conditions[target_id] = budget_condition

            def _watch_active_budget() -> None:
                while not budget_stop.is_set():
                    with self.db._lock:
                        row = self.db._conn.execute(
                            "SELECT status, active_seconds, active_started_at "
                            "FROM collaboration_turn_targets WHERE target_id=?",
                            (target_id,),
                        ).fetchone()
                    if row is None or row["status"] not in {"running", "waiting_approval"}:
                        return
                    if row["status"] == "waiting_approval":
                        wait_for = self.poll_seconds
                    else:
                        elapsed = max(
                            0.0,
                            time.time() - float(row["active_started_at"] or time.time()),
                        )
                        remaining = (
                            self.active_budget_seconds
                            - float(row["active_seconds"])
                            - elapsed
                        )
                        if remaining <= 0:
                            with budget_race_lock:
                                if budget_winner[0] is None:
                                    budget_winner[0] = "timeout"
                                    self.runner.interrupt(hidden_id)
                            return
                        wait_for = min(remaining, self.poll_seconds)
                    with budget_condition:
                        budget_condition.wait(timeout=wait_for)

            budget_thread = threading.Thread(
                target=_watch_active_budget,
                name=f"collaboration-budget-{target_id}",
                daemon=True,
            )
            budget_thread.start()
            try:
                result = self.runner.run(
                    stored_session_id=str(claimed["stored_session_id"]),
                    hidden_session_id=hidden_id,
                    employee_policy=dict(claimed["employee_policy"]),
                    prompt=prompt,
                    target_id=target_id,
                    external_receipt_key=receipt_key,
                    collaboration_context=self._agent_context(claimed),
                    on_delta=lambda text: self.emit(
                        "collaboration.execution.delta",
                        {
                            "group_id": str(claimed["group_id"]),
                            "turn_id": str(claimed["turn_id"]),
                            "target_id": target_id,
                            "execution_id": str(claimed["execution_id"]),
                            "text": str(text),
                        },
                    ),
                    on_approval=lambda data: self.request_approval(
                        target_id=target_id,
                        tool_call_id=str(data.get("tool_call_id") or f"approval-{time.time_ns()}"),
                        tool_name=str(data.get("tool_name") or "terminal"),
                        request=data,
                    ),
                )
                with budget_race_lock:
                    if budget_winner[0] is None:
                        budget_winner[0] = "result"
            finally:
                budget_stop.set()
                with budget_condition:
                    budget_condition.notify_all()
                budget_thread.join()
                with self._active_lock:
                    self._budget_conditions.pop(target_id, None)
            status = str(result.get("status") or "complete")
            text = str(result.get("text") or "")
            target_status = (
                "timed_out"
                if budget_winner[0] == "timeout"
                else "completed"
                if status == "complete"
                else "cancelled"
                if status == "interrupted"
                else "failed"
            )
            if target_status == "completed":
                self._commit_completed(
                    target_id,
                    receipt_key=receipt_key,
                    text=text,
                    result_status=status,
                )
            else:
                self.db.complete_external_turn(
                    turn_key=receipt_key,
                    worker_id=self.fence.worker_id,
                    worker_generation=self.fence.worker_generation,
                    result_text=text,
                    result_status=status,
                )
                self._finish(
                    target_id,
                    status=target_status,
                    text=text,
                    result_status=status,
                    error=text or status,
                )
        except Exception as exc:
            _log.warning("collaboration target execution failed", exc_info=True)
            self._finish(target_id, status="failed", error=str(exc))
        finally:
            with self._active_lock:
                self._active.pop(target_id, None)
            self._schedule_coordinator_if_terminal(str(claimed["turn_id"]))
            self.wake()

    def _schedule_coordinator_if_terminal(self, turn_id: str) -> None:
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT t.status, t.group_id, task.task_id FROM collaboration_turns t "
                "JOIN collaboration_groups g ON g.group_id=t.group_id "
                "LEFT JOIN collaboration_tasks task ON task.group_id=t.group_id "
                "WHERE t.turn_id=? AND g.owner_key=?",
                (turn_id, self.fence.owner_key),
            ).fetchone()
        if (
            row is None
            or row["task_id"] is None
            or str(row["status"])
            not in {"completed", "partial", "failed", "ambiguous", "cancelled"}
        ):
            return
        task_id = str(row["task_id"])
        with self._active_lock:
            if task_id in self._coordinating:
                return
            self._coordinating.add(task_id)
        try:
            self._pool.submit(self._run_creator_coordinator, task_id, turn_id)
        except Exception:
            with self._active_lock:
                self._coordinating.discard(task_id)
            raise

    def _run_creator_coordinator(self, task_id: str, turn_id: str) -> None:
        before_round: int | None = None
        receipt_key: str | None = None
        receipt_claimed = False
        try:
            with self.db._lock:
                row = self.db._conn.execute(
                    "SELECT t.*, o.provider, o.account_id AS origin_account_id, o.binding_id, "
                    "o.conversation_id, o.thread_id, o.source_session_id, o.source_group_id "
                    "FROM collaboration_tasks t "
                    "JOIN collaboration_origins o ON o.group_id=t.group_id "
                    "JOIN collaboration_groups g ON g.group_id=t.group_id "
                    "WHERE t.task_id=? AND g.owner_key=?",
                    (task_id, self.fence.owner_key),
                ).fetchone()
                turn = self.db._conn.execute(
                    "SELECT ct.status, ce.body_json FROM collaboration_turns ct "
                    "JOIN collaboration_events ce ON ce.event_id=ct.event_id "
                    "WHERE ct.turn_id=? AND ct.group_id=?",
                    (turn_id, row["group_id"] if row is not None else ""),
                ).fetchone()
            if row is None or turn is None or str(row["status"]) != "open":
                return
            turn_body = json.loads(str(turn["body_json"] or "{}"))
            before_round = int(turn_body.get("ai_round") or 0)
            if before_round < 1:
                raise RuntimeError("creator coordinator source round is invalid")
            resolved = self.resolver.resolve_pinned(
                account_id=str(row["creator_account_id"]),
                profile_revision=int(row["creator_profile_revision"]),
                profile_fingerprint=str(row["creator_profile_fingerprint"]),
            )
            if not resolved.may_participate:
                raise RuntimeError("collaboration participation is revoked")
            from .agent_tools import CollaborationAgentContext

            context = CollaborationAgentContext(
                service=getattr(self.runner, "service", None),
                creator_account_id=str(row["creator_account_id"]),
                source_kind=str(row["source_kind"]),
                source_conversation_id=str(row["conversation_id"]),
                source_provider=str(row["provider"]),
                source_account_id=(
                    str(row["origin_account_id"])
                    if row["origin_account_id"] is not None
                    else None
                ),
                source_binding_id=(
                    str(row["binding_id"]) if row["binding_id"] is not None else None
                ),
                source_thread_id=str(row["thread_id"] or ""),
                source_session_id=(
                    str(row["source_session_id"])
                    if row["source_session_id"] is not None
                    else None
                ),
                source_group_id=(
                    str(row["source_group_id"])
                    if row["source_group_id"] is not None
                    else None
                ),
                source_event_id=(
                    str(row["source_event_id"])
                    if row["source_event_id"] is not None
                    else None
                ),
                source_task_id=(
                    str(row["source_task_id"])
                    if row["source_task_id"] is not None
                    else None
                ),
                source_depth=int(row["depth"]),
                allowed_origin_attachment_ids=tuple(
                    json.loads(str(row["allowed_attachment_ids_json"] or "[]"))
                ),
                task_id=task_id,
                role="coordinator",
            )
            prompt = self._coordinator_prompt(dict(row), turn_id, str(turn["status"]))
            coordinator_policy = collaboration_member_policy(
                resolved.employee_policy, str(row["creator_membership_id"])
            )
            stored_session_id, hidden_session_id = self.runner.ensure_coordinator_session(
                task_id=task_id,
                employee_policy=coordinator_policy,
            )
            receipt_key = f"collaboration-coordinator:{task_id}:{turn_id}"
            receipt = self.db.begin_external_turn(
                turn_key=receipt_key,
                stored_session_id=stored_session_id,
                worker_id=self.fence.worker_id,
                worker_generation=self.fence.worker_generation,
            )
            if receipt.get("status") == "ambiguous":
                self._mark_task_ambiguous(
                    task_id,
                    "creator coordination outcome is ambiguous",
                )
                return
            if receipt.get("status") == "completed":
                self._require_coordinator_action(task_id, before_round=before_round)
                return
            receipt_claimed = True
            result = self.runner.run(
                stored_session_id=stored_session_id,
                hidden_session_id=hidden_session_id,
                employee_policy=coordinator_policy,
                prompt=prompt,
                target_id=f"coordinator:{task_id}:{turn_id}",
                external_receipt_key=receipt_key,
                collaboration_context=context,
                on_delta=lambda _text: None,
                on_approval=lambda _data: None,
            )
            self._require_coordinator_action(task_id, before_round=before_round)
            self.db.complete_external_turn(
                turn_key=receipt_key,
                worker_id=self.fence.worker_id,
                worker_generation=self.fence.worker_generation,
                result_text=str(result.get("text") or ""),
                result_status=str(result.get("status") or "complete"),
            )
        except Exception as exc:
            _log.warning("collaboration creator coordination failed", exc_info=True)
            if receipt_claimed and receipt_key is not None:
                try:
                    self.db.complete_external_turn(
                        turn_key=receipt_key,
                        worker_id=self.fence.worker_id,
                        worker_generation=self.fence.worker_generation,
                        result_text=str(exc),
                        result_status="ambiguous",
                    )
                except Exception:
                    _log.warning(
                        "failed to terminally receipt creator coordination",
                        exc_info=True,
                    )
            self._mark_task_ambiguous(task_id, str(exc))
        finally:
            with self._active_lock:
                self._coordinating.discard(task_id)

    def _require_coordinator_action(self, task_id: str, *, before_round: int) -> None:
        task = self.store.ai_task(task_id)
        dispatched = str(task["status"]) == "open" and int(task["round"]) == before_round + 1
        finished = str(task["status"]) == "completed" and int(task["round"]) == before_round
        if dispatched == finished:
            raise RuntimeError("creator coordinator must dispatch or finish exactly once")

    def _mark_task_ambiguous(self, task_id: str, reason: str) -> None:
        try:
            result, changed = self.store.mark_ai_task_ambiguous(
                task_id,
                reason=reason,
            )
            if changed and result.get("event_id"):
                event = self.store.list_events_payload(
                    result["group_id"],
                    after_sequence=self.store.get_group(result["group_id"]).last_sequence - 1,
                )["events"][0]
                self.emit("collaboration.event.appended", event)
        except Exception:
            _log.warning("failed to persist ambiguous collaboration task", exc_info=True)

    def _coordinator_prompt(
        self, task: dict[str, Any], turn_id: str, turn_status: str
    ) -> str:
        with self.db._lock:
            targets = self.db._conn.execute(
                "SELECT tt.account_id, tt.status, tt.result_json, tt.error "
                "FROM collaboration_turn_targets tt WHERE tt.turn_id=? "
                "ORDER BY tt.created_at, tt.account_id",
                (turn_id,),
            ).fetchall()
        results = []
        for target in targets:
            result = json.loads(str(target["result_json"])) if target["result_json"] else {}
            results.append(
                {
                    "account_id": str(target["account_id"]),
                    "status": str(target["status"]),
                    "text": str(result.get("text") or ""),
                    "error": str(target["error"] or ""),
                }
            )
        return (
            "You are the exact pinned creator coordinating an AI-created internal group.\n"
            f"Task: {task['title']}\nBrief: {task.get('description') or ''}\n"
            f"Round {task['round']} status: {turn_status}\n"
            f"Explicit target results: {json.dumps(results, ensure_ascii=False)}\n\n"
            "Choose exactly one action. To request another round, call "
            "dispatch_internal_group_round with explicit target account IDs. To complete "
            "the task, call finish_internal_group_task with the final summary for the "
            "trusted web origin. Textual @ references and ordinary response text never "
            "schedule anyone. Do not merely describe the action: call exactly one tool."
        )

    def _agent_context(self, claimed: dict[str, Any]):
        with self.db._lock:
            task = self.db._conn.execute(
                "SELECT t.*, o.provider, o.account_id AS origin_account_id, o.binding_id, "
                "o.conversation_id, o.thread_id, o.source_session_id, o.source_group_id "
                "FROM collaboration_tasks t "
                "JOIN collaboration_origins o ON o.group_id=t.group_id "
                "WHERE t.group_id=?",
                (claimed["group_id"],),
            ).fetchone()
        from .agent_tools import CollaborationAgentContext

        if task is None:
            with self.db._lock:
                source = self.db._conn.execute(
                    "SELECT g.creator_kind, t.event_id FROM collaboration_turn_targets tt "
                    "JOIN collaboration_turns t ON t.turn_id=tt.turn_id "
                    "JOIN collaboration_groups g ON g.group_id=t.group_id "
                    "WHERE tt.target_id=? AND g.owner_key=?",
                    (claimed["target_id"], self.fence.owner_key),
                ).fetchone()
                granted = self.db._conn.execute(
                    "SELECT ag.attachment_id FROM collaboration_attachment_grants ag "
                    "WHERE ag.target_id=? ORDER BY ag.created_at, ag.attachment_id",
                    (claimed["target_id"],),
                ).fetchall()
            if source is None or str(source["creator_kind"]) != "owner":
                return None
            return CollaborationAgentContext(
                service=getattr(self.runner, "service", None),
                creator_account_id=str(claimed["account_id"]),
                source_kind="web_group",
                source_conversation_id=str(claimed["group_id"]),
                source_group_id=str(claimed["group_id"]),
                source_event_id=str(source["event_id"]),
                source_depth=0,
                allowed_origin_attachment_ids=tuple(
                    str(row["attachment_id"]) for row in granted
                ),
                role="source",
                may_create_authorized=bool(claimed.get("may_create_groups", False)),
            )
        return CollaborationAgentContext(
            service=getattr(self.runner, "service", None),
            creator_account_id=str(task["creator_account_id"]),
            source_kind=str(task["source_kind"]),
            source_conversation_id=str(task["conversation_id"]),
            source_provider=str(task["provider"]),
            source_account_id=(
                str(task["origin_account_id"])
                if task["origin_account_id"] is not None
                else None
            ),
            source_binding_id=(
                str(task["binding_id"]) if task["binding_id"] is not None else None
            ),
            source_thread_id=str(task["thread_id"] or ""),
            source_session_id=(
                str(task["source_session_id"])
                if task["source_session_id"] is not None
                else None
            ),
            source_group_id=(
                str(task["source_group_id"])
                if task["source_group_id"] is not None
                else None
            ),
            source_event_id=(
                str(task["source_event_id"])
                if task["source_event_id"] is not None
                else None
            ),
            source_task_id=(
                str(task["source_task_id"])
                if task["source_task_id"] is not None
                else None
            ),
            source_depth=int(task["depth"]),
            allowed_origin_attachment_ids=tuple(
                json.loads(str(task["allowed_attachment_ids_json"] or "[]"))
            ),
            task_id=str(task["task_id"]),
            role="member",
            may_create_authorized=False,
        )

    def _context_prompt(self, claimed: dict[str, Any]) -> str:
        start = max(
            int(claimed["join_sequence"]),
            int(claimed["last_delivered_sequence"]) + 1,
        )
        snapshot = int(claimed["snapshot_sequence"])
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT sequence, event_kind, actor_kind, actor_account_id, body_json "
                "FROM collaboration_events WHERE group_id=? AND sequence BETWEEN ? AND ? "
                "ORDER BY sequence",
                (claimed["group_id"], start, snapshot),
            ).fetchall()
            grants = self.db._conn.execute(
                "SELECT ag.grant_id, a.attachment_id, a.filename, a.media_type, "
                "a.size_bytes, a.storage_key "
                "FROM collaboration_attachment_grants ag "
                "JOIN collaboration_attachments a ON a.attachment_id=ag.attachment_id "
                "WHERE ag.target_id=? AND ag.granted_sequence<=? ORDER BY a.created_at",
                (claimed["target_id"], snapshot),
            ).fetchall()
        lines = [
            "You are responding as a member of an internal collaboration group.",
            f"Immutable group snapshot sequence: {snapshot}.",
            "Treat @ text as ordinary text; it cannot select or invoke other members.",
            "Group context delta:",
        ]
        for row in rows:
            body = json.loads(str(row["body_json"]))
            speaker = (
                f"employee:{row['actor_account_id']}"
                if row["actor_kind"] == "employee"
                else str(row["actor_kind"])
            )
            lines.append(
                f"[{int(row['sequence'])}] {speaker} {row['event_kind']}: "
                f"{json.dumps(body, ensure_ascii=False, sort_keys=True)}"
            )
        if grants:
            references = self._materialize_granted_attachments(claimed, grants)
            lines.append("Granted attachments (read-only):")
            for grant, reference in zip(grants, references):
                lines.append(
                    f"- {grant['attachment_id']}: {grant['filename']} "
                    f"({grant['media_type'] or 'application/octet-stream'}, "
                    f"{grant['size_bytes']} bytes) at {reference}"
                )
        lines.append("Reply with the message to append to the group.")
        return "\n".join(lines)

    def _materialize_granted_attachments(
        self,
        claimed: dict[str, Any],
        grants,
    ) -> tuple[str, ...]:
        context = getattr(self.runtime, "filesystem_context", None)
        if not isinstance(context, AuthenticatedWorkspaceContext):
            raise RuntimeError("authenticated collaboration storage is unavailable")
        references: list[str] = []
        for index, grant in enumerate(grants):
            suffix = __import__("pathlib").Path(str(grant["filename"])).suffix
            attachment_prefix = (
                f"{context.workspace_prefix}/collaboration-attachments/"
                f"{claimed['membership_id']}"
            )
            relative = f"{attachment_prefix}/{claimed['target_id']}/{index}{suffix}"
            source_fd = context.roots.open_relative(
                RootKind.OWNER_WRITABLE,
                str(grant["storage_key"]),
                expected_type=ExpectedType.REGULAR_FILE,
            )
            try:
                chunks: list[bytes] = []
                remaining = int(grant["size_bytes"])
                while remaining:
                    chunk = os.read(source_fd, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if remaining or os.read(source_fd, 1):
                    raise RuntimeError("collaboration attachment size is inconsistent")
                context.roots.replace_bytes(
                    RootKind.WORKSPACE,
                    relative,
                    b"".join(chunks),
                    overwrite=True,
                )
            finally:
                os.close(source_fd)
            now = time.time()

            def _write(conn, grant_id=str(grant["grant_id"]), path=relative):
                existing = conn.execute(
                    "SELECT materialization_id FROM collaboration_attachment_materializations "
                    "WHERE grant_id=?",
                    (grant_id,),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO collaboration_attachment_materializations "
                        "(materialization_id, grant_id, status, materialized_path, "
                        "worker_owner_key, worker_id, worker_generation, lease_version, "
                        "recovery_generation, created_at, updated_at, completed_at) "
                        "VALUES (?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            f"cam_{__import__('uuid').uuid4().hex}",
                            grant_id,
                            path,
                            *self.fence.values(),
                            now,
                            now,
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE collaboration_attachment_materializations SET "
                        "status='completed', materialized_path=?, error=NULL, "
                        "worker_owner_key=?, worker_id=?, worker_generation=?, "
                        "lease_version=?, recovery_generation=?, updated_at=?, "
                        "completed_at=? WHERE grant_id=?",
                        (path, *self.fence.values(), now, now, grant_id),
                    )

            self.db._execute_write(_write)
            references.append(f"/knowledge/{len(claimed['employee_policy']['knowledge_relative_paths']) - 1}/{claimed['target_id']}/{index}{suffix}")
        return tuple(references)

    def _commit_completed(
        self,
        target_id: str,
        *,
        receipt_key: str,
        text: str,
        result_status: str,
    ) -> None:
        def _commit(conn, *, result_text, result_status, now, reconcile):
            row = self._owned_target(conn, target_id)
            if row is None:
                raise RuntimeError("collaboration target is unavailable")
            event_row = None
            result = json.loads(str(row["result_json"])) if row["result_json"] else None
            group = conn.execute(
                "SELECT status FROM collaboration_groups WHERE group_id=? AND owner_key=?",
                (row["group_id"], self.fence.owner_key),
            ).fetchone()
            if group is None or str(group["status"]) != "active":
                raise RuntimeError("collaboration group is archived")
            if row["status"] == "completed":
                event_id = str((result or {}).get("event_id") or "")
                if event_id:
                    event_row = conn.execute(
                        "SELECT * FROM collaboration_events WHERE event_id=?",
                        (event_id,),
                    ).fetchone()
                event_created = False
                if event_row is None:
                    event_row = self.store._append_event(
                        conn,
                        group_id=str(row["group_id"]),
                        event_kind="message.employee",
                        actor_kind="employee",
                        actor_account_id=str(row["account_id"]),
                        actor_membership_id=str(row["membership_id"]),
                        body={"text": result_text, "target_id": target_id},
                        now=now,
                    )
                    event_created = True
                    result = {"text": result_text, "status": result_status, "event_id": event_row["event_id"]}
                    conn.execute(
                        "UPDATE collaboration_turn_targets SET result_json=?, updated_at=? WHERE target_id=?",
                        (json.dumps(result, ensure_ascii=False, separators=(",", ":")), now, target_id),
                    )
                return {"row": dict(self._owned_target(conn, target_id)), "event": dict(event_row), "event_id": event_row["event_id"], "event_created": event_created}
            if row["status"] != "running":
                if reconcile:
                    raise RuntimeError("completed receipt cannot reconcile a non-running target")
                raise RuntimeError("collaboration target completion fence is no longer valid")
            elapsed = max(0.0, now - float(row["active_started_at"] or now))
            changed = conn.execute(
                "UPDATE collaboration_turn_targets SET status='completed', error=NULL, "
                "last_delivered_sequence=snapshot_sequence, active_seconds=active_seconds+?, "
                "active_started_at=NULL, completed_at=?, updated_at=? WHERE target_id=? "
                "AND status='running' AND worker_owner_key=? AND worker_id=? "
                "AND worker_generation=? AND lease_version=? AND recovery_generation=?",
                (elapsed, now, now, target_id, *self.fence.values()),
            ).rowcount
            if changed != 1:
                raise RuntimeError("collaboration target completion fence is no longer valid")
            event_row = self.store._append_event(
                conn,
                group_id=str(row["group_id"]),
                event_kind="message.employee",
                actor_kind="employee",
                actor_account_id=str(row["account_id"]),
                actor_membership_id=str(row["membership_id"]),
                body={"text": result_text, "target_id": target_id},
                now=now,
            )
            result = {"text": result_text, "status": result_status, "event_id": event_row["event_id"]}
            conn.execute(
                "UPDATE collaboration_turn_targets SET result_json=? WHERE target_id=?",
                (json.dumps(result, ensure_ascii=False, separators=(",", ":")), target_id),
            )
            aggregate_collaboration_turn(conn, str(row["turn_id"]), now)
            return {"row": dict(self._owned_target(conn, target_id)), "event": event_row, "event_id": event_row["event_id"], "event_created": True}

        outcome = self.db.complete_collaboration_external_turn(
            turn_key=receipt_key,
            target_id=target_id,
            worker_id=self.fence.worker_id,
            worker_generation=self.fence.worker_generation,
            result_text=text,
            result_status=result_status,
            commit_target=_commit,
        )
        if outcome.get("event_created"):
            self.emit("collaboration.event.appended", self.store._event(outcome["event"]).__dict__)
        self.emit("collaboration.target.changed", self._public_target(outcome["row"]))

    def _finish(
        self,
        target_id: str,
        *,
        status: str,
        text: str = "",
        result_status: str = "",
        error: str | None = None,
    ) -> None:
        if status not in _TERMINAL_TARGET_STATUSES:
            raise ValueError("target terminal status is invalid")
        now = time.time()

        def _write(conn):
            row = self._owned_target(conn, target_id)
            if row is None:
                raise RuntimeError("collaboration target is unavailable")
            if row["status"] in _TERMINAL_TARGET_STATUSES:
                return dict(row), None
            elapsed = max(0.0, now - float(row["active_started_at"] or now))
            total_active = float(row["active_seconds"]) + elapsed
            final_status = "timed_out" if total_active >= self.active_budget_seconds else status
            final_error = "active execution budget exceeded" if final_status == "timed_out" else error
            changed = conn.execute(
                "UPDATE collaboration_turn_targets SET status=?, error=?, "
                "last_delivered_sequence=snapshot_sequence, active_seconds=?, "
                "active_started_at=NULL, completed_at=?, updated_at=? WHERE target_id=? "
                "AND status IN ('running','waiting_approval') AND worker_owner_key=? "
                "AND worker_id=? AND worker_generation=? AND lease_version=? "
                "AND recovery_generation=?",
                (
                    final_status,
                    final_error,
                    total_active,
                    now,
                    now,
                    target_id,
                    *self.fence.values(),
                ),
            ).rowcount
            if changed != 1:
                return dict(self._owned_target(conn, target_id)), None
            event_row = None
            if final_status == "completed":
                raise RuntimeError("completed collaboration targets require atomic receipt commit")
            result = {
                "text": text,
                "status": result_status or final_status,
                **({"event_id": event_row["event_id"]} if event_row else {}),
            }
            conn.execute(
                "UPDATE collaboration_turn_targets SET result_json=? WHERE target_id=?",
                (
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    target_id,
                ),
            )
            aggregate_collaboration_turn(conn, str(row["turn_id"]), now)
            return dict(self._owned_target(conn, target_id)), event_row

        row, event = self.db._execute_write(_write)
        if event is not None:
            self.emit("collaboration.event.appended", self.store._event(event).__dict__)
        self.emit("collaboration.target.changed", self._public_target(row))

    def _reject_candidate(self, target_id: str, error: str) -> None:
        now = time.time()

        def _write(conn):
            row = self._owned_target(conn, target_id)
            if row is None or row["status"] != "queued":
                return None
            conn.execute(
                "UPDATE collaboration_turn_targets SET status='failed', error=?, "
                "completed_at=?, updated_at=? WHERE target_id=? AND status='queued'",
                (error, now, now, target_id),
            )
            aggregate_collaboration_turn(conn, str(row["turn_id"]), now)
            return dict(self._owned_target(conn, target_id))

        row = self.db._execute_write(_write)
        if row is not None:
            self.emit("collaboration.target.changed", self._public_target(row))

    def _recover_uncertain_targets(self) -> None:
        now = time.time()

        def _write(conn):
            rows = conn.execute(
                "SELECT tt.target_id, tt.turn_id, tt.execution_id, t.group_id "
                "FROM collaboration_turn_targets tt "
                "JOIN collaboration_turns t ON t.turn_id=tt.turn_id "
                "JOIN collaboration_groups g ON g.group_id=t.group_id "
                "WHERE g.owner_key=? AND tt.status IN ('running','waiting_approval') "
                "AND ((tt.worker_owner_key IS NOT ? OR tt.worker_id IS NOT ? "
                "OR tt.worker_generation IS NOT ? OR tt.lease_version IS NOT ? "
                "OR tt.recovery_generation IS NOT ?) OR EXISTS ("
                "SELECT 1 FROM collaboration_approvals a "
                "WHERE a.target_id=tt.target_id AND a.status='pending' AND "
                "(a.worker_owner_key IS NOT ? OR a.worker_id IS NOT ? "
                "OR a.worker_generation IS NOT ? OR a.lease_version IS NOT ? "
                "OR a.recovery_generation IS NOT ?)))",
                (
                    self.fence.owner_key,
                    *self.fence.values(),
                    *self.fence.values(),
                ),
            ).fetchall()
            turn_ids: set[str] = set()
            for row in rows:
                conn.execute(
                    "UPDATE collaboration_turn_targets SET status='ambiguous', "
                    "error='previous worker outcome is uncertain', active_seconds="
                    "active_seconds + CASE WHEN active_started_at IS NULL THEN 0 "
                    "ELSE MAX(0, ? - active_started_at) END, active_started_at=NULL, "
                    "completed_at=?, updated_at=? WHERE target_id=?",
                    (now, now, now, row["target_id"]),
                )
                turn_ids.add(str(row["turn_id"]))
            approvals = conn.execute(
                "SELECT a.approval_id, a.target_id, tt.execution_id, tt.turn_id, t.group_id "
                "FROM collaboration_approvals a "
                "JOIN collaboration_turn_targets tt ON tt.target_id=a.target_id "
                "JOIN collaboration_turns t ON t.turn_id=tt.turn_id "
                "JOIN collaboration_groups g ON g.group_id=t.group_id "
                "WHERE g.owner_key=? AND a.status='pending' AND "
                "(a.worker_owner_key IS NOT ? OR a.worker_id IS NOT ? "
                "OR a.worker_generation IS NOT ? OR a.lease_version IS NOT ? "
                "OR a.recovery_generation IS NOT ?)",
                (self.fence.owner_key, *self.fence.values()),
            ).fetchall()
            for approval in approvals:
                conn.execute(
                    "UPDATE collaboration_approvals SET status='ambiguous', updated_at=? "
                    "WHERE approval_id=? AND status='pending'",
                    (now, approval["approval_id"]),
                )
            for turn_id in turn_ids:
                aggregate_collaboration_turn(conn, turn_id, now)
            return [dict(row) for row in rows], [dict(row) for row in approvals]

        recovered, approvals = self.db._execute_write(_write)
        for row in recovered:
            target = self._owned_target_snapshot(str(row["target_id"]))
            if target is not None:
                self.emit("collaboration.target.changed", self._public_target(target))
        for approval in approvals:
            self.emit(
                "collaboration.approval.changed",
                {
                    "approval_id": str(approval["approval_id"]),
                    "group_id": str(approval["group_id"]),
                    "turn_id": str(approval["turn_id"]),
                    "target_id": str(approval["target_id"]),
                    "execution_id": str(approval["execution_id"]),
                    "status": "ambiguous",
                },
            )
        if recovered:
            _log.warning(
                "marked %d collaboration targets ambiguous during recovery",
                len(recovered),
            )

    def _owned_target_snapshot(self, target_id: str) -> dict[str, Any] | None:
        with self.db._lock:
            row = self._owned_target(self.db._conn, target_id)
        return dict(row) if row is not None else None

    def _owned_target(self, conn, target_id: str):
        return conn.execute(
            "SELECT tt.*, t.group_id FROM collaboration_turn_targets tt "
            "JOIN collaboration_turns t ON t.turn_id=tt.turn_id "
            "JOIN collaboration_groups g ON g.group_id=t.group_id "
            "WHERE tt.target_id=? AND g.owner_key=?",
            (target_id, self.fence.owner_key),
        ).fetchone()

    @staticmethod
    def _public_target(row: dict[str, Any]) -> dict[str, Any]:
        result = json.loads(str(row["result_json"])) if row.get("result_json") else None
        return {
            **({"group_id": str(row["group_id"])} if row.get("group_id") is not None else {}),
            "target_id": str(row["target_id"]),
            "execution_id": str(row["execution_id"]),
            "turn_id": str(row["turn_id"]),
            "account_id": str(row["account_id"]),
            "membership_id": str(row["membership_id"]),
            "snapshot_sequence": int(row["snapshot_sequence"]),
            "status": str(row["status"]),
            "error": str(row["error"]) if row.get("error") is not None else None,
            "result": result,
            "active_seconds": float(row["active_seconds"]),
            "attempt": int(row["attempt"]),
        }
