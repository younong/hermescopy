from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tarfile


DEPLOY_SCRIPT = Path(__file__).parents[2] / "deploy" / "deploy.mjs"


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
