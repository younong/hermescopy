"""Parser for the authenticated Web control-plane administration CLI."""

import argparse

PRE_ARGPARSE_INHERITED_FLAGS: list[tuple[str, bool]] = []


def build_top_level_parser():
    parser = argparse.ArgumentParser(
        prog="hermes",
        description="Hermes authenticated Web control plane",
    )
    parser.add_argument("--version", "-V", action="store_true", help="Show version")
    subparsers = parser.add_subparsers(dest="command", help="Administration command")
    return parser, subparsers
