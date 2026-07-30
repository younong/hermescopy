from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tarfile


DEPLOY_SCRIPT = Path(__file__).parents[2] / "deploy" / "deploy.mjs"


def test_remote_cutover_stops_before_atomic_current_switch():
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    cutover = script.index("# Stop the old release before changing any active artifact")
    stop_dashboard = script.index("systemctl stop hermes-dashboard.service", cutover)
    stop_gateway = script.index("systemctl stop hermes-gateway.service", stop_dashboard)
    switch_current = script.index('mv -Tf "$next_current" "$current"')
    start_gateway = script.index("systemctl start hermes-gateway.service", switch_current)
    assert stop_dashboard < stop_gateway < switch_current < start_gateway
    assert "pgrep -f '[h]ermes_cli.owner_worker.entrypoint'" not in script
    assert "systemctl restart hermes-gateway.service" not in script
    assert 'ln -sfnT "$release" "$current"' not in script
    prepare = script.index('runContinuityConversationSmoke(args, "prepare")')
    deploy = script.index("deployArchive(args, archivePath)", prepare)
    verify = script.index('runContinuityConversationSmoke(args, "verify")', deploy)
    assert prepare < deploy < verify
    assert '"hermes.public-continuity-smoke"' in script
    assert "cross-release continuity preparation failed before remote deployment" in script
    drain = script.index("write_drain_request(principal=", cutover - 10_000)
    assert drain < stop_dashboard
    authority_preflight = script.index("HERMES_DEPLOY_STAGE authority_preflight=passed")
    assert authority_preflight < stop_dashboard
    assert 'dashboard authority status --json' in script
    assert "documented offline recovery workflow" in script
    assert 'gateway_drain_status" = "draining:0"' in script
    assert 'print("{}:{}".format(s.get("gateway_state", ""), s.get("active_agents", 0)))' in script
    assert "is_gateway_runtime_lock_active() or get_running_pid()" in script
    assert 'case "--initial-continuity-transition"' in script
    assert "args.initialContinuityTransition || continuitySmoke.status" in script
    assert "clear_drain_request; clear_drain_request()" in script


def _write(root: Path, relative: str, content: str = "fixture\n") -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_release_archive_prunes_non_runtime_trees_and_keeps_runtime_payload(tmp_path):
    build_dir = tmp_path / "artifact"
    archive_path = tmp_path / "hermes.tar.gz"

    omitted = [
        "tests/test_agent.py",
        "website/static/screenshot.png",
        "apps/desktop/public/icon.png",
        ".github/workflows/ci.yml",
        "docs/deployment.md",
    ]
    retained = [
        "hermes_cli/main.py",
        "hermes_cli/web_dist/index.html",
        "ui-tui/dist/entry.js",
        "deploy/smoke-authority-concurrency.py",
        "deploy/powerpoint-runtime/runtime-modules/pptxgenjs/index.js",
        "skills/productivity/powerpoint/SKILL.md",
        "optional-skills/mlops/SKILL.md",
        "plugins/hermes-achievements/plugin.yaml",
    ]
    excluded_dependencies = [
        "node_modules/root-package/index.js",
        "web/node_modules/web-package/index.js",
        "ui-tui/node_modules/tui-package/index.js",
        "deploy/powerpoint-runtime/runtime-modules/.package-lock.json",
    ]
    for relative in [*omitted, *retained, *excluded_dependencies]:
        _write(build_dir, relative)

    source = f"""
import {{ createReleaseArchive }} from {json.dumps(DEPLOY_SCRIPT.as_uri())};
createReleaseArchive(process.argv[1], process.argv[2]);
"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "--eval",
            source,
            str(build_dir),
            str(archive_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Release archive:" in result.stdout
    with tarfile.open(archive_path, "r:gz") as archive:
        members = {
            member.name.removeprefix("./")
            for member in archive.getmembers()
            if member.isfile()
        }

    assert not members.intersection(omitted)
    assert not members.intersection(excluded_dependencies)
    assert set(retained) <= members
