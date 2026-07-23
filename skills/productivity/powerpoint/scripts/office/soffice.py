#!/usr/bin/env python3
"""Run LibreOffice with an isolated writable profile."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


def _resolve_soffice() -> str:
    for name in ("soffice", "libreoffice"):
        executable = shutil.which(name)
        if executable:
            return executable
    raise FileNotFoundError("LibreOffice executable not found (expected soffice or libreoffice)")


def run_soffice(arguments: Sequence[str]) -> int:
    """Execute LibreOffice once without sharing a user profile."""
    try:
        executable = _resolve_soffice()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 127

    temp_root = os.environ.get("TMPDIR") or tempfile.gettempdir()
    with tempfile.TemporaryDirectory(prefix="hermes-soffice-", dir=temp_root) as profile:
        profile_uri = Path(profile).resolve().as_uri()
        command = [
            executable,
            f"-env:UserInstallation={profile_uri}",
            *arguments,
        ]
        environment = dict(os.environ)
        environment.setdefault("SAL_USE_VCLPLUGIN", "svp")
        try:
            return subprocess.run(command, env=environment, check=False).returncode
        except OSError as exc:
            print(f"Failed to run LibreOffice: {exc}", file=sys.stderr)
            return 126


def main(argv: Sequence[str] | None = None) -> int:
    return run_soffice(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
