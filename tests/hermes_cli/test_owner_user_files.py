import hashlib
import json
from pathlib import Path

from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import controlled_roots_for
from hermes_cli.owner_runtime import ensure_owner_runtime_dirs, owner_worker_runtime_paths
from hermes_cli.owner_worker.user_files import (
    migrate_legacy_user_files,
    publish_unique_user_bytes,
)


def _context(tmp_path, monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)
    owner = ensure_owner_runtime_dirs(tmp_path / "owner")
    roots = controlled_roots_for(
        owner_worker_runtime_paths(owner_home=owner, worker_generation=1)
    )
    return owner, roots, AuthenticatedWorkspaceContext(roots)


def test_authenticated_context_maps_api_paths_to_selected_workspace(tmp_path, monkeypatch):
    owner, roots, context = _context(tmp_path, monkeypatch)
    try:
        assert context.controlled_api_path("", allow_workspace_root=True) == "default"
        assert context.controlled_api_path("reports/a.txt") == "default/reports/a.txt"
        assert context.controlled_api_path("/workspace/reports/a.txt") == "default/reports/a.txt"
        absolute = owner / "workspaces" / "default" / "reports" / "a.txt"
        assert context.controlled_api_path(str(absolute)) == "default/reports/a.txt"
        assert context.controlled_api_path(
            str(owner / "workspaces" / "default"),
            allow_workspace_root=True,
        ) == "default"
        assert context.visible_workspace_path("default/reports/a.txt") == "reports/a.txt"
        assert context.diagnostic_path("default/reports/a.txt") == absolute
    finally:
        roots.close()


def test_publish_unique_user_bytes_uses_user_directories(tmp_path, monkeypatch):
    owner, roots, context = _context(tmp_path, monkeypatch)
    try:
        first = publish_unique_user_bytes(context, "upload", "report.pdf", b"one")
        second = publish_unique_user_bytes(context, "upload", "report.pdf", b"two")

        assert first.visible_path == "uploads/report.pdf"
        assert first.diagnostic_path.read_bytes() == b"one"
        assert second.visible_path.startswith("uploads/report-")
        assert second.diagnostic_path.read_bytes() == b"two"
        assert second.diagnostic_path.parent == owner / "workspaces" / "default" / "uploads"
    finally:
        roots.close()


def test_migrate_legacy_user_files_classifies_and_is_idempotent(tmp_path, monkeypatch):
    owner, roots, context = _context(tmp_path, monkeypatch)
    (owner / "images").mkdir()
    (owner / "images" / "upload_20260101_1.png").write_bytes(b"upload")
    (owner / "images" / "apiyi_generated.png").write_bytes(b"image")
    (owner / "cache" / "audio").mkdir(parents=True)
    (owner / "cache" / "audio" / "voice.mp3").write_bytes(b"audio")
    (owner / "cache" / "audio" / "model.bin").write_bytes(b"internal")
    try:
        first = migrate_legacy_user_files(context)
        second = migrate_legacy_user_files(context)

        workspace = owner / "workspaces" / "default"
        assert (workspace / "uploads" / "upload_20260101_1.png").read_bytes() == b"upload"
        assert (workspace / "generated" / "images" / "apiyi_generated.png").read_bytes() == b"image"
        assert (workspace / "generated" / "audio" / "voice.mp3").read_bytes() == b"audio"
        assert (owner / "cache" / "audio" / "model.bin").read_bytes() == b"internal"
        assert first["complete"] is True
        assert second == first
        manifest = json.loads((owner / "runtime" / "user-files-migration-v1.json").read_text())
        assert manifest["version"] == 1
    finally:
        roots.close()


def test_migrate_legacy_user_files_preserves_conflicting_destination(tmp_path, monkeypatch):
    owner, roots, context = _context(tmp_path, monkeypatch)
    legacy = owner / "cache" / "videos"
    legacy.mkdir(parents=True)
    (legacy / "clip.mp4").write_bytes(b"legacy")
    destination = owner / "workspaces" / "default" / "generated" / "videos"
    destination.mkdir(parents=True)
    (destination / "clip.mp4").write_bytes(b"current")
    digest = hashlib.sha256(b"legacy").hexdigest()[:10]
    (destination / f"clip-migrated-{digest}.mp4").write_bytes(b"another collision")
    try:
        result = migrate_legacy_user_files(context)

        migrated = Path(result["mappings"]["cache/videos/clip.mp4"])
        assert migrated.name == f"clip-migrated-{digest}-1.mp4"
        assert (owner / "workspaces" / "default" / migrated).read_bytes() == b"legacy"
        assert (destination / "clip.mp4").read_bytes() == b"current"
        assert (destination / f"clip-migrated-{digest}.mp4").read_bytes() == b"another collision"
    finally:
        roots.close()
