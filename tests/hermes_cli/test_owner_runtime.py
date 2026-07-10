import os
import stat
from pathlib import Path

import pytest

from hermes_cli.owner_runtime import (
    REQUIRED_OWNER_DIRS,
    assert_owner_runtime_paths,
    ensure_owner_runtime_dirs,
    get_default_workspace,
    get_workspace_root,
    propagate_owner_env,
    resolve_workspace_cwd,
)


def test_runtime_owner_home_is_derived_only_from_worker_environment(tmp_path, monkeypatch):
    host_owner_home = tmp_path / "host-global" / "users" / "ok1_owner"
    runtime_owner_home = tmp_path / "runtime-view" / "owner"
    monkeypatch.setenv("HERMES_HOME", str(runtime_owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_owner")
    monkeypatch.delenv("HERMES_WORKSPACE_ROOT", raising=False)

    assert get_workspace_root() == runtime_owner_home / "workspaces"
    assert get_default_workspace() == runtime_owner_home / "workspaces" / "default"
    assert get_workspace_root() != host_owner_home / "workspaces"


def test_workspace_root_defaults_under_owner_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_WORKSPACE_ROOT", raising=False)

    assert get_workspace_root() == (tmp_path / "workspaces").resolve()
    assert get_default_workspace() == (tmp_path / "workspaces" / "default").resolve()


def test_resolve_workspace_cwd_rejects_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(tmp_path / "owner" / "workspaces"))
    inside = tmp_path / "owner" / "workspaces" / "project"
    inside.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    assert resolve_workspace_cwd(inside) == inside.resolve()
    with pytest.raises(ValueError):
        resolve_workspace_cwd(outside)


def test_resolve_workspace_cwd_rejects_symlink_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    root = tmp_path / "owner" / "workspaces"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ValueError):
        resolve_workspace_cwd(link)


def test_ensure_owner_runtime_dirs_creates_canonical_dirs(tmp_path):
    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")

    for rel in REQUIRED_OWNER_DIRS:
        assert (owner_home / rel).is_dir()
    assert (owner_home / "memories").is_dir()
    assert not (owner_home / "memory").exists()


def test_ensure_owner_runtime_dirs_uses_private_modes_on_posix(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX mode bits unavailable")
    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")

    for path in [owner_home, owner_home / "runtime", owner_home / "workspaces", owner_home / "workspaces" / "default"]:
        assert path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def test_ensure_owner_runtime_dirs_rejects_symlink_escape(tmp_path):
    owner = tmp_path / "owner"
    outside = tmp_path / "outside"
    outside.mkdir()
    (owner / "runtime").mkdir(parents=True)
    link = owner / "sessions"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(RuntimeError):
        ensure_owner_runtime_dirs(owner)


def test_propagate_owner_env_includes_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok_owner")
    monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(tmp_path / "owner" / "workspaces"))
    env = {}

    propagate_owner_env(env)

    assert env["HERMES_HOME"] == str(tmp_path / "owner")
    assert env["HERMES_OWNER_KEY"] == "ok_owner"
    assert env["HERMES_WORKSPACE_ROOT"] == str(tmp_path / "owner" / "workspaces")


def test_assert_owner_runtime_paths_rejects_workspace_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok_owner")
    monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(tmp_path / "other" / "workspaces"))

    with pytest.raises(RuntimeError):
        assert_owner_runtime_paths()
