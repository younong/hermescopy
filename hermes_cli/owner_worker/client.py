"""HTTP client helpers for Owner Worker Unix-domain sockets."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .tokens import AUD_OWNER_WORKER_HTTP, mint_internal_token


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

    def health(self, *, owner_key: str | None = None) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if owner_key:
            headers["Authorization"] = f"Bearer {mint_internal_token(owner_key, audience=AUD_OWNER_WORKER_HTTP, path='/internal/health', control_home=self.control_home)}"
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
        owner_key: str,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        """Send an owner-authenticated HTTP request to the worker."""
        request_headers = dict(headers or {})
        token_path = str(path or "").split("?", 1)[0] or "/"
        request_headers["Authorization"] = f"Bearer {mint_internal_token(owner_key, audience=AUD_OWNER_WORKER_HTTP, path=token_path, control_home=self.control_home)}"
        try:
            with self._client() as client:
                return client.request(method, path, headers=request_headers, content=content)
        except Exception as exc:  # pragma: no cover - exact httpx types vary by transport
            raise OwnerWorkerHealthError(f"owner worker request failed: {exc}") from exc

    def verify_health(self, *, owner_key: str, owner_home: str | Path) -> dict[str, Any]:
        """Fetch and validate health against the exact owner identity."""
        data = self.health(owner_key=owner_key)
        if data.get("ready") is not True:
            raise OwnerWorkerHealthError("owner worker is not ready")
        if data.get("owner_key") != owner_key:
            raise OwnerWorkerHealthError("owner worker owner_key mismatch")
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
