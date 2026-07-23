"""Trusted start-gated launcher for resource-bounded Tool Executors."""
from __future__ import annotations

import argparse
import os
import resource


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--start-fd", required=True, type=int)
    parser.add_argument("--nofile", required=True, type=int)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = list(args.argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if args.start_fd < 3 or args.nofile <= 0 or not argv:
        raise SystemExit("tool executor launcher is invalid")
    try:
        if os.read(args.start_fd, 1) != b"1":
            raise SystemExit("tool executor launcher was not admitted")
    finally:
        try:
            os.close(args.start_fd)
        except OSError:
            pass
    resource.setrlimit(resource.RLIMIT_NOFILE, (args.nofile, args.nofile))
    os.execvpe(argv[0], argv, os.environ)


if __name__ == "__main__":
    main()
