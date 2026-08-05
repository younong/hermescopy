"""Real-process coverage for explicit Owner-scoped cron store locking."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from cron import jobs

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(jobs.__file__)))


@pytest.mark.skipif(jobs.fcntl is None, reason="POSIX fcntl/flock required")
def test_jobs_lock_excludes_another_process_for_same_owner_store(tmp_path):
    owner_home = tmp_path / "owner"
    ready = tmp_path / "child_holds_lock"
    release = tmp_path / "child_may_release"
    holder = tmp_path / "holder.py"
    holder.write_text(
        textwrap.dedent(
            f"""
            import pathlib, sys, time
            sys.path.insert(0, {_REPO_ROOT!r})
            from cron.jobs import CronStore, _jobs_lock, use_store

            with use_store(CronStore({str(owner_home)!r})):
                with _jobs_lock():
                    pathlib.Path({str(ready)!r}).write_text("1")
                    for _ in range(1000):
                        if pathlib.Path({str(release)!r}).exists():
                            break
                        time.sleep(0.01)
            """
        ),
        encoding="utf-8",
    )

    child = subprocess.Popen([sys.executable, str(holder)])
    try:
        for _ in range(1000):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists(), "child never acquired Owner cron lock"

        lock_file = jobs.CronStore(owner_home).cron_dir / ".jobs.lock"
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        try:
            with pytest.raises(OSError):
                jobs.fcntl.flock(fd, jobs.fcntl.LOCK_EX | jobs.fcntl.LOCK_NB)
        finally:
            os.close(fd)
    finally:
        release.write_text("1", encoding="utf-8")
        child.wait(timeout=15)
