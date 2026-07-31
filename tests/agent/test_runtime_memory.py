import threading
from concurrent.futures import wait
from unittest.mock import MagicMock

import agent.runtime_memory as runtime_memory


def test_inference_pool_reuses_bounded_daemon_workers(monkeypatch):
    executor = runtime_memory.DaemonThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="test-inference",
    )
    monkeypatch.setattr(runtime_memory, "_executor", executor)
    release = threading.Event()

    def identify_worker():
        release.wait(timeout=2)
        thread = threading.current_thread()
        return thread.ident, thread.daemon

    futures = [runtime_memory.submit_inference(identify_worker) for _ in range(6)]
    release.set()
    wait(futures, timeout=5)

    workers = {future.result() for future in futures}
    assert len(workers) <= 2
    assert all(is_daemon for _, is_daemon in workers)
    executor.shutdown(wait=True)


def test_current_rss_bytes_reads_linux_proc_status(monkeypatch):
    monkeypatch.setattr(runtime_memory.sys, "platform", "linux")
    monkeypatch.setattr(
        runtime_memory.Path,
        "read_text",
        lambda self, **kwargs: "Name:\tpython\nVmRSS:\t1234 kB\n",
    )

    assert runtime_memory.current_rss_bytes() == 1234 * 1024


def test_trim_threshold_uses_half_finite_cgroup_limit(monkeypatch):
    monkeypatch.setattr(runtime_memory.sys, "platform", "linux")

    def read_text(path, **kwargs):
        if str(path) == "/proc/self/cgroup":
            return "0::/hermes/worker\n"
        if str(path) == "/sys/fs/cgroup/hermes/worker/memory.max":
            return str(896 * 1024 * 1024)
        raise AssertionError(path)

    monkeypatch.setattr(runtime_memory.Path, "read_text", read_text)

    assert runtime_memory.trim_threshold_bytes() == 448 * 1024 * 1024


def test_trim_skips_while_inference_is_active(monkeypatch):
    monkeypatch.setattr(runtime_memory.sys, "platform", "linux")
    monkeypatch.setattr(runtime_memory, "current_rss_bytes", lambda: 600 * 1024 * 1024)
    monkeypatch.setattr(runtime_memory, "trim_threshold_bytes", lambda: 512 * 1024 * 1024)
    malloc_trim = MagicMock(return_value=1)
    monkeypatch.setattr(runtime_memory.ctypes, "CDLL", lambda _: MagicMock(malloc_trim=malloc_trim))

    with runtime_memory._inference_activity():
        assert runtime_memory.maybe_trim_allocator("test") is False

    malloc_trim.assert_not_called()


def test_trim_obeys_cooldown_and_records_success(monkeypatch):
    rss_values = iter([600 * 1024 * 1024, 550 * 1024 * 1024, 600 * 1024 * 1024])
    malloc_trim = MagicMock(return_value=1)
    libc = MagicMock(malloc_trim=malloc_trim)
    monkeypatch.setattr(runtime_memory.sys, "platform", "linux")
    monkeypatch.setattr(runtime_memory, "current_rss_bytes", lambda: next(rss_values))
    monkeypatch.setattr(runtime_memory, "trim_threshold_bytes", lambda: 512 * 1024 * 1024)
    monkeypatch.setattr(runtime_memory.ctypes, "CDLL", lambda _: libc)
    monkeypatch.setattr(runtime_memory.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(runtime_memory, "_last_trim_at", float("-inf"))

    assert runtime_memory.maybe_trim_allocator("foreground") is True
    assert runtime_memory.maybe_trim_allocator("foreground") is False
    malloc_trim.assert_called_once_with(0)


def test_trim_is_noop_on_unsupported_platform(monkeypatch):
    monkeypatch.setattr(runtime_memory.sys, "platform", "darwin")
    monkeypatch.setattr(runtime_memory.ctypes, "CDLL", MagicMock())

    assert runtime_memory.maybe_trim_allocator("test") is False
    runtime_memory.ctypes.CDLL.assert_not_called()
