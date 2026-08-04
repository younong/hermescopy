"""``hermes dashboard`` authenticated Web administration parser.

The dashboard command starts the authenticated Web control plane. Extracted from
``hermes_cli/main.py:main()`` (god-file Phase 2); handler injected to avoid
importing ``main``.
"""

from __future__ import annotations

import argparse
from typing import Callable


def _add_server_runtime_args(parser) -> None:
    """Attach authenticated Web server runtime flags.

    """
    parser.add_argument(
        "--port", type=int, default=9119, help="Port (default 9119, 0 for auto-assign by OS)"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Host (default 127.0.0.1)"
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help=(
            "DEPRECATED / NO-OP. Formerly bypassed auth on a non-loopback "
            "bind. As of the June 2026 hardening it no longer disables "
            "authentication — a public bind always requires an auth provider "
            "(password or OAuth). Bind 127.0.0.1 + tunnel to keep it local."
        ),
    )
    parser.add_argument(
        "--require-auth",
        action="store_true",
        help=(
            "Require cookie-based dashboard authentication on a loopback bind. "
            "Use for multi-user access through an SSH tunnel or trusted proxy."
        ),
    )
    parser.add_argument(
        "--trust-proxy-headers",
        action="store_true",
        help=(
            "Trust forwarded request metadata from a loopback reverse proxy. "
            "Requires --require-auth, a loopback bind, and dashboard.public_url."
        ),
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help=(
            "Skip the web UI build step and serve the existing dist directly. "
            "Useful for non-interactive contexts (Windows Scheduled Tasks, CI) "
            "where npm may not be available. Pre-build with: cd web && npm run build"
        ),
    )
    # Lifecycle flags — mutually exclusive with each other and with the
    # start-a-server flags above (if both are passed, --stop / --status win
    # because they exit before the server is started).  The server has no
    # service manager and no PID file, so these scan the process table for
    # `hermes dashboard` / `hermes serve` cmdlines and SIGTERM them directly —
    # the same path `hermes update` uses to clean up stale servers.
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop all running Hermes web server processes and exit",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="List running Hermes web server processes and exit",
    )


def build_dashboard_parser(
    subparsers,
    *,
    cmd_dashboard: Callable,
    cmd_dashboard_register: Callable,
    cmd_dashboard_users: Callable,
    cmd_dashboard_authority: Callable,
) -> None:
    """Attach the authenticated ``dashboard`` command."""
    # =========================================================================
    # dashboard command — the browser web UI
    # =========================================================================
    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="Start the web UI dashboard",
        description="Launch the Hermes Agent web dashboard for managing config, API keys, and sessions",
    )
    _add_server_runtime_args(dashboard_parser)
    dashboard_parser.add_argument(
        "--no-open", action="store_true", help="Don't open browser automatically"
    )
    dashboard_parser.set_defaults(func=cmd_dashboard)

    # `hermes dashboard register` — register a self-hosted dashboard OAuth
    # client with Nous Portal and write the client_id into ~/.hermes/.env.
    # Nested subparser so bare `hermes dashboard` keeps launching the server
    # (set_defaults(func=cmd_dashboard) above remains the default).
    dashboard_subparsers = dashboard_parser.add_subparsers(
        dest="dashboard_subcommand"
    )
    dashboard_register_parser = dashboard_subparsers.add_parser(
        "register",
        help="Register a self-hosted dashboard with Nous Portal (writes the OAuth client ID to .env)",
        description=(
            "Register this install as a self-hosted dashboard with your Nous "
            "Portal account. Creates an OAuth client, writes "
            "HERMES_DASHBOARD_OAUTH_CLIENT_ID into ~/.hermes/.env, and prints "
            "how to engage the login gate. Requires being logged in (hermes setup)."
        ),
    )
    dashboard_register_parser.add_argument(
        "--name",
        default=None,
        help="Human-readable label for the dashboard (default: an auto-generated name)",
    )
    dashboard_register_parser.add_argument(
        "--redirect-uri",
        dest="redirect_uri",
        default=None,
        help=(
            "Optional public HTTPS OAuth redirect URI for the dashboard, e.g. "
            "https://hermes.example.com/auth/callback. Omit for localhost-only use."
        ),
    )
    dashboard_register_parser.add_argument(
        "--portal-url",
        dest="portal_url",
        default=None,
        help=(
            "Override the Nous Portal base URL for registration (default: the "
            "portal you logged into). The access token must be valid at this "
            "portal. Also settable via HERMES_DASHBOARD_PORTAL_URL. Mainly for "
            "testing against a staging/preview portal."
        ),
    )
    dashboard_register_parser.set_defaults(func=cmd_dashboard_register)

    authority_parser = dashboard_subparsers.add_parser(
        "authority",
        help="Inspect, preserve, or recover Control Plane authorization state",
    )
    authority_subparsers = authority_parser.add_subparsers(
        dest="dashboard_authority_action",
        required=True,
    )
    authority_status_parser = authority_subparsers.add_parser(
        "status", help="Inspect authority health without changing it"
    )
    authority_status_parser.add_argument(
        "--json", action="store_true", help="Print safe status metadata as JSON"
    )
    authority_status_parser.set_defaults(func=cmd_dashboard_authority)
    authority_preserve_parser = authority_subparsers.add_parser(
        "preserve", help="Create or verify forensic evidence without repairing"
    )
    authority_preserve_parser.add_argument(
        "--json", action="store_true", help="Print safe status metadata as JSON"
    )
    authority_preserve_parser.set_defaults(func=cmd_dashboard_authority)
    authority_recover_parser = authority_subparsers.add_parser(
        "recover", help="Recover from a digest-pinned authority source"
    )
    authority_recover_parser.add_argument("--incident", required=True, metavar="ID")
    authority_recover_parser.add_argument("--source", required=True, metavar="PATH")
    authority_recover_parser.add_argument("--sha256", required=True, metavar="HEX")
    authority_recover_parser.add_argument(
        "--repair-tls-offset-5",
        action="store_true",
        help="Repair only the exact SQLit + TLS-record-at-offset-5 signature",
    )
    authority_recover_parser.add_argument(
        "--json", action="store_true", help="Print safe status metadata as JSON"
    )
    authority_recover_parser.set_defaults(func=cmd_dashboard_authority)

    # ``hermes dashboard users`` manages the durable, local multi-user Basic
    # auth authority. Passwords never travel in argv: reset reads stdin (or an
    # interactive hidden prompt), and bootstrap generation is TTY reveal-once.
    dashboard_users_parser = dashboard_subparsers.add_parser(
        "users",
        help="Manage durable local dashboard users",
        description=(
            "Manage the local Basic-auth account authority. Bootstrap creates "
            "the configured account cap (five by default); generated credentials "
            "are displayed once only on an interactive terminal."
        ),
    )
    dashboard_users_subparsers = dashboard_users_parser.add_subparsers(
        dest="dashboard_users_action",
        required=True,
    )

    dashboard_users_list_parser = dashboard_users_subparsers.add_parser(
        "list", help="List safe local dashboard user metadata"
    )
    dashboard_users_list_parser.add_argument(
        "--json", action="store_true", help="Print safe metadata as JSON"
    )
    dashboard_users_list_parser.set_defaults(func=cmd_dashboard_users)

    dashboard_users_bootstrap_parser = dashboard_users_subparsers.add_parser(
        "bootstrap", help="Create the initial configured set of local users"
    )
    dashboard_users_bootstrap_parser.add_argument(
        "--generate",
        action="store_true",
        required=True,
        help="Generate passwords and reveal them once on an interactive terminal",
    )
    dashboard_users_bootstrap_parser.set_defaults(func=cmd_dashboard_users)

    dashboard_users_reset_parser = dashboard_users_subparsers.add_parser(
        "reset-password", help="Reset a user's password and revoke sessions"
    )
    dashboard_users_reset_parser.add_argument("username", metavar="USERNAME")
    dashboard_users_reset_parser.add_argument(
        "--require-reset",
        action="store_true",
        help="Leave the account pending reset rather than enabled",
    )
    dashboard_users_reset_parser.set_defaults(func=cmd_dashboard_users)

    for action, help_text in (
        ("make-admin", "Grant administrator role to a user"),
        ("disable", "Disable a user and revoke sessions"),
        ("enable", "Enable a user and revoke sessions"),
        ("revoke-sessions", "Revoke every session for a user"),
    ):
        action_parser = dashboard_users_subparsers.add_parser(action, help=help_text)
        action_parser.add_argument("username", metavar="USERNAME")
        action_parser.set_defaults(func=cmd_dashboard_users)
