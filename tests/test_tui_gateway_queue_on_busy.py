"""A prompt that lands mid-turn is interrupted + queued, never dropped.

Before this, ``prompt.submit`` on a running session returned ``session busy``,
forcing clients into a deadline-bounded busy-retry. When turn teardown outlived
the deadline — e.g. a slow, non-interruptible tool (``web_search``) still
running when the user hit stop — the resubmitted message was silently dropped
("it just doesn't listen"). The gateway now applies the ``busy_input_mode``
policy: interrupt the live turn (default) and queue the message to run as the
next turn, drained in ``run``'s tail.
"""

import threading
import types

from tui_gateway import server


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "transport": None,
        "attached_images": [],
        **extra,
    }


# ── _enqueue_prompt ────────────────────────────────────────────────────────

def test_enqueue_pins_text_transport_and_active_generation():
    session = _session(_active_turn_generation=7)
    server._enqueue_prompt(session, "hello", "ws-1")
    assert session["queued_prompt"] == {
        "text": "hello",
        "transport": "ws-1",
        "owner_generation": 7,
    }


def test_enqueue_merges_second_arrival_losslessly():
    session = _session()
    server._enqueue_prompt(session, "first", "ws-1")
    server._enqueue_prompt(session, "second", "ws-2")
    assert session["queued_prompt"]["text"] == "first\n\nsecond"
    # Latest transport wins so the drain streams to the most recent client.
    assert session["queued_prompt"]["transport"] == "ws-2"


# ── _handle_busy_submit (policy) ───────────────────────────────────────────

def test_busy_interrupt_mode_interrupts_and_queues(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    calls = {"interrupt": 0}
    agent = types.SimpleNamespace(interrupt=lambda *a, **k: calls.__setitem__("interrupt", calls["interrupt"] + 1))
    session = _session(agent=agent)

    resp = server._handle_busy_submit("r1", "sid", session, "redirect", "ws-1")

    assert resp["result"]["status"] == "queued"
    assert calls["interrupt"] == 1
    assert session["queued_prompt"]["text"] == "redirect"


def test_busy_queue_mode_queues_without_interrupting(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    calls = {"interrupt": 0}
    agent = types.SimpleNamespace(interrupt=lambda *a, **k: calls.__setitem__("interrupt", calls["interrupt"] + 1))
    session = _session(agent=agent)

    resp = server._handle_busy_submit("r1", "sid", session, "later", "ws-1")

    assert resp["result"]["status"] == "queued"
    assert calls["interrupt"] == 0
    assert session["queued_prompt"]["text"] == "later"


def test_busy_steer_mode_injects_when_accepted(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    agent = types.SimpleNamespace(steer=lambda text: True, interrupt=lambda *a, **k: None)
    session = _session(agent=agent)

    resp = server._handle_busy_submit("r1", "sid", session, "nudge", "ws-1")

    assert resp["result"]["status"] == "steered"
    assert session.get("queued_prompt") is None


def test_busy_steer_mode_falls_back_to_queue_when_rejected(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    agent = types.SimpleNamespace(steer=lambda text: False, interrupt=lambda *a, **k: None)
    session = _session(agent=agent)

    resp = server._handle_busy_submit("r1", "sid", session, "nudge", "ws-1")

    assert resp["result"]["status"] == "queued"
    assert session["queued_prompt"]["text"] == "nudge"


# ── _drain_queued_prompt ───────────────────────────────────────────────────

def test_drain_fires_queued_prompt_and_claims_running(monkeypatch):
    fired = {}
    monkeypatch.setattr(
        server, "_run_prompt_submit",
        lambda rid, sid, session, text, **kwargs: fired.update(
            rid=rid, sid=sid, text=text, generation=kwargs.get("generation")
        ),
    )
    session = _session(queued_prompt={"text": "go", "transport": "ws-9"})

    assert server._drain_queued_prompt("r1", "sid", session) is True
    assert fired == {
        "rid": "r1",
        "sid": "sid",
        "text": "go",
        "generation": 1,
    }
    assert session["running"] is True
    assert session["_active_turn_generation"] == 1
    assert session["queued_prompt"] is None
    assert session["transport"] == "ws-9"


def test_drain_noop_when_nothing_queued(monkeypatch):
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not fire")))
    session = _session()
    assert server._drain_queued_prompt("r1", "sid", session) is False
    assert session["running"] is False


def test_drain_noop_when_session_already_running(monkeypatch):
    """A fresh turn that claimed the session beats a stale queued entry —
    the drain leaves it for that turn's own tail."""
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not fire")))
    session = _session(running=True, queued_prompt={"text": "go", "transport": None})
    assert server._drain_queued_prompt("r1", "sid", session) is False
    assert session["queued_prompt"]["text"] == "go"


def test_drain_releases_running_on_dispatch_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("dispatch failed")
    monkeypatch.setattr(server, "_run_prompt_submit", _boom)
    session = _session(queued_prompt={"text": "go", "transport": None})

    assert server._drain_queued_prompt("r1", "sid", session) is True
    # Failure must not leave the session wedged as running.
    assert session["running"] is False


def test_stop_discards_only_same_generation_queued_prompt(monkeypatch):
    interrupted = []
    agent = types.SimpleNamespace(interrupt=lambda: interrupted.append(True))
    session = _session(
        agent=agent,
        running=True,
        _active_turn_generation=4,
        queued_prompt={
            "text": "same turn replacement",
            "transport": None,
            "owner_generation": 4,
        },
    )
    monkeypatch.setattr(server, "_sess", lambda _params, _rid: (session, None))
    monkeypatch.setattr(server, "_clear_pending", lambda *_a, **_k: None)

    response = server._methods["session.interrupt"](
        "r1", {"session_id": "sid"}
    )

    assert response["result"]["status"] == "interrupted"
    assert interrupted == [True]
    assert session["queued_prompt"] is None
    assert session["_turn_cancel_generation"] == 4


def test_stale_turn_cleanup_cannot_clear_new_generation():
    session = _session(
        running=True,
        _active_turn_generation=2,
        inflight_turn={"generation": 2, "user": "new"},
    )

    server._clear_inflight_turn(session, generation=1)

    assert session["inflight_turn"] == {"generation": 2, "user": "new"}
    assert session["running"] is True


def test_prompt_after_stop_is_owned_by_new_generation(monkeypatch):
    session = _session(
        running=False,
        _turn_generation=5,
        _active_turn_generation=5,
        _turn_cancel_requested=True,
        _turn_cancel_generation=5,
        queued_prompt={
            "text": "after stop",
            "transport": "ws-2",
            "owner_generation": 5,
        },
    )
    fired = {}
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda rid, sid, session, text, **kwargs: fired.update(
            text=text, generation=kwargs["generation"]
        ),
    )

    assert server._drain_queued_prompt("r2", "sid", session) is True

    assert fired == {"text": "after stop", "generation": 6}
    assert session["_active_turn_generation"] == 6
    assert session["_turn_cancel_requested"] is False
