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
    owner_worker_env_for,
    owner_worker_runtime_paths,
    propagate_owner_env,
    resolve_workspace_cwd,
    validate_owner_worker_runtime_environment,
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

    for path in [
        owner_home,
        owner_home / "runtime",
        owner_home / "runtime" / "tmp",
        owner_home / "workspaces",
        owner_home / "workspaces" / "default",
    ]:
        assert path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def test_owner_runtime_temporary_root_is_canonical_and_ignores_tmpdir(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "shared-tmp"))
    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")

    paths = owner_worker_runtime_paths(owner_home=owner_home, worker_generation=1)

    assert paths.paths["temporary_root"] == owner_home / "runtime" / "tmp"
    assert paths.paths["temporary_root"].is_dir()
    assert paths.paths["temporary_root"] != tmp_path / "shared-tmp"


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


def _worker_env(tmp_path) -> tuple[Path, dict[str, str]]:
    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    return owner_home, owner_worker_env_for(
        owner_key="ok_owner",
        owner_home=owner_home,
        control_home=tmp_path / "control",
        worker_generation=3,
        worker_id="worker-3",
        lease_version=2,
        recovery_generation=0,
        capability_issuer="owc1-1",
        capability_public_key="public-key",
        capability_retained_public_keys="{}",
    )


def test_owner_worker_runtime_contract_requires_exact_minimal_environment(tmp_path):
    owner_home, env = _worker_env(tmp_path)

    paths = validate_owner_worker_runtime_environment(
        owner_home=owner_home,
        owner_key="ok_owner",
        worker_generation=3,
        worker_id="worker-3",
        source=env,
    )

    assert paths.workspace_root == owner_home / "workspaces"
    assert paths.default_workspace == owner_home / "workspaces" / "default"
    assert paths.worker_socket == owner_home / "runtime" / "workers" / "3" / "worker.sock"
    assert paths.paths["channel_directory"] == owner_home / "channel_directory.json"
    assert paths.paths["mirror_sessions_index"] == owner_home / "sessions" / "sessions.json"


@pytest.mark.parametrize("key", ["HERMES_PROFILE", "HERMES_CONFIG", "HERMES_ENV", "TERMINAL_CWD"])
def test_owner_worker_runtime_contract_rejects_forbidden_inherited_environment(tmp_path, key):
    owner_home, env = _worker_env(tmp_path)
    env[key] = "poisoned"

    with pytest.raises(RuntimeError, match="forbidden"):
        validate_owner_worker_runtime_environment(owner_home=owner_home, source=env)


def test_owner_worker_environment_serializes_only_safe_deployment_descriptor(tmp_path):
    from hermes_cli.deployment_inference import DeploymentInferenceDescriptor

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    env = owner_worker_env_for(
        owner_key="ok_owner",
        owner_home=owner_home,
        control_home=tmp_path / "control",
        worker_generation=3,
        worker_id="worker-3",
        lease_version=2,
        recovery_generation=0,
        capability_issuer="owc1-1",
        capability_public_key="public-key",
        capability_retained_public_keys="{}",
        deployment_inference_descriptor=DeploymentInferenceDescriptor(
            provider="custom:deployment",
            model="gpt-safe",
            api_mode="chat_completions",
            policy_id="policy-v1",
            allowed_models=("gpt-safe",),
            supports_vision=True,
        ),
    )

    assert env["HERMES_DEPLOYMENT_INFERENCE_PROVIDER"] == "custom:deployment"
    assert env["HERMES_DEPLOYMENT_INFERENCE_MODEL"] == "gpt-safe"
    assert env["HERMES_DEPLOYMENT_INFERENCE_SUPPORTS_VISION"] == "true"
    assert "API_KEY" not in " ".join(env)
    assert "BASE_URL" not in " ".join(env)
    validate_owner_worker_runtime_environment(owner_home=owner_home, source=env)


def test_owner_worker_environment_omits_unknown_deployment_vision_capability(tmp_path):
    from hermes_cli.deployment_inference import DeploymentInferenceDescriptor

    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    env = owner_worker_env_for(
        owner_key="ok_owner",
        owner_home=owner_home,
        control_home=tmp_path / "control",
        worker_generation=3,
        worker_id="worker-3",
        lease_version=2,
        recovery_generation=0,
        capability_issuer="owc1-1",
        capability_public_key="public-key",
        capability_retained_public_keys="{}",
        deployment_inference_descriptor=DeploymentInferenceDescriptor(
            provider="custom:deployment",
            model="gpt-safe",
            api_mode="chat_completions",
            policy_id="policy-v1",
            allowed_models=("gpt-safe",),
        ),
    )

    assert "HERMES_DEPLOYMENT_INFERENCE_SUPPORTS_VISION" not in env


def test_owner_worker_runtime_contract_rejects_unknown_or_wrong_owner_local_path(tmp_path):
    owner_home, env = _worker_env(tmp_path)
    env["HERMES_UNEXPECTED_SELECTOR"] = "poisoned"
    with pytest.raises(RuntimeError, match="unexpected"):
        validate_owner_worker_runtime_environment(owner_home=owner_home, source=env)

    env.pop("HERMES_UNEXPECTED_SELECTOR")
    paths = validate_owner_worker_runtime_environment(owner_home=owner_home, source=env)
    with pytest.raises(RuntimeError, match="canonical"):
        assert_owner_runtime_paths(
            [("process_registry", owner_home / "runtime" / "processes.json")],
            expected_paths=paths,
        )


def test_assert_owner_runtime_paths_rejects_workspace_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok_owner")
    monkeypatch.setenv("HERMES_WORKSPACE_ROOT", str(tmp_path / "other" / "workspaces"))

    with pytest.raises(RuntimeError):
        assert_owner_runtime_paths()
