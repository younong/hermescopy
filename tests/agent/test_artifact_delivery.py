from pathlib import Path

import pytest

from agent.artifact_delivery import (
    MAX_DOWNLOAD_BYTES,
    append_artifact_delivery_warning,
    append_artifact_validation_failure,
    extract_declared_artifact_paths,
    validate_declared_artifacts,
)


def test_extracts_only_explicit_local_file_references():
    text = """完成。\n\n[下载](dist/tool.zip \"archive\")\n生成文件：`dist/report.pdf`\nFull output saved to: dist/log.txt\n[网站](https://example.com)\n```md\n[example](/tmp/not-real.zip)\n```\n"""
    assert extract_declared_artifact_paths(text) == [
        "dist/tool.zip",
        "dist/report.pdf",
        "dist/log.txt",
    ]


def test_validates_regular_file_inside_task_workspace(tmp_path, monkeypatch):
    archive = tmp_path / "dist" / "tool.zip"
    archive.parent.mkdir()
    archive.write_bytes(b"zip")
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
            "size_bytes": 3,
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
