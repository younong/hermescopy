from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tarfile

import pytest


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
    snapshot_authority = script.index("snapshot_authority", cutover)
    install_dashboard_unit = script.index(
        'install -o root -g root -m 0644 "$staged_dashboard_unit" "$dashboard_unit"',
        snapshot_authority,
    )
    dashboard_dropin_dir = script.index(
        'dashboard_dropin_dir="/etc/systemd/system/hermes-dashboard.service.d"'
    )
    backup_dropin_guard = script.index(
        'if [ -d "$dashboard_dropin_dir" ]; then',
        script.index("backup_deployment_state()"),
    )
    backup_dropin_copy = script.index(
        'cp -a -- "$dashboard_dropin_dir" "$rollback_dir/$(printf \'%s\' "$dashboard_dropin_dir" | sed \'s#/#_#g\')"',
        backup_dropin_guard,
    )
    backup_state_call = script.index("\nbackup_deployment_state\n")
    cleanup_log = script.index(
        'echo "Removing legacy Dashboard systemd drop-in directory: $dashboard_dropin_dir"',
        install_dashboard_unit,
    )
    cleanup_dropin = script.index(
        'rm -rf -- "$dashboard_dropin_dir"',
        cleanup_log,
    )
    reload_before_stop = script.index("systemctl daemon-reload", install_dashboard_unit)
    timeout_check = script.index('systemctl show hermes-dashboard.service -p TimeoutStopUSec --value', reload_before_stop)
    stop_dashboard = script.index("systemctl stop hermes-dashboard.service", timeout_check)
    stop_gateway = script.index("systemctl stop hermes-gateway.service", stop_dashboard)
    switch_current = script.index('mv -Tf "$next_current" "$current"')
    start_dashboard = script.index("systemctl start hermes-dashboard.service", switch_current)
    assert dashboard_dropin_dir < backup_dropin_guard < backup_dropin_copy
    assert backup_state_call < install_dashboard_unit < cleanup_log < cleanup_dropin < reload_before_stop
    assert snapshot_authority < install_dashboard_unit < reload_before_stop < timeout_check < stop_dashboard
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
    assert stop_gateway < switch_current
    rollback_stop = script.index(
        "systemctl stop hermes-dashboard.service hermes-gateway.service || true"
    )
    rollback_authority = script.index("restore_authority_snapshot || true", rollback_stop)
    rollback_artifacts = script.index("restore_deployment_state || true", rollback_authority)
    rollback_reload = script.index("systemctl daemon-reload || true", rollback_artifacts)
    assert rollback_stop < rollback_authority < rollback_artifacts < rollback_reload
    restore_dropin_backup = script.index(
        'dashboard_dropin_backup="$rollback_dir/$(printf \'%s\' "$dashboard_dropin_dir" | sed \'s#/#_#g\')"'
    )
    restore_dropin_if = script.index(
        'if [ -e "$dashboard_dropin_backup" ]; then',
        restore_dropin_backup,
    )
    restore_dropin_remove = script.index(
        'rm -rf -- "$dashboard_dropin_dir"',
        restore_dropin_if,
    )
    restore_dropin_copy = script.index(
        'cp -a -- "$dashboard_dropin_backup" "$dashboard_dropin_dir"',
        restore_dropin_remove,
    )
    restore_dropin_else = script.index("else", restore_dropin_copy)
    restore_dropin_remove_absent = script.index(
        'rm -rf -- "$dashboard_dropin_dir"',
        restore_dropin_else,
    )
    assert restore_dropin_backup < restore_dropin_if
    assert restore_dropin_if < restore_dropin_remove < restore_dropin_copy
    assert restore_dropin_copy < restore_dropin_else < restore_dropin_remove_absent
    assert "[ -d \"$dashboard_dropin_dir\" ]" in script
    assert "cp -a -- \"$dashboard_dropin_dir\"" in script
    assert "cp -a -- \"$dashboard_dropin_backup\" \"$dashboard_dropin_dir\"" in script
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


@pytest.mark.live_system_guard_bypass
def test_remote_deploy_script_renders_without_reference_error():
    """The ``String.raw`` template body in ``deploy.mjs`` MUST evaluate as a
    JavaScript template literal without throwing ``ReferenceError``.

    The embedded remote deploy script writes bash ``${var}`` placeholders
    inside a ``String.raw`...``` block. JavaScript evaluates every
    ``${...}`` as a template expression; any bare ``${var}`` the author
    intended for bash therefore resolves against JS scope and throws, and
    the script never reaches the server. The existing convention used
    elsewhere in this file (positional arg expansion) is
    ``${"${"}var}``: the inner expression evaluates to a literal ``${``,
    and the surrounding ``}`` closes that literal as a bash placeholder.
    See the follow-up commit for PR #355.

    The existing ``test_remote_cutover_stops_before_atomic_current_switch``
    tests assert positions inside the source text, so it would never catch
    a JS-side render failure. This test actually round-trips the body
    through Node and is the fail-closed guard against the whole class.
    """
    import re

    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    # Match the entire `function remoteDeployScript() { return String.raw`...`;
    # }` shape; `[\s\S]*?` is a non-greedy match across the closing backtick.
    match = re.search(
        r"function remoteDeployScript\(\)\s*\{\s*return\s+String\.raw`([\s\S]*?)`;\s*\}",
        script,
    )
    assert match, "remoteDeployScript() shape changed; update this test"
    body = match.group(1)

    # The bash body should not contain its own backticks. If it did,
    # the deploy.mjs source itself would not parse cleanly, so guard here.
    assert "`" not in body, "remote deploy script body contains a stray backtick"

    # Bake the body INTO a JS template literal as raw source content so
    # Node parses it as if it were a `String.raw` template literal in
    # production. JS will then evaluate every `${...}` it sees; the only
    # ones that pass are ${"${"}var}, which evaluate to literal bash
    # placeholders. Bare `${var}` references against JS scope throw
    # ReferenceError, which is what this test guards against.
    #
    # The body is inlined as the literal text of a `String.raw` template
    # in the generated JS source. Python's f-string substitution only
    # parses `{...}` patterns inside the `{}` boundaries of the f-string,
    # not in the substituted value, so `{` / `}` inside `body` survive.
    python_literal_body = body
    eval_source = (
        "(() => {\n"
        "  function __test__() {\n"
        f"    return String.raw`{python_literal_body}`;\n"
        "  }\n"
        "  const out = __test__();\n"
        "  process.stdout.write('OK ' + out.length);\n"
        "})();\n"
    )

    result = subprocess.run(
        ["node", "--eval", eval_source],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(DEPLOY_SCRIPT.parents[1]),
        timeout=30,
    )
    assert result.returncode == 0, (
        "remote deploy script failed to render — bash `${var}` placeholders "
        "likely leaked into a JS template expression. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    head, sep, tail = result.stdout.rpartition("OK ")
    assert sep and tail.strip().isdigit(), (
        f"unexpected render marker: stdout={result.stdout!r}"
    )
    # Sanity: a healthy bash deploy script is well over 10k bytes.
    assert int(tail.strip()) > 10000, (
        f"unexpected render length: stdout={result.stdout!r}"
    )


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
