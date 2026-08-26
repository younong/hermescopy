from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).parents[2]
LOCAL_PLATFORM = ROOT / "deploy" / "local-platform.mjs"
ARCHIVE = ROOT / "deploy" / "archive.mjs"
DEPLOY = ROOT / "deploy" / "deploy.mjs"


def _node(source: str, *args: str, env: dict[str, str] | None = None):
    return subprocess.run(
        ["node", "--input-type=module", "--eval", source, *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_local_runner_scrubs_deploy_password_and_redacts_output(tmp_path):
    probe = tmp_path / "probe.mjs"
    probe.write_text(
        "console.log(process.env.HERMES_DEPLOY_PASSWORD || 'missing')\n",
        encoding="utf-8",
    )
    source = f"""
import {{ runLocal }} from {json.dumps(LOCAL_PLATFORM.as_uri())};
const result = runLocal(process.execPath, [process.argv[1]], {{ quiet: true }});
console.log(result.stdout);
"""
    env = {**os.environ, "HERMES_DEPLOY_PASSWORD": "fake-sentinel-password"}

    result = _node(source, str(probe), env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "missing"
    assert "fake-sentinel-password" not in result.stdout + result.stderr


def test_move_directory_and_required_file_are_platform_native(tmp_path):
    source_dir = tmp_path / "node_modules"
    target_dir = tmp_path / "runtime-modules"
    web_entry = tmp_path / "index.html"
    source_dir.mkdir()
    web_entry.write_text("ok", encoding="utf-8")
    source = f"""
import {{ moveDirectory, requireFile }} from {json.dumps(LOCAL_PLATFORM.as_uri())};
moveDirectory(process.argv[1], process.argv[2]);
requireFile(process.argv[3], 'Web build entry point');
"""

    result = _node(source, str(source_dir), str(target_dir), str(web_entry))

    assert result.returncode == 0, result.stderr
    assert target_dir.is_dir()
    assert not source_dir.exists()


def test_archive_filter_matches_only_release_roots():
    source = f"""
import {{ shouldIncludeReleasePath }} from {json.dumps(ARCHIVE.as_uri())};
for (const value of process.argv.slice(1)) console.log(value + '=' + shouldIncludeReleasePath(value));
"""
    result = _node(
        source,
        "docs/guide.md",
        "mydocs/runtime.txt",
        "nested/docs/runtime.txt",
        "web/node_modules/pkg/index.js",
        "runtime/node_modules/pkg/index.js",
        "../escape",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "docs/guide.md=false",
        "mydocs/runtime.txt=true",
        "nested/docs/runtime.txt=true",
        "web/node_modules/pkg/index.js=false",
        "runtime/node_modules/pkg/index.js=true",
        "../escape=false",
    ]


def test_release_archive_restores_git_executable_mode(tmp_path):
    build_dir = tmp_path / "artifact"
    archive_path = tmp_path / "artifact.tar.gz"
    script = build_dir / "scripts" / "run.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    source = f"""
import {{ createReleaseArchiveFile }} from {json.dumps(ARCHIVE.as_uri())};
createReleaseArchiveFile(process.argv[1], process.argv[2], {{
  gitModes: new Map([['scripts/run.sh', '100755']]),
}});
"""

    result = _node(source, str(build_dir), str(archive_path))

    assert result.returncode == 0, result.stderr
    with tarfile.open(archive_path, "r:gz") as archive:
        member = archive.getmember("./scripts/run.sh")
    assert member.mode == 0o755


def test_connection_check_dry_run_does_not_require_release_source():
    result = subprocess.run(
        ["node", str(DEPLOY), "--check-connection", "--dry-run"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Connection check passed" in result.stdout
    assert "git archive" not in result.stdout
    assert "mkdir -p" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="stub ssh relies on a POSIX shell")
def test_connection_check_survives_sshd_argv_rejoin(tmp_path):
    # Regression for #339: sshd re-joins command argv with spaces before the
    # remote shell parses it, so deploy must send a single pre-quoted command
    # string. The stub ssh simulates the server-side join faithfully: skip the
    # option pairs, drop the target, re-join, and hand the result to a shell.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "ssh"
    stub.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        "    -*) shift 2 ;;\n"
        "    *) shift; break ;;\n"
        "  esac\n"
        "done\n"
        'exec sh -c "$*"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "HERMES_DEPLOY_PASSWORD"}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        ["node", str(DEPLOY), "--check-connection"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "Connection check passed" in result.stdout
