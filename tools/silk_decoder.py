"""Minimal subprocess entry point for decoding a trusted local SILK file."""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        import pilk

        pilk.silk_to_wav(sys.argv[1], sys.argv[2], rate=16000)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
