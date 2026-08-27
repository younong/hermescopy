from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tarfile


DEPLOY_SCRIPT = Path(__file__).parents[2] / "deploy" / "deploy.mjs"


def _release_manifest(
    *, release_id: str, source_commit: str, source_tag: str | None
) -> subprocess.CompletedProcess[str]:
    source = f"""
import {{ releaseManifest }} from {json.dumps(DEPLOY_SCRIPT.as_uri())};
try {{
  console.log(JSON.stringify(releaseManifest({{
    releaseId: process.argv[1],
    sourceCommit: process.argv[2],
    sourceTag: process.argv[3] === "null" ? null : process.argv[3],
  }})));
}} catch (error) {{
  console.error(error.message);
  process.exit(1);
}}
"""
    return subprocess.run(
        [
            "node",
            "--input-type=module",
            "--eval",
            source,
            release_id,
            source_commit,
            "null" if source_tag is None else source_tag,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_manifest_is_fixed_to_tag_source():
    result = _release_manifest(
        release_id="v-test-manifest",
        source_commit="a" * 40,
        source_tag="v-test-manifest",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "schemaVersion": 1,
        "releaseId": "v-test-manifest",
        "source": {
            "kind": "tag",
            "commit": "a" * 40,
            "tag": "v-test-manifest",
        },
    }


def test_release_manifest_rejects_legacy_commit_release_shape():
    result = _release_manifest(
        release_id=f"commit-{'a' * 40}",
        source_commit="a" * 40,
        source_tag=None,
    )

    assert result.returncode != 0
    assert "Tag release ID must match the source tag" in result.stderr


def test_remote_release_reuse_requires_exact_tag_manifest():
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert (
        'expected_manifest="{\\"schemaVersion\\":1,\\"releaseId\\":\\"$release_id\\",'
        '\\"source\\":{\\"kind\\":\\"tag\\",\\"commit\\":\\"$source_commit\\",'
        '\\"tag\\":\\"$source_tag\\"}}"'
    ) in script
    existing_check = script.index('if [ -e "$release" ]; then')
    exact_match = script.index('if [ "$actual_manifest" != "$expected_manifest" ]; then', existing_check)
    reject = script.index("Existing release does not match immutable source", exact_match)
    reuse = script.index(
        'echo "Remote release already exists with matching source, reusing: $release"',
        reject,
    )
    extract_check = script.index(
        'if [ "$actual_manifest" != "$expected_manifest" ]; then', reuse
    )
    extract_reject = script.index("Release manifest does not match immutable source", extract_check)
    assert existing_check < exact_match < reject < reuse < extract_check < extract_reject


def test_remote_cutover_stops_before_atomic_current_switch():
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    cutover = script.index("# Stop the old release before changing any active artifact")
    stop_dashboard = script.index("systemctl stop hermes-dashboard.service", cutover)
    stop_gateway = script.index("systemctl stop hermes-gateway.service", stop_dashboard)
    switch_current = script.index('mv -Tf "$next_current" "$current"')
    start_dashboard = script.index("systemctl start hermes-dashboard.service", switch_current)
    assert stop_dashboard < stop_gateway < switch_current < start_dashboard
    assert "pgrep -f '[h]ermes_cli.owner_worker.entrypoint'" not in script
    assert "systemctl restart hermes-gateway.service" not in script
    assert "gateway run --replace" not in script
    assert "staged_gateway_unit" not in script
    assert "After=network-online.target hermes-gateway.service" not in script
    assert "Services: hermes-gateway.service" not in script
    assert 'ln -sfnT "$release" "$current"' not in script
    prepare = script.index('runContinuityConversationSmoke(args, "prepare")')
    deploy = script.index("deployArchive(args, archivePath)", prepare)
    verify = script.index('runContinuityConversationSmoke(args, "verify")', deploy)
    assert prepare < deploy < verify
    assert '"hermes.public-continuity-smoke"' in script
    assert "cross-release continuity preparation failed before remote deployment" in script
    authority_preflight = script.index("HERMES_DEPLOY_STAGE authority_preflight=passed")
    assert authority_preflight < stop_dashboard
    authority_snapshot = script.index("snapshot_authority", stop_gateway)
    assert stop_gateway < authority_snapshot < switch_current
    rollback_stop = script.index(
        "systemctl stop hermes-dashboard.service hermes-gateway.service || true"
    )
    rollback_authority = script.index("restore_authority_snapshot || true", rollback_stop)
    rollback_artifacts = script.index("restore_deployment_state || true", rollback_authority)
    assert rollback_stop < rollback_authority < rollback_artifacts
    assert "source.backup(target)" in script
    assert 'mode=ro&immutable=1' in script
    assert "PRAGMA integrity_check" in script
    assert 'dashboard authority status --json' in script
    assert (
        "Restart cannot recover authority; offline recovery fencing is required."
        in script
    )
    assert "write_drain_request" not in script
    assert "read_runtime_status" not in script
    assert "is_gateway_runtime_lock_active" not in script
    assert "clear_drain_request" not in script
    assert 'case "--initial-continuity-transition"' in script
    assert "args.initialContinuityTransition || continuitySmoke.status" in script


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
        ".github/workflows/ci.yml",
        "docs/deployment.md",
    ]
    retained = [
        "hermes_cli/main.py",
        "hermes_cli/web_dist/index.html",
        "deploy/smoke-authority-concurrency.py",
        "deploy/powerpoint-runtime/runtime-modules/pptxgenjs/index.js",
        "skills/productivity/powerpoint/SKILL.md",
        "optional-skills/mlops/SKILL.md",
        "plugins/hermes-achievements/plugin.yaml",
    ]
    excluded_dependencies = [
        "node_modules/root-package/index.js",
        "web/node_modules/web-package/index.js",
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
