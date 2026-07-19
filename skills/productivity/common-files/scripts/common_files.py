#!/usr/bin/env python3
"""Inspect and extract common local document formats."""

from __future__ import annotations

import argparse
import base64
import csv
import importlib.util
import io
import json
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_ZIP_MEMBERS = 10_000
MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_CHARS = 2_000_000
DEFAULT_MAX_FILES = 1_000
CONVERSION_TIMEOUT_SECONDS = 300

STRUCTURED_EXTENSIONS = {".docx", ".xlsx", ".ipynb"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".xml",
    ".yaml", ".yml", ".toml", ".ini", ".cfg",
}
TABLE_EXTENSIONS = {".csv", ".tsv"}
HTML_EXTENSIONS = {".html", ".htm"}
LEGACY_OFFICE_EXTENSIONS = {".doc", ".xls", ".odt", ".ods"}
IWORK_EXTENSIONS = {".numbers", ".pages", ".key", ".keynote"}
RICH_TEXT_EXTENSIONS = {".rtf", ".rtfd"}
MAC_DATA_EXTENSIONS = {".plist", ".webarchive"}
PACKAGE_EXTENSIONS = IWORK_EXTENSIONS | {".rtfd"}
SUPPORTED_EXTENSIONS = (
    STRUCTURED_EXTENSIONS | TEXT_EXTENSIONS | TABLE_EXTENSIONS |
    HTML_EXTENSIONS | LEGACY_OFFICE_EXTENSIONS | IWORK_EXTENSIONS |
    RICH_TEXT_EXTENSIONS | MAC_DATA_EXTENSIONS | {".pdf"}
)

IWORK_TARGETS = {
    ".numbers": ("Numbers", ".xlsx", "Microsoft Excel"),
    ".pages": ("Pages", ".docx", "Microsoft Word"),
    ".key": ("Keynote", ".pdf", "PDF"),
    ".keynote": ("Keynote", ".pdf", "PDF"),
}


class CommonFilesError(Exception):
    exit_code = 4


class UnsupportedFormatError(CommonFilesError):
    exit_code = 2


class BackendUnavailableError(CommonFilesError):
    exit_code = 3


class UnsafeDocumentError(CommonFilesError):
    pass


@dataclass
class ExtractionResult:
    content: str
    kind: str
    backend: str
    warnings: list[str] = field(default_factory=list)


class DocumentHTMLParser(HTMLParser):
    """Render useful HTML structure without executing or fetching anything."""

    _ignored = {"script", "style", "template", "noscript"}
    _blocks = {"p", "div", "section", "article", "header", "footer", "blockquote"}
    _headings = {f"h{i}" for i in range(1, 7)}

    def __init__(self, markdown: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.markdown = markdown
        self.parts: list[str] = []
        self.ignored_depth = 0
        self.list_depth = 0
        self.href_stack: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._ignored:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag in self._blocks or tag in {"table", "tr"}:
            self._break(2 if tag in self._blocks else 1)
        elif tag == "br":
            self._break(1)
        elif tag in {"ul", "ol"}:
            self.list_depth += 1
            self._break(1)
        elif tag == "li":
            self._break(1)
            self.parts.append("  " * max(self.list_depth - 1, 0) + ("- " if self.markdown else "• "))
        elif tag in {"td", "th"}:
            if self.parts and not self.parts[-1].endswith(("\n", "\t", "| ")):
                self.parts.append(" | " if self.markdown else "\t")
        elif tag in self._headings:
            self._break(2)
            if self.markdown:
                self.parts.append("#" * int(tag[1]) + " ")
        elif tag == "a":
            self.href_stack.append(dict(attrs).get("href"))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._ignored:
            self.ignored_depth = max(0, self.ignored_depth - 1)
            return
        if self.ignored_depth:
            return
        if tag == "a":
            href = self.href_stack.pop() if self.href_stack else None
            if self.markdown and href:
                self.parts.append(f" ({href})")
        elif tag in {"ul", "ol"}:
            self.list_depth = max(0, self.list_depth - 1)
            self._break(1)
        elif tag in self._blocks or tag in {"table", "tr", "li"} or tag in self._headings:
            self._break(2 if tag in self._blocks or tag in self._headings else 1)

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        text = " ".join(data.split())
        if text:
            if self.parts and self.parts[-1] and not self.parts[-1].endswith((" ", "\n", "\t", "- ", "• ")):
                self.parts.append(" ")
            self.parts.append(text)

    def _break(self, count: int) -> None:
        self.parts.append("\n" * count)

    def render(self) -> str:
        lines = [line.rstrip() for line in "".join(self.parts).splitlines()]
        output: list[str] = []
        blank = False
        for line in lines:
            if line.strip():
                output.append(line.strip() if not line.startswith(("  ", "- ", "• ")) else line)
                blank = False
            elif output and not blank:
                output.append("")
                blank = True
        return "\n".join(output).strip() + "\n"


def classify(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".docx":
        return "word"
    if ext == ".xlsx":
        return "spreadsheet"
    if ext == ".ipynb":
        return "notebook"
    if ext in TABLE_EXTENSIONS:
        return "table"
    if ext in HTML_EXTENSIONS:
        return "html"
    if ext in TEXT_EXTENSIONS:
        return "text"
    if ext == ".pdf":
        return "pdf"
    if ext in LEGACY_OFFICE_EXTENSIONS:
        return "legacy-office"
    if ext == ".numbers":
        return "numbers"
    if ext == ".pages":
        return "pages"
    if ext in {".key", ".keynote"}:
        return "keynote"
    if ext in RICH_TEXT_EXTENSIONS:
        return "rich-text"
    if ext == ".plist":
        return "property-list"
    if ext == ".webarchive":
        return "webarchive"
    raise UnsupportedFormatError(f"unsupported format: {ext or '(no extension)'}")


def _package_entries(path: Path) -> tuple[list[Path], int]:
    entries: list[Path] = []
    total = 0

    def visit(directory: Path) -> None:
        nonlocal total
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise CommonFilesError(f"cannot read package {path}: {exc}") from exc
        for child in children:
            if child.is_symlink():
                raise UnsafeDocumentError(f"package contains a symbolic link: {child.relative_to(path)}")
            if child.is_dir():
                visit(child)
                continue
            if not child.is_file():
                raise UnsafeDocumentError(f"package contains a special entry: {child.relative_to(path)}")
            try:
                size = child.stat().st_size
            except OSError as exc:
                raise CommonFilesError(f"cannot read package member {child}: {exc}") from exc
            if size > MAX_ZIP_MEMBER_BYTES:
                raise UnsafeDocumentError(f"package member is too large: {child.relative_to(path)}")
            entries.append(child)
            if len(entries) > MAX_ZIP_MEMBERS:
                raise UnsafeDocumentError(f"package has too many members: {len(entries)}")
            total += size
            if total > MAX_ZIP_TOTAL_BYTES:
                raise UnsafeDocumentError("package expands beyond the safety limit")

    visit(path)
    return entries, total


def _is_supported_source(path: Path) -> bool:
    return path.is_file() or (path.is_dir() and path.suffix.lower() in PACKAGE_EXTENSIONS)


def _require_source(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_dir() and path.suffix.lower() in PACKAGE_EXTENSIONS:
        _package_entries(path)
        return path
    if not path.is_file():
        raise CommonFilesError(f"file or package not found: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CommonFilesError(f"cannot read {path}: {exc}") from exc
    if size > MAX_FILE_BYTES:
        raise UnsafeDocumentError(f"file exceeds {MAX_FILE_BYTES} byte limit: {path}")
    return path


def _check_archive(path: Path, label: str) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                raise UnsafeDocumentError(f"archive has too many members: {len(infos)}")
            total = 0
            for info in infos:
                if info.file_size > MAX_ZIP_MEMBER_BYTES:
                    raise UnsafeDocumentError(f"archive member is too large: {info.filename}")
                total += info.file_size
                if total > MAX_ZIP_TOTAL_BYTES:
                    raise UnsafeDocumentError("archive expands beyond the safety limit")
    except zipfile.BadZipFile as exc:
        raise CommonFilesError(f"invalid {label} archive: {path}") from exc


def _check_structured_archive(path: Path) -> None:
    ext = path.suffix.lower()
    if ext in {".docx", ".xlsx"}:
        _check_archive(path, "Office")
    elif ext in IWORK_EXTENSIONS and path.is_file():
        _check_archive(path, "iWork")


def _decode_bytes(data: bytes, encoding: str | None = None) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if encoding:
        try:
            return data.decode(encoding), warnings
        except (LookupError, UnicodeDecodeError) as exc:
            raise CommonFilesError(f"cannot decode content as {encoding}: {exc}") from exc
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), warnings
    try:
        return data.decode("utf-8-sig"), warnings
    except UnicodeDecodeError:
        warnings.append("invalid UTF-8 bytes were replaced")
        return data.decode("utf-8", errors="replace"), warnings


def _decode_text(path: Path, encoding: str | None) -> tuple[str, list[str]]:
    try:
        return _decode_bytes(path.read_bytes(), encoding)
    except OSError as exc:
        raise CommonFilesError(f"cannot read {path}: {exc}") from exc


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def _extract_table(path: Path, output_format: str, encoding: str | None) -> ExtractionResult:
    text, warnings = _decode_text(path, encoding)
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error as exc:
        raise CommonFilesError(f"cannot parse {path}: {exc}") from exc
    widths = {len(row) for row in rows}
    if len(widths) > 1:
        warnings.append("rows have inconsistent column counts")
    if output_format == "markdown":
        width = max(widths, default=0)
        if not width:
            content = "\n"
        else:
            normalized = [row + [""] * (width - len(row)) for row in rows]
            header = normalized[0] if normalized else [f"Column {i + 1}" for i in range(width)]
            body = normalized[1:] if normalized else []
            esc = lambda value: value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")
            lines = ["| " + " | ".join(map(esc, header)) + " |", "| " + " | ".join(["---"] * width) + " |"]
            lines.extend("| " + " | ".join(map(esc, row)) + " |" for row in body)
            content = "\n".join(lines) + "\n"
    else:
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerows(rows)
        content = output.getvalue()
    return ExtractionResult(content, "table", "stdlib-csv", warnings)


def _render_html(text: str, output_format: str, label: str) -> str:
    parser = DocumentHTMLParser(markdown=output_format == "markdown")
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise CommonFilesError(f"cannot parse HTML {label}: {exc}") from exc
    return parser.render()


def _extract_html(path: Path, output_format: str, encoding: str | None) -> ExtractionResult:
    text, warnings = _decode_text(path, encoding)
    return ExtractionResult(_render_html(text, output_format, str(path)), "html", "stdlib-html-parser", warnings)


def _extract_structured(path: Path) -> ExtractionResult:
    _check_structured_archive(path)
    try:
        from tools.read_extract import ExtractionError, extract_document_text
    except ImportError as exc:
        raise BackendUnavailableError("Hermes tools.read_extract is unavailable") from exc
    try:
        content = extract_document_text(str(path))
    except ExtractionError as exc:
        raise CommonFilesError(str(exc)) from exc
    return ExtractionResult(content, classify(path), "hermes-read-extract")


def _json_safe_plist(value):
    if isinstance(value, dict):
        return {str(key): _json_safe_plist(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_plist(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return {"$type": "data", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return {"$type": "date", "iso8601": value.isoformat()}
    if isinstance(value, plistlib.UID):
        return {"$type": "uid", "value": value.data}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise CommonFilesError(f"unsupported property-list value: {type(value).__name__}")


def _load_plist(path: Path):
    try:
        with path.open("rb") as stream:
            return plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError) as exc:
        raise CommonFilesError(f"cannot parse property list {path}: {exc}") from exc


def _extract_plist(path: Path) -> ExtractionResult:
    content = json.dumps(_json_safe_plist(_load_plist(path)), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return ExtractionResult(content, "property-list", "python-stdlib-plist")


def _extract_webarchive(path: Path, output_format: str, encoding: str | None) -> ExtractionResult:
    archive = _load_plist(path)
    if not isinstance(archive, dict) or not isinstance(archive.get("WebMainResource"), dict):
        raise CommonFilesError("webarchive has no main resource")
    resource = archive["WebMainResource"]
    data = resource.get("WebResourceData")
    if not isinstance(data, bytes):
        raise CommonFilesError("webarchive main resource has no readable data")
    mime = str(resource.get("WebResourceMIMEType", "")).lower()
    declared = resource.get("WebResourceTextEncodingName")
    selected_encoding = encoding or (declared if isinstance(declared, str) and declared else None)
    text, warnings = _decode_bytes(data, selected_encoding)
    if archive.get("WebSubresources"):
        warnings.append("webarchive subresources were omitted")
    if archive.get("WebSubframeArchives"):
        warnings.append("webarchive subframes were omitted")
    if mime in {"text/html", "application/xhtml+xml", "application/xml", "text/xml"} or "html" in mime:
        content = _render_html(text, output_format, str(path))
    elif mime.startswith("text/"):
        content = _normalize_text(text)
    else:
        raise CommonFilesError(f"unsupported webarchive main resource type: {mime or '(missing)'}")
    return ExtractionResult(content, "webarchive", "python-stdlib-webarchive", warnings)


def _ocr_scripts_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "ocr-and-documents" / "scripts"


def _extract_pdf(path: Path, backend: str) -> ExtractionResult:
    selected = "pymupdf" if backend == "auto" else backend
    module = "pymupdf" if selected == "pymupdf" else "marker"
    if importlib.util.find_spec(module) is None:
        hint = "requires preinstalled pymupdf; load the ocr-and-documents skill for guidance"
        if selected == "marker":
            hint = "requires preinstalled marker-pdf and its multi-gigabyte model assets"
        raise BackendUnavailableError(f"{selected} backend unavailable; {hint}")
    script = _ocr_scripts_dir() / ("extract_pymupdf.py" if selected == "pymupdf" else "extract_marker.py")
    if not script.is_file():
        raise BackendUnavailableError(f"PDF helper script not found: {script}")
    try:
        result = subprocess.run(
            [sys.executable, str(script), str(path)],
            capture_output=True,
            text=True,
            timeout=CONVERSION_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommonFilesError(f"PDF extraction failed: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise CommonFilesError(f"PDF extraction failed: {detail}")
    warnings = []
    if selected == "pymupdf" and not result.stdout.strip():
        warnings.append("no text found; retry with --pdf-backend marker for scanned/OCR content")
    content = _normalize_text(result.stdout) if result.stdout else ""
    return ExtractionResult(content, "pdf", selected, warnings)


def _office_converter(option: str) -> str:
    if option == "none":
        raise BackendUnavailableError("LibreOffice conversion is disabled")
    if option != "auto":
        candidate = Path(option).expanduser()
        if not candidate.is_file():
            raise BackendUnavailableError(f"Office converter not found: {candidate}")
        return str(candidate)
    converter = shutil.which("soffice") or shutil.which("libreoffice")
    if not converter:
        raise BackendUnavailableError("LibreOffice is required for this conversion")
    return converter


def _run_libreoffice(path: Path, target_ext: str, converter_option: str, pdf_backend: str = "auto") -> ExtractionResult:
    converter = _office_converter(converter_option)
    with tempfile.TemporaryDirectory(prefix="hermes-common-files-") as temp_dir:
        try:
            result = subprocess.run(
                [converter, "--headless", "--convert-to", target_ext.lstrip("."), "--outdir", temp_dir, str(path)],
                capture_output=True,
                text=True,
                timeout=CONVERSION_TIMEOUT_SECONDS,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CommonFilesError(f"LibreOffice conversion failed: {exc}") from exc
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise CommonFilesError(f"LibreOffice conversion failed: {detail}")
        converted = sorted(Path(temp_dir).glob(f"*{target_ext}"))
        if len(converted) != 1:
            raise CommonFilesError("LibreOffice did not produce one expected converted file")
        extracted = _extract_pdf(converted[0], pdf_backend) if target_ext == ".pdf" else _extract_structured(converted[0])
        extracted.backend = f"libreoffice+{extracted.backend}"
        return extracted


def _extract_legacy(path: Path, converter_option: str) -> ExtractionResult:
    target_ext = ".docx" if path.suffix.lower() in {".doc", ".odt"} else ".xlsx"
    extracted = _run_libreoffice(path, target_ext, converter_option)
    extracted.kind = "legacy-office"
    return extracted


def _textutil_path() -> str | None:
    candidate = Path("/usr/bin/textutil")
    return str(candidate) if platform.system() == "Darwin" and candidate.is_file() else None


def _rich_text_source(path: Path) -> tuple[Path, list[str]]:
    if path.suffix.lower() != ".rtfd":
        return path, []
    entries, _ = _package_entries(path)
    payload = path / "TXT.rtf"
    if payload not in entries:
        raise CommonFilesError("RTFD package has no TXT.rtf payload")
    warnings = ["RTFD attachments and images were omitted"] if len(entries) > 1 else []
    return payload, warnings


def _extract_rich_text(path: Path, backend: str, converter_option: str) -> ExtractionResult:
    source, warnings = _rich_text_source(path)
    selected = backend
    if selected == "auto":
        selected = "textutil" if _textutil_path() else "libreoffice"
    if selected == "none":
        raise BackendUnavailableError("rich-text conversion is disabled")
    if selected == "libreoffice":
        extracted = _run_libreoffice(source, ".docx", converter_option)
        extracted.kind = "rich-text"
        extracted.warnings.extend(warnings)
        return extracted
    executable = _textutil_path()
    if not executable:
        raise BackendUnavailableError("macOS /usr/bin/textutil is unavailable")
    try:
        result = subprocess.run(
            [executable, "-convert", "txt", "-stdout", "-encoding", "UTF-8", "--", str(source)],
            capture_output=True,
            text=True,
            timeout=CONVERSION_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommonFilesError(f"textutil conversion failed: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise CommonFilesError(f"textutil conversion failed: {detail}")
    return ExtractionResult(_normalize_text(result.stdout), "rich-text", "textutil", warnings)


def _apple_app_path(app: str) -> Path | None:
    candidates = [Path("/Applications") / f"{app}.app", Path("/System/Applications") / f"{app}.app", Path.home() / "Applications" / f"{app}.app"]
    return next((candidate for candidate in candidates if candidate.is_dir()), None)


def _apple_backend_available(app: str) -> bool:
    return platform.system() == "Darwin" and Path("/usr/bin/osascript").is_file() and _apple_app_path(app) is not None


def _apple_export_script(app: str, export_format: str) -> str:
    return f'''on run argv
    set inputPath to item 1 of argv
    set outputPath to item 2 of argv
    tell application "{app}"
        set openedDocument to open POSIX file inputPath
        try
            export openedDocument to POSIX file outputPath as {export_format}
        on error errorMessage number errorNumber
            try
                close openedDocument saving no
            end try
            error errorMessage number errorNumber
        end try
        close openedDocument saving no
    end tell
end run'''


def _extract_iwork_apple(path: Path, pdf_backend: str) -> ExtractionResult:
    app, target_ext, export_format = IWORK_TARGETS[path.suffix.lower()]
    if not _apple_backend_available(app):
        raise BackendUnavailableError(f"Apple {app} export requires macOS, /usr/bin/osascript, and {app}.app")
    if target_ext == ".pdf":
        selected = "pymupdf" if pdf_backend == "auto" else pdf_backend
        module = "pymupdf" if selected == "pymupdf" else "marker"
        if importlib.util.find_spec(module) is None:
            raise BackendUnavailableError(f"{selected} PDF backend is unavailable for Keynote export")
    with tempfile.TemporaryDirectory(prefix="hermes-common-files-iwork-") as temp_dir:
        temp = Path(temp_dir)
        output = temp / f"export{target_ext}"
        script = _apple_export_script(app, export_format)
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", script, str(path), str(output)],
                capture_output=True,
                text=True,
                timeout=CONVERSION_TIMEOUT_SECONDS,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommonFilesError("Apple export timed out; the app may be waiting for Automation permission or a GUI session") from exc
        except OSError as exc:
            raise CommonFilesError(f"Apple export failed: {exc}") from exc
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise CommonFilesError(f"Apple {app} export failed: {detail}")
        if not output.is_file():
            raise CommonFilesError(f"Apple {app} did not produce the expected {target_ext} file")
        extracted = _extract_pdf(output, pdf_backend) if target_ext == ".pdf" else _extract_structured(output)
        extracted.kind = classify(path)
        extracted.backend = f"apple-{app.lower()}+{extracted.backend}"
        return extracted


def _numbers_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (str, int, float)):
        return str(value)
    raise CommonFilesError(
        f"unsupported Numbers cell value: {type(value).__name__}"
    )


def _extract_numbers(path: Path) -> ExtractionResult:
    try:
        from numbers_parser import Document
        from numbers_parser.exceptions import NumbersError
    except ImportError as exc:
        raise BackendUnavailableError(
            "numbers-parser backend unavailable; use the packaged documents extra"
        ) from exc
    try:
        document = Document(path)
        output: list[str] = []
        for sheet in document.sheets:
            output.append(f"# ── Sheet: {sheet.name} ──")
            for table in sheet.tables:
                output.append(f"## ── Table: {table.name} ──")
                output.extend(
                    "\t".join(_numbers_value(value) for value in row)
                    for row in table.rows(values_only=True)
                )
                if not table.num_rows:
                    output.append("(empty)")
                output.append("")
    except (NumbersError, OSError, ValueError, TypeError, IndexError) as exc:
        raise CommonFilesError(f"cannot parse Numbers document {path}: {exc}") from exc
    if not output:
        raise CommonFilesError("Numbers document has no extractable sheets")
    return ExtractionResult(
        "\n".join(output).rstrip("\n") + "\n",
        "numbers",
        "numbers-parser",
        ["formulas use stored computed values and were not recalculated"],
    )


def _extract_iwork(path: Path, backend: str, converter_option: str, pdf_backend: str) -> ExtractionResult:
    _check_structured_archive(path)
    ext = path.suffix.lower()
    selected = "numbers-parser" if backend == "auto" and ext == ".numbers" else backend
    if selected == "numbers-parser":
        if ext != ".numbers":
            raise BackendUnavailableError(
                "numbers-parser supports only .numbers; choose LibreOffice or Apple for this iWork format"
            )
        return _extract_numbers(path)
    if selected in {"auto", "none"}:
        raise BackendUnavailableError(
            "iWork extraction requires the packaged Numbers parser or an explicit --iwork-backend libreoffice or apple"
        )
    app, target_ext, _ = IWORK_TARGETS[ext]
    if selected == "apple":
        return _extract_iwork_apple(path, pdf_backend)
    if path.is_dir():
        raise BackendUnavailableError("LibreOffice cannot convert an iWork package directory; use --iwork-backend apple on macOS")
    extracted = _run_libreoffice(path, target_ext, converter_option, pdf_backend)
    extracted.kind = classify(path)
    extracted.warnings.append(f"{app} conversion through LibreOffice is best-effort and may omit layout or features")
    return extracted


def extract_path(
    source: Path,
    output_format: str = "text",
    encoding: str | None = None,
    pdf_backend: str = "auto",
    office_converter: str = "auto",
    max_chars: int = DEFAULT_MAX_CHARS,
    iwork_backend: str = "auto",
    rich_text_backend: str = "auto",
) -> ExtractionResult:
    path = _require_source(source)
    ext = path.suffix.lower()
    classify(path)
    if ext in STRUCTURED_EXTENSIONS:
        result = _extract_structured(path)
    elif ext in TABLE_EXTENSIONS:
        result = _extract_table(path, output_format, encoding)
    elif ext in HTML_EXTENSIONS:
        result = _extract_html(path, output_format, encoding)
    elif ext in TEXT_EXTENSIONS:
        text, warnings = _decode_text(path, encoding)
        result = ExtractionResult(_normalize_text(text), "text", "stdlib-text", warnings)
    elif ext == ".pdf":
        result = _extract_pdf(path, pdf_backend)
    elif ext in LEGACY_OFFICE_EXTENSIONS:
        result = _extract_legacy(path, office_converter)
    elif ext in IWORK_EXTENSIONS:
        result = _extract_iwork(path, iwork_backend, office_converter, pdf_backend)
    elif ext in RICH_TEXT_EXTENSIONS:
        result = _extract_rich_text(path, rich_text_backend, office_converter)
    elif ext == ".plist":
        result = _extract_plist(path)
    else:
        result = _extract_webarchive(path, output_format, encoding)
    if len(result.content) > max_chars:
        result.content = result.content[:max_chars].rstrip("\n") + "\n"
        result.warnings.append(f"output truncated to {max_chars} characters")
    return result


def _source_size(path: Path) -> int | None:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir() and path.suffix.lower() in PACKAGE_EXTENSIONS:
        return _package_entries(path)[1]
    return None


def inspect_path(source: Path) -> dict[str, object]:
    path = source.expanduser().resolve()
    exists = _is_supported_source(path)
    item: dict[str, object] = {
        "path": str(path),
        "extension": path.suffix.lower(),
        "exists": exists,
        "source_type": "package" if path.is_dir() and path.suffix.lower() in PACKAGE_EXTENSIONS else "file",
    }
    try:
        item["kind"] = classify(path)
    except UnsupportedFormatError as exc:
        item.update({"kind": "unsupported", "available": False, "error": str(exc), "operations": []})
        return item
    try:
        item["size_bytes"] = _source_size(path) if exists else None
    except CommonFilesError as exc:
        item.update({"available": False, "error": str(exc), "operations": []})
        return item
    item["operations"] = ["extract"]
    item["available"] = exists
    kind = item["kind"]
    if kind in {"word", "spreadsheet", "notebook"}:
        item["backend"] = "hermes-read-extract"
    elif kind == "pdf":
        available = importlib.util.find_spec("pymupdf") is not None
        item.update({"backend": "pymupdf", "available": exists and available, "requires": [] if available else ["pymupdf"]})
    elif kind == "legacy-office":
        converter = shutil.which("soffice") or shutil.which("libreoffice")
        item.update({"backend": "libreoffice", "available": exists and bool(converter), "requires": [] if converter else ["libreoffice"]})
    elif kind in {"numbers", "pages", "keynote"}:
        app = IWORK_TARGETS[path.suffix.lower()][0]
        libreoffice = bool(shutil.which("soffice") or shutil.which("libreoffice")) and path.is_file()
        apple = _apple_backend_available(app)
        native = kind == "numbers" and importlib.util.find_spec("numbers_parser") is not None
        backends = {
            "libreoffice": {"available": libreoffice, "best_effort": True},
            "apple": {"available": apple, "may_launch_app": True, "automation_permission": "unknown"},
        }
        if kind == "numbers":
            backends = {"numbers-parser": {"available": native}, **backends}
        item.update({
            "backend": "numbers-parser" if native else "none",
            "available": exists and native,
            "backends": backends,
            "requires": [] if native else [
                "packaged numbers-parser" if kind == "numbers" else
                "choose --iwork-backend libreoffice or apple"
            ],
        })
    elif kind == "rich-text":
        textutil = _textutil_path()
        libreoffice = shutil.which("soffice") or shutil.which("libreoffice")
        backend = "textutil" if textutil else "libreoffice" if libreoffice else "none"
        item.update({"backend": backend, "available": exists and backend != "none", "requires": [] if backend != "none" else ["macOS textutil or LibreOffice"]})
    elif kind == "property-list":
        item["backend"] = "python-stdlib-plist"
    elif kind == "webarchive":
        item["backend"] = "python-stdlib-webarchive"
    else:
        item["backend"] = "python-stdlib"
    return item


def _write_output(path: Path, content: str, force: bool) -> None:
    if path.exists() and not force:
        raise CommonFilesError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _result_payload(path: Path, result: ExtractionResult) -> dict:
    return {
        "ok": True,
        "path": str(path.resolve()),
        "kind": result.kind,
        "backend": result.backend,
        "content": result.content,
        "chars": len(result.content),
        "warnings": result.warnings,
    }


def _directory_candidates(root: Path, recursive: bool) -> tuple[list[tuple[Path, Path]], list[dict]]:
    candidates: list[tuple[Path, Path]] = []
    skipped: list[dict] = []

    def visit(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: str(item)):
            relative = child.relative_to(root)
            if child.is_symlink():
                skipped.append({"path": str(child), "reason": "symbolic links are not followed"})
            elif child.is_dir() and child.suffix.lower() in PACKAGE_EXTENSIONS:
                candidates.append((child, relative))
            elif child.is_dir():
                if recursive:
                    visit(child)
            elif child.is_file():
                if child.suffix.lower() in SUPPORTED_EXTENSIONS:
                    candidates.append((child, relative))
                else:
                    skipped.append({"path": str(child), "reason": "unsupported format"})

    visit(root)
    return candidates, skipped


def _discover(inputs: list[str], recursive: bool, max_files: int) -> tuple[list[tuple[Path, Path]], list[dict]]:
    found: list[tuple[Path, Path]] = []
    skipped: list[dict] = []
    for raw in inputs:
        source = Path(raw).expanduser().resolve()
        if _is_supported_source(source):
            candidates = [(source, Path(source.name))]
        elif source.is_dir():
            candidates, directory_skips = _directory_candidates(source, recursive)
            skipped.extend(directory_skips)
        else:
            raise CommonFilesError(f"input not found: {source}")
        found.extend(candidates)
        if len(found) > max_files:
            raise UnsafeDocumentError(f"batch exceeds --max-files {max_files}")
    found.sort(key=lambda item: str(item[0]))
    skipped.sort(key=lambda item: item["path"])
    return found, skipped


def _batch(args: argparse.Namespace) -> int:
    files, skipped = _discover(args.inputs, args.recursive, args.max_files)
    output_dir = Path(args.output_dir).expanduser().resolve()
    suffix = ".md" if args.format == "markdown" else ".txt"
    planned: list[tuple[Path, Path]] = [(source, output_dir / relative.with_suffix(suffix)) for source, relative in files]
    destinations = [destination for _, destination in planned]
    if len(set(destinations)) != len(destinations):
        raise CommonFilesError("batch output paths collide; use distinct input roots")
    if not args.force:
        existing = [str(path) for path in destinations if path.exists()]
        if existing:
            raise CommonFilesError(f"refusing to overwrite existing output: {existing[0]}")
    successes: list[dict] = []
    failures: list[dict] = []
    for source, destination in planned:
        try:
            result = extract_path(
                source, args.format, args.encoding, args.pdf_backend, args.office_converter,
                args.max_chars, args.iwork_backend, args.rich_text_backend,
            )
            _write_output(destination, result.content, args.force)
            successes.append({"source": str(source), "output": str(destination), "backend": result.backend, "warnings": result.warnings})
        except CommonFilesError as exc:
            failures.append({"source": str(source), "error": str(exc)})
            if args.fail_fast:
                break
    print(json.dumps({"ok": not failures, "successes": successes, "skipped": skipped, "failures": failures}, indent=2, ensure_ascii=False))
    return 5 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    inspect_parser = sub.add_parser("inspect", help="classify files and report available backends")
    inspect_parser.add_argument("paths", nargs="+")

    def add_extract_options(target: argparse.ArgumentParser) -> None:
        target.add_argument("--format", choices=("text", "markdown", "json"), default="text")
        target.add_argument("--encoding")
        target.add_argument("--pdf-backend", choices=("auto", "pymupdf", "marker"), default="auto")
        target.add_argument("--office-converter", default="auto", help="auto, none, or a LibreOffice executable path")
        target.add_argument(
            "--iwork-backend",
            choices=("auto", "numbers-parser", "none", "libreoffice", "apple"),
            default="auto",
        )
        target.add_argument("--rich-text-backend", choices=("auto", "textutil", "libreoffice", "none"), default="auto")
        target.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)

    extract_parser = sub.add_parser("extract", help="extract one file")
    extract_parser.add_argument("source")
    extract_parser.add_argument("--output", "-o")
    extract_parser.add_argument("--force", action="store_true")
    add_extract_options(extract_parser)

    batch_parser = sub.add_parser("batch", help="extract files into an output directory")
    batch_parser.add_argument("inputs", nargs="+")
    batch_parser.add_argument("--output-dir", required=True)
    batch_parser.add_argument("--recursive", action="store_true")
    batch_parser.add_argument("--force", action="store_true")
    batch_parser.add_argument("--fail-fast", action="store_true")
    batch_parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    add_extract_options(batch_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.operation == "inspect":
            print(json.dumps({"files": [inspect_path(Path(path)) for path in args.paths]}, indent=2, ensure_ascii=False))
            return 0
        if args.operation == "batch":
            return _batch(args)
        source = _require_source(Path(args.source))
        result = extract_path(
            source, args.format, args.encoding, args.pdf_backend, args.office_converter,
            args.max_chars, args.iwork_backend, args.rich_text_backend,
        )
        content = json.dumps(_result_payload(source, result), indent=2, ensure_ascii=False) + "\n" if args.format == "json" else result.content
        if args.output and args.output != "-":
            _write_output(Path(args.output).expanduser().resolve(), content, args.force)
        else:
            sys.stdout.write(content)
        for warning in result.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        return 0
    except CommonFilesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
