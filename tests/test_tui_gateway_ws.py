import asyncio
import json
import threading
import time

from hermes_cli import mcp_startup
from tui_gateway import server
from tui_gateway import ws as ws_mod


def test_owner_worker_ws_rejects_missing_runtime_before_gateway_dispatch(monkeypatch):
    class FakeWS:
        def __init__(self):
            self.closed = []

        async def close(self, *, code=1000, reason=""):
            self.closed.append((code, reason))

    ws = FakeWS()
    discovery_calls = []
    monkeypatch.setattr(
        mcp_startup,
        "start_background_mcp_discovery",
        lambda **kwargs: discovery_calls.append(kwargs),
    )

    asyncio.run(ws_mod.handle_ws(ws, require_owner_runtime=True))

    assert ws.closed == [(1011, "owner gateway runtime unavailable")]
    assert discovery_calls == []


def test_ws_gateway_ping_round_trip_requires_no_session(monkeypatch):
    sent = []
    requests = iter(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "ping-1",
                    "method": "gateway.ping",
                    "params": {},
                }
            )
        ]
    )
    monkeypatch.setattr(
        mcp_startup, "start_background_mcp_discovery", lambda **_kwargs: None
    )

    class FakeWS:
        query_params = {}

        async def accept(self):
            pass

        async def send_text(self, line):
            sent.append(json.loads(line))

        async def receive_text(self):
            try:
                return next(requests)
            except StopIteration:
                raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    previous = dict(server._sessions.items())
    server._sessions.clear()
    try:
        asyncio.run(ws_mod.handle_ws(FakeWS()))
    finally:
        server._sessions.clear()
        server._sessions.update(previous)

    assert sent[0]["method"] == "event"
    assert sent[0]["params"]["type"] == "gateway.ready"
    assert sent[1] == {"jsonrpc": "2.0", "id": "ping-1", "result": {"ok": True}}


def test_ws_startup_starts_background_mcp_discovery(monkeypatch):
    """The desktop app and dashboard chat reach the agent through this WS
    sidecar, not through tui_gateway.entry.main() (which spawns the discovery
    thread for the stdio TUI). handle_ws must start discovery itself, otherwise
    _make_agent's wait_for_mcp_discovery no-ops and the agent snapshots an
    MCP-less tool list. Regression test for #38945."""
    calls = []
    monkeypatch.setattr(
        mcp_startup,
        "start_background_mcp_discovery",
        lambda **kw: calls.append(kw),
    )

    class FakeWS:
        query_params = {}

        async def accept(self):
            pass

        async def send_text(self, line):
            pass

        async def receive_text(self):
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    server._sessions.clear()
    try:
        asyncio.run(ws_mod.handle_ws(FakeWS()))
    finally:
        server._sessions.clear()

    assert calls == [{"logger": ws_mod._log, "thread_name": "tui-ws-mcp-discovery"}]


def _run_disconnect(monkeypatch, seed):
    """Drive handle_ws to its disconnect `finally`, seeding sessions against the
    live WSTransport the moment it exists. Returns nothing; inspect _sessions."""
    # Disable the grace-reap Timer: detached sessions normally schedule a
    # threading.Timer via _schedule_ws_orphan_reap, which would outlive the test
    # and fire _reap during interpreter teardown — touching _sessions/DB and
    # producing spurious post-run errors under the per-file CI runner. Grace=0
    # short-circuits the Timer (see _schedule_ws_orphan_reap) so the test leaves
    # no lingering thread.
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)

    # Mirror the real _finalize_session chokepoint: it is the single place that
    # closes the slash-worker (#38095). Stub it but keep that behavior so the
    # disconnect-reap path still exercises worker teardown.
    def _fake_finalize(s, end_reason="tui_close"):
        w = s.get("slash_worker")
        if w:
            w.close()

    monkeypatch.setattr(server, "_finalize_session", _fake_finalize)

    created = []
    real_transport = ws_mod.WSTransport
    monkeypatch.setattr(
        ws_mod, "WSTransport",
        lambda ws, loop, **kw: created.append(real_transport(ws, loop, **kw)) or created[-1],
    )

    class FakeWS:
        query_params = {}

        async def accept(self):
            pass

        async def send_text(self, line):
            pass

        async def receive_text(self):
            seed(created[0])  # transport now exists; attach it to sessions
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    asyncio.run(ws_mod.handle_ws(FakeWS()))


def test_ws_disconnect_reaps_flagged_session_and_closes_worker(monkeypatch):
    closed = []

    class FakeWorker:
        def close(self):
            closed.append(True)

    server._sessions.clear()
    try:
        _run_disconnect(
            monkeypatch,
            lambda t: server._sessions.update(
                flagged={
                    "transport": t,
                    "close_on_disconnect": True,
                    "slash_worker": FakeWorker(),
                    "session_key": "k",
                }
            ),
        )
        assert "flagged" not in server._sessions
        assert closed == [True]
    finally:
        server._sessions.clear()


def test_ws_disconnect_preserves_and_repoints_reconnectable_session(monkeypatch):
    server._sessions.clear()
    try:
        _run_disconnect(
            monkeypatch,
            lambda t: server._sessions.update(
                plain={"transport": t, "close_on_disconnect": False, "session_key": "k"}
            ),
        )
        assert server._sessions["plain"]["transport"] is server._detached_ws_transport
    finally:
        server._sessions.clear()


class _FakeTimerHandle:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        assert not self.cancelled
        self.callback()


class _FakeTransportLoop:
    def __init__(self):
        self.timers = []

    def call_soon_threadsafe(self, callback):
        callback()

    def call_later(self, delay, callback):
        handle = _FakeTimerHandle(delay, callback)
        self.timers.append(handle)
        return handle

    def create_task(self, coroutine):
        return asyncio.run(coroutine)


def _gateway_event(event_type, *, text=None, session_id="sid"):
    payload = {} if text is None else {"text": text}
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "payload": payload,
            "session_id": session_id,
            "type": event_type,
        },
    }


def test_ws_transport_arms_one_30fps_timer_and_preserves_stream_boundaries():
    sent = []

    class FakeWS:
        async def send_text(self, line):
            sent.append(json.loads(line))

    loop = _FakeTransportLoop()
    transport = ws_mod.WSTransport(FakeWS(), loop)

    assert transport.write(_gateway_event("message.delta", text="hel")) is True
    assert transport.write(_gateway_event("message.delta", text="lo")) is True
    assert transport.write(_gateway_event("reasoning.delta", text="why")) is True
    assert transport.write(
        _gateway_event("message.delta", text="other", session_id="other-sid")
    ) is True

    assert len(loop.timers) == 1
    assert loop.timers[0].delay == ws_mod._TOKEN_COALESCE_S == 0.033

    loop.timers[0].fire()

    assert [item["params"]["type"] for item in sent] == [
        "message.delta",
        "reasoning.delta",
        "message.delta",
    ]
    assert [item["params"]["payload"]["text"] for item in sent] == [
        "hello",
        "why",
        "other",
    ]
    assert [item["params"]["session_id"] for item in sent] == [
        "sid",
        "sid",
        "other-sid",
    ]


def test_ws_transport_control_frame_immediately_drains_pending_stream(monkeypatch):
    sent = []

    class FakeWS:
        async def send_text(self, line):
            sent.append(json.loads(line))

    class FakeFuture:
        def result(self, timeout):
            return None

    def schedule(coroutine, loop):
        asyncio.run(coroutine)
        return FakeFuture()

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", schedule)
    loop = _FakeTransportLoop()
    transport = ws_mod.WSTransport(FakeWS(), loop)

    assert transport.write(_gateway_event("message.delta", text="partial ")) is True
    assert transport.write(_gateway_event("message.delta", text="answer")) is True
    assert transport.write(
        _gateway_event("message.complete", text="partial answer")
    ) is True

    assert len(loop.timers) == 1
    assert [item["params"]["type"] for item in sent] == [
        "message.delta",
        "message.complete",
    ]
    assert sent[0]["params"]["payload"]["text"] == "partial answer"
    assert sent[1]["params"]["payload"]["text"] == "partial answer"

    loop.timers[0].fire()
    assert len(sent) == 2


def test_ws_dashboard_attach_filters_inactive_and_unknown_sessionless_events(monkeypatch):
    sent = []

    class FakeWS:
        async def send_text(self, line):
            sent.append(json.loads(line))

    class FakeFuture:
        def result(self, timeout):
            return None

    def schedule(coroutine, loop):
        asyncio.run(coroutine)
        return FakeFuture()

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", schedule)
    transport = ws_mod.WSTransport(FakeWS(), _FakeTransportLoop())

    assert transport.begin_dashboard_attach(1, browser_id="browser-a") is None
    assert transport.commit_dashboard_attach(1, "runtime-a") is True
    assert transport.write(_gateway_event("message.complete", session_id="runtime-a")) is True
    assert transport.write(_gateway_event("message.complete", session_id="runtime-b")) is True
    assert transport.write(_gateway_event("approval.request", session_id="")) is True
    assert transport.write(_gateway_event("skin.changed", session_id="")) is True
    assert transport.write({"jsonrpc": "2.0", "id": "rpc", "result": {"ok": True}}) is True

    assert [item.get("id") or item["params"]["type"] for item in sent] == [
        "message.complete",
        "skin.changed",
        "rpc",
    ]


def test_ws_dashboard_attach_generation_and_scope_are_atomic():
    transport = ws_mod.WSTransport(object(), _FakeTransportLoop())

    assert transport.begin_dashboard_attach(
        4, browser_id="browser-a", profile="worker"
    ) is None
    assert transport.begin_dashboard_attach(
        4, browser_id="browser-a", profile="worker"
    ) == "session attach superseded"
    assert transport.begin_dashboard_attach(
        5, browser_id="browser-b", profile="worker"
    ) == "dashboard attach scope mismatch"
    assert transport.begin_dashboard_attach(
        5, browser_id="browser-a", profile="other"
    ) == "dashboard attach scope mismatch"
    assert transport.begin_dashboard_attach(
        5, browser_id="browser-a", profile="worker"
    ) is None

    assert transport.commit_dashboard_attach(4, "runtime-old") is False
    assert transport.commit_dashboard_attach(5, "runtime-new") is True
    assert transport.dashboard_attach_is_current(4) is False
    assert transport.dashboard_attach_is_current(5) is True


def test_ws_dashboard_mutation_fence_tracks_pending_and_committed_runtime():
    transport = ws_mod.WSTransport(object(), _FakeTransportLoop())

    assert transport.dashboard_mutation_error("runtime-any") is None
    assert transport.begin_dashboard_attach(1, browser_id="browser-a") is None
    assert transport.dashboard_mutation_error("runtime-a") == (
        "dashboard session switch in progress"
    )
    assert transport.commit_dashboard_attach(1, "runtime-a") is True
    assert transport.dashboard_mutation_error("runtime-a") is None
    assert transport.dashboard_mutation_error("runtime-b") == (
        "dashboard mutation targets an inactive session"
    )
    assert transport.dashboard_mutation_error("") == (
        "dashboard mutation targets an inactive session"
    )


def test_ws_dashboard_attach_abort_restores_previous_runtime_without_clearing_newer():
    transport = ws_mod.WSTransport(object(), _FakeTransportLoop())
    assert transport.begin_dashboard_attach(1, browser_id="browser-a") is None
    assert transport.commit_dashboard_attach(1, "runtime-a") is True

    assert transport.begin_dashboard_attach(2, browser_id="browser-a") is None
    transport.abort_dashboard_attach(2)
    assert transport.dashboard_mutation_error("runtime-a") is None

    assert transport.begin_dashboard_attach(3, browser_id="browser-a") is None
    assert transport.begin_dashboard_attach(4, browser_id="browser-a") is None
    transport.abort_dashboard_attach(3)
    assert transport.dashboard_mutation_error("runtime-a") == (
        "dashboard session switch in progress"
    )
    assert transport.commit_dashboard_attach(4, "runtime-b") is True
    assert transport.dashboard_mutation_error("runtime-b") is None
    assert transport.dashboard_mutation_error("runtime-a") == (
        "dashboard mutation targets an inactive session"
    )


def test_ws_dashboard_pending_attach_keeps_previous_outbound_subscription():
    sent = []

    class FakeWS:
        async def send_text(self, line):
            sent.append(json.loads(line))

    transport = ws_mod.WSTransport(FakeWS(), _FakeTransportLoop())
    assert transport.begin_dashboard_attach(1, browser_id="browser-a") is None
    assert transport.commit_dashboard_attach(1, "runtime-a") is True
    assert transport.begin_dashboard_attach(2, browser_id="browser-a") is None

    asyncio.run(
        transport._safe_send(
            json.dumps(_gateway_event("message.delta", session_id="runtime-a"))
        )
    )
    asyncio.run(
        transport._safe_send(
            json.dumps(_gateway_event("message.delta", session_id="runtime-b"))
        )
    )

    assert [item["params"]["session_id"] for item in sent] == ["runtime-a"]


def test_ws_dashboard_attach_drops_coalesced_tokens_after_subscription_switch():
    sent = []

    class FakeWS:
        async def send_text(self, line):
            sent.append(json.loads(line))

    loop = _FakeTransportLoop()
    transport = ws_mod.WSTransport(FakeWS(), loop)
    assert transport.begin_dashboard_attach(1, browser_id="browser-a") is None
    assert transport.commit_dashboard_attach(1, "runtime-a") is True

    assert transport.write(
        _gateway_event("message.delta", text="stale", session_id="runtime-a")
    ) is True
    assert len(loop.timers) == 1

    assert transport.begin_dashboard_attach(2, browser_id="browser-a") is None
    assert transport.commit_dashboard_attach(2, "runtime-b") is True
    loop.timers[0].fire()

    assert sent == []


def test_ws_transport_merges_streaming_frames_in_one_flush():
    lines = [
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "payload": {"text": "hel"},
                    "session_id": "sid",
                    "type": "message.delta",
                },
            }
        ),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "payload": {"text": "lo", "rendered": "hello"},
                    "session_id": "sid",
                    "type": "message.delta",
                },
            }
        ),
        json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "tool.start"}}),
    ]

    merged = ws_mod._merge_streaming_lines(lines)

    assert len(merged) == 2
    first = json.loads(merged[0])
    assert first["params"]["payload"] == {"text": "hello", "rendered": "hello"}
    assert json.loads(merged[1])["params"]["type"] == "tool.start"


def test_ws_write_loop_stall_does_not_latch_transport(monkeypatch):
    """A write that times out because the event loop is stalled (GIL-heavy
    agent turn) must NOT latch the transport closed — the frame is already
    scheduled and flushes when the loop recovers. Latching here permanently
    silenced live watch windows after one slow write."""
    monkeypatch.setattr(ws_mod, "_WS_WRITE_TIMEOUT_S", 0.05)
    sent = []

    class FakeWS:
        async def send_text(self, line):
            sent.append(line)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        transport = ws_mod.WSTransport(FakeWS(), loop, peer="stall-test")
        # Stall the loop well past the write timeout, then write from this
        # (non-loop) thread: the wait times out but the send stays in flight.
        loop.call_soon_threadsafe(time.sleep, 0.3)
        assert transport.write({"a": 1}) is True
        assert transport._closed is False

        # Once the loop breathes again, both the stalled frame and new writes
        # must reach the socket.
        assert transport.write({"b": 2}) is True
        deadline = time.time() + 2
        while len(sent) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert len(sent) == 2
        assert transport._closed is False
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
