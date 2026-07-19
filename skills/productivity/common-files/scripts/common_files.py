#!/usr/bin/env python3
"""Inspect and extract common local document formats."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_ZIP_MEMBERS = 10_000
MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_CHARS = 2_000_000
DEFAULT_MAX_FILES = 1_000
PDF_TIMEOUT_SECONDS = 300

STRUCTURED_EXTENSIONS = {".docx", ".xlsx", ".ipynb"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".xml",
    ".yaml", ".yml", ".toml", ".ini", ".cfg",
}
TABLE_EXTENSIONS = {".csv", ".tsv"}
HTML_EXTENSIONS = {".html", ".htm"}
LEGACY_OFFICE_EXTENSIONS = {".doc", ".xls", ".odt", ".ods"}
SUPPORTED_EXTENSIONS = (
    STRUCTURED_EXTENSIONS | TEXT_EXTENSIONS | TABLE_EXTENSIONS |
    HTML_EXTENSIONS | LEGACY_OFFICE_EXTENSIONS | {".pdf"}
)


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
        elif tag == "td" or tag == "th":
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
    raise UnsupportedFormatError(f"unsupported format: {ext or '(no extension)'}")


def _require_source(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise CommonFilesError(f"file not found: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CommonFilesError(f"cannot read {path}: {exc}") from exc
    if size > MAX_FILE_BYTES:
        raise UnsafeDocumentError(f"file exceeds {MAX_FILE_BYTES} byte limit: {path}")
    return path


def _check_ooxml_archive(path: Path) -> None:
    if path.suffix.lower() not in {".docx", ".xlsx"}:
        return
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
        raise CommonFilesError(f"invalid Office archive: {path}") from exc


def _decode_text(path: Path, encoding: str | None) -> tuple[str, list[str]]:
    data = path.read_bytes()
    warnings: list[str] = []
    if encoding:
        try:
            return data.decode(encoding), warnings
        except (LookupError, UnicodeDecodeError) as exc:
            raise CommonFilesError(f"cannot decode {path} as {encoding}: {exc}") from exc
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), warnings
    try:
        return data.decode("utf-8-sig"), warnings
    except UnicodeDecodeError:
        warnings.append("invalid UTF-8 bytes were replaced")
        return data.decode("utf-8", errors="replace"), warnings


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


def _extract_html(path: Path, output_format: str, encoding: str | None) -> ExtractionResult:
    text, warnings = _decode_text(path, encoding)
    parser = DocumentHTMLParser(markdown=output_format == "markdown")
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise CommonFilesError(f"cannot parse HTML {path}: {exc}") from exc
    return ExtractionResult(parser.render(), "html", "stdlib-html-parser", warnings)


def _extract_structured(path: Path) -> ExtractionResult:
    _check_ooxml_archive(path)
    try:
        from tools.read_extract import ExtractionError, extract_document_text
    except ImportError as exc:
        raise BackendUnavailableError("Hermes tools.read_extract is unavailable") from exc
    try:
        content = extract_document_text(str(path))
    except ExtractionError as exc:
        raise CommonFilesError(str(exc)) from exc
    return ExtractionResult(content, classify(path), "hermes-read-extract")


def _ocr_scripts_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "ocr-and-documents" / "scripts"


def _extract_pdf(path: Path, backend: str) -> ExtractionResult:
    selected = "pymupdf" if backend == "auto" else backend
    module = "pymupdf" if selected == "pymupdf" else "marker"
    if importlib.util.find_spec(module) is None:
        hint = "install pymupdf or load the ocr-and-documents skill"
        if selected == "marker":
            hint = "install marker-pdf after checking its multi-gigabyte disk requirement"
        raise BackendUnavailableError(f"{selected} backend unavailable; {hint}")
    script = _ocr_scripts_dir() / ("extract_pymupdf.py" if selected == "pymupdf" else "extract_marker.py")
    if not script.is_file():
        raise BackendUnavailableError(f"PDF helper script not found: {script}")
    try:
        result = subprocess.run(
            [sys.executable, str(script), str(path)],
            capture_output=True,
            text=True,
            timeout=PDF_TIMEOUT_SECONDS,
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
        raise BackendUnavailableError("legacy Office conversion is disabled")
    if option != "auto":
        candidate = Path(option).expanduser()
        if not candidate.is_file():
            raise BackendUnavailableError(f"Office converter not found: {candidate}")
        return str(candidate)
    converter = shutil.which("soffice") or shutil.which("libreoffice")
    if not converter:
        raise BackendUnavailableError("LibreOffice is required for legacy Office files")
    return converter


def _extract_legacy(path: Path, converter_option: str) -> ExtractionResult:
    converter = _office_converter(converter_option)
    target_ext = ".docx" if path.suffix.lower() in {".doc", ".odt"} else ".xlsx"
    target_format = target_ext.lstrip(".")
    with tempfile.TemporaryDirectory(prefix="hermes-common-files-") as temp_dir:
        result = subprocess.run(
            [converter, "--headless", "--convert-to", target_format, "--outdir", temp_dir, str(path)],
            capture_output=True,
            text=True,
            timeout=PDF_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise CommonFilesError(f"LibreOffice conversion failed: {detail}")
        converted = sorted(Path(temp_dir).glob(f"*{target_ext}"))
        if not converted:
            raise CommonFilesError("LibreOffice did not produce the expected converted file")
        extracted = _extract_structured(converted[0])
        extracted.backend = "libreoffice+hermes-read-extract"
        extracted.kind = "legacy-office"
        return extracted


def extract_path(
    source: Path,
    output_format: str = "text",
    encoding: str | None = None,
    pdf_backend: str = "auto",
    office_converter: str = "auto",
    max_chars: int = DEFAULT_MAX_CHARS,
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
    else:
        result = _extract_legacy(path, office_converter)
    if len(result.content) > max_chars:
        result.content = result.content[:max_chars].rstrip("\n") + "\n"
        result.warnings.append(f"output truncated to {max_chars} characters")
    return result


def inspect_path(source: Path) -> dict[str, object]:
    path = source.expanduser().resolve()
    item: dict[str, object] = {
        "path": str(path),
        "extension": path.suffix.lower(),
        "exists": path.is_file(),
    }
    try:
        item["kind"] = classify(path)
    except UnsupportedFormatError as exc:
        item.update({"kind": "unsupported", "available": False, "error": str(exc), "operations": []})
        return item
    item["size_bytes"] = path.stat().st_size if path.is_file() else None
    item["operations"] = ["extract"]
    item["available"] = path.is_file()
    if item["kind"] in {"word", "spreadsheet", "notebook"}:
        item["backend"] = "hermes-read-extract"
    elif item["kind"] == "pdf":
        available = importlib.util.find_spec("pymupdf") is not None
        item.update({"backend": "pymupdf", "available": item["available"] and available, "requires": [] if available else ["pymupdf"]})
    elif item["kind"] == "legacy-office":
        converter = shutil.which("soffice") or shutil.which("libreoffice")
        item.update({"backend": "libreoffice", "available": item["available"] and bool(converter), "requires": [] if converter else ["libreoffice"]})
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


def _discover(inputs: list[str], recursive: bool, max_files: int) -> tuple[list[tuple[Path, Path]], list[dict]]:
    found: list[tuple[Path, Path]] = []
    skipped: list[dict] = []
    for raw in inputs:
        source = Path(raw).expanduser().resolve()
        if source.is_file():
            candidates = [(source, Path(source.name))]
        elif source.is_dir():
            iterator = source.rglob("*") if recursive else source.glob("*")
            candidates = [(p, p.relative_to(source)) for p in iterator if p.is_file()]
        else:
            raise CommonFilesError(f"input not found: {source}")
        for path, relative in sorted(candidates, key=lambda item: str(item[0])):
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                skipped.append({"path": str(path), "reason": "unsupported format"})
                continue
            found.append((path, relative))
            if len(found) > max_files:
                raise UnsafeDocumentError(f"batch exceeds --max-files {max_files}")
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
            result = extract_path(source, args.format, args.encoding, args.pdf_backend, args.office_converter, args.max_chars)
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
        result = extract_path(source, args.format, args.encoding, args.pdf_backend, args.office_converter, args.max_chars)
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
