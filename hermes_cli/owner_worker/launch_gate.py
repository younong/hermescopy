"""Minimal gated launcher used to place an Owner Worker before exec.

The parent moves and verifies this process in its reserved cgroup before writing
the single start byte.  This avoids Python ``preexec_fn`` in the threaded
Dashboard process while retaining descriptor-bound cwd selection.
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cwd-fd", required=True, type=int)
    parser.add_argument("--start-fd", required=True, type=int)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = list(args.argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if args.cwd_fd < 3 or args.start_fd < 3 or args.cwd_fd == args.start_fd or not argv:
        raise SystemExit("owner worker launch gate is invalid")
    try:
        os.fchdir(args.cwd_fd)
        os.close(args.cwd_fd)
        if os.read(args.start_fd, 1) != b"1":
            raise SystemExit("owner worker launch gate was not admitted")
    finally:
        try:
            os.close(args.start_fd)
        except OSError:
            pass
    os.execvpe(argv[0], argv, os.environ)


if __name__ == "__main__":
    main()
