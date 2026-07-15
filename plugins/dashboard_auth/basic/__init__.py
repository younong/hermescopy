"""Username/password dashboard authentication provider.

``dashboard.basic_auth.store: local`` enables the durable multi-user
:class:`hermes_cli.dashboard_auth.local_users.LocalUserStore`: it stores only
scrypt password hashes and keyed digests of opaque access/refresh tokens.
Session validation checks the stored account authorization revision and real
server-side revocation state.  The configured secret is required in this mode
and is used only as the store digest key; it is never logged or returned.

The legacy, single configured username/password mode remains available when
``store`` is unset, retaining its stateless HMAC-signed token behavior for
existing self-hosted deployments.  Environment values take precedence over
``config.yaml`` in both modes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Optional

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    InvalidCredentialsError,
    LoginStart,
    ProviderError,
    RefreshExpiredError,
    Session,
)
from hermes_cli.dashboard_auth.local_users import (
    LocalUserStore,
    LocalUserStoreConflict,
    LocalUserStoreUnavailable,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Access-token lifetime. The middleware transparently refreshes via the
# refresh token (30-day) when the access token lapses, so this controls
# how often a refresh round trip happens, not how long the user stays
# logged in.
_DEFAULT_TTL_SECONDS = 12 * 60 * 60  # 12h
_REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60  # 30d

# scrypt parameters (RFC 7914 / stdlib hashlib.scrypt). n must be a power
# of two; these are the widely-recommended interactive-login parameters
# (~16 MiB, a few ms on commodity hardware).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16

# Length of the HMAC-SHA256 digest appended as a fixed-length suffix to
# signed tokens (no separator — binary HMAC bytes can't be confused with
# a delimiter).
_SIG_LEN = hashlib.sha256().digest_size


LAST_SKIP_REASON: str = ""


# ---------------------------------------------------------------------------
# Password hashing (stdlib scrypt)
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Return a ``scrypt$n$r$p$<salt_b64>$<dk_b64>`` hash string.

    Use this to precompute ``password_hash`` for config.yaml so plaintext
    never sits at rest. Exposed as a module function so operators can run
    ``python -c "from plugins.dashboard_auth.basic import hash_password;
    print(hash_password('pw'))"``.
    """
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=0,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$"
        f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
    )


def _verify_password(password: str, encoded: str) -> bool:
    """Constant-time scrypt verify. False on any malformed hash string."""
    try:
        scheme, n_s, r_s, p_s, salt_b64, dk_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except (ValueError, TypeError):
        return False
    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=0,
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


# A fixed dummy hash used to spend ~equal time when the username is
# unknown, so an attacker can't distinguish "no such user" (fast) from
# "wrong password" (slow scrypt) by timing. Computed once at import.
_DUMMY_HASH = hash_password("dummy-password-for-constant-time-verify")


# ---------------------------------------------------------------------------
# Token signing (stateless HMAC-signed blobs)
# ---------------------------------------------------------------------------


def _sign(payload: dict, secret: bytes) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(secret, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + sig).decode()


def _unsign(token: str, secret: bytes) -> Optional[dict]:
    try:
        blob = base64.urlsafe_b64decode(token.encode())
        if len(blob) <= _SIG_LEN:
            return None
        raw, sig = blob[:-_SIG_LEN], blob[-_SIG_LEN:]
        expected = hmac.new(secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        return json.loads(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class BasicAuthProvider(DashboardAuthProvider):
    """Username/password provider using either legacy or durable storage.

    Pass ``store`` to use the durable local multi-user authority.  Omitting it
    preserves the former single configured-account interface for callers that
    construct this provider directly.
    """

    name = "basic"
    display_name = "Username & Password"
    supports_password = True

    def __init__(
        self,
        *,
        secret: bytes,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        username: str | None = None,
        password_hash: str | None = None,
        store: LocalUserStore | None = None,
    ) -> None:
        if len(secret) < 16:
            raise ValueError("secret must be at least 16 bytes")
        if store is None:
            if not username:
                raise ValueError("username must be non-empty")
            if not password_hash:
                raise ValueError("password_hash must be non-empty")
        elif username is not None or password_hash is not None:
            raise ValueError("store mode does not accept configured credentials")
        self._username = username
        self._password_hash = password_hash
        self._store = store
        self._secret = secret
        self._ttl = max(60, int(ttl_seconds))

    # ---- OAuth methods: not used (pure-password provider) ------------------

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError(
            "BasicAuthProvider is password-only; there is no OAuth redirect "
            "flow. The login page POSTs to /auth/password-login instead."
        )

    def complete_login(
        self, *, code: str, state: str, code_verifier: str, redirect_uri: str
    ) -> Session:
        raise NotImplementedError(
            "BasicAuthProvider is password-only; use complete_password_login."
        )

    # ---- password login ----------------------------------------------------

    def complete_password_login(
        self, *, username: str, password: str
    ) -> Session:
        if self._store is not None:
            try:
                account = self._store.verify_credentials(
                    username=username, password=password
                )
                if account is None:
                    raise InvalidCredentialsError("invalid username or password")
                return self._session_from_local(self._store.create_session(
                    account=account,
                    access_ttl_seconds=self._ttl,
                    refresh_ttl_seconds=_REFRESH_TTL_SECONDS,
                ))
            except InvalidCredentialsError:
                raise
            except (LocalUserStoreConflict, LocalUserStoreUnavailable) as exc:
                raise ProviderError("local account store is unavailable") from exc

        # Legacy configured-account behavior, retained for compatibility.
        assert self._username is not None and self._password_hash is not None
        username_ok = hmac.compare_digest(
            username.encode("utf-8"), self._username.encode("utf-8")
        )
        target_hash = self._password_hash if username_ok else _DUMMY_HASH
        password_ok = _verify_password(password, target_hash)
        if not (username_ok and password_ok):
            raise InvalidCredentialsError("invalid username or password")
        return self._mint_session(self._username)

    # ---- session lifecycle -------------------------------------------------

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        if self._store is not None:
            try:
                verified = self._store.verify_access_token(access_token)
            except LocalUserStoreUnavailable as exc:
                raise ProviderError("local account store is unavailable") from exc
            if verified is None:
                return None
            return Session(
                user_id=verified.account.account_id,
                email="",
                display_name=verified.account.display_name,
                org_id="",
                provider=self.name,
                expires_at=verified.access_expires_at,
                access_token=access_token,
                refresh_token="",
            )

        payload = _unsign(access_token, self._secret)
        if (
            payload is None
            or payload.get("kind") != "access"
            or payload.get("exp", 0) <= int(time.time())
        ):
            return None
        return self._session_from_payload(access_token, "", payload)

    def refresh_session(self, *, refresh_token: str) -> Session:
        if self._store is not None:
            try:
                session = self._store.rotate_refresh_token(
                    refresh_token,
                    access_ttl_seconds=self._ttl,
                    refresh_ttl_seconds=_REFRESH_TTL_SECONDS,
                )
            except LocalUserStoreUnavailable as exc:
                raise ProviderError("local account store is unavailable") from exc
            if session is None:
                raise RefreshExpiredError("refresh token expired or invalid")
            return self._session_from_local(session)

        if not refresh_token:
            raise RefreshExpiredError("no refresh token present in session")
        payload = _unsign(refresh_token, self._secret)
        if (
            payload is None
            or payload.get("kind") != "refresh"
            or payload.get("exp", 0) <= int(time.time())
        ):
            raise RefreshExpiredError("refresh token expired or invalid")
        return self._mint_session(str(payload.get("sub", self._username)))

    def revoke_session(self, *, refresh_token: str) -> None:
        if self._store is not None:
            try:
                self._store.revoke_refresh_token(refresh_token)
            except LocalUserStoreUnavailable:
                logger.warning("dashboard-auth-basic: local session revocation failed")
        # Legacy stateless sessions cannot be revoked server-side.

    # ---- internals ---------------------------------------------------------

    def _mint_session(self, user_id: str) -> Session:
        now = int(time.time())
        exp = now + self._ttl
        access_token = _sign(
            {"sub": user_id, "kind": "access", "exp": exp}, self._secret
        )
        refresh_token = _sign(
            {"sub": user_id, "kind": "refresh", "exp": now + _REFRESH_TTL_SECONDS},
            self._secret,
        )
        return Session(
            user_id=user_id,
            email="",
            display_name=user_id,
            org_id="",
            provider=self.name,
            expires_at=exp,
            access_token=access_token,
            refresh_token=refresh_token,
        )

    def _session_from_local(self, session) -> Session:
        return Session(
            user_id=session.account.account_id,
            email="",
            display_name=session.account.display_name,
            org_id="",
            provider=self.name,
            expires_at=session.access_expires_at,
            access_token=session.access_token,
            refresh_token=session.refresh_token,
        )

    def _session_from_payload(
        self, access_token: str, refresh_token: str, payload: dict
    ) -> Session:
        user_id = str(payload.get("sub", ""))
        return Session(
            user_id=user_id,
            email="",
            display_name=user_id,
            org_id="",
            provider=self.name,
            expires_at=int(payload["exp"]),
            access_token=access_token,
            refresh_token=refresh_token,
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def _load_config_basic_auth_section() -> dict:
    """Return ``dashboard.basic_auth`` from config.yaml, or ``{}``.

    Robust to load_config() raising, the keys being absent, or the value
    not being a dict — every shape falls through to ``{}``.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config()
    except Exception as exc:  # noqa: BLE001 — broad catch is intentional
        logger.debug(
            "dashboard-auth-basic: load_config() raised %s; "
            "falling back to env-only configuration",
            exc,
        )
        return {}
    section = cfg_get(cfg, "dashboard", "basic_auth", default=None)
    return section if isinstance(section, dict) else {}


def _resolve(env_name: str, cfg_section: dict, cfg_key: str) -> str:
    """Env-wins-over-config resolution; empty env treated as unset."""
    env = os.environ.get(env_name, "").strip()
    if env:
        return env
    return str(cfg_section.get(cfg_key, "") or "").strip()


def _resolve_secret(cfg_section: dict) -> bytes:
    """Resolve the token-signing secret.

    Accepts base64 or hex or raw text from config/env. When unset,
    generates a random per-process secret (sessions then don't survive a
    restart or span multiple workers — logged at INFO).
    """
    raw = _resolve(
        "HERMES_DASHBOARD_BASIC_AUTH_SECRET", cfg_section, "secret"
    )
    if not raw:
        logger.info(
            "dashboard-auth-basic: no 'secret' configured; generating a "
            "random per-process signing key. Sessions will not survive a "
            "restart or span multiple workers. Set dashboard.basic_auth."
            "secret (or HERMES_DASHBOARD_BASIC_AUTH_SECRET) for stable "
            "sessions."
        )
        return secrets.token_bytes(32)
    # Try base64, then hex, then fall back to the raw UTF-8 bytes.
    for decoder in (base64.b64decode, bytes.fromhex):
        try:
            decoded = decoder(raw)
            if len(decoded) >= 16:
                return decoded
        except (ValueError, TypeError):
            pass
    return raw.encode("utf-8")


def register(ctx) -> None:
    """Register basic authentication from legacy or durable-store config.

    ``store: local`` (or ``HERMES_DASHBOARD_BASIC_AUTH_STORE=local``) selects
    the durable multi-user SQLite authority.  In that mode account lifecycle
    is deliberately external to this plugin; an empty store registers safely
    but rejects every login until accounts are provisioned.  With no store
    configured, the established single configured-account behavior applies.
    """
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""

    section = _load_config_basic_auth_section()
    store_mode = _resolve(
        "HERMES_DASHBOARD_BASIC_AUTH_STORE", section, "store"
    ).lower()
    username = _resolve(
        "HERMES_DASHBOARD_BASIC_AUTH_USERNAME", section, "username"
    )
    password_hash = _resolve(
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH", section, "password_hash"
    )
    plaintext = _resolve(
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", section, "password"
    )
    ttl_raw = _resolve(
        "HERMES_DASHBOARD_BASIC_AUTH_TTL_SECONDS", section, "session_ttl_seconds"
    )

    if store_mode not in ("", "local"):
        LAST_SKIP_REASON = "dashboard.basic_auth.store must be 'local' when set"
        logger.warning("dashboard-auth-basic: %s", LAST_SKIP_REASON)
        return

    secret_raw = _resolve("HERMES_DASHBOARD_BASIC_AUTH_SECRET", section, "secret")
    if store_mode == "local" and not secret_raw:
        LAST_SKIP_REASON = (
            "dashboard.basic_auth.store=local requires dashboard.basic_auth.secret "
            "(or HERMES_DASHBOARD_BASIC_AUTH_SECRET)"
        )
        logger.warning("dashboard-auth-basic: %s", LAST_SKIP_REASON)
        return
    secret = _resolve_secret(section)
    if store_mode == "local" and len(secret) < 32:
        LAST_SKIP_REASON = "dashboard.basic_auth.store=local requires a secret of at least 32 bytes"
        logger.warning("dashboard-auth-basic: %s", LAST_SKIP_REASON)
        return

    try:
        ttl = int(ttl_raw) if ttl_raw else _DEFAULT_TTL_SECONDS
    except ValueError:
        ttl = _DEFAULT_TTL_SECONDS

    if store_mode == "local":
        try:
            provider = BasicAuthProvider(
                store=LocalUserStore(secret=secret), secret=secret, ttl_seconds=ttl
            )
        except (LocalUserStoreUnavailable, ValueError) as exc:
            LAST_SKIP_REASON = "BasicAuthProvider durable store construction failed"
            logger.warning("dashboard-auth-basic: %s", LAST_SKIP_REASON)
            logger.debug("dashboard-auth-basic: durable store setup error: %s", exc)
            return
        ctx.register_dashboard_auth_provider(provider)
        logger.info("dashboard-auth-basic: registered durable local password provider")
        return

    if not username:
        LAST_SKIP_REASON = (
            "dashboard.basic_auth.username is not set (and "
            "HERMES_DASHBOARD_BASIC_AUTH_USERNAME is empty). Set a username "
            "and a password (or password_hash) under dashboard.basic_auth in "
            "config.yaml to enable username/password dashboard login, or use "
            "the OAuth provider, or pass --insecure to skip the auth gate."
        )
        logger.debug("dashboard-auth-basic: %s", LAST_SKIP_REASON)
        return

    if not password_hash and not plaintext:
        LAST_SKIP_REASON = (
            "dashboard.basic_auth.username is set but neither password_hash "
            "nor password is configured. Provide one of them (password_hash "
            "is preferred — compute it with "
            "plugins.dashboard_auth.basic.hash_password)."
        )
        logger.warning("dashboard-auth-basic: %s", LAST_SKIP_REASON)
        return

    # An env plaintext password intentionally overrides a configured hash.
    plaintext_from_env = os.environ.get(
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", ""
    ).strip()
    if plaintext_from_env:
        password_hash = hash_password(plaintext_from_env)
        logger.info("dashboard-auth-basic: hashed env-supplied password in-memory")
    elif not password_hash:
        password_hash = hash_password(plaintext)
        logger.info("dashboard-auth-basic: hashed configured plaintext password in-memory")

    try:
        provider = BasicAuthProvider(
            username=username,
            password_hash=password_hash,
            secret=secret,
            ttl_seconds=ttl,
        )
    except ValueError as exc:
        LAST_SKIP_REASON = f"BasicAuthProvider construction failed: {exc}"
        logger.warning("dashboard-auth-basic: %s", LAST_SKIP_REASON)
        return

    ctx.register_dashboard_auth_provider(provider)
    logger.info("dashboard-auth-basic: registered configured password provider")
