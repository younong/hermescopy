import errno
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.controlled_roots import ExpectedType, RootKind, controlled_roots_for
from hermes_cli.owner_runtime import ensure_owner_runtime_dirs, owner_worker_runtime_paths


def _runtime_paths(tmp_path):
    owner_home = ensure_owner_runtime_dirs(tmp_path / "owner")
    return owner_worker_runtime_paths(owner_home=owner_home, worker_generation=1)


def _linux(monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)


def test_controlled_roots_open_all_trusted_directory_capabilities(tmp_path):
    paths = _runtime_paths(tmp_path)
    roots = controlled_roots_for(paths)

    try:
        assert set(roots.roots) == set(RootKind)
        assert roots.get(RootKind.GLOBAL_READONLY).writable is False
        assert roots.get(RootKind.OWNER_WRITABLE).canonical_path == paths.owner_home
        assert roots.get(RootKind.WORKSPACE).canonical_path == paths.workspace_root
        assert roots.get(RootKind.TEMPORARY).canonical_path == paths.paths["temporary_root"]
        for root in roots.roots.values():
            assert stat.S_ISDIR(os.fstat(root.directory_fd).st_mode)
            assert os.get_inheritable(root.directory_fd) is False
    finally:
        roots.close()


def test_controlled_roots_are_distinct_for_separate_owner_homes(tmp_path):
    owner_a = controlled_roots_for(_runtime_paths(tmp_path / "a"))
    owner_b = controlled_roots_for(_runtime_paths(tmp_path / "b"))

    try:
        assert os.fstat(owner_a.get(RootKind.OWNER_WRITABLE).directory_fd).st_ino != os.fstat(
            owner_b.get(RootKind.OWNER_WRITABLE).directory_fd
        ).st_ino
        assert owner_a.get(RootKind.WORKSPACE).canonical_path != owner_b.get(RootKind.WORKSPACE).canonical_path
    finally:
        owner_a.close()
        owner_b.close()


def test_controlled_roots_close_is_idempotent(tmp_path):
    roots = controlled_roots_for(_runtime_paths(tmp_path))
    fd = roots.get(RootKind.WORKSPACE).directory_fd

    roots.close()
    roots.close()

    with pytest.raises(OSError) as exc_info:
        os.fstat(fd)
    assert exc_info.value.errno == errno.EBADF


def test_controlled_roots_close_previously_opened_fds_when_later_open_fails(tmp_path, monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots

    paths = _runtime_paths(tmp_path)
    real_open = controlled_roots._open_directory
    opened_fds: list[int] = []

    def fail_workspace(path):
        if path == paths.workspace_root:
            raise OSError(errno.EACCES, "denied")
        fd = real_open(path)
        opened_fds.append(fd)
        return fd

    monkeypatch.setattr(controlled_roots, "_open_directory", fail_workspace)

    with pytest.raises(OSError, match="denied"):
        controlled_roots_for(paths)

    for fd in opened_fds:
        with pytest.raises(OSError) as exc_info:
            os.fstat(fd)
        assert exc_info.value.errno == errno.EBADF


def test_controlled_roots_reject_platform_without_directory_fd_support(tmp_path, monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.delattr(controlled_roots.os, "O_DIRECTORY", raising=False)

    with pytest.raises(RuntimeError, match="O_DIRECTORY"):
        controlled_roots_for(_runtime_paths(tmp_path))


def test_open_relative_fallback_opens_nested_objects_without_process_cwd(tmp_path, monkeypatch):
    _linux(monkeypatch)
    paths = _runtime_paths(tmp_path)
    target = paths.workspace_root / "project" / "note.txt"
    target.parent.mkdir()
    target.write_text("safe\n")
    monkeypatch.chdir(tmp_path)
    roots = controlled_roots_for(paths)

    try:
        fd = roots.open_relative(RootKind.WORKSPACE, "project/note.txt", expected_type=ExpectedType.REGULAR_FILE)
        try:
            assert os.read(fd, 100) == b"safe\n"
            assert os.get_inheritable(fd) is False
        finally:
            os.close(fd)

        directory_fd = roots.open_relative(RootKind.WORKSPACE, "project", expected_type=ExpectedType.DIRECTORY)
        try:
            assert stat.S_ISDIR(os.fstat(directory_fd).st_mode)
        finally:
            os.close(directory_fd)
    finally:
        roots.close()


@pytest.mark.parametrize(
    "relative_path",
    ["", ".", "..", "a/../b", "/etc/passwd", "a//b", "a/", "a\x00b", b"a", Path("a")],
)
def test_open_relative_rejects_untrusted_path_forms(tmp_path, monkeypatch, relative_path):
    _linux(monkeypatch)
    roots = controlled_roots_for(_runtime_paths(tmp_path))

    try:
        with pytest.raises((TypeError, ValueError)):
            roots.open_relative(RootKind.WORKSPACE, relative_path, expected_type=ExpectedType.REGULAR_FILE)
    finally:
        roots.close()


def test_open_relative_rejects_writes_to_global_readonly_root(tmp_path, monkeypatch):
    _linux(monkeypatch)
    roots = controlled_roots_for(_runtime_paths(tmp_path))

    try:
        with pytest.raises(PermissionError, match="read-only"):
            roots.open_relative(
                RootKind.GLOBAL_READONLY,
                "hermes_cli/controlled_roots.py",
                expected_type=ExpectedType.REGULAR_FILE,
                writable=True,
            )
    finally:
        roots.close()


def test_open_relative_fallback_rejects_symlinks_special_files_and_wrong_types(tmp_path, monkeypatch):
    _linux(monkeypatch)
    paths = _runtime_paths(tmp_path)
    workspace = paths.workspace_root
    (workspace / "directory").mkdir()
    (workspace / "regular.txt").write_text("safe")
    (workspace / "not-a-directory").write_text("safe")
    try:
        (workspace / "link.txt").symlink_to(workspace / "regular.txt")
    except OSError:
        pytest.skip("symlinks unavailable")
    roots = controlled_roots_for(paths)

    try:
        with pytest.raises(OSError):
            roots.open_relative(RootKind.WORKSPACE, "link.txt", expected_type=ExpectedType.REGULAR_FILE)
        with pytest.raises(RuntimeError, match="expected regular_file"):
            roots.open_relative(RootKind.WORKSPACE, "directory", expected_type=ExpectedType.REGULAR_FILE)
        with pytest.raises(OSError):
            roots.open_relative(RootKind.WORKSPACE, "regular.txt", expected_type=ExpectedType.DIRECTORY)
        with pytest.raises(OSError):
            roots.open_relative(RootKind.WORKSPACE, "not-a-directory/child", expected_type=ExpectedType.REGULAR_FILE)
    finally:
        roots.close()


def test_open_relative_openat2_receives_required_resolution_policy(tmp_path, monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    paths = _runtime_paths(tmp_path)
    target = paths.workspace_root / "note.txt"
    target.write_text("safe")
    roots = controlled_roots_for(paths)
    calls = []

    def fake_openat2(directory_fd, relative_path, flags, resolve_flags):
        calls.append((directory_fd, relative_path, flags, resolve_flags))
        return os.open(target, os.O_RDONLY)

    monkeypatch.setattr(controlled_roots, "_openat2", fake_openat2)
    try:
        fd = roots.open_relative(RootKind.WORKSPACE, "note.txt", expected_type=ExpectedType.REGULAR_FILE)
        try:
            assert calls == [
                (
                    roots.get(RootKind.WORKSPACE).directory_fd,
                    "note.txt",
                    calls[0][2],
                    controlled_roots.RESOLVE_BENEATH
                    | controlled_roots.RESOLVE_NO_SYMLINKS
                    | controlled_roots.RESOLVE_NO_MAGICLINKS
                    | controlled_roots.RESOLVE_NO_XDEV,
                )
            ]
            assert calls[0][2] & os.O_NOFOLLOW
        finally:
            os.close(fd)
    finally:
        roots.close()


def test_open_relative_does_not_fallback_after_openat2_policy_failure(tmp_path, monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    roots = controlled_roots_for(_runtime_paths(tmp_path))
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: (_ for _ in ()).throw(OSError(errno.ELOOP, "symlink")))
    monkeypatch.setattr(controlled_roots, "_open_relative_fallback", lambda *_args, **_kwargs: pytest.fail("fallback used"))

    try:
        with pytest.raises(OSError) as exc_info:
            roots.open_relative(RootKind.WORKSPACE, "missing.txt", expected_type=ExpectedType.REGULAR_FILE)
        assert exc_info.value.errno == errno.ELOOP
    finally:
        roots.close()


def test_open_relative_fallback_rejects_device_boundary_and_closes_fd(tmp_path, monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots

    _linux(monkeypatch)
    paths = _runtime_paths(tmp_path)
    target = paths.workspace_root / "note.txt"
    target.write_text("safe")
    roots = controlled_roots_for(paths)
    real_fstat = controlled_roots.os.fstat
    captured_fds = []

    def mismatched_fstat(fd):
        metadata = real_fstat(fd)
        if fd not in {roots.get(RootKind.WORKSPACE).directory_fd}:
            captured_fds.append(fd)
            return SimpleNamespace(st_mode=metadata.st_mode, st_dev=metadata.st_dev + 1)
        return metadata

    monkeypatch.setattr(controlled_roots.os, "fstat", mismatched_fstat)
    try:
        with pytest.raises(RuntimeError, match="filesystem boundary"):
            roots.open_relative(RootKind.WORKSPACE, "note.txt", expected_type=ExpectedType.REGULAR_FILE)
    finally:
        roots.close()

    for fd in captured_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_open_relative_rejects_non_linux_without_path_fallback(tmp_path, monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.setattr(controlled_roots.sys, "platform", "darwin")
    roots = controlled_roots_for(_runtime_paths(tmp_path))
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: pytest.fail("openat2 called"))

    try:
        with pytest.raises(RuntimeError, match="require Linux"):
            roots.open_relative(RootKind.WORKSPACE, "anything", expected_type=ExpectedType.REGULAR_FILE)
    finally:
        roots.close()


def test_open_relative_rejects_missing_fallback_prerequisites(tmp_path, monkeypatch):
    import hermes_cli.controlled_roots as controlled_roots

    _linux(monkeypatch)
    roots = controlled_roots_for(_runtime_paths(tmp_path))
    monkeypatch.setattr(controlled_roots, "_fallback_supported", lambda: False)

    try:
        with pytest.raises(RuntimeError, match="fallback"):
            roots.open_relative(RootKind.WORKSPACE, "anything", expected_type=ExpectedType.REGULAR_FILE)
    finally:
        roots.close()


def test_descriptor_operations_list_replace_rename_and_remove(tmp_path, monkeypatch):
    _linux(monkeypatch)
    paths = _runtime_paths(tmp_path)
    roots = controlled_roots_for(paths)

    try:
        roots.mkdirs(RootKind.WORKSPACE, "project/assets")
        entry = roots.replace_bytes(RootKind.WORKSPACE, "project/assets/old.txt", b"safe")
        assert entry.relative_path == "project/assets/old.txt"
        assert entry.size == 4
        assert [item.name for item in roots.list_directory(RootKind.WORKSPACE, "project/assets")] == ["old.txt"]

        roots.rename(RootKind.WORKSPACE, "project/assets/old.txt", "project/assets/new.txt")
        with pytest.raises(FileExistsError):
            roots.replace_bytes(RootKind.WORKSPACE, "project/assets/new.txt", b"no", overwrite=False)
        roots.replace_bytes(RootKind.WORKSPACE, "project/assets/new.txt", b"updated")
        assert (paths.workspace_root / "project/assets/new.txt").read_bytes() == b"updated"

        roots.remove(RootKind.WORKSPACE, "project", recursive=True)
        assert not (paths.workspace_root / "project").exists()
    finally:
        roots.close()


def test_open_relative_rejects_multiply_linked_regular_files(tmp_path, monkeypatch):
    _linux(monkeypatch)
    paths = _runtime_paths(tmp_path)
    source = tmp_path / "outside.txt"
    source.write_text("outside")
    os.link(source, paths.workspace_root / "linked.txt")
    roots = controlled_roots_for(paths)

    try:
        with pytest.raises(RuntimeError, match="multiply-linked"):
            roots.open_relative(RootKind.WORKSPACE, "linked.txt", expected_type=ExpectedType.REGULAR_FILE)
        with pytest.raises(RuntimeError, match="multiply-linked"):
            roots.replace_bytes(RootKind.WORKSPACE, "linked.txt", b"blocked")
        assert source.read_text() == "outside"
    finally:
        roots.close()


def test_descriptor_atomic_writer_streams_and_aborts_without_promoting_partial_data(tmp_path, monkeypatch):
    _linux(monkeypatch)
    paths = _runtime_paths(tmp_path)
    roots = controlled_roots_for(paths)

    try:
        writer = roots.begin_atomic_replace(RootKind.WORKSPACE, "streamed/data.bin")
        writer.write(b"first-")
        writer.write(b"second")
        assert writer.bytes_written == len(b"first-second")
        entry = writer.commit()
        assert entry.size == len(b"first-second")
        assert (paths.workspace_root / "streamed/data.bin").read_bytes() == b"first-second"

        aborted = roots.begin_atomic_replace(RootKind.WORKSPACE, "streamed/aborted.bin")
        aborted.write(b"partial")
        aborted.abort()
        assert not (paths.workspace_root / "streamed/aborted.bin").exists()
        assert not [path for path in (paths.workspace_root / "streamed").iterdir() if ".upload" in path.name]
    finally:
        roots.close()


def test_descriptor_operations_do_not_follow_symlinks_for_mutation(tmp_path, monkeypatch):
    _linux(monkeypatch)
    paths = _runtime_paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    link = paths.workspace_root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    roots = controlled_roots_for(paths)

    try:
        with pytest.raises(OSError):
            roots.replace_bytes(RootKind.WORKSPACE, "link/escape.txt", b"blocked")
        assert not (outside / "escape.txt").exists()
        with pytest.raises((OSError, RuntimeError)):
            roots.remove(RootKind.WORKSPACE, "link", recursive=True)
        assert (outside / "keep.txt").read_text() == "keep"
    finally:
        roots.close()
