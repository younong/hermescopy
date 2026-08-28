from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "deploy" / "check-executor-cgroup-host.py"
_spec = importlib.util.spec_from_file_location("check_executor_cgroup_host", SCRIPT)
assert _spec is not None and _spec.loader is not None
preflight = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = preflight
_spec.loader.exec_module(preflight)


def _host_tree(tmp_path: Path) -> tuple[Path, Path]:
    service_root = tmp_path / "sys" / "fs" / "cgroup" / "system.slice" / "hermes-dashboard.service"
    managed_root = service_root / "authenticated-owners"
    managed_root.mkdir(parents=True)
    (service_root / "cgroup.controllers").write_text("cpu memory pids\n", encoding="ascii")
    (service_root / "memory.swap.max").write_text("max\n", encoding="ascii")
    (service_root / "cgroup.freeze").write_text("0\n", encoding="ascii")
    (service_root / "cgroup.kill").write_text("\n", encoding="ascii")
    (service_root / "cgroup.procs").write_text("\n", encoding="ascii")
    (managed_root / "cgroup.procs").write_text("\n", encoding="ascii")
    return service_root, managed_root


def _systemd_show(*, soft: str = "65536", hard: str = "1048576") -> str:
    return "\n".join(
        (
            "Delegate=cpu memory pids",
            "CPUAccounting=yes",
            "MemoryAccounting=yes",
            "TasksAccounting=yes",
            "KillMode=mixed",
            f"LimitNOFILESoft={soft}",
            f"LimitNOFILE={hard}",
        )
    )


def _inspect(tmp_path: Path, monkeypatch, *, soft: str = "65536", hard: str = "1048576"):
    service_root, managed_root = _host_tree(tmp_path)
    monkeypatch.setattr(preflight, "_mountpoint", lambda: service_root.parent)
    calls = []

    def _run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, _systemd_show(soft=soft, hard=hard), "")

    monkeypatch.setattr(preflight.subprocess, "run", _run)
    result = preflight.inspect(
        managed_root,
        service="hermes-dashboard.service",
        expected_soft_nofile=65536,
        expected_hard_nofile=1048576,
    )
    return result, calls


def test_parse_systemd_limit_accepts_only_unsigned_decimal_integers():
    assert preflight._parse_systemd_limit("65536") == 65536
    assert preflight._parse_systemd_limit("0") == 0
    for value in (None, "", "infinity", " 65536", "+65536", "65_536", "１２"):
        assert preflight._parse_systemd_limit(value) is None


def test_inspect_requires_expected_systemd_nofile_limits(tmp_path, monkeypatch):
    result, calls = _inspect(tmp_path, monkeypatch)

    assert result["ready"] is True
    assert result["resourceReady"] is True
    assert result["mandatoryReady"] is True
    assert result["capabilities"]["systemdNofileLimits"] is True
    assert result["limits"]["nofile"] == {
        "expectedSoft": 65536,
        "expectedHard": 1048576,
        "observedSoft": 65536,
        "observedHard": 1048576,
    }
    command, kwargs = calls[0]
    assert "--property=LimitNOFILESoft" in command
    assert "--property=LimitNOFILE" in command
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin"}


@pytest.mark.parametrize(
    ("soft", "hard", "observed_soft", "observed_hard"),
    [
        ("65535", "1048576", 65535, 1048576),
        ("65536", "1048575", 65536, 1048575),
        ("infinity", "1048576", None, 1048576),
        ("65536", "not-a-number", 65536, None),
    ],
)
def test_inspect_fails_closed_for_low_or_malformed_nofile_limits(
    tmp_path, monkeypatch, soft, hard, observed_soft, observed_hard
):
    result, _ = _inspect(tmp_path, monkeypatch, soft=soft, hard=hard)

    assert result["ready"] is False
    assert result["resourceReady"] is True
    assert result["mandatoryReady"] is False
    assert result["capabilities"]["systemdNofileLimits"] is False
    assert result["limits"]["nofile"]["observedSoft"] == observed_soft
    assert result["limits"]["nofile"]["observedHard"] == observed_hard


def test_require_mandatory_rejects_nofile_drift_without_requiring_cgroup_readiness(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(
        preflight,
        "inspect",
        lambda *_args, **_kwargs: {
            "ready": False,
            "resourceReady": True,
            "mandatoryReady": False,
        },
    )

    result = preflight.main(
        [
            "--managed-root",
            str(tmp_path),
            "--expected-soft-nofile",
            "65536",
            "--expected-hard-nofile",
            "1048576",
            "--require-mandatory",
        ]
    )

    assert result == 1
    assert '"mandatoryReady":false' in capsys.readouterr().out


def test_require_mandatory_allows_optional_cgroup_migration_state(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        preflight,
        "inspect",
        lambda *_args, **_kwargs: {
            "ready": False,
            "resourceReady": False,
            "mandatoryReady": True,
        },
    )

    assert preflight.main(
        [
            "--managed-root",
            str(tmp_path),
            "--expected-soft-nofile",
            "65536",
            "--expected-hard-nofile",
            "1048576",
            "--require-mandatory",
        ]
    ) == 0


def test_inspect_rejects_invalid_expected_thresholds(tmp_path):
    _, managed_root = _host_tree(tmp_path)

    invalid_limits = (
        (1048576, 65536),
        (0, 1048576),
        (65536.0, 1048576),
    )
    for soft, hard in invalid_limits:
        with pytest.raises(ValueError, match="expected nofile limits must be positive"):
            preflight.inspect(
                managed_root,
                service="hermes-dashboard.service",
                expected_soft_nofile=soft,  # type: ignore[arg-type]
                expected_hard_nofile=hard,
            )
