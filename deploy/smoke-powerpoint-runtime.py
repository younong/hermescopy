#!/usr/bin/env python3
"""Bounded pre-activation smoke for the managed PowerPoint runtime."""

from __future__ import annotations

import argparse
import functools
import json
import os
import resource
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Sequence


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


def _run_checks(
    *, wrapper: str, timeout: int, expected_nofile: int
) -> dict[str, object]:
    started = time.monotonic()
    checks: dict[str, str] = {}
    failure: dict[str, str] | None = None
    cleanup = "passed"
    work = Path(tempfile.mkdtemp(prefix="hermes-powerpoint-smoke-"))

    try:
        if resource.getrlimit(resource.RLIMIT_NOFILE) != (
            expected_nofile,
            expected_nofile,
        ):
            raise RuntimeError("executor_nofile_limit")
        checks["executor_nofile_limit"] = f"passed:{expected_nofile}"

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


_NETWORK_SMOKE_QUERY = "hermes-owner-relay-loopback-smoke"
_NETWORK_SMOKE_MARKER = "HERMES_OWNER_RELAY_NETWORK_OK"


class _NetworkSmokeHandler(BaseHTTPRequestHandler):
    marker = _NETWORK_SMOKE_MARKER

    def do_GET(self) -> None:
        if self.path != "/network-smoke":
            self.send_error(404)
            return
        payload = json.dumps({"marker": self.marker}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: Any) -> None:
        del args


def _dispatch_network_smoke(
    base_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    _invocation: Any,
    _materializer: Any,
) -> str:
    if tool_name != "web_search" or arguments != {
        "query": _NETWORK_SMOKE_QUERY,
        "limit": 1,
    }:
        raise RuntimeError("owner_relay_network_invocation")
    with urllib.request.urlopen(
        f"{base_url}/network-smoke", timeout=5
    ) as response:
        result = json.loads(response.read(4096))
    if result != {"marker": _NETWORK_SMOKE_MARKER}:
        raise RuntimeError("owner_relay_network_response")
    return json.dumps({"success": True, "marker": _NETWORK_SMOKE_MARKER})


def _open_fd_pressure(target_fd: int) -> list[int]:
    descriptors: list[int] = []
    try:
        while not descriptors or descriptors[-1] <= target_fd:
            descriptors.append(os.open(os.devnull, os.O_RDONLY))
    except Exception:
        for descriptor in descriptors:
            os.close(descriptor)
        raise
    return descriptors


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
    relay = None
    network_server = None
    network_thread = None
    pressure_fds: list[int] = []
    original_cwd: Path | None = None

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
        from hermes_cli.owner_worker.owner_tool_relay import OwnerToolRelayBroker
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
        original_cwd = Path.cwd()
        os.chdir(runtime_paths.default_workspace)
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
        network_server = HTTPServer(("127.0.0.1", 0), _NetworkSmokeHandler)
        network_thread = threading.Thread(
            target=network_server.serve_forever,
            daemon=True,
            name="owner-relay-network-smoke",
        )
        network_thread.start()
        relay = OwnerToolRelayBroker(
            identity_validator=lambda identity: (
                supervisor._require_active_executor_identity(identity)
            ),
            dispatcher=functools.partial(
                _dispatch_network_smoke,
                f"http://127.0.0.1:{network_server.server_port}",
            ),
            workspace_context=AuthenticatedWorkspaceContext(roots),
        )
        supervisor = ToolExecutorSupervisor(
            owner_home=owner_home,
            workspace_context=AuthenticatedWorkspaceContext(roots),
            lease=lease,
            deployment_policy=deployment_policy,
            resource_controller=resource_controller,
            owner_tool_relay=relay,
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
                "--expected-nofile",
                str(
                    deployment_policy.resource_policy.executor_limits.file_descriptors
                ),
            )
        )
        nofile_limit = (
            deployment_policy.resource_policy.executor_limits.file_descriptors
        )
        pressure_fds = _open_fd_pressure(nofile_limit + 8)
        if pressure_fds[-1] <= nofile_limit:
            raise RuntimeError("high_fd_launch_pressure")
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
        checks["high_fd_launch_pressure"] = f"passed:{pressure_fds[-1]}"
        for descriptor in pressure_fds:
            os.close(descriptor)
        pressure_fds = []

        network_raw = supervisor.dispatch(
            function_name="web_search",
            function_args={"query": _NETWORK_SMOKE_QUERY, "limit": 1},
            task_id="deploy-owner-relay-network",
            session_id="deploy-owner-relay-network",
            tool_call_id="deploy-owner-relay-network",
            turn_id="deploy-owner-relay-network",
            api_request_id="deploy-owner-relay-network",
        )
        network_result = json.loads(network_raw)
        if network_result != {
            "success": True,
            "marker": _NETWORK_SMOKE_MARKER,
        }:
            raise RuntimeError("owner_relay_network_response")
        checks["owner_relay_network"] = "passed"

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
        for descriptor in pressure_fds:
            try:
                os.close(descriptor)
            except OSError:
                cleanup = "failed"
        if network_server is not None:
            try:
                network_server.shutdown()
                network_server.server_close()
            except Exception:
                cleanup = "failed"
        if network_thread is not None:
            network_thread.join(timeout=5)
            if network_thread.is_alive():
                cleanup = "failed"
        if supervisor is not None:
            try:
                supervisor.stop_generation()
            except Exception:
                cleanup = "failed"
        if relay is not None:
            try:
                relay.close()
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
        if original_cwd is not None:
            try:
                os.chdir(original_cwd)
            except OSError:
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
    parser.add_argument("--expected-nofile", type=int)
    args = parser.parse_args(argv)

    if args.inside:
        if not args.wrapper or not args.expected_nofile:
            parser.error("--inside requires --wrapper and --expected-nofile")
        result = _run_checks(
            wrapper=args.wrapper,
            timeout=args.timeout,
            expected_nofile=args.expected_nofile,
        )
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
