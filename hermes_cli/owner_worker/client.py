"""HTTP client helpers for Owner Worker Unix-domain sockets."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease

from .tokens import AUD_OWNER_WORKER_HTTP, SCOPE_OWNER_WORKER_HTTP, mint_owner_worker_capability


class OwnerWorkerHealthError(RuntimeError):
    """Raised when an owner worker fails health verification."""


class OwnerWorkerClient:
    """Minimal HTTP client for an Owner Worker listening on a Unix socket."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        timeout: float = 2.0,
        control_home: str | Path | None = None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self.control_home = Path(control_home).resolve() if control_home else None

    def _client(self) -> httpx.Client:
        transport = httpx.HTTPTransport(uds=str(self.socket_path))
        return httpx.Client(transport=transport, base_url="http://owner-worker", timeout=self.timeout)

    def _capability_header(self, lease: OwnerWorkerAuthorityLease, path: str) -> dict[str, str]:
        token = mint_owner_worker_capability(
            lease,
            audience=AUD_OWNER_WORKER_HTTP,
            scope=SCOPE_OWNER_WORKER_HTTP,
            path=path,
            control_home=self.control_home,
        )
        return {"Authorization": f"Bearer {token}"}

    def health(self, *, lease: OwnerWorkerAuthorityLease | None = None) -> dict[str, Any]:
        headers = {} if lease is None else self._capability_header(lease, "/internal/health")
        try:
            with self._client() as client:
                response = client.get("/internal/health", headers=headers)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # pragma: no cover - exact httpx types vary by transport
            raise OwnerWorkerHealthError(f"owner worker health request failed: {exc}") from exc
        if not isinstance(data, dict):
            raise OwnerWorkerHealthError("owner worker health response was not an object")
        return data

    def request(
        self,
        method: str,
        path: str,
        *,
        lease: OwnerWorkerAuthorityLease,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        """Send an exact-worker capability-authenticated request to the worker."""
        request_headers = dict(headers or {})
        token_path = str(path or "").split("?", 1)[0] or "/"
        request_headers.update(self._capability_header(lease, token_path))
        try:
            with self._client() as client:
                return client.request(method, path, headers=request_headers, content=content)
        except Exception as exc:  # pragma: no cover - exact httpx types vary by transport
            raise OwnerWorkerHealthError(f"owner worker request failed: {exc}") from exc

    def verify_health(
        self,
        *,
        owner_key: str,
        owner_home: str | Path,
        worker_generation: int | None = None,
        worker_id: str | None = None,
        lease_version: int | None = None,
        recovery_generation: int | None = None,
        lease: OwnerWorkerAuthorityLease | None = None,
    ) -> dict[str, Any]:
        """Fetch and validate health against the exact worker identity."""
        data = self.health(lease=lease)
        if data.get("ready") is not True:
            raise OwnerWorkerHealthError("owner worker is not ready")
        if data.get("owner_key") != owner_key:
            raise OwnerWorkerHealthError("owner worker owner_key mismatch")
        if worker_generation is not None and data.get("worker_generation") != int(worker_generation):
            raise OwnerWorkerHealthError("owner worker generation mismatch")
        if worker_id is not None and data.get("worker_id") != worker_id:
            raise OwnerWorkerHealthError("owner worker identity mismatch")
        if lease_version is not None and data.get("lease_version") != int(lease_version):
            raise OwnerWorkerHealthError("owner worker lease mismatch")
        if recovery_generation is not None and data.get("recovery_generation") != int(recovery_generation):
            raise OwnerWorkerHealthError("owner worker recovery generation mismatch")
        expected_home = str(Path(owner_home).resolve())
        reported_home = str(Path(str(data.get("owner_home", ""))).resolve())
        reported_hermes_home = str(Path(str(data.get("hermes_home", ""))).resolve())
        if reported_home != expected_home:
            raise OwnerWorkerHealthError("owner worker owner_home mismatch")
        if reported_hermes_home != expected_home:
            raise OwnerWorkerHealthError("owner worker HERMES_HOME mismatch")
        reported_workspace = data.get("workspace_root")
        if reported_workspace is not None:
            expected_workspace = str((Path(owner_home) / "workspaces").resolve())
            if str(Path(str(reported_workspace)).resolve()) != expected_workspace:
                raise OwnerWorkerHealthError("owner worker workspace_root mismatch")
        if data.get("forbidden_env_present"):
            raise OwnerWorkerHealthError("owner worker reported forbidden environment variables")
        if not isinstance(data.get("pid"), int) or int(data["pid"]) <= 0:
            raise OwnerWorkerHealthError("owner worker reported invalid pid")
        return data
