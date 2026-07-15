"""Controlled management commands for durable local dashboard users.

Passwords are accepted only from an interactive prompt or standard input.  They
are never echoed, logged, stored outside the local-user authority, or included
in command output.  Generated bootstrap credentials are reveal-once and only
shown when both standard input and output are terminals.
"""
from __future__ import annotations

import getpass
import json
import os
import secrets
import sys
from typing import TextIO

from hermes_cli.dashboard_auth.local_users import (
    LocalAccount,
    LocalUserStore,
    LocalUserStoreConflict,
    LocalUserStoreUnavailable,
)

_BOOTSTRAP_ACCOUNT_COUNT = 5
_GENERATED_PASSWORD_BYTES = 24


def _store() -> LocalUserStore:
    """Build the durable authority using its configured stable provider secret."""
    try:
        from plugins.dashboard_auth.basic import _load_config_basic_auth_section, _resolve, _resolve_secret

        section = _load_config_basic_auth_section()
        if _resolve("HERMES_DASHBOARD_BASIC_AUTH_STORE", section, "store").lower() != "local":
            raise LocalUserStoreUnavailable("local account authority is not configured")
        if not _resolve("HERMES_DASHBOARD_BASIC_AUTH_SECRET", section, "secret"):
            raise LocalUserStoreUnavailable("local account authority requires a stable secret")
        secret = _resolve_secret(section)
    except LocalUserStoreUnavailable:
        raise
    except Exception as exc:
        raise LocalUserStoreUnavailable("local account authority is unavailable") from exc
    if len(secret) < 32:
        raise LocalUserStoreUnavailable("local account authority requires a stable secret")
    return LocalUserStore(secret=secret, max_accounts=_BOOTSTRAP_ACCOUNT_COUNT)


def _configure_durable_store() -> None:
    """Enable durable Basic auth without ever persisting a plaintext password."""
    try:
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        dashboard = cfg.setdefault("dashboard", {})
        basic = dashboard.setdefault("basic_auth", {})
        if not isinstance(basic, dict):
            raise ValueError("dashboard.basic_auth must be a mapping")
        basic["store"] = "local"
        # An injected environment secret remains authoritative and does not need
        # duplication on disk. Otherwise write a high-entropy stable store key.
        if not os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_SECRET", "").strip() and not str(
            basic.get("secret", "") or ""
        ).strip():
            basic["secret"] = secrets.token_urlsafe(32)
        basic["username"] = ""
        basic["password_hash"] = ""
        basic["password"] = ""
        save_config(cfg)
    except ValueError:
        raise
    except Exception as exc:
        raise LocalUserStoreUnavailable("local account authority cannot be configured") from exc


def _bootstrap_store() -> LocalUserStore:
    """Configure durable auth and return a store with no existing accounts.

    This preflight leaves existing configuration untouched so a second bootstrap
    cannot silently migrate or overwrite a configured authentication provider.
    """
    try:
        existing = _store()
        if existing.list_accounts():
            raise LocalUserStoreConflict("local accounts are already initialized")
        return existing
    except LocalUserStoreUnavailable as exc:
        if str(exc) != "local account authority is not configured":
            raise
    _configure_durable_store()
    return _store()


def _account_metadata(account: LocalAccount) -> dict[str, object]:
    """Serialize only non-secret, operator-safe account metadata."""
    return {
        "account_id": account.account_id,
        "username": account.username,
        "display_name": account.display_name,
        "status": account.status,
        "auth_revision": account.auth_revision,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
        "password_changed_at": account.password_changed_at,
        "disabled_at": account.disabled_at,
    }


def _read_password(*, stdin: TextIO | None = None) -> str:
    """Read one password without accepting secret material in process argv."""
    source = stdin or sys.stdin
    if getattr(source, "isatty", lambda: False)():
        password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Confirm new password: ")
        if password != confirm:
            raise ValueError("passwords do not match")
        return password
    password = source.readline()
    if not password:
        raise ValueError("password standard input is empty")
    return password.rstrip("\r\n")


def _generated_accounts() -> list[tuple[str, str, str]]:
    return [
        (
            f"user{number}",
            secrets.token_urlsafe(_GENERATED_PASSWORD_BYTES),
            f"User {number}",
        )
        for number in range(1, _BOOTSTRAP_ACCOUNT_COUNT + 1)
    ]


def _require_tty_for_reveal_once() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("bootstrap --generate requires an interactive TTY")


def cmd_dashboard_users(args) -> None:
    """Dispatch a local dashboard account lifecycle command."""
    action = getattr(args, "dashboard_users_action", None)
    try:
        # Bootstrap establishes store configuration as part of its one-time
        # transaction; all other commands require that authority to exist.
        store = None if action == "bootstrap" else _store()
        if action == "list":
            accounts = [_account_metadata(account) for account in store.list_accounts()]
            if getattr(args, "json", False):
                print(json.dumps(accounts, sort_keys=True))
            elif accounts:
                for account in accounts:
                    print(
                        "{username}\t{status}\t{display_name}\t"
                        "created={created_at}\tupdated={updated_at}".format(**account)
                    )
            else:
                print("No local dashboard users configured.")
            return

        if action == "bootstrap":
            _require_tty_for_reveal_once()
            store = _bootstrap_store()
            accounts = _generated_accounts()
            store.bootstrap_accounts(accounts, expected_count=_BOOTSTRAP_ACCOUNT_COUNT)
            print("Local dashboard users bootstrapped. Record these credentials now; they will not be shown again.")
            for username, password, _display_name in accounts:
                print(f"{username}\t{password}")
            return

        username = getattr(args, "username", "")
        if action == "reset-password":
            # Do not include password values in output, logging, or exceptions.
            store.set_password(
                username=username,
                password=_read_password(),
                require_reset=bool(getattr(args, "require_reset", False)),
            )
            print(f"Password reset and sessions revoked for user {username}.")
            return
        if action == "disable":
            store.set_account_status(username=username, status="disabled")
            print(f"Disabled user {username} and revoked sessions.")
            return
        if action == "enable":
            store.set_account_status(username=username, status="active")
            print(f"Enabled user {username} and revoked sessions.")
            return
        if action == "revoke-sessions":
            store.revoke_all_sessions(username=username)
            print(f"Revoked sessions for user {username}.")
            return
        raise ValueError("a dashboard users action is required")
    except (LocalUserStoreConflict, LocalUserStoreUnavailable, ValueError) as exc:
        # Store errors are deliberately credential-free.  Never interpolate a
        # supplied/generated password into an error or log message.
        print(f"Dashboard user management failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
