from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "deploy.mjs"
GENERATOR = ROOT / "deploy" / "sandbox" / "generate_seccomp.py"
ARTIFACT = ROOT / "deploy" / "sandbox" / "executor-x86_64.bpf"
MANIFEST = ROOT / "deploy" / "sandbox" / "executor-x86_64.json"


def test_deploy_uses_nonroot_service_immutable_runtime_and_host_policy():
    source = DEPLOY.read_text(encoding="utf-8")

    assert 'runtimes_dir="$remote_root/runtimes/python"' in source
    assert 'runtime_id="py311-${"${"}architecture}-${"${"}runtime_inputs_hash}-sandbox9"' in source
    assert 'powerpoint_lock_hash="$(sha256sum "$release/deploy/powerpoint-runtime/package-lock.json"' in source
    assert 'powerpoint_package_hash=' in source
    assert 'node_identity=' in source
    powerpoint_packages = {
        package["name"]
        for package in json.loads(
            (ROOT / "deploy/runtime/alicloud3-powerpoint-packages.json").read_text(
                encoding="utf-8"
            )
        )["packages"]
    }
    assert {
        "nss-softokn",
        "nss-softokn-freebl",
        "nss-sysinit",
        "p11-kit-trust",
        "sqlite-libs",
    } <= powerpoint_packages
    assert 'venv="$shared/venv"' not in source
    assert 'service_user="hermes"' in source
    assert 'service_group="hermes"' in source
    assert 'chown -R "$service_user:$service_group" "$hermes_home"' in source
    assert source.count("User=$service_user") == 2
    assert source.count("Group=$service_group") == 2
    assert "Environment=HERMES_DASHBOARD_PUBLIC_URL=$dashboard_public_url" in source
    assert source.count("Environment=HERMES_SANDBOX_DEPLOYMENT_POLICY=") == 2
    assert source.count("Environment=HERMES_DISABLE_LAZY_INSTALLS=1") == 2
    assert "--require-auth --trust-proxy-headers" in source
    assert (
        "Environment=HERMES_SANDBOX_DEPLOYMENT_POLICY="
        "hermes_cli.owner_worker.host_sandbox:host_sandbox_deployment_policy"
    ) in source
    assert "ExecStartPre=$venv/bin/python" in source
    assert "uv python install \"$python_version\" --install-dir \"$runtime_tmp/python-base\" --no-bin" in source
    assert 'const DEFAULT_PYTHON_PACKAGE_INDEX = "https://mirrors.aliyun.com/pypi/simple"' in source
    assert 'UV_DEFAULT_INDEX="$python_package_index"' in source
    assert "uv sync --extra all --extra ddgs --locked --no-editable --link-mode copy" in source
    optional_dependencies = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["optional-dependencies"]
    assert "hermes-agent[documents]" in optional_dependencies["all"]
    assert optional_dependencies["documents"] == ["numbers-parser==4.18.2"]
    assert 'resolved_python="$(readlink -f "$runtime_tmp/bin/python3")"' in source
    assert 'final_python_relative="$(realpath --relative-to="$venv/bin" "$final_python")"' in source
    assert 'ln -sfn "$final_python_relative" "$venv/bin/python"' in source
    assert 'ldd "$resolved_python"' in source
    assert 'find "$runtime_tmp/lib/python3.11/site-packages" -type f -name \'*.so\' -print0' in source
    assert 'ldd "$extension"' in source
    assert 'for destination in /bin /usr/bin /lib /lib64 /usr/lib /usr/lib64 /usr/share /etc/fonts; do' in source
    assert '/etc/X11/fontpath.d/*)' in source
    assert 'if [ ! -L "$packaged_path" ]; then' in source
    assert '/usr/share/fonts/*) ;;' in source
    assert '/etc/X11' not in source[source.index("readonly_mounts=''") : source.index('policy_tmp="$sandbox_policy.tmp.$$"')]
    assert 'runtime_tmp/toolchain' in source
    assert 'cp -a "$release/deploy/powerpoint-runtime/runtime-modules" "$runtime_tmp/powerpoint/node_modules"' in source
    assert 'path.join(buildDir, "deploy/powerpoint-runtime/node_modules")' in source
    assert 'path.join(buildDir, "deploy/powerpoint-runtime/runtime-modules")' in source
    assert 'test -d "$release/deploy/powerpoint-runtime/runtime-modules/pptxgenjs"' in source
    archive_block = source[source.index('"-czf"') : source.index('return { tmp, archivePath }')]
    assert '"--exclude=./node_modules"' in archive_block
    assert '"--exclude=./deploy/powerpoint-runtime/runtime-modules/.package-lock.json"' in archive_block
    assert '"--exclude=./deploy/powerpoint-runtime/runtime-modules"' not in archive_block
    assert "maxBuffer: 64 * 1024 * 1024" in source
    assert '"--no-xattrs"' in source
    assert 'executor_commands="bash sh /bin/sh ls pwd printf cat chmod grep find head mktemp mv rm stat awk basename dirname sed uname which node soffice"' in source
    assert source.count('for command in $executor_commands; do') == 2
    assert '[ "$command" != "soffice" ] || continue' in source
    assert '/*) command_path="$command" ;;' in source
    assert '/*) test -x "$venv/toolchain$command" ;;' in source
    assert 'soffice_source="$(type -P soffice || true)"' in source
    assert '[ "$soffice_source" != "/usr/bin/soffice" ]' in source
    assert 'soffice_link="$(readlink "$soffice_source")"' in source
    assert '[ "$soffice_link" != "/usr/lib64/libreoffice/program/soffice" ]' in source
    assert 'rm -f -- "$soffice_target"' in source
    assert 'ln -s ../lib64/libreoffice/program/soffice "$soffice_target"' in source
    assert source.index('rm -f -- "$soffice_target"') < source.index(
        'ln -s ../lib64/libreoffice/program/soffice "$soffice_target"'
    )
    assert 'command_path="$(type -P "$command" || true)"' in source
    assert 'command_path="$(command -v "$command" || true)"' not in source
    assert 'chown -R root:root "$release_tmp"' in source
    assert 'find "$release_tmp" -type d -exec chmod go-w {} +' in source
    assert source.index("host_sandbox_deployment_policy()") < source.index('ln -sfnT "$release" "$current"')
    assert source.index("smoke-powerpoint-runtime.py") < source.index('ln -sfnT "$release" "$current"')
    assert "HERMES_DEPLOY_STAGE powerpoint_runtime_smoke=passed" in source
    assert 'function_args={"command": inside_command, "timeout": timeout}' in (
        ROOT / "deploy" / "smoke-powerpoint-runtime.py"
    ).read_text(encoding="utf-8")
    assert '--policy "$sandbox_policy"' in source
    assert "NODE_PATH=\"$venv/powerpoint/node_modules\"" not in source
    assert "npm ci" not in source[source.index("function remoteDeployScript"):]
    assert source.index('deployment_committed="1"', source.index("manage_hermes_proxy.py")) > source.index("manage_hermes_proxy.py")
    assert "restoring previous deployment state" in source
    assert "restore_deployment_state" in source
    assert "HERMES_EXECUTOR_START_GATE_FD" not in source


def test_deploy_gates_commit_on_isolated_conversation_smoke():
    source = DEPLOY.read_text(encoding="utf-8")

    auth_ready = source.index('if [ "$login_status" != "302" ] || [ "$api_status" != "401" ]')
    smoke = source.index('"$release/deploy/smoke-conversation.py" --timeout 90')
    nginx = source.index('action="reconcile"', smoke)
    commit = source.index('deployment_committed="1"', nginx)
    assert auth_ready < smoke < nginx < commit
    assert 'runuser -u "$service_user" -- env -i' in source
    assert 'HOME="$smoke_root"' in source
    assert 'TMPDIR="$smoke_root"' in source
    assert 'PYTHONPATH="$release"' in source
    smoke_block = source[source.index("if ! (", auth_ready) : nginx]
    assert "$env_file" not in smoke_block
    assert ". $env_file" not in smoke_block
    assert 'rm -rf -- "$smoke_root"' in source[source.index("cleanup_release_tmp"):source.index("trap cleanup_release_tmp EXIT")]
    assert "HERMES_DEPLOY_STAGE deterministic_smoke=passed" in source
    assert "HERMES_DEPLOY_STAGE deployment=committed" in source


def test_deploy_runs_public_smoke_only_after_remote_commit_and_does_not_roll_back():
    source = DEPLOY.read_text(encoding="utf-8")

    orchestration = source[source.index("const remoteResult = deployArchive") : source.index("} finally {", source.index("const remoteResult = deployArchive"))]
    assert orchestration.index("deployment=committed") < orchestration.index("runPublicConversationSmoke(args)")
    assert "deployment committed but public smoke failed" in orchestration
    assert "automatic rollback was not attempted" in orchestration
    assert "restore_deployment_state" not in orchestration
    public_runner = source[source.index("function runPublicConversationSmoke") : source.index("function printSummary")]
    assert "smoke_dashboard_conversation.py" in public_runner
    assert '"--url"' in public_runner
    assert "args.dashboardPublicUrl" in public_runner
    assert "dryRun: args.dryRun" in public_runner
    assert "deployment committed and all smoke passed" in source
    assert "rolled back before commit" in source
    assert 'remoteStagePassed(error, "powerpoint_runtime_smoke")' in source
    assert 'console.log(`PowerPoint runtime smoke: ${result.powerpointSmoke}`)' in source


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
