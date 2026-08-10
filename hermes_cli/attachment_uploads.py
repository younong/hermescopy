"""Shared validation for authenticated browser byte uploads."""

from __future__ import annotations

import base64
import binascii
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

IMAGE_MAX_BYTES = 25 * 1024 * 1024
PDF_MAX_BYTES = 50 * 1024 * 1024
FILE_MAX_BYTES = 25 * 1024 * 1024
PDF_MAX_PAGES = 25

_IMAGE_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
    (b"BM", ".bmp", "image/bmp"),
)
_BLOCKED_FILE_SUFFIXES = frozenset(
    {".app", ".bat", ".cmd", ".com", ".dll", ".dmg", ".exe", ".msi", ".ps1", ".scr"}
)
_DATA_URL_RE = re.compile(
    r"^data:([^;,]*)(?:;[^;,=]+=[^;,]+)*;base64,(.*)$", re.DOTALL | re.I
)
_PDF_PAGE_RE = re.compile(rb"/Type\s*/Page\b")


@dataclass(frozen=True)
class ValidatedUpload:
    filename: str
    media_type: str
    data: bytes
    kind: str


def decode_base64_upload(raw: str) -> tuple[bytes, str | None]:
    cleaned = str(raw or "").strip()
    match = _DATA_URL_RE.match(cleaned)
    declared = None
    if match:
        declared = match.group(1).lower() or None
        cleaned = match.group(2)
    cleaned = re.sub(r"\s+", "", cleaned)
    try:
        return base64.b64decode(cleaned, validate=True), declared
    except (ValueError, binascii.Error) as exc:
        raise ValueError("attachment content is not valid base64") from exc


def sanitize_upload_name(name: str, *, fallback: str = "attachment") -> str:
    candidate = Path(str(name or "").strip()).name
    candidate = re.sub(r"[\x00-\x1f]+", "_", candidate).strip().strip(".")
    return candidate or fallback


def validate_upload(
    *,
    kind: str,
    filename: str,
    content_base64: str,
    media_type: str | None = None,
) -> ValidatedUpload:
    kind = str(kind or "file").strip().lower()
    if kind not in {"image", "pdf", "file"}:
        raise ValueError("attachment kind is invalid")
    data, declared = decode_base64_upload(content_base64)
    if not data:
        raise ValueError("attachment is empty")
    name = sanitize_upload_name(filename)
    supplied_type = str(media_type or declared or "").strip().lower()

    if kind == "image":
        detected = None
        if data[:16].startswith(b"RIFF") and data[8:12] == b"WEBP":
            detected = (".webp", "image/webp")
        else:
            for magic, suffix, mime in _IMAGE_MAGIC:
                if data.startswith(magic):
                    detected = (suffix, mime)
                    break
        if detected is None:
            raise ValueError("unsupported or invalid image content")
        suffix, detected_type = detected
        if len(data) > IMAGE_MAX_BYTES:
            raise ValueError("image exceeds the 25 MB limit")
        if Path(name).suffix.lower() not in {suffix, ".jpeg" if suffix == ".jpg" else suffix}:
            name = f"{Path(name).stem or 'image'}{suffix}"
        if supplied_type and not supplied_type.startswith("image/"):
            raise ValueError("image media type is invalid")
        return ValidatedUpload(name, detected_type, data, kind)

    if kind == "pdf":
        if len(data) > PDF_MAX_BYTES:
            raise ValueError("PDF exceeds the 50 MB limit")
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
            raise ValueError("payload is not a valid PDF")
        pages = len(_PDF_PAGE_RE.findall(data))
        if pages > PDF_MAX_PAGES:
            raise ValueError(f"PDF exceeds the {PDF_MAX_PAGES}-page limit")
        if Path(name).suffix.lower() != ".pdf":
            name = f"{Path(name).stem or 'attachment'}.pdf"
        if supplied_type and supplied_type != "application/pdf":
            raise ValueError("PDF media type is invalid")
        return ValidatedUpload(name, "application/pdf", data, kind)

    if len(data) > FILE_MAX_BYTES:
        raise ValueError("file exceeds the 25 MB limit")
    if Path(name).suffix.lower() in _BLOCKED_FILE_SUFFIXES:
        raise ValueError("executable attachments are not supported")
    guessed = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if supplied_type and supplied_type != guessed and supplied_type != "application/octet-stream":
        raise ValueError("file media type does not match its filename")
    return ValidatedUpload(name, supplied_type or guessed, data, kind)
