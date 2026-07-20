from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli.owner_worker.skill_snapshot import (
    SkillSnapshotError,
    materialize_skill_snapshot,
)


def _skill(tmp_path: Path) -> Path:
    skill = tmp_path / "owner" / "skills" / "productivity" / "common-files"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        'Run ${HERMES_SKILL_DIR}/scripts/common_files.py\n', encoding="utf-8"
    )
    (skill / "scripts" / "common_files.py").write_text("print('ok')\n", encoding="utf-8")
    return skill


def test_materializes_selected_skill_as_read_only_generation_snapshot(tmp_path):
    skill = _skill(tmp_path)
    runtime_home = tmp_path / "runtime" / "gen-1"

    sandbox_path = materialize_skill_snapshot(skill, runtime_home)
    snapshot = runtime_home / sandbox_path.removeprefix("/executor/")

    assert sandbox_path.startswith("/executor/skill-snapshots/")
    assert (snapshot / "SKILL.md").read_text() == (skill / "SKILL.md").read_text()
    assert (snapshot / "scripts" / "common_files.py").read_text() == "print('ok')\n"
    assert snapshot.stat().st_mode & 0o777 == 0o555
    assert (snapshot / "scripts").stat().st_mode & 0o777 == 0o555
    assert (snapshot / "scripts" / "common_files.py").stat().st_mode & 0o777 == 0o444
    assert skill.stat().st_mode & 0o200


def test_concurrent_snapshot_materialization_reuses_one_published_copy(tmp_path):
    skill = _skill(tmp_path)
    runtime_home = tmp_path / "runtime" / "gen-1"

    with ThreadPoolExecutor(max_workers=8) as executor:
        paths = list(executor.map(
            lambda _index: materialize_skill_snapshot(skill, runtime_home),
            range(16),
        ))

    assert len(set(paths)) == 1
    snapshot_root = runtime_home / "skill-snapshots"
    assert [path.name for path in snapshot_root.iterdir()] == [paths[0].rsplit("/", 1)[1]]


def test_snapshot_content_identity_reuses_unchanged_and_rotates_changed(tmp_path):
    skill = _skill(tmp_path)
    runtime_home = tmp_path / "runtime" / "gen-1"

    first = materialize_skill_snapshot(skill, runtime_home)
    assert materialize_skill_snapshot(skill, runtime_home) == first
    (skill / "scripts" / "common_files.py").write_text("print('changed')\n")
    second = materialize_skill_snapshot(skill, runtime_home)

    assert second != first
    assert (runtime_home / first.removeprefix("/executor/")).exists()
    assert (runtime_home / second.removeprefix("/executor/")).exists()


def test_snapshot_rejects_symlink_special_file_and_limits(tmp_path):
    skill = _skill(tmp_path)
    runtime_home = tmp_path / "runtime" / "gen-1"
    outside = tmp_path / "secret"
    outside.write_text("secret")
    (skill / "scripts" / "leak").symlink_to(outside)
    with pytest.raises(SkillSnapshotError, match="symbolic link"):
        materialize_skill_snapshot(skill, runtime_home)
    (skill / "scripts" / "leak").unlink()

    if hasattr(os, "mkfifo"):
        fifo = skill / "scripts" / "pipe"
        os.mkfifo(fifo)
        with pytest.raises(SkillSnapshotError, match="non-regular"):
            materialize_skill_snapshot(skill, runtime_home)
        fifo.unlink()

    with pytest.raises(SkillSnapshotError, match="too large"):
        materialize_skill_snapshot(skill, runtime_home, max_bytes=1)
    with pytest.raises(SkillSnapshotError, match="too many"):
        materialize_skill_snapshot(skill, runtime_home, max_files=1)
