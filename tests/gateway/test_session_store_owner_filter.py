import json
from datetime import datetime
from unittest.mock import patch

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionEntry, SessionStore


def _entry(session_key: str, session_id: str, *, owner_key: str | None = None) -> dict:
    entry = SessionEntry(
        session_key=session_key,
        session_id=session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        owner_key=owner_key,
    ).to_dict()
    if owner_key is None:
        entry.pop("owner_key", None)
    return entry


def _load_entries(tmp_path, monkeypatch, *, owner_key: str | None):
    if owner_key is None:
        monkeypatch.delenv("HERMES_OWNER_KEY", raising=False)
    else:
        monkeypatch.setenv("HERMES_OWNER_KEY", owner_key)
    config = GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))
    with patch("gateway.session.SessionDB", side_effect=RuntimeError("no db")):
        store = SessionStore(sessions_dir=tmp_path, config=config)
    store._ensure_loaded()
    return store._entries


def test_owner_worker_loads_only_matching_sessions_json_entries(tmp_path, monkeypatch):
    data = {
        "mine": _entry("mine", "sid_mine", owner_key="ok1_mine"),
        "other": _entry("other", "sid_other", owner_key="ok1_other"),
        "legacy": _entry("legacy", "sid_legacy", owner_key=None),
    }
    (tmp_path / "sessions.json").write_text(json.dumps(data), encoding="utf-8")

    entries = _load_entries(tmp_path, monkeypatch, owner_key="ok1_mine")

    assert set(entries) == {"mine"}
    assert entries["mine"].session_id == "sid_mine"


def test_owner_worker_accepts_origin_owner_metadata(tmp_path, monkeypatch):
    entry = _entry("origin-owned", "sid_origin", owner_key=None)
    entry["origin"] = {
        "platform": "telegram",
        "chat_id": "chat-1",
        "chat_type": "dm",
        "owner_key": "ok1_mine",
    }
    (tmp_path / "sessions.json").write_text(json.dumps({"origin-owned": entry}), encoding="utf-8")

    entries = _load_entries(tmp_path, monkeypatch, owner_key="ok1_mine")

    assert set(entries) == {"origin-owned"}


def test_local_mode_keeps_legacy_sessions_json_entries(tmp_path, monkeypatch):
    data = {
        "mine": _entry("mine", "sid_mine", owner_key="ok1_mine"),
        "legacy": _entry("legacy", "sid_legacy", owner_key=None),
    }
    (tmp_path / "sessions.json").write_text(json.dumps(data), encoding="utf-8")

    entries = _load_entries(tmp_path, monkeypatch, owner_key=None)

    assert set(entries) == {"mine", "legacy"}
