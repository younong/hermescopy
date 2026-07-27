"""Asynchronous HTTP client for an owner Session Reader Unix socket."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from hermes_cli.dashboard_auth.authority import SessionReaderAuthorityLease
from .tokens import mint_session_reader_capability


_MAX_CONNECTIONS = 8
_MAX_KEEPALIVE_CONNECTIONS = 4
_HTTPX_WARMED = False


def warm_http_transport() -> None:
    """Pay httpcore's lazy async backend setup before a request is timed."""
    global _HTTPX_WARMED
    if _HTTPX_WARMED:
        return
    # Construction loads the async network backend but does not open a socket.
    httpx.AsyncHTTPTransport(uds="/nonexistent/hermes-session-reader.sock")
    _HTTPX_WARMED = True


class SessionReaderHealthError(RuntimeError):
    """Raised when a Session Reader request or identity check fails."""


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
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(self.socket_path)),
            base_url="http://session-reader",
            timeout=self.timeout,
            limits=httpx.Limits(
                max_connections=_MAX_CONNECTIONS,
                max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
            ),
        )
        self._closed = False

    def _headers(self, lease: SessionReaderAuthorityLease, path: str) -> dict[str, str]:
        token = mint_session_reader_capability(
            lease,
            path=path,
            control_home=self.control_home,
            signing_record=self.signing_record,
        )
        return {"Authorization": f"Bearer {token}"}

    async def request(
        self,
        method: str,
        path: str,
        *,
        lease: SessionReaderAuthorityLease,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        if self._closed:
            raise SessionReaderHealthError("session reader client is closed")
        token_path = str(path or "").split("?", 1)[0] or "/"
        request_headers = dict(headers or {})
        request_headers.update(self._headers(lease, token_path))
        try:
            return await self._client.request(
                method,
                path,
                headers=request_headers,
                content=content,
            )
        except Exception as exc:
            raise SessionReaderHealthError(f"session reader request failed: {exc}") from exc

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

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
