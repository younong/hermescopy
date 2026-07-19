"""Behavior tests for the bundled common-files skill."""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "productivity" / "common-files"
SCRIPT = SKILL_DIR / "scripts" / "common_files.py"
OCR_SKILL = REPO_ROOT / "skills" / "productivity" / "ocr-and-documents" / "SKILL.md"


def _load_module():
    spec = importlib.util.spec_from_file_location("common_files_skill", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def common_files():
    return _load_module()


def _frontmatter(path: Path) -> dict:
    content = path.read_text(encoding="utf-8")
    _, raw, _ = content.split("---", 2)
    return yaml.safe_load(raw)


def _write_docx(path: Path, text: str = "Report body") -> None:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document = (
        f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r>'
        f"<w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)


def _write_xlsx(path: Path) -> None:
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    workbook = (
        f'<workbook xmlns="{spreadsheet_ns}" xmlns:r="{rel_ns}"><sheets>'
        '<sheet name="Data" sheetId="1" r:id="rId1"/>'
        '<sheet name="Hidden" sheetId="2" state="hidden" r:id="rId2"/>'
        "</sheets></workbook>"
    )
    rels = (
        f'<Relationships xmlns="{package_rel_ns}">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/>'
        '<Relationship Id="rId2" Target="worksheets/sheet2.xml" Type="x"/>'
        "</Relationships>"
    )
    visible = (
        f'<worksheet xmlns="{spreadsheet_ns}"><sheetData><row r="1">'
        '<c r="A1" t="inlineStr"><is><t>Name</t></is></c>'
        '<c r="B1"><v>42</v></c></row></sheetData></worksheet>'
    )
    hidden = (
        f'<worksheet xmlns="{spreadsheet_ns}"><sheetData><row r="1">'
        '<c r="A1" t="inlineStr"><is><t>SECRET</t></is></c>'
        "</row></sheetData></worksheet>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", visible)
        archive.writestr("xl/worksheets/sheet2.xml", hidden)


def test_skill_contract_and_routing() -> None:
    frontmatter = _frontmatter(SKILL_DIR / "SKILL.md")
    description = frontmatter["description"]
    assert frontmatter["name"] == "common-files"
    assert len(description) <= 60 and description.endswith(".")
    assert set(frontmatter["platforms"]) == {"linux", "macos", "windows"}
    assert set(frontmatter["metadata"]["hermes"]["related_skills"]) == {
        "ocr-and-documents", "powerpoint",
    }
    content = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    for heading in ("## When to Use", "## Prerequisites", "## How to Run", "## Quick Reference", "## Procedure", "## Pitfalls", "## Verification"):
        assert heading in content
    assert "${HERMES_SKILL_DIR}/scripts/common_files.py" in content
    assert "common-files" in OCR_SKILL.read_text(encoding="utf-8")


def test_parser_has_documented_operations(common_files) -> None:
    parser = common_files.build_parser()
    assert parser.parse_args(["inspect", "x.txt"]).operation == "inspect"
    assert parser.parse_args(["extract", "x.txt"]).operation == "extract"
    assert parser.parse_args(["batch", "in", "--output-dir", "out"]).operation == "batch"


def test_inspect_reports_supported_and_unsupported(common_files, tmp_path: Path, capsys) -> None:
    text = tmp_path / "notes.txt"
    text.write_text("hello", encoding="utf-8")
    unknown = tmp_path / "archive.bin"
    unknown.write_bytes(b"x")
    assert common_files.main(["inspect", str(text), str(unknown)]) == 0
    files = json.loads(capsys.readouterr().out)["files"]
    assert files[0]["kind"] == "text" and files[0]["available"] is True
    assert files[1]["kind"] == "unsupported" and files[1]["available"] is False


def test_text_decoding_normalization_and_overwrite(common_files, tmp_path: Path, capsys) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"\xef\xbb\xbfhello\r\nworld\r")
    assert common_files.main(["extract", str(source)]) == 0
    assert capsys.readouterr().out == "hello\nworld\n"

    utf16 = tmp_path / "utf16.txt"
    utf16.write_bytes("snowman ☃".encode("utf-16"))
    output = tmp_path / "out.txt"
    output.write_text("keep", encoding="utf-8")
    assert common_files.main(["extract", str(utf16), "--output", str(output)]) == 4
    assert output.read_text(encoding="utf-8") == "keep"
    capsys.readouterr()
    assert common_files.main(["extract", str(utf16), "--output", str(output), "--force"]) == 0
    assert output.read_text(encoding="utf-8") == "snowman ☃\n"


def test_invalid_utf8_warns_and_json_contract(common_files, tmp_path: Path, capsys) -> None:
    source = tmp_path / "bad.txt"
    source.write_bytes(b"a\xffb")
    assert common_files.main(["extract", str(source), "--format", "json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["backend"] == "stdlib-text"
    assert "replaced" in payload["warnings"][0]
    assert "warning:" in captured.err


def test_csv_preserves_cells_and_warns_on_uneven_rows(common_files, tmp_path: Path) -> None:
    source = tmp_path / "data.csv"
    source.write_text('name,note,tail\nAlice,"x,y",\nBob,"two\nlines"\n', encoding="utf-8")
    text = common_files.extract_path(source)
    assert "Alice\tx,y\t" in text.content
    assert "two\nlines" in text.content
    assert any("inconsistent column counts" in warning for warning in text.warnings)
    markdown = common_files.extract_path(source, output_format="markdown")
    assert "| name | note | tail |" in markdown.content
    assert "two<br>lines" in markdown.content


def test_html_preserves_structure_and_ignores_active_content(common_files, tmp_path: Path) -> None:
    source = tmp_path / "page.html"
    source.write_text(
        "<h1>A &amp; B</h1><script>steal()</script><style>.x{}</style>"
        "<p>Hello <a href='https://example.test'>site</a></p>"
        "<ul><li>One</li><li>Two</li></ul><table><tr><td>X</td><td>Y</td></tr></table>",
        encoding="utf-8",
    )
    result = common_files.extract_path(source, output_format="markdown")
    assert "# A & B" in result.content
    assert "Hello site (https://example.test)" in result.content
    assert "- One" in result.content and "X | Y" in result.content
    assert "steal" not in result.content and ".x" not in result.content


def test_docx_and_xlsx_reuse_hermes_extractor(common_files, tmp_path: Path) -> None:
    docx = tmp_path / "report.docx"
    _write_docx(docx)
    assert "Report body" in common_files.extract_path(docx).content

    xlsx = tmp_path / "book.xlsx"
    _write_xlsx(xlsx)
    content = common_files.extract_path(xlsx).content
    assert "Name\t42" in content
    assert "SECRET" not in content


def test_corrupt_and_suspicious_office_archives_are_rejected(common_files, tmp_path: Path, monkeypatch) -> None:
    corrupt = tmp_path / "bad.docx"
    corrupt.write_bytes(b"not a zip")
    with pytest.raises(common_files.CommonFilesError, match="invalid Office archive"):
        common_files.extract_path(corrupt)

    suspicious = tmp_path / "large.docx"
    _write_docx(suspicious)
    monkeypatch.setattr(common_files, "MAX_ZIP_MEMBER_BYTES", 1)
    with pytest.raises(common_files.UnsafeDocumentError, match="too large"):
        common_files.extract_path(suspicious)


def test_pdf_auto_never_falls_through_to_marker(common_files, tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(common_files.importlib.util, "find_spec", lambda name: None)
    run = Mock()
    monkeypatch.setattr(common_files.subprocess, "run", run)
    with pytest.raises(common_files.BackendUnavailableError, match="pymupdf"):
        common_files.extract_path(pdf)
    run.assert_not_called()


def test_pdf_delegation_uses_safe_subprocess(common_files, tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "report with spaces.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(common_files.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(common_files, "_ocr_scripts_dir", lambda: SKILL_DIR.parent / "ocr-and-documents" / "scripts")
    completed = Mock(returncode=0, stdout="PDF body\n", stderr="")
    run = Mock(return_value=completed)
    monkeypatch.setattr(common_files.subprocess, "run", run)
    result = common_files.extract_path(pdf)
    assert result.content == "PDF body\n"
    args, kwargs = run.call_args
    assert args[0][0] == sys.executable
    assert args[0][-1] == str(pdf.resolve())
    assert kwargs["shell"] is False and kwargs["timeout"] > 0


def test_explicit_marker_and_empty_pdf_warning(common_files, tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(common_files.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(common_files, "_ocr_scripts_dir", lambda: SKILL_DIR.parent / "ocr-and-documents" / "scripts")
    run = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(common_files.subprocess, "run", run)
    empty = common_files.extract_path(pdf)
    assert "OCR" in empty.warnings[0]
    common_files.extract_path(pdf, pdf_backend="marker")
    assert run.call_args.args[0][1].endswith("extract_marker.py")


def test_legacy_office_conversion(common_files, tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "old file.doc"
    source.write_bytes(b"legacy")
    converter = tmp_path / "Libre Office"
    converter.write_text("binary", encoding="utf-8")

    def fake_run(command, **kwargs):
        output_dir = Path(command[command.index("--outdir") + 1])
        _write_docx(output_dir / "old file.docx", "Converted")
        return Mock(returncode=0, stdout="", stderr="")

    run = Mock(side_effect=fake_run)
    monkeypatch.setattr(common_files.subprocess, "run", run)
    result = common_files.extract_path(source, office_converter=str(converter))
    assert "Converted" in result.content
    command = run.call_args.args[0]
    assert command[0] == str(converter) and command[-1] == str(source.resolve())
    assert run.call_args.kwargs["shell"] is False


def test_missing_legacy_converter_is_actionable(common_files, tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "old.xls"
    source.write_bytes(b"legacy")
    monkeypatch.setattr(common_files.shutil, "which", lambda name: None)
    with pytest.raises(common_files.BackendUnavailableError, match="LibreOffice"):
        common_files.extract_path(source)


def test_batch_is_deterministic_and_reports_skips(common_files, tmp_path: Path, capsys) -> None:
    inputs = tmp_path / "inputs"
    (inputs / "nested").mkdir(parents=True)
    (inputs / "z.txt").write_text("z", encoding="utf-8")
    (inputs / "nested" / "a.html").write_text("<p>a</p>", encoding="utf-8")
    (inputs / "skip.bin").write_bytes(b"x")
    output = tmp_path / "output"
    code = common_files.main(["batch", str(inputs), "--recursive", "--output-dir", str(output)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert [Path(item["source"]).name for item in payload["successes"]] == ["a.html", "z.txt"]
    assert payload["skipped"][0]["reason"] == "unsupported format"
    assert (output / "nested" / "a.txt").read_text(encoding="utf-8") == "a\n"


def test_batch_preflights_overwrites_and_limits(common_files, tmp_path: Path, capsys) -> None:
    source = tmp_path / "one.txt"
    source.write_text("one", encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    (output / "one.txt").write_text("keep", encoding="utf-8")
    assert common_files.main(["batch", str(source), "--output-dir", str(output)]) == 4
    assert (output / "one.txt").read_text(encoding="utf-8") == "keep"
    capsys.readouterr()
    assert common_files.main(["batch", str(source), "--output-dir", str(output), "--max-files", "0"]) == 4


def test_batch_partial_failure_returns_five(common_files, tmp_path: Path, monkeypatch, capsys) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    original = common_files.extract_path

    def fail_second(path, *args, **kwargs):
        if Path(path).name == "b.txt":
            raise common_files.CommonFilesError("broken")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(common_files, "extract_path", fail_second)
    code = common_files.main(["batch", str(first), str(second), "--output-dir", str(tmp_path / "out")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 5 and len(payload["successes"]) == 1 and len(payload["failures"]) == 1
