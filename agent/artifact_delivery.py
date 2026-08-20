"""Validate files that an assistant explicitly offers to the user."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import zipfile
from pathlib import PurePosixPath

from gateway.platforms.response_media import MEDIA_TAG_CLEANUP_RE
from tools.file_tools import resolve_delegated_artifact_path

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)]\(([^)\n]+)\)")
_LABELED_PATH_RE = re.compile(
    r"(?:文件路径|文件地址|生成文件|生成路径|下载地址|输出文件|"
    r"full\s+(?:output|text)\s+saved\s+to|file\s*path|output\s*file|"
    r"generated\s*file|saved\s*(?:file\s*)?(?:at|to))"
    r"\s*[：:]\s*(?:\*{1,2})?\s*(?:`([^`\n]+)`|([^\s`]+))",
    re.IGNORECASE,
)
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_REMOTE_SCHEMES = ("http://", "https://", "data:", "blob:", "mailto:", "javascript:")
_ZIP_REQUEST_RE = re.compile(
    r"(?:\.zip\b|\bzip\b|压缩包|打包(?:成|为)?(?:\s*zip)?|归档文件)",
    re.IGNORECASE,
)
_ZIP_REJECTION_RE = re.compile(
    r"(?:不|无需|不用|不要).{0,4}(?:\.zip\b|\bzip\b|压缩包|打包)|"
    r"(?:do\s+not|don't|without)\s+(?:a\s+)?(?:zip|archive)",
    re.IGNORECASE,
)
_MAX_ZIP_DELIVERY_NUDGES = 2


def _inside_fenced_code(index: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in ranges)


def _normalize_reference(value: str) -> str | None:
    candidate = str(value or "").strip()
    markdown_target = re.match(r'^(<[^>]+>|\S+?)(?:\s+["\'][^"\']*["\'])?$', candidate)
    if markdown_target:
        candidate = markdown_target.group(1)
    if candidate.startswith("<") and candidate.endswith(">"):
        candidate = candidate[1:-1].strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "`\"'":
        candidate = candidate[1:-1].strip()
    candidate = candidate.rstrip(".,;:!?。，、；：！？")
    if not candidate or candidate.lower().startswith(_REMOTE_SCHEMES):
        return None
    if candidate.lower().startswith("file://"):
        candidate = candidate[7:]
    sandbox = re.match(r"^sandbox:/{0,2}(/.*)$", candidate, re.IGNORECASE)
    return sandbox.group(1) if sandbox else candidate


def extract_declared_artifact_paths(text: str) -> list[str]:
    """Return local paths explicitly presented as generated/downloadable files."""
    if not isinstance(text, str) or not text.strip():
        return []
    code_ranges = [match.span() for match in _FENCED_CODE_RE.finditer(text)]
    candidates: list[tuple[int, str]] = []
    for match in _MARKDOWN_LINK_RE.finditer(text):
        candidates.append((match.start(), match.group(2)))
    for match in _LABELED_PATH_RE.finditer(text):
        candidates.append((match.start(), match.group(1) or match.group(2)))
    for match in MEDIA_TAG_CLEANUP_RE.finditer(text):
        candidates.append((match.start(), match.group("path")))

    paths: list[str] = []
    seen: set[str] = set()
    for index, raw in sorted(candidates, key=lambda item: item[0]):
        if _inside_fenced_code(index, code_ranges):
            continue
        path = _normalize_reference(raw)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def zip_delivery_requested(user_message: object) -> bool:
    """Return whether the current user explicitly requested a ZIP archive."""
    if isinstance(user_message, str):
        text = user_message
    elif isinstance(user_message, list):
        text = "\n".join(
            str(part.get("text") or "")
            for part in user_message
            if isinstance(part, dict) and part.get("type") == "text"
        )
    else:
        text = ""
    return bool(_ZIP_REQUEST_RE.search(text)) and not bool(_ZIP_REJECTION_RE.search(text))


def _valid_zip(path: str) -> bool:
    """Require a readable archive with safe names and at least one real file."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = archive.infolist()
            if not members or not any(not member.is_dir() for member in members):
                return False
            for member in members:
                member_path = PurePosixPath(member.filename.replace("\\", "/"))
                if member_path.is_absolute() or ".." in member_path.parts:
                    return False
            return archive.testzip() is None
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return False


def zip_delivery_required(user_message: object, declared_paths: list[str]) -> bool:
    """Require one archive for an explicit ZIP request or a multi-file offer."""
    return zip_delivery_requested(user_message) or len(declared_paths) > 1


def zip_delivery_satisfied(
    declared_paths: list[str],
    artifacts: list[dict[str, object]],
) -> bool:
    """Return whether the response offers exactly one validated ZIP artifact."""
    return (
        len(declared_paths) == 1
        and len(artifacts) == 1
        and str(artifacts[0].get("name") or "").lower().endswith(".zip")
    )


def build_zip_delivery_nudge(
    *,
    user_message: object,
    final_response: str,
    task_id: str = "default",
    attempts: int = 0,
    max_attempts: int = _MAX_ZIP_DELIVERY_NUDGES,
    declared_paths: list[str] | None = None,
) -> str | None:
    """Keep the tool loop running until an explicitly requested ZIP is valid."""
    if attempts >= max_attempts:
        return None

    if declared_paths is None:
        declared_paths = extract_declared_artifact_paths(final_response)
    if not zip_delivery_required(user_message, declared_paths):
        return None

    artifacts, _rejected = validate_declared_artifacts(
        final_response,
        task_id=task_id,
        declared_paths=declared_paths,
    )
    if zip_delivery_satisfied(declared_paths, artifacts):
        return None

    return (
        "[System: This turn requires a ZIP archive, but your attempted final answer "
        "does not offer a valid, non-empty ZIP from the current task "
        "workspace. Continue now: create one archive containing only the intended "
        "deliverables (never the entire workspace), verify that it opens and its "
        "members are correct, then provide a markdown download link to the .zip file.]"
    )


def validate_declared_artifacts(
    text: str,
    *,
    task_id: str = "default",
    artifact_namespace: str = "",
    declared_paths: list[str] | None = None,
) -> tuple[list[dict[str, object]], list[str]]:
    """Validate explicit file offers and return gateway metadata plus rejected inputs."""
    artifacts: list[dict[str, object]] = []
    rejected: list[str] = []
    if declared_paths is None:
        declared_paths = extract_declared_artifact_paths(text)
    for path in declared_paths:
        try:
            resolved = resolve_delegated_artifact_path(
                path,
                task_id,
                require_workspace=True,
                maximum_bytes=MAX_DOWNLOAD_BYTES,
            )
        except ValueError:
            rejected.append(path)
            continue
        child_path = str(resolved["path"])
        name = PurePosixPath(child_path.replace("\\", "/")).name
        if name.lower().endswith(".zip") and not _valid_zip(
            str(resolved.get("diagnostic_path") or child_path)
        ):
            rejected.append(path)
            continue
        mime_type = (
            "application/zip"
            if name.lower().endswith(".zip")
            else mimetypes.guess_type(name)[0]
        )
        identity = (
            f"{artifact_namespace}\0{child_path}\0{int(resolved['size_bytes'])}"
        ).encode("utf-8")
        artifact_id = "artifact-" + hashlib.sha256(identity).hexdigest()[:20]
        artifact: dict[str, object] = {
            "id": artifact_id,
            "name": name,
            "path": child_path,
            "size_bytes": int(resolved["size_bytes"]),
        }
        if mime_type:
            artifact["mime_type"] = mime_type
        artifacts.append(artifact)
    return artifacts, rejected


def append_artifact_delivery_warning(text: str, rejected: list[str]) -> str:
    if not rejected:
        return text
    warning = (
        f"⚠️ 交付校验：上述 {len(rejected)} 个路径不是当前工作区内可下载的普通文件，"
        "因此没有生成下载卡片。多文件目录需要先打包为归档文件。"
    )
    return text.rstrip() + "\n\n" + warning


def append_artifact_validation_failure(text: str) -> str:
    """Fail closed when the delivery validator itself is unavailable."""
    warning = (
        "⚠️ 交付校验暂时不可用，因此本回复中的本地路径没有生成下载卡片。"
        "请不要将这些路径视为已交付文件。"
    )
    return text.rstrip() + "\n\n" + warning
