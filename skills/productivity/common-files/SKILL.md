---
name: common-files
description: Extract and normalize common local document files.
version: 1.2.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    category: productivity
    tags: [documents, docx, xlsx, numbers, pages, rtf, plist, html]
    related_skills: [ocr-and-documents, powerpoint]
---

# Common Files Skill

Use this skill to inspect, extract, and normalize common local files through one repeatable workflow. It handles lightweight formats directly, reuses Hermes's DOCX/XLSX reader, delegates PDF/OCR, and supports common macOS documents without silently launching Apple applications.

## When to Use

Use this skill when:

- a task includes TXT, Markdown, HTML, CSV, TSV, DOCX, XLSX, IPYNB, PDF, RTF, RTFD, plist, webarchive, Numbers, Pages, Keynote, or legacy Office files;
- normalized UTF-8 text or Markdown is needed for downstream analysis;
- several files or macOS document packages need deterministic batch extraction;
- you need to identify available conversion backends before processing a file.

Do not use it to author or visually edit documents. Use `powerpoint` for PPTX, the finance-specific `excel-author` skill for financial models, `ocr-and-documents` for advanced PDF/OCR, and image/vision tooling for HEIC or HEIF.

## Prerequisites

The common path uses Python's standard library and Hermes's installed `tools.read_extract` module. Optional formats require:

- PDF text: PyMuPDF; load `ocr-and-documents` for setup and OCR guidance.
- Scanned PDF OCR: marker-pdf, selected explicitly because its models require several gigabytes.
- `.numbers`: the packaged `numbers-parser` backend works directly on Linux, macOS, and Windows; LibreOffice and Apple export remain explicit alternatives.
- `.doc`, `.xls`, `.odt`, `.ods`, and best-effort Pages/Keynote conversion: an existing LibreOffice installation.
- RTF/RTFD on macOS: `/usr/bin/textutil`; LibreOffice is the cross-platform fallback.
- Native iWork export: macOS, `/usr/bin/osascript`, and the matching Numbers, Pages, or Keynote app.

Never install a dependency merely because `inspect` reports it missing. Do not run `pip install`, download parser source, manually decode iWork IWA/protobuf data, or start an exploratory fallback loop. If the helper exits with code 3, report its prerequisite immediately. Marker remains explicit because it can download several gigabytes of models. Native iWork export can launch an Apple app and request Automation permission, so it is never selected automatically.

## How to Run

The helper path is `${HERMES_SKILL_DIR}/scripts/common_files.py`.

```bash
python "${HERMES_SKILL_DIR}/scripts/common_files.py" inspect budget.numbers notes.rtfd settings.plist
python "${HERMES_SKILL_DIR}/scripts/common_files.py" extract page.html --format markdown
python "${HERMES_SKILL_DIR}/scripts/common_files.py" extract settings.plist --format json
python "${HERMES_SKILL_DIR}/scripts/common_files.py" extract notes.rtfd
python "${HERMES_SKILL_DIR}/scripts/common_files.py" extract budget.numbers
python "${HERMES_SKILL_DIR}/scripts/common_files.py" extract budget.numbers --iwork-backend libreoffice
python "${HERMES_SKILL_DIR}/scripts/common_files.py" extract budget.numbers --iwork-backend apple
python "${HERMES_SKILL_DIR}/scripts/common_files.py" batch documents --recursive --output-dir extracted
```

Use `--help` on the command or a subcommand for all options. Outputs are never overwritten unless `--force` is present. `inspect` only checks files, executables, and installed apps; it never runs converters or launches an application.

## Quick Reference

| Input | Preferred path | Notes |
|---|---|---|
| TXT, Markdown, JSON, XML, YAML | helper `extract` | BOM-aware, UTF-8 normalized |
| CSV, TSV | helper `extract` | text or escaped Markdown table |
| HTML | helper `extract` | ignores scripts/styles; no network access |
| DOCX, XLSX, IPYNB | `read_file` or helper | helper is best for saved/batch output |
| Numbers | helper `extract` | packaged native parser by default; Apple/LibreOffice are explicit alternatives |
| Pages | helper with explicit iWork backend | Apple export is native; LibreOffice is best-effort |
| Keynote (`.key`, `.keynote`) | helper with explicit iWork backend | exports to PDF; animations, media, notes are not extracted |
| RTF, RTFD | helper | macOS `textutil` or LibreOffice; RTFD attachments omitted |
| XML/binary plist | helper | deterministic JSON text via stdlib |
| Safari webarchive | helper | extracts main HTML/text only; never fetches resources |
| PDF URL | `web_extract` first | avoids local setup |
| Local text PDF | helper with PyMuPDF | delegates to `ocr-and-documents` script |
| Scanned PDF | `ocr-and-documents` | Marker must be selected explicitly |
| DOC, XLS, ODT, ODS | helper with LibreOffice | temporary conversion, then normal extraction |
| PPTX | `powerpoint` | presentation-specific workflow |
| HEIC, HEIF | image/vision or OCR workflow | images are not document containers |

## Procedure

1. **Classify first when uncertain.** Run `inspect` and confirm every requested input has the expected kind and backend candidates. Inspection has no conversion or GUI side effects.
2. **Choose the narrow path.** For one DOCX, XLSX, or IPYNB that only needs paginated reading, call `read_file` directly. Use the helper for repeatable artifacts, macOS formats, HTML/CSV normalization, legacy conversion, or batches.
3. **Use the bounded iWork path.** The default `auto` uses the packaged parser only for Numbers. Choose `--iwork-backend apple` for native fidelity on an interactive Mac with the matching app, or `--iwork-backend libreoffice` for best-effort conversion. Pages and Keynote without an explicitly selected available converter exit with code 3; report that prerequisite instead of reverse engineering the package.
4. **Extract without modifying sources.** Apple conversion opens the source read-only for export and closes it without saving. Write to a new output path or stdout. For batch jobs, use a dedicated output directory and review the JSON summary until every input is accounted for.
5. **Verify the content.** Check headings, row counts, representative values, and output encoding against the source. Successful text extraction can still omit visual-only, scanned, animated, or attached content.
6. **Return artifacts explicitly.** Name each output path in the final response so the user or delivery surface can retrieve it.

## Format Behavior

- XLSX values are extracted from visible sheets only. Hidden sheets stay excluded, and formulas are not recalculated.
- Native Numbers extraction walks sheets and tables in source order and reads stored cell values. Formulas are not recalculated. Explicit Apple/LibreOffice backends export to XLSX and remain conversion-fidelity dependent.
- Pages exports to DOCX before extraction. Native Apple export may launch Pages and trigger macOS Automation permission.
- Keynote exports to PDF before extraction. The result represents visible PDF text, not transitions, embedded media, skipped-slide state, or speaker notes.
- CSV/TSV text output is tab-separated. Markdown output treats the first row as the header and escapes pipes and embedded line breaks.
- HTML extraction preserves useful headings, paragraphs, lists, links, and table cells. It does not execute JavaScript or fetch referenced resources.
- Webarchive extraction reads only `WebMainResource`; subresources and subframes are omitted with warnings and never fetched.
- RTFD packages use `TXT.rtf`; attachments and images are not extracted or OCRed automatically.
- XML and binary plist files are rendered as deterministic JSON. Binary data, dates, and UID values use explicit tagged objects.
- iWork and RTFD package directories are treated as one bounded input. Symbolic links and unsafe package contents are rejected.
- Batch discovery is sorted and bounded. Relative paths are preserved below the output directory.
- PDF `auto` uses only lightweight PyMuPDF. It never falls through to Marker automatically.

## Pitfalls

1. **Treating extraction as visual fidelity.** Text extraction does not preserve exact Word, Excel, Pages, Numbers, Keynote, or HTML layout. Use the format-specific app when layout is the deliverable.
2. **Allowing unexpected GUI automation.** Never select the Apple backend implicitly. It may launch an app, display an Automation prompt, or fail in SSH/headless sessions.
3. **Assuming formulas were evaluated.** XLSX and exported Numbers extraction reads stored cell data; it is not a spreadsheet calculation engine.
4. **Trusting every iWork conversion.** LibreOffice support is version-dependent and may omit formulas, layout, transitions, or media. Verify representative content.
5. **Exposing hidden sheets.** Do not bypass the existing hidden-sheet omission unless the user explicitly requests a separate audited workflow.
6. **Running OCR by surprise.** Marker can download gigabytes of models. Select it only after confirming OCR is needed and the environment can support it.
7. **Overwriting a source or prior result.** Prefer a new output directory. Use `--force` only after checking the target.
8. **Ignoring omitted package content.** RTFD attachments, webarchive subresources, Keynote media, and visual-only content require separate handling.
9. **Ignoring partial batch failure.** Exit code 5 means some files failed even though others succeeded; inspect the JSON `failures` list.

## Verification

Before reporting completion:

- [ ] Every requested input or package appears in the result or batch summary.
- [ ] Output files are UTF-8 and open successfully.
- [ ] Representative headings, rows, and values match the source.
- [ ] iWork conversion used the backend the user expected, and the original file/package remained unchanged.
- [ ] PDF and Keynote output contains real text; empty output is escalated to the OCR workflow.
- [ ] XLSX/Numbers output does not disclose hidden sheets or claim formulas were recalculated.
- [ ] RTFD attachments and webarchive subresources are reported as omitted when present.
- [ ] No source file or existing output was overwritten unintentionally.
- [ ] Final response lists all generated artifact paths.
