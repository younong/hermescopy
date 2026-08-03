import json
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import controlled_roots_for
from hermes_cli.deployment_image import DeploymentImageDescriptor
from hermes_cli.owner_runtime import ensure_owner_runtime_dirs, owner_worker_runtime_paths
from hermes_cli.owner_worker.image_dispatch import dispatch_deployment_image
from hermes_cli.owner_worker.user_files import migrate_legacy_user_files


class Relay:
    def generate(self, **kwargs):
        self.kwargs = kwargs
        return {
            "image_bytes": b"generated",
            "mime_type": "image/png",
            "provider": "apiyi",
            "model": "gpt-image-2-medium",
            "aspect_ratio": kwargs["aspect_ratio"],
            "modality": "image" if kwargs["references"] else "text",
            "metadata": {"size": "1024x1024"},
        }


def _fixture(tmp_path):
    owner = tmp_path / "owner"
    ensure_owner_runtime_dirs(owner)
    paths = owner_worker_runtime_paths(owner_home=owner, worker_generation=1)
    roots = controlled_roots_for(paths)
    descriptor = DeploymentImageDescriptor(
        provider="apiyi",
        model="gpt-image-2-medium",
        policy_id="p",
        allowed_models=("gpt-image-2-medium",),
    )
    return owner, paths, roots, descriptor


def _dispatch(arguments, *, relay, descriptor, context, owner):
    return dispatch_deployment_image(
        arguments,
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
        assert relay.kwargs["references"][0] == {
            "name": "source.png", "mime_type": "image/png", "data": b"reference",
        }
        output = Path(payload["image"])
        assert output.parent == selected / "generated" / "images"
        assert output.read_bytes() == b"generated"
        assert payload["size"] == "1024x1024"
        assert "api_key" not in payload
        assert "base_url" not in payload
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
