"""HTTP client for an owner Session Reader Unix-domain socket."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from hermes_cli.dashboard_auth.authority import SessionReaderAuthorityLease
from .tokens import mint_session_reader_capability


class SessionReaderHealthError(RuntimeError):
    """Raised when a Session Reader request or identity check fails."""


_HTTPX_WARMED = False


def warm_http_transport() -> None:
    """Pay httpcore's lazy import/setup cost during Control Plane startup."""
    global _HTTPX_WARMED
    if _HTTPX_WARMED:
        return
    transport = httpx.HTTPTransport(uds="/nonexistent/hermes-session-reader.sock")
    try:
        with httpx.Client(transport=transport, base_url="http://session-reader") as client:
            try:
                client.get("/internal/health", timeout=0.01)
            except Exception:
                pass
    finally:
        transport.close()
    _HTTPX_WARMED = True


class SessionReaderClient:
    def __init__(
        self,
        socket_path: str | Path,
        *,
        timeout: float = 2.0,
        control_home: str | Path | None = None,
        signing_record: dict[str, Any] | None = None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self.control_home = Path(control_home).resolve() if control_home else None
        self.signing_record = signing_record

    def _client(self) -> httpx.Client:
        transport = httpx.HTTPTransport(uds=str(self.socket_path))
        return httpx.Client(transport=transport, base_url="http://session-reader", timeout=self.timeout)

    def _headers(self, lease: SessionReaderAuthorityLease, path: str) -> dict[str, str]:
        token = mint_session_reader_capability(
            lease,
            path=path,
            control_home=self.control_home,
            signing_record=self.signing_record,
        )
        return {"Authorization": f"Bearer {token}"}

    def request(
        self,
        method: str,
        path: str,
        *,
        lease: SessionReaderAuthorityLease,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        token_path = str(path or "").split("?", 1)[0] or "/"
        request_headers = dict(headers or {})
        request_headers.update(self._headers(lease, token_path))
        try:
            with self._client() as client:
                return client.request(method, path, headers=request_headers, content=content)
        except Exception as exc:
            raise SessionReaderHealthError(f"session reader request failed: {exc}") from exc

    def verify_health(
        self,
        *,
        lease: SessionReaderAuthorityLease,
        owner_home: str | Path,
    ) -> dict[str, Any]:
        response = self.request("GET", "/internal/health", lease=lease)
        try:
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise SessionReaderHealthError(f"session reader health request failed: {exc}") from exc
        self.verify_health_payload(data, lease=lease, owner_home=owner_home)
        return data

    @staticmethod
    def verify_health_payload(
        data: Any,
        *,
        lease: SessionReaderAuthorityLease,
        owner_home: str | Path,
    ) -> None:
        expected_home = str(Path(owner_home).resolve())
        if not isinstance(data, dict) or data.get("ready") is not True:
            raise SessionReaderHealthError("session reader is not ready")
        expected = {
            "owner_key": lease.owner_key,
            "reader_generation": lease.reader_generation,
            "reader_id": lease.reader_id,
            "lease_version": lease.lease_version,
            "recovery_generation": lease.recovery_generation,
            "owner_home": expected_home,
            "hermes_home": expected_home,
        }
        if any(data.get(key) != value for key, value in expected.items()):
            raise SessionReaderHealthError("session reader identity mismatch")
        if not isinstance(data.get("pid"), int) or data["pid"] <= 0:
            raise SessionReaderHealthError("session reader reported invalid pid")
        if data.get("forbidden_env_present"):
            raise SessionReaderHealthError("session reader reported forbidden environment")
