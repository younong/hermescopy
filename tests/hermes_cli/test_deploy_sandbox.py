from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "deploy.mjs"
GENERATOR = ROOT / "deploy" / "sandbox" / "generate_seccomp.py"
ARTIFACT = ROOT / "deploy" / "sandbox" / "executor-x86_64.bpf"
MANIFEST = ROOT / "deploy" / "sandbox" / "executor-x86_64.json"


def test_deploy_uses_nonroot_service_immutable_runtime_and_host_policy():
    source = DEPLOY.read_text(encoding="utf-8")

    assert 'runtimes_dir="$remote_root/runtimes/python"' in source
    assert 'runtime_id="py311-${"${"}architecture}-${"${"}lock_hash}-sandbox2"' in source
    assert 'venv="$shared/venv"' not in source
    assert "User=$service_user" in source
    assert "Group=$service_group" in source
    assert "Environment=HERMES_DISABLE_LAZY_INSTALLS=1" in source
    assert (
        "Environment=HERMES_SANDBOX_DEPLOYMENT_POLICY="
        "hermes_cli.owner_worker.host_sandbox:host_sandbox_deployment_policy"
    ) in source
    assert "ExecStartPre=$venv/bin/python" in source
    assert "uv python install \"$python_version\" --install-dir \"$runtime_tmp/python-base\" --no-bin" in source
    assert "uv sync --extra all --locked --no-editable --link-mode copy" in source
    assert 'resolved_python="$(readlink -f "$runtime_tmp/bin/python3")"' in source
    assert 'ldd "$resolved_python"' in source
    assert 'for destination in /bin /usr/bin /lib /lib64 /usr/lib /usr/lib64; do' in source
    assert 'runtime_tmp/toolchain' in source
    assert 'command_path="$(type -P "$command" || true)"' in source
    assert 'command_path="$(command -v "$command" || true)"' not in source
    assert 'chown -R root:root "$release_tmp"' in source
    assert 'find "$release_tmp" -type d -exec chmod go-w {} +' in source
    assert source.index("host_sandbox_deployment_policy()") < source.index('ln -sfnT "$release" "$current"')
    assert "restoring previous deployment state" in source
    assert "restore_deployment_state" in source
    assert "HERMES_EXECUTOR_START_GATE_FD" not in source


def test_seccomp_artifact_is_reproducible_and_manifest_bound(tmp_path):
    output = tmp_path / "executor.bpf"
    manifest = tmp_path / "executor.json"
    subprocess.run(
        ["python3", str(GENERATOR), "--output", str(output), "--manifest", str(manifest)],
        check=True,
    )

    assert output.read_bytes() == ARTIFACT.read_bytes()
    expected = json.loads(MANIFEST.read_text(encoding="utf-8"))
    actual = json.loads(manifest.read_text(encoding="utf-8"))
    assert actual == expected
    assert expected["artifact_sha256"] == hashlib.sha256(ARTIFACT.read_bytes()).hexdigest()
    assert {"mount", "setns", "unshare", "ptrace", "bpf"} <= set(expected["denied_syscalls"])
