import zipfile
from pathlib import Path

import pytest

from agent.artifact_delivery import (
    MAX_DOWNLOAD_BYTES,
    append_artifact_delivery_failure,
    append_artifact_delivery_warning,
    append_artifact_validation_failure,
    build_artifact_delivery_nudge,
    build_zip_delivery_nudge,
    extract_declared_artifact_paths,
    validate_declared_artifacts,
    zip_delivery_requested,
)


def test_extracts_only_explicit_local_file_references():
    text = """完成。\n\n[下载](dist/tool.zip \"archive\")\n生成文件：`dist/report.pdf`\nFull output saved to: dist/log.txt\n[网站](https://example.com)\n```md\n[example](/tmp/not-real.zip)\n```\n"""
    assert extract_declared_artifact_paths(text) == [
        "dist/tool.zip",
        "dist/report.pdf",
        "dist/log.txt",
    ]


@pytest.mark.parametrize(
    "message",
    [
        "请打包成 zip 发给我",
        "把这些文件做成压缩包",
        [{"type": "text", "text": "需要 ZIP 文件"}, {"type": "image_url"}],
    ],
)
def test_detects_explicit_zip_request(message):
    assert zip_delivery_requested(message)


@pytest.mark.parametrize("message", ["不要打包 zip", "不用压缩包，直接发文件"])
def test_does_not_treat_explicit_zip_rejection_as_request(message):
    assert not zip_delivery_requested(message)


def test_zip_gate_retries_when_requested_archive_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    nudge = build_zip_delivery_nudge(
        user_message="请打包成 zip 发给我",
        final_response="文件已经做好了。",
        task_id="artifact-test",
    )

    assert "never the entire workspace" in nudge
    assert "markdown download link" in nudge


def test_zip_gate_retries_multiple_loose_files(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    nudge = build_zip_delivery_nudge(
        user_message="把文件发给我",
        final_response="[a](a.txt)\n[b](b.txt)",
        task_id="artifact-test",
    )

    assert nudge is not None


def test_zip_gate_accepts_valid_nonempty_archive(tmp_path, monkeypatch):
    archive = tmp_path / "deliverables.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("a.txt", "a")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    nudge = build_zip_delivery_nudge(
        user_message="请给我 zip",
        final_response="[下载](deliverables.zip)",
        task_id="artifact-test",
    )

    assert nudge is None


@pytest.mark.parametrize("payload", [b"not a zip", None])
def test_zip_gate_rejects_invalid_or_empty_archive(tmp_path, monkeypatch, payload):
    archive = tmp_path / "deliverables.zip"
    if payload is None:
        with zipfile.ZipFile(archive, "w"):
            pass
    else:
        archive.write_bytes(payload)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    nudge = build_zip_delivery_nudge(
        user_message="请给我 zip",
        final_response="[下载](deliverables.zip)",
        task_id="artifact-test",
    )

    assert nudge is not None


def test_zip_gate_rejects_archive_with_parent_traversal(tmp_path, monkeypatch):
    archive = tmp_path / "deliverables.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../secret.txt", "secret")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    nudge = build_zip_delivery_nudge(
        user_message="请给我 zip",
        final_response="[下载](deliverables.zip)",
        task_id="artifact-test",
    )

    assert nudge is not None


def test_artifact_gate_retries_when_declared_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    nudge = build_artifact_delivery_nudge(
        final_response="生成完成：[下载](missing/report.pdf)",
        task_id="artifact-test",
    )
    assert nudge is not None
    assert "appropriate tool" in nudge


def test_extracts_successful_tool_artifact_paths_but_not_failures():
    from agent.artifact_delivery import extract_tool_artifact_paths

    assert extract_tool_artifact_paths(
        "image_generate", '{"success": true, "image": "/workspace/out.png"}'
    ) == ["/workspace/out.png"]
    assert extract_tool_artifact_paths(
        "terminal", '{"success": false, "output_file": "/workspace/out.txt"}'
    ) == []


def test_artifact_gate_does_not_nudge_for_workspace_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    nudge = build_artifact_delivery_nudge(
        final_response="[下载](/outside/report.pdf)",
        task_id="artifact-test",
    )
    assert nudge is None


def test_artifact_gate_stops_after_bounded_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    assert build_artifact_delivery_nudge(
        final_response="生成完成：[下载](missing/report.pdf)",
        task_id="artifact-test",
        attempts=2,
    ) is None


def test_artifact_failure_removes_unverifiable_claim():
    response = append_artifact_delivery_failure(
        "已生成：MEDIA:/missing/report.pdf", ["/missing/report.pdf"]
    )
    assert "MEDIA:" not in response
    assert "交付失败" in response


def test_zip_gate_stops_after_bounded_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    assert build_zip_delivery_nudge(
        user_message="请给我 zip",
        final_response="没有生成文件。",
        task_id="artifact-test",
        attempts=2,
    ) is None


def test_validates_regular_file_inside_task_workspace(tmp_path, monkeypatch):
    archive = tmp_path / "dist" / "tool.zip"
    archive.parent.mkdir()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("tool.txt", "zip")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    artifacts, rejected = validate_declared_artifacts(
        "[下载](dist/tool.zip)", task_id="artifact-test"
    )

    assert rejected == []
    assert artifacts == [
        {
            "id": artifacts[0]["id"],
            "mime_type": "application/zip",
            "name": "tool.zip",
            "path": str(archive),
            "size_bytes": archive.stat().st_size,
        }
    ]

    next_turn, _ = validate_declared_artifacts(
        "[下载](dist/tool.zip)",
        task_id="artifact-test",
        artifact_namespace="next-turn",
    )
    assert next_turn[0]["id"] != artifacts[0]["id"]


@pytest.mark.parametrize("target", ["directory", "missing.zip", "../outside.zip"])
def test_rejects_non_deliverable_paths(tmp_path, monkeypatch, target):
    (tmp_path / "directory").mkdir()
    outside = tmp_path.parent / "outside.zip"
    outside.write_bytes(b"outside")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    artifacts, rejected = validate_declared_artifacts(
        f"生成文件：`{target}`", task_id="artifact-test"
    )

    assert artifacts == []
    assert rejected == [target]


def test_rejects_symlink_escape_and_oversized_file(tmp_path, monkeypatch):
    outside = tmp_path.parent / "secret.zip"
    outside.write_bytes(b"secret")
    (tmp_path / "link.zip").symlink_to(outside)
    large = tmp_path / "large.zip"
    with large.open("wb") as stream:
        stream.truncate(MAX_DOWNLOAD_BYTES + 1)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    artifacts, rejected = validate_declared_artifacts(
        "[link](link.zip)\n[large](large.zip)", task_id="artifact-test"
    )

    assert artifacts == []
    assert rejected == ["link.zip", "large.zip"]


def test_warning_does_not_claim_invalid_reference_was_delivered():
    response = append_artifact_delivery_warning("工具已完成。", ["project-dir"])
    assert "没有生成下载卡片" in response
    assert "先打包为归档文件" in response


def test_validator_failure_warning_fails_closed():
    response = append_artifact_validation_failure("[下载](tool.zip)")
    assert "交付校验暂时不可用" in response
    assert "请不要将这些路径视为已交付文件" in response
