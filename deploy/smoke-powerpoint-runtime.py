#!/usr/bin/env python3
"""Bounded pre-activation smoke for the managed PowerPoint runtime."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Sequence


def _run(command: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _result(
    *,
    started: float,
    checks: dict[str, str],
    failure: dict[str, str] | None,
    cleanup: str,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "status": "failed" if failure or cleanup != "passed" else "passed",
        "checks": checks,
        "durationMs": round((time.monotonic() - started) * 1000),
        "cleanup": cleanup,
        "failure": failure,
    }


def _run_checks(*, wrapper: str, timeout: int) -> dict[str, object]:
    started = time.monotonic()
    checks: dict[str, str] = {}
    failure: dict[str, str] | None = None
    cleanup = "passed"
    work = Path(tempfile.mkdtemp(prefix="hermes-powerpoint-smoke-"))

    try:
        generator = work / "generate.js"
        deck = work / "runtime-smoke.pptx"
        generator.write_text(
            """const pptxgen = require('pptxgenjs');
const pptx = new pptxgen();
pptx.layout = 'LAYOUT_WIDE';
for (const marker of ['HERMES_PPTX_SMOKE_ALPHA', 'HERMES_PPTX_SMOKE_OMEGA']) {
  const slide = pptx.addSlide();
  slide.addText(marker, {x: 1, y: 1, w: 10, h: 1, fontSize: 28});
}
pptx.writeFile({ fileName: process.argv[2] }).catch(error => { console.error(error); process.exit(1); });
""",
            encoding="utf-8",
        )
        generated = _run(["node", str(generator), str(deck)], cwd=work, timeout=timeout)
        if generated.returncode or not deck.is_file() or deck.stat().st_size == 0:
            raise RuntimeError("pptxgenjs_generation")
        checks["pptxgenjs_generation"] = "passed"

        extracted = _run(
            ["python", "-m", "markitdown", str(deck)],
            cwd=work,
            timeout=timeout,
        )
        if extracted.returncode:
            raise RuntimeError("markitdown_extract")
        alpha = extracted.stdout.find("HERMES_PPTX_SMOKE_ALPHA")
        omega = extracted.stdout.find("HERMES_PPTX_SMOKE_OMEGA")
        if alpha < 0 or omega <= alpha:
            raise RuntimeError("markitdown_order")
        checks["markitdown_extract"] = "passed"
        checks["markitdown_order"] = "passed"

        converted = _run(
            [
                "python",
                wrapper,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(work),
                str(deck),
            ],
            cwd=work,
            timeout=timeout,
        )
        pdf = deck.with_suffix(".pdf")
        if converted.returncode or not pdf.is_file() or pdf.stat().st_size == 0:
            raise RuntimeError("libreoffice_conversion")
        checks["libreoffice_conversion"] = "passed"
    except Exception as exc:
        check = str(exc) if isinstance(exc, RuntimeError) else "unexpected"
        failure = {"check": check, "code": type(exc).__name__}
    finally:
        try:
            shutil.rmtree(work)
        except OSError:
            cleanup = "failed"
            if failure is None:
                failure = {"check": "temporary_cleanup", "code": "OSError"}

    return _result(
        started=started,
        checks=checks,
        failure=failure,
        cleanup=cleanup,
    )


def _run_authenticated_executor(
    *,
    owner_home: Path,
    policy_path: Path,
    timeout: int,
) -> dict[str, object]:
    started = time.monotonic()
    checks: dict[str, str] = {}
    failure: dict[str, str] | None = None
    cleanup = "passed"
    roots = None
    supervisor = None
    resource_controller = None

    try:
        from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
        from hermes_cli.controlled_roots import controlled_roots_for
        from hermes_cli.dashboard_auth.authority import (
            OwnerWorkerAuthorityLease,
            WorkerLeaseState,
        )
        from hermes_cli.owner_runtime import (
            ensure_owner_runtime_dirs,
            owner_worker_runtime_paths,
        )
        from hermes_cli.owner_worker.cgroup_v2 import CgroupV2Manager
        from hermes_cli.owner_worker.host_sandbox import host_sandbox_deployment_policy
        from hermes_cli.owner_worker.tool_executor_supervisor import ToolExecutorSupervisor

        class LocalResourceController:
            def __init__(self, manager):
                self.manager = manager

            def reserve_executor(self, identity, invocation_id):
                scope = self.manager.admit_executor(identity, invocation_id)

                class LocalScope:
                    def attach_pids(self, pids):
                        for pid in pids:
                            scope.attach(pid)

                    def verify_pids(self, pids):
                        return all(scope.verify_membership(pid) for pid in pids)

                    def read_events(self):
                        return scope.read_events()

                    def release(self):
                        scope.cleanup()

                return LocalScope()

            def shutdown_generation(self):
                # This one-shot smoke owns only invocation leases it returns;
                # never sweep scopes held by the live Dashboard manager.
                return None

            def close(self):
                self.manager.close()

        ensure_owner_runtime_dirs(owner_home)
        runtime_paths = owner_worker_runtime_paths(
            owner_home=owner_home,
            worker_generation=1,
        )
        roots = controlled_roots_for(runtime_paths)
        lease = OwnerWorkerAuthorityLease(
            "ok1_deploy_powerpoint_smoke",
            1,
            "deploy-powerpoint-smoke",
            WorkerLeaseState.ACTIVE,
            1,
            0,
        )
        deployment_policy = host_sandbox_deployment_policy(policy_path)
        manager = CgroupV2Manager(deployment_policy.resource_policy)
        checks["startup_recovery"] = (
            f"passed:{manager.startup_cleanup_count}"
        )
        resource_controller = LocalResourceController(manager)
        supervisor = ToolExecutorSupervisor(
            owner_home=owner_home,
            workspace_context=AuthenticatedWorkspaceContext(roots),
            lease=lease,
            deployment_policy=deployment_policy,
            resource_controller=resource_controller,
        )
        inside_command = " ".join(
            shlex.quote(part)
            for part in (
                "/opt/hermes/python/bin/python3",
                "/opt/hermes/release/deploy/smoke-powerpoint-runtime.py",
                "--inside",
                "--wrapper",
                "/opt/hermes/release/skills/productivity/powerpoint/scripts/office/soffice.py",
                "--timeout",
                str(timeout),
            )
        )
        raw = supervisor.dispatch(
            function_name="terminal",
            function_args={"command": inside_command, "timeout": timeout},
            task_id="deploy-powerpoint-smoke",
            session_id="deploy-powerpoint-smoke",
            tool_call_id="deploy-powerpoint-smoke",
            turn_id="deploy-powerpoint-smoke",
            api_request_id="deploy-powerpoint-smoke",
        )
        terminal_result = json.loads(raw)
        if terminal_result.get("exit_code") != 0 or terminal_result.get("error"):
            raise RuntimeError("authenticated_executor_command")
        inside = json.loads(str(terminal_result.get("output", "")))
        if inside.get("schemaVersion") != 1 or inside.get("status") != "passed":
            failed_check = (inside.get("failure") or {}).get("check")
            raise RuntimeError(str(failed_check or "authenticated_executor_checks"))
        inside_checks = inside.get("checks")
        if not isinstance(inside_checks, dict):
            raise RuntimeError("authenticated_executor_result")
        checks.update({str(key): str(value) for key, value in inside_checks.items()})
        checks["authenticated_executor"] = "passed"

        for function_args, check, failure_check in (
            (
                {
                    "command": (
                        "/opt/hermes/python/bin/python3 -c "
                        + shlex.quote("import time; time.sleep(180)")
                    ),
                    "timeout": 180,
                },
                "deadline_enforced",
                "resource_deadline_exceeded",
            ),
            (
                {
                    "command": (
                        "/opt/hermes/python/bin/python3 -c "
                        + shlex.quote("print('x' * 400000)")
                    ),
                    "timeout": timeout,
                },
                "output_enforced",
                "resource_output_limit_exceeded",
            ),
        ):
            try:
                supervisor.dispatch(
                    function_name="terminal",
                    function_args=function_args,
                    task_id=f"deploy-{check}",
                    session_id=f"deploy-{check}",
                    tool_call_id=f"deploy-{check}",
                    turn_id=f"deploy-{check}",
                    api_request_id=f"deploy-{check}",
                )
            except Exception as exc:
                name = type(exc).__name__
                expected = (
                    name == "ExecutorDeadlineExceeded"
                    if check == "deadline_enforced"
                    else name == "ExecutorOutputExceeded"
                )
                if not expected:
                    raise RuntimeError(failure_check) from exc
            else:
                raise RuntimeError(failure_check)
            checks[check] = "passed"
    except Exception as exc:
        check = str(exc) if isinstance(exc, RuntimeError) else "authenticated_executor"
        failure = {"check": check, "code": type(exc).__name__}
    finally:
        if supervisor is not None:
            try:
                supervisor.stop_generation()
            except Exception:
                cleanup = "failed"
        if resource_controller is not None:
            try:
                resource_controller.close()
            except Exception:
                cleanup = "failed"
        if roots is not None:
            try:
                roots.close()
            except Exception:
                cleanup = "failed"
        try:
            shutil.rmtree(owner_home)
        except FileNotFoundError:
            pass
        except OSError:
            cleanup = "failed"
        if cleanup != "passed" and failure is None:
            failure = {"check": "owner_cleanup", "code": "OSError"}

    return _result(
        started=started,
        checks=checks,
        failure=failure,
        cleanup=cleanup,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--wrapper")
    parser.add_argument("--owner-home")
    parser.add_argument("--policy", default="/etc/hermes/executor-sandbox.json")
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args(argv)

    if args.inside:
        if not args.wrapper:
            parser.error("--inside requires --wrapper")
        result = _run_checks(wrapper=args.wrapper, timeout=args.timeout)
    else:
        if not args.owner_home:
            parser.error("authenticated smoke requires --owner-home")
        result = _run_authenticated_executor(
            owner_home=Path(args.owner_home).resolve(),
            policy_path=Path(args.policy).resolve(),
            timeout=args.timeout,
        )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
