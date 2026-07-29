"""Structured compression statuses remain transient through the gateway."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def server():
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(
                get_hermes_home=MagicMock(return_value="/tmp/hermes_test_compaction")
            ),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        yield importlib.import_module("tui_gateway.server")


def _capture(server, monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(
        server, "_emit", lambda event, sid, payload=None: events.append(payload or {})
    )
    return events


def test_structured_compression_status_is_forwarded_unchanged(server, monkeypatch):
    events = _capture(server, monkeypatch)
    server._status_update(
        "sid",
        "compression.preparing",
        "Summarizing earlier conversation…",
    )

    assert events == [
        {
            "kind": "compression.preparing",
            "text": "Summarizing earlier conversation…",
        }
    ]


def test_lifecycle_text_is_not_retagged_by_content(server, monkeypatch):
    from agent.conversation_compression import COMPACTION_STATUS

    events = _capture(server, monkeypatch)
    server._status_update("sid", "lifecycle", COMPACTION_STATUS)

    assert events == [{"kind": "lifecycle", "text": COMPACTION_STATUS}]


def test_manual_compressing_kind_is_preserved(server, monkeypatch):
    events = _capture(server, monkeypatch)
    server._status_update("sid", "compressing", "⠋ compressing 40 messages…")

    assert events == [{"kind": "compressing", "text": "⠋ compressing 40 messages…"}]
