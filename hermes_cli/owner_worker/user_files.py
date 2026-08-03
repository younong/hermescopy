"""Authenticated owner workspace publication and legacy artifact migration."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import ExpectedType, RootKind

_MIGRATION_VERSION = 1
_MIGRATION_MANIFEST = "runtime/user-files-migration-v1.json"
_USER_DIRECTORIES = {
    "upload": "uploads",
    "image": "generated/images",
    "audio": "generated/audio",
    "video": "generated/videos",
}
_LEGACY_SOURCES = (
    ("images", "image"),
    ("cache/images", "image"),
    ("cache/audio", "audio"),
    ("cache/videos", "video"),
)
_UPLOAD_IMAGE_RE = re.compile(r"^(?:upload|clip|pdf_p\d+)_", re.IGNORECASE)
_ALLOWED_SUFFIXES = {
    "image": frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}),
    "audio": frozenset({".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac"}),
    "video": frozenset({".mp4", ".webm", ".mov", ".m4v"}),
}


@dataclass(frozen=True)
class PublishedUserFile:
    visible_path: str
    controlled_path: str
    diagnostic_path: Path


def _safe_name(name: str) -> str:
    value = Path(str(name or "")).name.strip().strip(".")
    if not value or value in {".", ".."} or "\x00" in value or "/" in value:
        raise ValueError("artifact filename is invalid")
    return value


def user_file_path(
    context: AuthenticatedWorkspaceContext,
    category: str,
    filename: str,
) -> PublishedUserFile:
    try:
        directory = _USER_DIRECTORIES[category]
    except KeyError as exc:
        raise ValueError("artifact category is invalid") from exc
    visible = f"{directory}/{_safe_name(filename)}"
    controlled = context.controlled_workspace_path(visible)
    return PublishedUserFile(visible, controlled, context.diagnostic_path(controlled))


def publish_user_bytes(
    context: AuthenticatedWorkspaceContext,
    category: str,
    filename: str,
    data: bytes,
    *,
    overwrite: bool = False,
) -> PublishedUserFile:
    """Atomically publish bytes into the authenticated user's file area."""
    published = user_file_path(context, category, filename)
    context.roots.replace_bytes(
        RootKind.WORKSPACE,
        published.controlled_path,
        data,
        overwrite=overwrite,
    )
    return published


def publish_unique_user_bytes(
    context: AuthenticatedWorkspaceContext,
    category: str,
    filename: str,
    data: bytes,
) -> PublishedUserFile:
    """Publish without overwriting, adding a random suffix on collision."""
    safe = _safe_name(filename)
    stem = Path(safe).stem or "artifact"
    suffix = Path(safe).suffix
    for attempt in range(32):
        candidate = safe if attempt == 0 else f"{stem}-{secrets.token_hex(4)}{suffix}"
        try:
            return publish_user_bytes(context, category, candidate, data)
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate an artifact filename")


def _read_all(context: AuthenticatedWorkspaceContext, kind: RootKind, path: str) -> bytes:
    fd = context.roots.open_relative(kind, path, expected_type=ExpectedType.REGULAR_FILE)
    try:
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _manifest(context: AuthenticatedWorkspaceContext) -> dict[str, Any] | None:
    try:
        raw = _read_all(context, RootKind.OWNER_WRITABLE, _MIGRATION_MANIFEST)
        value = json.loads(raw)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_manifest(context: AuthenticatedWorkspaceContext, value: dict[str, Any]) -> None:
    context.roots.replace_bytes(
        RootKind.OWNER_WRITABLE,
        _MIGRATION_MANIFEST,
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
    )


def _migration_destination(category: str, name: str) -> tuple[str, str]:
    if category == "image" and _UPLOAD_IMAGE_RE.match(name):
        return "upload", name
    return category, name


def _same_bytes(context: AuthenticatedWorkspaceContext, controlled: str, data: bytes) -> bool:
    try:
        existing = _read_all(context, RootKind.WORKSPACE, controlled)
    except FileNotFoundError:
        return False
    return hashlib.sha256(existing).digest() == hashlib.sha256(data).digest()


def _migration_target(
    context: AuthenticatedWorkspaceContext,
    category: str,
    name: str,
    data: bytes,
) -> tuple[PublishedUserFile, bool]:
    initial = user_file_path(context, category, name)
    try:
        fd = context.roots.open_relative(
            RootKind.WORKSPACE,
            initial.controlled_path,
            expected_type=ExpectedType.REGULAR_FILE,
        )
    except FileNotFoundError:
        return initial, False
    else:
        os.close(fd)

    if _same_bytes(context, initial.controlled_path, data):
        return initial, True

    stem = Path(name).stem or "artifact"
    suffix = Path(name).suffix
    digest = hashlib.sha256(data).hexdigest()[:10]
    for attempt in range(32):
        qualifier = digest if attempt == 0 else f"{digest}-{attempt}"
        candidate = user_file_path(
            context,
            category,
            f"{stem}-migrated-{qualifier}{suffix}",
        )
        try:
            fd = context.roots.open_relative(
                RootKind.WORKSPACE,
                candidate.controlled_path,
                expected_type=ExpectedType.REGULAR_FILE,
            )
        except FileNotFoundError:
            return candidate, False
        else:
            os.close(fd)
        if _same_bytes(context, candidate.controlled_path, data):
            return candidate, True
    raise FileExistsError("could not allocate a migration filename")


def migrate_legacy_user_files(context: AuthenticatedWorkspaceContext) -> dict[str, Any]:
    """Move known legacy user artifacts into the selected workspace once."""
    prior = _manifest(context)
    if prior and prior.get("version") == _MIGRATION_VERSION and prior.get("complete") is True:
        return prior

    mappings = dict(prior.get("mappings") or {}) if prior else {}
    failures: dict[str, str] = {}
    skipped: list[str] = []

    for source_dir, category in _LEGACY_SOURCES:
        try:
            entries = context.roots.list_directory(RootKind.OWNER_WRITABLE, source_dir)
        except FileNotFoundError:
            continue
        for entry in entries:
            source = entry.relative_path
            if entry.is_directory or Path(entry.name).suffix.lower() not in _ALLOWED_SUFFIXES[category]:
                skipped.append(source)
                continue
            try:
                data = _read_all(context, RootKind.OWNER_WRITABLE, source)
                destination_category, destination_name = _migration_destination(category, entry.name)
                target, already_present = _migration_target(
                    context,
                    destination_category,
                    destination_name,
                    data,
                )
                if not already_present:
                    context.roots.replace_bytes(
                        RootKind.WORKSPACE,
                        target.controlled_path,
                        data,
                        overwrite=False,
                    )
                context.roots.remove(RootKind.OWNER_WRITABLE, source)
                mappings[source] = target.visible_path
            except Exception as exc:  # retry this exact entry on the next startup
                failures[source] = type(exc).__name__

    manifest = {
        "version": _MIGRATION_VERSION,
        "complete": not failures,
        "mappings": mappings,
        "skipped": sorted(set(skipped)),
        "failures": failures,
    }
    _write_manifest(context, manifest)
    return manifest


def migrated_legacy_path(
    context: AuthenticatedWorkspaceContext,
    owner_relative_path: str,
) -> str | None:
    """Return the new workspace-visible path recorded for a legacy artifact."""
    manifest = _manifest(context)
    if not manifest:
        return None
    value = (manifest.get("mappings") or {}).get(owner_relative_path)
    return str(value) if isinstance(value, str) and value else None
