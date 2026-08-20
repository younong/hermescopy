from __future__ import annotations

from dataclasses import dataclass


def test_list_jobs_annotations_are_local_profile_only(tmp_path):
    from hermes_cli.cron_management import create_job, list_jobs

    home = tmp_path / "profile"
    created = create_job(
        home,
        {"prompt": "report", "schedule": "every 1h"},
        profile="worker",
    )

    assert created["profile"] == "worker"
    assert created["profile_name"] == "worker"
    assert created["hermes_home"] == str(home.resolve())
    assert created["is_default_profile"] is False
    assert list_jobs(home, profile="worker")[0]["profile"] == "worker"
    assert "profile" not in list_jobs(home)[0]


def test_delivery_targets_load_each_selected_home_without_global_env_mutation(
    tmp_path, monkeypatch,
):
    from hermes_cli.cron_management import delivery_targets
    from hermes_constants import get_hermes_home

    first = (tmp_path / "first").resolve()
    second = (tmp_path / "second").resolve()
    observed = []

    @dataclass(frozen=True)
    class _Platform:
        value: str

    class _Config:
        def __init__(self, platform: _Platform, has_home: bool):
            self.platform = platform
            self.has_home = has_home

        def get_connected_platforms(self):
            return [self.platform]

        def get_home_channel(self, platform):
            assert platform is self.platform
            return object() if self.has_home else None

    def _load_gateway_config():
        selected = get_hermes_home().resolve()
        observed.append(selected)
        if selected == first:
            return _Config(_Platform("matrix"), True)
        if selected == second:
            return _Config(_Platform("slack"), False)
        raise AssertionError(f"unexpected selected home: {selected}")

    monkeypatch.setattr("gateway.config.load_gateway_config", _load_gateway_config)
    outer_home = get_hermes_home()

    first_targets = {item["id"]: item for item in delivery_targets(first)}
    second_targets = {item["id"]: item for item in delivery_targets(second)}

    assert observed == [first, second]
    assert get_hermes_home() == outer_home
    assert first_targets["matrix"]["home_target_set"] is True
    assert "slack" not in first_targets
    assert second_targets["slack"]["home_target_set"] is False
    assert "matrix" not in second_targets
