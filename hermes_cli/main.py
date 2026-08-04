#!/usr/bin/env python3
"""Local administration entry point for the authenticated Hermes Web service."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from hermes_cli import __release_date__, __version__
from hermes_cli._parser import build_top_level_parser
from hermes_cli.subcommands.dashboard import build_dashboard_parser

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _build_web_ui(web_dir: Path) -> None:
    if not (web_dir / "package.json").is_file():
        raise SystemExit(f"Web workspace not found: {web_dir}")
    subprocess.run(
        ["npm", "run", "build", "--workspace", "web"],
        cwd=PROJECT_ROOT,
        check=True,
    )


def cmd_dashboard(args: argparse.Namespace) -> None:
    """Build and start the authenticated Web control plane."""
    if getattr(args, "status", False) or getattr(args, "stop", False):
        raise SystemExit("dashboard process management was removed; use your service manager")
    if not getattr(args, "skip_build", False):
        _build_web_ui(PROJECT_ROOT / "web")

    from hermes_cli.plugins import discover_plugins
    from hermes_cli.web_server import start_server

    discover_plugins()
    start_server(
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        trust_proxy_headers=getattr(args, "trust_proxy_headers", False),
    )


def cmd_dashboard_register(args: argparse.Namespace) -> None:
    from hermes_cli.dashboard_register import cmd_dashboard_register as run

    run(args)


def cmd_dashboard_users(args: argparse.Namespace) -> None:
    from hermes_cli.dashboard_users import cmd_dashboard_users as run

    run(args)


def cmd_dashboard_authority(args: argparse.Namespace) -> None:
    from hermes_cli.dashboard_authority import cmd_dashboard_authority as run

    run(args)


def main() -> None:
    parser, subparsers = build_top_level_parser()
    build_dashboard_parser(
        subparsers,
        cmd_dashboard=cmd_dashboard,
        cmd_dashboard_register=cmd_dashboard_register,
        cmd_dashboard_users=cmd_dashboard_users,
        cmd_dashboard_authority=cmd_dashboard_authority,
    )
    args = parser.parse_args()
    if args.version:
        print(f"Hermes Agent v{__version__} ({__release_date__})")
        return
    if not hasattr(args, "func"):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
