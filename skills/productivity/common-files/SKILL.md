---
name: common-files
description: Extract and normalize common local document files.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    category: productivity
    tags: [documents, docx, xlsx, csv, html, text]
    related_skills: [ocr-and-documents, powerpoint]
---

# Common Files Skill

Use this skill to inspect, extract, and normalize common local files through one repeatable workflow. It covers lightweight text and tabular formats directly, reuses Hermes's existing DOCX/XLSX reader, and delegates PDF/OCR rather than rebuilding those processors.

## When to Use

Use this skill when:

- a task includes TXT, Markdown, HTML, CSV, TSV, DOCX, XLSX, IPYNB, PDF, or legacy Office files;
- normalized UTF-8 text or Markdown is needed for downstream analysis;
- several files need deterministic batch extraction;
- you need to identify the available backend before processing a file.

Do not use it to author or visually edit Office documents. Use `powerpoint` for PPTX, the finance-specific `excel-author` skill for financial models, and `ocr-and-documents` for advanced PDF/OCR work.

## Prerequisites

The common path uses Python's standard library and Hermes's installed `tools.read_extract` module. Optional formats require:

- PDF text: PyMuPDF; load `ocr-and-documents` for setup and OCR guidance.
- Scanned PDF OCR: marker-pdf, selected explicitly because its models require several gigabytes.
- `.doc`, `.xls`, `.odt`, `.ods`: an existing LibreOffice installation.

Never install a dependency merely because `inspect` reports it missing. Explain the requirement and get the user's agreement first, especially for marker-pdf.

## How to Run

The helper path is `${HERMES_SKILL_DIR}/scripts/common_files.py`.

```bash
python "${HERMES_SKILL_DIR}/scripts/common_files.py" inspect report.docx data.csv
python "${HERMES_SKILL_DIR}/scripts/common_files.py" extract page.html --format markdown
python "${HERMES_SKILL_DIR}/scripts/common_files.py" extract report.xlsx --output report.txt
python "${HERMES_SKILL_DIR}/scripts/common_files.py" batch documents --recursive --output-dir extracted
```

Use `--help` on the command or a subcommand for all options. Outputs are never overwritten unless `--force` is present.

## Quick Reference

| Input | Preferred path | Notes |
|---|---|---|
| TXT, Markdown, JSON, XML, YAML | helper `extract` | BOM-aware, UTF-8 normalized |
| CSV, TSV | helper `extract` | text or escaped Markdown table |
| HTML | helper `extract` | ignores scripts/styles; no network access |
| DOCX, XLSX, IPYNB | `read_file` or helper | helper is best for saved/batch output |
| PDF URL | `web_extract` first | avoids local setup |
| Local text PDF | helper with PyMuPDF | delegates to `ocr-and-documents` script |
| Scanned PDF | `ocr-and-documents` | Marker must be selected explicitly |
| DOC, XLS, ODT, ODS | helper with LibreOffice | temporary conversion, then normal extraction |
| PPTX | `powerpoint` | presentation-specific workflow |

## Procedure

1. **Classify first when uncertain.** Run `inspect` and confirm every requested input has the expected kind and backend. Missing optional software must be visible before processing starts.
2. **Choose the narrow path.** For one DOCX, XLSX, or IPYNB that only needs paginated reading, call `read_file` directly. Use the helper for repeatable artifacts, HTML/CSV normalization, legacy conversion, or batches.
3. **Extract without modifying sources.** Write to a new output path or stdout. For batch jobs, use a dedicated output directory and review the JSON summary until every input is accounted for as success, skip, or failure.
4. **Verify the content.** Check headings, row counts, representative values, and output encoding against the source. An extraction that exits successfully can still omit visual-only or scanned content.
5. **Return artifacts explicitly.** Name each output path in the final response so the user or delivery surface can retrieve it.

## Format Behavior

- XLSX values are extracted from visible sheets only. Hidden sheets stay excluded, and formulas are not recalculated.
- CSV/TSV text output is tab-separated. Markdown output treats the first row as the header and escapes pipes and embedded line breaks.
- HTML extraction preserves useful headings, paragraphs, lists, links, and table cells. It does not execute JavaScript or fetch referenced resources.
- Batch discovery is sorted and bounded. Relative paths are preserved below the output directory.
- PDF `auto` uses only lightweight PyMuPDF. It never falls through to Marker automatically.

## Pitfalls

1. **Treating extraction as visual fidelity.** Text extraction does not preserve exact Word/Excel/HTML layout. Use the format-specific skill when layout is the deliverable.
2. **Assuming formulas were evaluated.** XLSX extraction reads stored cell data; it is not a spreadsheet calculation engine.
3. **Exposing hidden sheets.** Do not bypass the existing hidden-sheet omission unless the user explicitly requests a separate audited workflow.
4. **Running OCR by surprise.** Marker can download gigabytes of models. Select it only after confirming OCR is needed and the environment can support it.
5. **Overwriting a source or prior result.** Prefer a new output directory. Use `--force` only after checking the target.
6. **Ignoring partial batch failure.** Exit code 5 means some files failed even though others succeeded; inspect the JSON `failures` list.

## Verification

Before reporting completion:

- [ ] Every requested input appears in the result or batch summary.
- [ ] Output files are UTF-8 and open successfully.
- [ ] Representative headings, rows, and values match the source.
- [ ] PDF output contains real text; empty output is escalated to the OCR workflow.
- [ ] XLSX output does not disclose hidden sheets or claim formulas were recalculated.
- [ ] No source file or existing output was overwritten unintentionally.
- [ ] Final response lists all generated artifact paths.
