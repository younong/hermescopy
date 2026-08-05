"""Token-only authentication backed by immutable Owner machine credentials."""
from __future__ import annotations

from typing import Optional

from hermes_cli.dashboard_auth.authority import AuthorityStore, AuthorityUnavailable
from hermes_cli.dashboard_auth.base import (
    DashboardAuthProvider,
    LoginStart,
    ProviderError,
    Session,
    TokenPrincipal,
)


class MachineCredentialProvider(DashboardAuthProvider):
    """Verify reveal-once bearer tokens stored in the Control Plane authority DB."""

    name = "machine-credential"
    display_name = "Machine credential"
    supports_token = True
    supports_session = False

    def __init__(self, store: AuthorityStore) -> None:
        self._store = store

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        try:
            credential = self._store.verify_machine_token(token)
        except AuthorityUnavailable as exc:
            raise ProviderError("machine credential authority is unavailable") from exc
        if credential is None or credential.provider != self.name:
            return None
        return TokenPrincipal(
            principal=credential.principal,
            provider=credential.provider,
            scopes=(credential.scope,),
        )

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError("Machine credentials do not support interactive login")

    def complete_login(
        self,
        *,
        code: str,
        state: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> Session:
        raise NotImplementedError("Machine credentials do not support interactive login")

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError("Machine credentials do not support interactive login")

    def revoke_session(self, *, refresh_token: str) -> None:
        return None
