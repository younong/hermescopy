"""Regressions for issue #29507 — cross-thread close of the per-request OpenAI
client could release a TLS socket FD whose integer was still cached in the
owning httpx worker's SSL BIO. The kernel then recycled the FD into the next
``open()`` (e.g. the kanban dispatcher's ``kanban.db``), and the worker's
delayed TLS flush wrote a 24-byte TLS application-data record on top of the
SQLite header.

The fix has two prongs:

1. ``abort_tcp_sockets`` no longer calls ``sock.close()`` — only
   ``shutdown(SHUT_RDWR)``. Shutdown unblocks the worker's pending
   ``recv``/``send`` without releasing the FD.

2. ``_RequestClientOwner`` is thread-aware: a stranger thread (the
   interrupt-check / stale-call loop) only aborts the sockets and leaves
   the client in the owner; the worker's own ``finally`` performs the
   actual ``client.close()`` from its own thread context.

Both prongs together close the FD-recycling window. The tests below pin
each prong individually and one end-to-end test simulates the reporter's
timeline at object granularity (no network, no real sockets).
"""
from __future__ import annotations

import datetime
import ipaddress
import logging
import socket as _socket
import sqlite3
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID



# ---------------------------------------------------------------------------
# Prong 1: abort_tcp_sockets must NOT release file descriptors.
# ---------------------------------------------------------------------------


class _FakeSocket:
    """Records shutdown/close calls without touching real FDs."""

    def __init__(self):
        self.shutdown_calls = 0
        self.close_calls = 0

    def shutdown(self, _how):
        self.shutdown_calls += 1

    def close(self):
        self.close_calls += 1


def _build_fake_client(sock):
    """Mimic the httpcore-1 layout that ``_iter_pool_sockets`` walks."""
    stream = SimpleNamespace(_sock=sock)
    http11 = SimpleNamespace(_network_stream=stream)
    pool_entry = SimpleNamespace(_connection=http11)
    pool = SimpleNamespace(_connections=[pool_entry])
    transport = SimpleNamespace(_pool=pool)
    http_client = SimpleNamespace(_transport=transport)
    return SimpleNamespace(_client=http_client)


def test_abort_tcp_sockets_shutdown_only_no_close():
    """The smoking-gun guarantee: shutdown is called, close is NOT.

    If a future refactor reintroduces ``sock.close()`` here, the
    FD-recycling race that corrupted ``kanban.db`` (issue #29507) will
    re-open. Pin the contract explicitly.
    """
    from agent.agent_runtime_helpers import abort_tcp_sockets

    sock = _FakeSocket()
    client = _build_fake_client(sock)

    n = abort_tcp_sockets(client)

    assert n == 1
    assert sock.shutdown_calls == 1, "shutdown() must run — it's how we unblock the worker"
    assert sock.close_calls == 0, (
        "close() must NOT run from this helper — releasing the FD here is the "
        "race that wrote TLS bytes into kanban.db (#29507)"
    )


def test_abort_tcp_sockets_uses_shut_rdwr():
    """Both directions must be shut down so the SSL state machine fully unwinds.

    Half-close (e.g. SHUT_WR only) wouldn't unblock a worker blocked in
    ``recv``, defeating the whole point of the helper.
    """
    from agent.agent_runtime_helpers import abort_tcp_sockets

    captured = []

    class _ProbingSocket:
        def shutdown(self, how):
            captured.append(how)

        def close(self):  # pragma: no cover — must not run, asserted below
            captured.append("CLOSE_CALLED")

    sock = _ProbingSocket()
    client = _build_fake_client(sock)

    abort_tcp_sockets(client)

    assert captured == [_socket.SHUT_RDWR]


def test_abort_tcp_sockets_swallows_oserror_on_shutdown():
    """A socket already shut down / not connected raises ``OSError`` — benign."""
    from agent.agent_runtime_helpers import abort_tcp_sockets

    class _AlreadyShut:
        def shutdown(self, _how):
            raise OSError("not connected")

        def close(self):  # pragma: no cover — must not run
            raise AssertionError("close() must not be called")

    client = _build_fake_client(_AlreadyShut())

    # No exception escapes; the helper still counts the socket as handled.
    assert abort_tcp_sockets(client) == 1


def test_abort_tcp_sockets_handles_multiple_pool_entries():
    """Walk every pool connection — the bug equally applies to all of them."""
    from agent.agent_runtime_helpers import abort_tcp_sockets

    socks = [_FakeSocket(), _FakeSocket(), _FakeSocket()]
    entries = [
        SimpleNamespace(_connection=SimpleNamespace(_network_stream=SimpleNamespace(_sock=s)))
        for s in socks
    ]
    pool = SimpleNamespace(_connections=entries)
    transport = SimpleNamespace(_pool=pool)
    http_client = SimpleNamespace(_transport=transport)
    client = SimpleNamespace(_client=http_client)

    assert abort_tcp_sockets(client) == 3
    for s in socks:
        assert s.shutdown_calls == 1
        assert s.close_calls == 0


# ---------------------------------------------------------------------------
# Prong 2: _RequestClientOwner is thread-aware.
# ---------------------------------------------------------------------------


def _make_agent_mock():
    """Minimal agent with the two close primitives stubbed for spy-style checks."""
    agent = MagicMock()
    agent._interrupt_requested = False
    agent._close_request_api_client = MagicMock()
    agent._abort_request_api_client = MagicMock()
    return agent


def _call_inside_owner_thread(callable_):
    """Run callable_ on a separate thread so its ``threading.get_ident()``
    differs from the test thread."""
    result = {"value": None, "exc": None}

    def runner():
        try:
            result["value"] = callable_()
        except BaseException as e:  # noqa: BLE001 — propagate test failures faithfully
            result["exc"] = e

    t = threading.Thread(target=runner)
    t.start()
    t.join(timeout=5.0)
    if result["exc"] is not None:
        raise result["exc"]
    return result["value"]


def test_close_from_stranger_thread_aborts_only_no_close():
    """A foreign thread aborts once and leaves final close to the owner."""
    from agent.chat_completion_helpers import _RequestClientOwner

    agent = _make_agent_mock()
    owner = _RequestClientOwner(
        close_client=agent._close_request_api_client,
        abort_client=agent._abort_request_api_client,
    )
    sentinel = object()
    registered = threading.Event()
    release = threading.Event()

    def owner_workload():
        owner.set(sentinel)
        registered.set()
        release.wait(timeout=2.0)
        owner.finish("request_complete")

    worker = threading.Thread(target=owner_workload)
    worker.start()
    assert registered.wait(timeout=2.0)

    owner.finish("interrupt_abort")
    owner.finish("duplicate_interrupt_abort")
    agent._abort_request_api_client.assert_called_once_with(
        sentinel, reason="interrupt_abort"
    )
    agent._close_request_api_client.assert_not_called()

    release.set()
    worker.join(timeout=5.0)
    assert not worker.is_alive()
    agent._close_request_api_client.assert_called_once_with(
        sentinel, reason="request_complete"
    )


def test_close_from_owner_thread_runs_full_close_exactly_once():
    """The registering thread releases the client exactly once."""
    from agent.chat_completion_helpers import _RequestClientOwner

    agent = _make_agent_mock()
    owner = _RequestClientOwner(
        close_client=agent._close_request_api_client,
        abort_client=agent._abort_request_api_client,
    )
    sentinel = object()

    def workload():
        assert owner.set(sentinel) is sentinel
        owner.finish("request_complete")
        owner.finish("duplicate_request_complete")

    _call_inside_owner_thread(workload)

    agent._close_request_api_client.assert_called_once_with(
        sentinel, reason="request_complete"
    )
    agent._abort_request_api_client.assert_not_called()


# ---------------------------------------------------------------------------
# End-to-end: the agent's ``_abort_request_api_client`` shuts sockets and
# logs deferred_close=stranger_thread without ever calling client.close().
# ---------------------------------------------------------------------------


def test_agent_abort_request_api_client_does_not_call_client_close(caplog):
    """``_abort_request_api_client`` must shutdown sockets but NEVER close().

    This is the actual entry point used by the stranger-thread path. If a
    future refactor accidentally wires it back to ``_close_openai_client``
    the FD race is back. Pin both the shutdown side-effect AND the absence
    of any ``client.close()`` call.
    """
    from run_agent import AIAgent

    sock = _FakeSocket()
    client = _build_fake_client(sock)

    # ``client.close()`` would mutate the holder if invoked — give it a
    # MagicMock spy so we can assert no call.
    client.close = MagicMock()

    agent = AIAgent.__new__(AIAgent)
    agent._client_log_context = lambda: "provider=test"

    with caplog.at_level(logging.INFO, logger="run_agent"):
        agent._abort_request_api_client(client, reason="interrupt_abort")

    # Sockets shut down (one in our fake pool).
    assert sock.shutdown_calls == 1
    assert sock.close_calls == 0
    # And critically: client.close() never ran here.
    client.close.assert_not_called()

    # The log line is parseable: same ``tcp_force_closed=N`` field shape as
    # the existing ``close`` log so dashboards keep working, plus a
    # ``deferred_close=stranger_thread`` marker to make the new path
    # observable in production triage.
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "Request API client aborted (interrupt_abort" in m
        and "tcp_force_closed=1" in m
        and "deferred_close=stranger_thread" in m
        for m in msgs
    ), f"missing abort log line; got: {msgs!r}"


def test_agent_abort_request_api_client_null_client_is_noop():
    """A ``None`` client must short-circuit cleanly (defensive)."""
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._client_log_context = lambda: "provider=test"

    # No exception, no side effect.
    agent._abort_request_api_client(None, reason="interrupt_abort")


# ---------------------------------------------------------------------------
# Real TLS regression: foreign-thread abort plus SQLite FD churn.
# ---------------------------------------------------------------------------


def _tls_context(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(minutes=5))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


def test_real_tls_abort_with_sqlite_fd_churn_preserves_database(tmp_path):
    request_started = threading.Event()
    release_response = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request_started.set()
            release_response.wait(timeout=5)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = _tls_context(tmp_path).wrap_socket(server.socket, server_side=True)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    db_path = tmp_path / "authority.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE evidence(value TEXT NOT NULL)")
        conn.execute("INSERT INTO evidence VALUES ('intact')")
    original = db_path.read_bytes()
    client = httpx.Client(verify=False, timeout=10, trust_env=False)
    outcome = {}

    def request_owner():
        try:
            client.get(f"https://127.0.0.1:{server.server_port}/blocked")
        except Exception as exc:  # abort intentionally breaks the request
            outcome["error"] = exc
        finally:
            client.close()

    owner = threading.Thread(target=request_owner)
    owner.start()
    try:
        assert request_started.wait(timeout=5)
        from agent.agent_runtime_helpers import abort_tcp_sockets

        assert abort_tcp_sockets(SimpleNamespace(_client=client)) >= 1
        for _ in range(250):
            with sqlite3.connect(db_path) as conn:
                assert conn.execute("SELECT value FROM evidence").fetchone() == ("intact",)
        release_response.set()
        owner.join(timeout=5)
        assert not owner.is_alive()
        assert "error" in outcome
        assert db_path.read_bytes() == original
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        release_response.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        owner.join(timeout=5)


# ---------------------------------------------------------------------------
# FD-recycling proof: when shutdown-only is honored, a stranger-thread abort
# CANNOT release an FD that the owning thread still references.
# ---------------------------------------------------------------------------


def test_fd_recycle_window_closed_by_shutdown_only():
    """Construct the exact race the reporter saw — abort from a stranger
    thread, then have the (simulated) kernel recycle the FD into a new file.
    With the fix, the worker's surviving socket reference cannot be
    confused with the recycled file descriptor.
    """
    from agent.agent_runtime_helpers import abort_tcp_sockets

    # Tracks "was the FD released by the abort path?" — that is the only
    # signal the kernel needs to recycle the integer to a new ``open()``.
    fd_released = {"yes": False}

    class _OwnedSocket:
        """Simulates a socket whose FD is shared with the owner's SSL BIO.

        ``close`` flips ``fd_released`` so the test can assert that with
        the fix the abort path NEVER releases the FD (and therefore the
        kernel never recycles it under the owner's still-active reference).
        """

        def __init__(self):
            self.shutdowns = 0

        def shutdown(self, _how):
            self.shutdowns += 1

        def close(self):
            fd_released["yes"] = True

    sock = _OwnedSocket()
    client = _build_fake_client(sock)

    # Stranger thread runs the abort sweep (== what asyncio_0 did in the
    # reporter's session).
    _call_inside_owner_thread(lambda: abort_tcp_sockets(client))

    assert sock.shutdowns == 1, "shutdown must wake the worker"
    assert fd_released["yes"] is False, (
        "abort_tcp_sockets released the FD from a stranger thread — "
        "this is exactly the #29507 race. The owner thread must own close()."
    )
