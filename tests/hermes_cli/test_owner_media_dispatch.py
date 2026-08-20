import json
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import controlled_roots_for
from hermes_cli.deployment_media import DeploymentMediaRouteDescriptor
from hermes_cli.owner_runtime import ensure_owner_runtime_dirs, owner_worker_runtime_paths
from hermes_cli.owner_worker.media_dispatch import (
    active_media_selection,
    dispatch_deployment_media,
)
from hermes_cli.owner_worker.user_files import migrate_legacy_user_files


class Relay:
    def execute(self, operation, **kwargs):
        self.operation = operation
        self.kwargs = kwargs
        result = {
            "provider": kwargs["provider"],
            "model": kwargs["model"],
            "aspect_ratio": kwargs["aspect_ratio"],
            "modality": "image" if kwargs.get("references") else "text",
            "metadata": {"size": "1024x1024"},
        }
        if operation == "image_generate":
            result.update({"image_bytes": b"generated", "mime_type": "image/png"})
        else:
            result["video_url"] = "https://cdn.example.com/video.mp4"
        return result


def _fixture(tmp_path):
    owner = tmp_path / "owner"
    ensure_owner_runtime_dirs(owner)
    paths = owner_worker_runtime_paths(owner_home=owner, worker_generation=1)
    roots = controlled_roots_for(paths)
    descriptor = DeploymentMediaRouteDescriptor(
        kind="image",
        provider="apiyi",
        models=("gpt-image-2-medium",),
        default_model="gpt-image-2-medium",
    )
    return owner, paths, roots, descriptor


def _video_descriptor():
    return DeploymentMediaRouteDescriptor(
        kind="video",
        provider="fal",
        models=("fal-video-1",),
        default_model="fal-video-1",
    )


def _dispatch(arguments, *, relay, descriptor, context, owner, kind="image"):
    return dispatch_deployment_media(
        arguments,
        kind=kind,
        model=descriptor.default_model,
        relay_client=relay,
        descriptor=descriptor,
        workspace_context=context,
        owner_home=owner,
    )


def _linux(monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)


def test_dispatch_reads_and_writes_selected_workspace(tmp_path, monkeypatch):
    _linux(monkeypatch)
    owner, paths, roots, descriptor = _fixture(tmp_path)
    selected = paths.workspace_root / "projects" / "selected"
    selected.mkdir(parents=True)
    context = AuthenticatedWorkspaceContext(roots, workspace_prefix="projects/selected")
    reference = selected / "source.png"
    reference.write_bytes(b"reference")
    relay = Relay()
    try:
        payload = json.loads(_dispatch(
            {"prompt": "edit", "aspect_ratio": "square", "image_url": str(reference)},
            relay=relay,
            descriptor=descriptor,
            context=context,
            owner=owner,
        ))
        assert relay.operation == "image_generate"
        assert relay.kwargs["references"][0] == {
            "name": "source.png", "mime_type": "image/png", "data": b"reference",
        }
        output = Path(payload["image"])
        assert payload["image"].startswith("generated/images/")
        assert (selected / output).read_bytes() == b"generated"
        assert str(owner) not in json.dumps(payload)
        assert payload["size"] == "1024x1024"
        assert "api_key" not in payload
        assert "base_url" not in payload
    finally:
        roots.close()


def test_dispatch_forwards_resolution_tier_as_params(tmp_path, monkeypatch):
    _linux(monkeypatch)
    owner, _paths, roots, descriptor = _fixture(tmp_path)
    context = AuthenticatedWorkspaceContext(roots)
    relay = Relay()
    try:
        _dispatch(
            {"prompt": "draw", "aspect_ratio": "landscape", "resolution": "4K"},
            relay=relay,
            descriptor=descriptor,
            context=context,
            owner=owner,
        )
        assert relay.kwargs["params"] == {"resolution": "4K"}
        _dispatch(
            {"prompt": "draw", "aspect_ratio": "landscape"},
            relay=relay,
            descriptor=descriptor,
            context=context,
            owner=owner,
        )
        assert relay.kwargs["params"] == {}
    finally:
        roots.close()


def test_dispatch_video_returns_url_passthrough(tmp_path, monkeypatch):
    _linux(monkeypatch)
    owner, paths, roots, _descriptor = _fixture(tmp_path)
    context = AuthenticatedWorkspaceContext(roots)
    relay = Relay()
    try:
        payload = json.loads(_dispatch(
            {"prompt": "animate", "duration": 6, "resolution": "720p"},
            relay=relay,
            descriptor=_video_descriptor(),
            context=context,
            owner=owner,
            kind="video",
        ))
        assert relay.operation == "video_generate"
        assert relay.kwargs["params"] == {"duration": 6, "resolution": "720p"}
        assert payload["video"] == "https://cdn.example.com/video.mp4"
        assert payload["success"] is True
    finally:
        roots.close()


def test_dispatch_video_publishes_returned_bytes(tmp_path, monkeypatch):
    class BytesRelay(Relay):
        def execute(self, operation, **kwargs):
            super().execute(operation, **kwargs)
            return {
                "provider": kwargs["provider"],
                "model": kwargs["model"],
                "aspect_ratio": kwargs["aspect_ratio"],
                "modality": "text",
                "metadata": {},
                "video_bytes": b"video-bytes",
                "mime_type": "video/mp4",
            }

    _linux(monkeypatch)
    owner, paths, roots, _descriptor = _fixture(tmp_path)
    context = AuthenticatedWorkspaceContext(roots)
    relay = BytesRelay()
    try:
        payload = json.loads(_dispatch(
            {"prompt": "animate"},
            relay=relay,
            descriptor=_video_descriptor(),
            context=context,
            owner=owner,
            kind="video",
        ))
        output = Path(payload["video"])
        assert payload["video"].startswith("generated/videos/")
        assert (paths.default_workspace / output).read_bytes() == b"video-bytes"
        assert str(owner) not in json.dumps(payload)
        assert payload["mime_type"] == "video/mp4"
    finally:
        roots.close()


def test_dispatch_resolves_migrated_legacy_reference(tmp_path, monkeypatch):
    _linux(monkeypatch)
    owner, paths, roots, descriptor = _fixture(tmp_path)
    selected = paths.workspace_root / "projects" / "selected"
    selected.mkdir(parents=True)
    context = AuthenticatedWorkspaceContext(roots, workspace_prefix="projects/selected")
    reference = owner / "images" / "source.webp"
    reference.parent.mkdir(exist_ok=True)
    reference.write_bytes(b"legacy-reference")
    relay = Relay()
    try:
        migration = migrate_legacy_user_files(context)
        assert migration["mappings"]["images/source.webp"] == "generated/images/source.webp"
        assert not reference.exists()

        _dispatch(
            {"prompt": "edit", "aspect_ratio": "portrait", "image_url": str(reference)},
            relay=relay,
            descriptor=descriptor,
            context=context,
            owner=owner,
        )
        assert relay.kwargs["references"][0]["data"] == b"legacy-reference"
    finally:
        roots.close()


def test_dispatch_rejects_unmigrated_owner_reference(tmp_path, monkeypatch):
    _linux(monkeypatch)
    owner, _paths, roots, descriptor = _fixture(tmp_path)
    reference = owner / "images" / "source.webp"
    reference.parent.mkdir(exist_ok=True)
    reference.write_bytes(b"owner-reference")
    try:
        with pytest.raises(ValueError, match="outside the authenticated workspace"):
            _dispatch(
                {"prompt": "edit", "aspect_ratio": "portrait", "image_url": str(reference)},
                relay=Relay(),
                descriptor=descriptor,
                context=AuthenticatedWorkspaceContext(roots),
                owner=owner,
            )
    finally:
        roots.close()


def test_dispatch_rejects_sibling_workspace_reference(tmp_path):
    owner, paths, roots, descriptor = _fixture(tmp_path)
    selected = paths.workspace_root / "selected"
    sibling = paths.workspace_root / "sibling"
    selected.mkdir()
    sibling.mkdir()
    reference = sibling / "source.png"
    reference.write_bytes(b"reference")
    try:
        with pytest.raises(ValueError, match="outside the authenticated workspace"):
            _dispatch(
                {"prompt": "edit", "aspect_ratio": "square", "image_url": str(reference)},
                relay=Relay(),
                descriptor=descriptor,
                context=AuthenticatedWorkspaceContext(roots, workspace_prefix="selected"),
                owner=owner,
            )
    finally:
        roots.close()


def test_dispatch_rejects_cross_owner_reference(tmp_path):
    owner, _paths, roots, descriptor = _fixture(tmp_path)
    outside = tmp_path / "other" / "source.png"
    outside.parent.mkdir()
    outside.write_bytes(b"reference")
    try:
        with pytest.raises(ValueError, match="outside the authenticated workspace"):
            _dispatch(
                {"prompt": "edit", "aspect_ratio": "square", "image_url": str(outside)},
                relay=Relay(),
                descriptor=descriptor,
                context=AuthenticatedWorkspaceContext(roots),
                owner=owner,
            )
    finally:
        roots.close()


def test_dispatch_rejects_relative_reference(tmp_path):
    owner, _paths, roots, descriptor = _fixture(tmp_path)
    try:
        with pytest.raises(ValueError, match="absolute workspace path"):
            _dispatch(
                {"prompt": "edit", "aspect_ratio": "square", "image_url": "source.png"},
                relay=Relay(),
                descriptor=descriptor,
                context=AuthenticatedWorkspaceContext(roots),
                owner=owner,
            )
    finally:
        roots.close()


def test_dispatch_rejects_symlink_reference(tmp_path, monkeypatch):
    _linux(monkeypatch)
    owner, paths, roots, descriptor = _fixture(tmp_path)
    target = paths.default_workspace / "target.png"
    target.write_bytes(b"reference")
    link = paths.default_workspace / "link.png"
    link.symlink_to(target)
    try:
        with pytest.raises(OSError):
            _dispatch(
                {"prompt": "edit", "aspect_ratio": "square", "image_url": str(link)},
                relay=Relay(),
                descriptor=descriptor,
                context=AuthenticatedWorkspaceContext(roots),
                owner=owner,
            )
    finally:
        roots.close()


def test_dispatch_rejects_unsupported_mime_before_read(tmp_path):
    owner, paths, roots, descriptor = _fixture(tmp_path)
    reference = paths.default_workspace / "source.svg"
    reference.write_text("<svg/>")
    try:
        with pytest.raises(ValueError, match="type is unsupported"):
            _dispatch(
                {"prompt": "edit", "aspect_ratio": "square", "image_url": str(reference)},
                relay=Relay(),
                descriptor=descriptor,
                context=AuthenticatedWorkspaceContext(roots),
                owner=owner,
            )
    finally:
        roots.close()


def test_dispatch_rejects_per_file_and_total_limits(tmp_path, monkeypatch):
    _linux(monkeypatch)
    owner, paths, roots, descriptor = _fixture(tmp_path)
    first = paths.default_workspace / "first.png"
    second = paths.default_workspace / "second.png"
    first.write_bytes(b"abc")
    second.write_bytes(b"def")
    context = AuthenticatedWorkspaceContext(roots)
    try:
        with pytest.raises(ValueError, match="too large"):
            _dispatch(
                {"prompt": "edit", "aspect_ratio": "square", "image_url": str(first)},
                relay=Relay(),
                descriptor=replace(
                    descriptor,
                    max_reference_bytes=2,
                    max_total_reference_bytes=2,
                ),
                context=context,
                owner=owner,
            )
        with pytest.raises(ValueError, match="images are too large"):
            _dispatch(
                {
                    "prompt": "edit",
                    "aspect_ratio": "square",
                    "reference_image_urls": [str(first), str(second)],
                },
                relay=Relay(),
                descriptor=replace(
                    descriptor,
                    max_reference_bytes=4,
                    max_total_reference_bytes=5,
                ),
                context=context,
                owner=owner,
            )
    finally:
        roots.close()


def test_active_media_selection_reads_kind_section(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "image_gen": {"provider": "apiyi", "model": "gpt-image-2-medium"},
            "video_gen": {"provider": "fal", "model": "fal-video-1"},
        },
    )
    assert active_media_selection("image") == ("apiyi", "gpt-image-2-medium")
    assert active_media_selection("video") == ("fal", "fal-video-1")


def test_active_media_selection_defaults_to_empty(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    assert active_media_selection("image") == ("", "")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: (_ for _ in ()).throw(RuntimeError("no config")),
    )
    assert active_media_selection("video") == ("", "")
