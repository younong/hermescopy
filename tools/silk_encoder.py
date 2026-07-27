"""Minimal subprocess entry point for Tencent SILK encoding."""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        from pilk import SilkEncoder

        encoder = SilkEncoder(
            pcm_rate=24000,
            silk_rate=24000,
            max_rate=24000,
            complexity=2,
            packet_size=20,
            packet_loss=0,
            use_in_band_fec=False,
            use_dtx=False,
        )
        encoder.encode(sys.argv[1], sys.argv[2], tencent=True)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
