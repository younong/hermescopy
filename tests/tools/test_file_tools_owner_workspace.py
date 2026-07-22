"""Descriptor-backed file-tool coverage for authenticated owner workers."""

from __future__ import annotations

import json

import pytest

import tools.file_tools as file_tools
from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import RootKind, controlled_roots_for
from hermes_cli.owner_runtime import ensure_owner_runtime_dirs, owner_worker_runtime_paths
from tools.file_operations import ControlledWorkspaceFileOperations
from tui_gateway.server import OwnerWorkerGatewayRuntime, _gateway_runtime


def _runtime_paths(tmp_path):
    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    return owner_worker_runtime_paths(owner_home=owner_home, worker_generation=1)


@pytest.fixture(autouse=True)
def _simulate_linux_controlled_roots(monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)


@pytest.fixture
def owner_a_workspace(tmp_path):
    roots = controlled_roots_for(_runtime_paths(tmp_path / "owner-a"))
    try:
        yield roots
    finally:
        roots.close()


@pytest.fixture
def owner_b_workspace(tmp_path):
    roots = controlled_roots_for(_runtime_paths(tmp_path / "owner-b"))
    try:
        yield roots
    finally:
        roots.close()


def _bind_runtime(roots):
    runtime = OwnerWorkerGatewayRuntime(
        owner_key="owner-a",
        worker_generation=1,
        worker_id="worker-a",
        lease_version=1,
        recovery_generation=0,
        filesystem_context=AuthenticatedWorkspaceContext(roots),
    )
    return _gateway_runtime.set(runtime)


def test_controlled_workspace_adapter_is_owner_isolated(owner_a_workspace, owner_b_workspace, monkeypatch, tmp_path):
    owner_a = ControlledWorkspaceFileOperations(AuthenticatedWorkspaceContext(owner_a_workspace))
    owner_b = ControlledWorkspaceFileOperations(AuthenticatedWorkspaceContext(owner_b_workspace))
    monkeypatch.chdir(tmp_path)

    assert owner_a.write_file("project/note.txt", "owner a\n").error is None
    assert owner_a.read_file_raw("project/note.txt").content == "owner a\n"
    assert owner_b.read_file_raw("project/note.txt").error == "File not found: project/note.txt"

    owner_b_path = owner_b.diagnostic_path("project/note.txt")
    denied = owner_a.read_file_raw(owner_b_path)
    assert denied.error is not None
    assert "workspace-relative" in denied.error


def test_owner_a_denial_leaves_owner_b_workspace_and_checkpoint_state_intact(
    owner_a_workspace, owner_b_workspace, monkeypatch, tmp_path
):
    owner_a = ControlledWorkspaceFileOperations(AuthenticatedWorkspaceContext(owner_a_workspace))
    owner_b = ControlledWorkspaceFileOperations(AuthenticatedWorkspaceContext(owner_b_workspace))
    checkpoint = owner_b_workspace.get(RootKind.TEMPORARY).canonical_path / "checkpoints" / "resume.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text('{"owner":"b"}\n')
    assert owner_b.write_file("project/keep.txt", "owner b\n").error is None
    monkeypatch.chdir(tmp_path)

    denied = owner_a.write_file(str(owner_b.diagnostic_path("project/keep.txt")), "overwrite")

    assert denied.error is not None
    assert owner_b.read_file_raw("project/keep.txt").content == "owner b\n"
    assert checkpoint.read_text() == '{"owner":"b"}\n'


def test_controlled_workspace_adapter_rejects_untrusted_path_forms(owner_a_workspace):
    adapter = ControlledWorkspaceFileOperations(AuthenticatedWorkspaceContext(owner_a_workspace))

    for path in ("/etc/passwd", "../outside", "./note.txt", "dir//note.txt", "dir/", "note\x00.txt", "~/.ssh/id"):
        result = adapter.write_file(path, "denied")
        assert result.error is not None, path


def test_authenticated_get_file_ops_uses_runtime_capability_without_terminal_fallback(owner_a_workspace, monkeypatch):
    monkeypatch.delenv("HERMES_OWNER_KEY", raising=False)
    token = _bind_runtime(owner_a_workspace)
    try:
        monkeypatch.setattr(
            file_tools,
            "ShellFileOperations",
            lambda *_args, **_kwargs: pytest.fail("legacy backend must not be created"),
        )
        assert isinstance(file_tools._get_file_ops(), ControlledWorkspaceFileOperations)
    finally:
        _gateway_runtime.reset(token)


def test_authenticated_read_does_not_resolve_ambient_cwd(owner_a_workspace, monkeypatch):
    owner_a_workspace.replace_bytes(RootKind.WORKSPACE, "default/note.txt", b"controlled\n")
    token = _bind_runtime(owner_a_workspace)
    try:
        monkeypatch.setattr(
            file_tools,
            "_resolve_base_dir",
            lambda *_args, **_kwargs: pytest.fail("authenticated read must not resolve a terminal cwd"),
        )
        monkeypatch.setattr(
            file_tools,
            "_resolve_path_for_task",
            lambda *_args, **_kwargs: pytest.fail("authenticated read must not resolve a host path"),
        )
        result = json.loads(file_tools.read_file_tool("note.txt"))
        assert result["content"] == "1|controlled\n2|"
    finally:
        _gateway_runtime.reset(token)


def test_authenticated_artifact_paths_stay_in_selected_workspace(
    owner_a_workspace, owner_b_workspace, monkeypatch
):
    owner_a_workspace.replace_bytes(
        RootKind.WORKSPACE, "default/slides/slide-01.jpg", b"image"
    )
    owner_b_workspace.replace_bytes(
        RootKind.WORKSPACE, "default/slides/secret.jpg", b"secret"
    )
    selected = ControlledWorkspaceFileOperations(
        AuthenticatedWorkspaceContext(owner_a_workspace)
    )
    token = _bind_runtime(owner_a_workspace)
    try:
        ambient_root = owner_b_workspace.get(RootKind.WORKSPACE).canonical_path
        monkeypatch.setenv("TERMINAL_CWD", str(ambient_root))
        relative = file_tools.resolve_delegated_artifact_path("slides/slide-01.jpg")
        diagnostic = file_tools.resolve_delegated_artifact_path(
            selected.diagnostic_path("slides/slide-01.jpg")
        )

        assert relative["path"] == "slides/slide-01.jpg"
        assert diagnostic["path"] == "slides/slide-01.jpg"
        assert relative["diagnostic_path"] == diagnostic["diagnostic_path"]
        assert str(ambient_root) not in relative["diagnostic_path"]
    finally:
        _gateway_runtime.reset(token)


@pytest.mark.parametrize(
    "bad_path",
    ["/workspace/slide.jpg", "../outside.jpg", "slides", "/etc/passwd"],
)
def test_authenticated_artifact_paths_fail_closed(
    owner_a_workspace, bad_path
):
    owner_a_workspace.replace_bytes(RootKind.WORKSPACE, "default/slides/file.jpg", b"x")
    token = _bind_runtime(owner_a_workspace)
    try:
        with pytest.raises(ValueError, match="invalid delegated artifact"):
            file_tools.resolve_delegated_artifact_path(bad_path)
    finally:
        _gateway_runtime.reset(token)


def test_authenticated_artifact_rejects_symlink_escape(owner_a_workspace, tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    link = owner_a_workspace.get(RootKind.WORKSPACE).canonical_path / "default/link.jpg"
    link.symlink_to(outside)
    token = _bind_runtime(owner_a_workspace)
    try:
        with pytest.raises(ValueError, match="invalid delegated artifact"):
            file_tools.resolve_delegated_artifact_path("link.jpg")
    finally:
        _gateway_runtime.reset(token)


def test_authenticated_write_does_not_resolve_ambient_cwd(owner_a_workspace, monkeypatch):
    token = _bind_runtime(owner_a_workspace)
    try:
        monkeypatch.setattr(
            file_tools,
            "_resolve_path_for_task",
            lambda *_args, **_kwargs: pytest.fail("authenticated write must not resolve a host path"),
        )
        result = json.loads(file_tools.write_file_tool("nested/note.txt", "controlled\n"))
        assert result["bytes_written"] == len("controlled\n")
        assert (
            owner_a_workspace.get(RootKind.WORKSPACE).canonical_path / "default/nested/note.txt"
        ).read_text() == "controlled\n"
    finally:
        _gateway_runtime.reset(token)


def test_authenticated_v4a_supports_add_update_delete_and_move(owner_a_workspace):
    token = _bind_runtime(owner_a_workspace)
    try:
        added = json.loads(file_tools.patch_tool(
            mode="patch",
            patch=(
                "*** Begin Patch\n"
                "*** Add File: src/example.py\n"
                "+before\n"
                "*** End Patch\n"
            ),
        ))
        assert added["success"] is True
        assert added["files_created"] == ["src/example.py"]

        updated = json.loads(file_tools.patch_tool(
            mode="patch",
            patch=(
                "*** Begin Patch\n"
                "*** Update File: src/example.py\n"
                "@@\n"
                "-before\n"
                "+after\n"
                "*** End Patch\n"
            ),
        ))
        assert updated["success"] is True
        assert updated["files_modified"] == ["src/example.py"]

        moved = json.loads(file_tools.patch_tool(
            mode="patch",
            patch=(
                "*** Begin Patch\n"
                "*** Move File: src/example.py -> src/renamed.py\n"
                "*** End Patch\n"
            ),
        ))
        assert moved["success"] is True
        assert moved["files_modified"] == ["src/example.py -> src/renamed.py"]

        deleted = json.loads(file_tools.patch_tool(
            mode="patch",
            patch=(
                "*** Begin Patch\n"
                "*** Delete File: src/renamed.py\n"
                "*** End Patch\n"
            ),
        ))
        assert deleted["success"] is True
        assert deleted["files_deleted"] == ["src/renamed.py"]
        assert not (owner_a_workspace.get(RootKind.WORKSPACE).canonical_path / "default/src/renamed.py").exists()
    finally:
        _gateway_runtime.reset(token)


@pytest.mark.parametrize("bad_path", ["/tmp/file", "../file", "~/.ssh/id", "dir//file", "dir/"])
def test_authenticated_v4a_rejects_all_untrusted_paths_before_apply(owner_a_workspace, bad_path):
    token = _bind_runtime(owner_a_workspace)
    try:
        original = owner_a_workspace.get(RootKind.WORKSPACE).canonical_path / "default/keep.txt"
        original.write_text("keep\n")
        result = json.loads(file_tools.patch_tool(
            mode="patch",
            patch=(
                "*** Begin Patch\n"
                "*** Update File: keep.txt\n"
                "@@\n"
                "-keep\n"
                "+changed\n"
                f"*** Add File: {bad_path}\n"
                "+denied\n"
                "*** End Patch\n"
            ),
        ))
        assert result["success"] is False
        assert "workspace-relative" in result["error"] or "empty, dot, or parent" in result["error"]
        assert original.read_text() == "keep\n"
    finally:
        _gateway_runtime.reset(token)


def test_authenticated_read_limit_bounds_descriptor_reads(owner_a_workspace, monkeypatch):
    monkeypatch.setattr("tools.file_operations._CONTROLLED_MAX_READ_BYTES", 4)
    adapter = ControlledWorkspaceFileOperations(AuthenticatedWorkspaceContext(owner_a_workspace))
    assert adapter.write_file("large.txt", "12345").error is None

    result = adapter.read_file_raw("large.txt")
    assert result.error == "File exceeds the authenticated read limit of 4 bytes"


def test_authenticated_search_stays_under_workspace_and_supports_modes(owner_a_workspace, owner_b_workspace):
    owner_a_workspace.replace_bytes(
        RootKind.WORKSPACE,
        "default/src/example.py",
        b"after\nafter again\n",
    )
    owner_a_workspace.replace_bytes(RootKind.WORKSPACE, "default/src/other.txt", b"after\n")
    owner_a_workspace.replace_bytes(RootKind.WORKSPACE, "default/.hidden.txt", b"after\n")
    owner_b_workspace.replace_bytes(RootKind.WORKSPACE, "default/src/secret.py", b"after\n")
    token = _bind_runtime(owner_a_workspace)
    try:
        content = json.loads(file_tools.search_tool("after", path="src", limit=1, offset=1))
        assert content["total_count"] == 3
        assert content["truncated"] is True
        assert content["matches"][0]["path"] == "src/example.py"
        assert content["matches"][0]["line"] == 2

        counts = json.loads(file_tools.search_tool("after", path="src", output_mode="count"))
        assert counts["counts"] == {"src/example.py": 2, "src/other.txt": 1}

        files = json.loads(file_tools.search_tool("src/*.py", target="files"))
        assert files["files"] == ["src/example.py"]

        globbed = json.loads(file_tools.search_tool("after", path="src", file_glob="*.py"))
        assert globbed["total_count"] == 2
        assert {match["path"] for match in globbed["matches"]} == {"src/example.py"}

        regex_error = json.loads(file_tools.search_tool("[", path="src"))
        assert "Search failed" in regex_error["error"]

        invalid_path = json.loads(file_tools.search_tool("after", path="../outside"))
        assert "empty, dot, or parent" in invalid_path["error"]
    finally:
        _gateway_runtime.reset(token)


def test_authenticated_missing_capability_fails_closed(monkeypatch):
    runtime = OwnerWorkerGatewayRuntime(
        owner_key="owner-a",
        worker_generation=1,
        worker_id="worker-a",
        lease_version=1,
        recovery_generation=0,
    )
    token = _gateway_runtime.set(runtime)
    try:
        monkeypatch.setattr(
            file_tools,
            "ShellFileOperations",
            lambda *_args, **_kwargs: pytest.fail("missing capability must not fall back to shell"),
        )
        with pytest.raises(RuntimeError, match="lacks filesystem capability"):
            file_tools._get_file_ops()
    finally:
        _gateway_runtime.reset(token)
