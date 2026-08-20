from __future__ import annotations

import pytest


def test_profile_homes_use_default_root_and_named_directories(tmp_path, monkeypatch):
    from hermes_cli import cron_dashboard

    root = tmp_path / "hermes"
    (root / "profiles" / "worker").mkdir(parents=True)
    (root / "profiles" / "Invalid.Name").mkdir()
    monkeypatch.setattr(cron_dashboard, "get_default_hermes_root", lambda: root)

    assert cron_dashboard.profile_homes() == [
        ("default", root),
        ("worker", root / "profiles" / "worker"),
    ]
    assert cron_dashboard.profile_home("Worker") == (
        "worker",
        root / "profiles" / "worker",
    )


def test_profile_home_rejects_invalid_and_missing_names(tmp_path, monkeypatch):
    from hermes_cli import cron_dashboard

    root = tmp_path / "hermes"
    monkeypatch.setattr(cron_dashboard, "get_default_hermes_root", lambda: root)

    assert cron_dashboard.profile_home("default") == ("default", root)
    with pytest.raises(ValueError):
        cron_dashboard.profile_home("../other")
    with pytest.raises(FileNotFoundError):
        cron_dashboard.profile_home("missing")
