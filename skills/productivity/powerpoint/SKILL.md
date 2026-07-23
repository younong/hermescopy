---
name: powerpoint
description: "Create, read, edit .pptx decks, slides, notes, templates."
license: Proprietary. LICENSE.txt has complete terms
platforms: [linux, macos, windows]
---

# Powerpoint Skill

## When to use

Use this skill any time a .pptx file is involved in any way — as input, output, or both. This includes: creating slide decks, pitch decks, or presentations; reading, parsing, or extracting text from any .pptx file (even if the extracted content will be used elsewhere, like in an email or summary); editing, modifying, or updating existing presentations; combining or splitting slide files; working with templates, layouts, speaker notes, or comments. Trigger whenever the user mentions "deck," "slides," "presentation," or references a .pptx filename, regardless of what they plan to do with the content afterward. If a .pptx file needs to be opened, created, or touched, use this skill.

## Quick Reference

| Task | Guide |
|------|-------|
| Read/analyze content | `python -m markitdown presentation.pptx` |
| Edit or create from template | Read [editing.md](editing.md) |
| Create from scratch | Read [pptxgenjs.md](pptxgenjs.md) |

---

## Default Fast Workflow

Use this path unless the user explicitly asks for independent visual review, pixel-perfect polish, or another high-assurance deliverable. Aim for 2-4 substantive model decisions: one combined content/design plan, one generation pass, an optional repair only when validation finds a concrete defect, and delivery.

1. **Plan once**: decide the outline, slide count, palette, typography, visual motif, and per-slide layouts together. Do not delegate planning. Do not browse the web unless the user asks for current facts or required source material is missing.
2. **Generate once**: use one PptxGenJS program for a from-scratch deck, or complete template structure and content edits in one owned workflow. Prefer local shapes, charts, icons, and user-supplied assets over exploratory image searches.
3. **Validate deterministically**:
   - Confirm the `.pptx` exists and is non-empty.
   - Run `python -m markitdown output.pptx` once and compare its slide order and key content with the outline.
   - For template work, grep the extracted text for leftover placeholders.
   - Run one headless LibreOffice conversion to PDF to prove the package opens.
4. **Repair only concrete failures**: regenerate or patch only when a check reports missing content, placeholders, corruption, or another specific defect. Do not invent a cosmetic change to force a revision cycle.
5. **Deliver immediately**: return the exact artifact path reported by the file or terminal operation. Do not block ordinary delivery on rasterization, subagents, or visual-image inspection.

Do not use `delegate_task`, `pdftoppm`, or web research in the default path. Provider retries and real validation failures may require extra turns, but the workflow should not create them by design.

---

## Reading Content

```bash
# Text extraction
python -m markitdown presentation.pptx

# Visual overview
python scripts/thumbnail.py presentation.pptx

# Raw XML
python scripts/office/unpack.py presentation.pptx unpacked/
```

---

## Editing Workflow

**Read [editing.md](editing.md) for full details.**

1. Analyze template with `thumbnail.py`
2. Unpack → manipulate slides → edit content → clean → pack

---

## Creating from Scratch

**Read [pptxgenjs.md](pptxgenjs.md) for full details.**

Use when no template or reference presentation is available.

---

## Design Ideas

**Don't create boring slides.** Plain bullets on a white background won't impress anyone. Consider ideas from this list for each slide.

### Before Starting

- **Pick a bold, content-informed color palette**: The palette should feel designed for THIS topic. If swapping your colors into a completely different presentation would still "work," you haven't made specific enough choices.
- **Dominance over equality**: One color should dominate (60-70% visual weight), with 1-2 supporting tones and one sharp accent. Never give all colors equal weight.
- **Dark/light contrast**: Dark backgrounds for title + conclusion slides, light for content ("sandwich" structure). Or commit to dark throughout for a premium feel.
- **Commit to a visual motif**: Pick ONE distinctive element and repeat it — rounded image frames, icons in colored circles, thick single-side borders. Carry it across every slide.

### Color Palettes

Choose colors that match your topic — don't default to generic blue. Use these palettes as inspiration:

| Theme | Primary | Secondary | Accent |
|-------|---------|-----------|--------|
| **Midnight Executive** | `1E2761` (navy) | `CADCFC` (ice blue) | `FFFFFF` (white) |
| **Forest & Moss** | `2C5F2D` (forest) | `97BC62` (moss) | `F5F5F5` (cream) |
| **Coral Energy** | `F96167` (coral) | `F9E795` (gold) | `2F3C7E` (navy) |
| **Warm Terracotta** | `B85042` (terracotta) | `E7E8D1` (sand) | `A7BEAE` (sage) |
| **Ocean Gradient** | `065A82` (deep blue) | `1C7293` (teal) | `21295C` (midnight) |
| **Charcoal Minimal** | `36454F` (charcoal) | `F2F2F2` (off-white) | `212121` (black) |
| **Teal Trust** | `028090` (teal) | `00A896` (seafoam) | `02C39A` (mint) |
| **Berry & Cream** | `6D2E46` (berry) | `A26769` (dusty rose) | `ECE2D0` (cream) |
| **Sage Calm** | `84B59F` (sage) | `69A297` (eucalyptus) | `50808E` (slate) |
| **Cherry Bold** | `990011` (cherry) | `FCF6F5` (off-white) | `2F3C7E` (navy) |

### For Each Slide

**Every slide needs a visual element** — image, chart, icon, or shape. Text-only slides are forgettable.

**Layout options:**
- Two-column (text left, illustration on right)
- Icon + text rows (icon in colored circle, bold header, description below)
- 2x2 or 2x3 grid (image on one side, grid of content blocks on other)
- Half-bleed image (full left or right side) with content overlay

**Data display:**
- Large stat callouts (big numbers 60-72pt with small labels below)
- Comparison columns (before/after, pros/cons, side-by-side options)
- Timeline or process flow (numbered steps, arrows)

**Visual polish:**
- Icons in small colored circles next to section headers
- Italic accent text for key stats or taglines

### Typography

**Choose an interesting font pairing** — don't default to Arial. Pick a header font with personality and pair it with a clean body font.

| Header Font | Body Font |
|-------------|-----------|
| Georgia | Calibri |
| Arial Black | Arial |
| Calibri | Calibri Light |
| Cambria | Calibri |
| Trebuchet MS | Calibri |
| Impact | Arial |
| Palatino | Garamond |
| Consolas | Calibri |

| Element | Size |
|---------|------|
| Slide title | 36-44pt bold |
| Section header | 20-24pt bold |
| Body text | 14-16pt |
| Captions | 10-12pt muted |

### Spacing

- 0.5" minimum margins
- 0.3-0.5" between content blocks
- Leave breathing room—don't fill every inch

### Avoid (Common Mistakes)

- **Don't repeat the same layout** — vary columns, cards, and callouts across slides
- **Don't center body text** — left-align paragraphs and lists; center only titles
- **Don't skimp on size contrast** — titles need 36pt+ to stand out from 14-16pt body
- **Don't default to blue** — pick colors that reflect the specific topic
- **Don't mix spacing randomly** — choose 0.3" or 0.5" gaps and use consistently
- **Don't style one slide and leave the rest plain** — commit fully or keep it simple throughout
- **Don't create text-only slides** — add images, icons, charts, or visual elements; avoid plain title + bullets
- **Don't forget text box padding** — when aligning lines or shapes with text edges, set `margin: 0` on the text box or offset the shape to account for padding
- **Don't use low-contrast elements** — icons AND text need strong contrast against the background; avoid light text on light backgrounds or dark text on dark backgrounds
- **NEVER use accent lines under titles** — these are a hallmark of AI-generated slides; use whitespace or background color instead

---

## Default Validation

Run these checks for every generated or edited deck:

```bash
# Content and ordering
python -m markitdown output.pptx

# Template placeholder check (no output means clean)
python -m markitdown output.pptx | grep -iE "xxxx|lorem|ipsum|this.*(page|slide).*layout"

# Structural/openability check
python "${HERMES_SKILL_DIR}/scripts/office/soffice.py" --headless --convert-to pdf output.pptx
```

Also confirm the `.pptx` is non-empty. Fix and repeat only a check that found a concrete defect. A clean deterministic pass is sufficient for ordinary delivery.

---

## Thorough Visual QA (Opt-in)

Use this path only when the user explicitly asks for independent visual inspection, pixel-perfect polish, or another high-assurance review. It is not part of ordinary generation.

1. Complete the default validation first.
2. Convert the deck to slide images once.
3. Delegate one independent visual inspection. Pass the actual rendered image paths through `delegate_task.artifact_paths`; put expected slide descriptions in `context`.
4. Fix concrete findings only.
5. Re-render and re-check affected slides where possible. Stop when a full pass finds no concrete issue; do not make a fake change.

Before delegation, use the exact canonical/result paths returned by the generation or rendering command. If file tools created an artifact, use their `resolved_path` or `files_modified` result. Never synthesize `/workspace/<name>`, forward an attachment label as a path, or ask the reviewer to discover a missing file. Locate or regenerate the artifact in the parent and retry delegation.

The reviewer prompt should inspect the validated images for:

- Overlapping or cut-off elements
- Text overflow and excessive wrapping
- Misaligned columns, cards, titles, citations, or footers
- Uneven or insufficient spacing and slide-edge margins
- Low-contrast text or icons
- Leftover placeholders

### Converting to Images

```bash
python "${HERMES_SKILL_DIR}/scripts/office/soffice.py" --headless --convert-to pdf output.pptx
pdftoppm -jpeg -r 150 output.pdf slide
```

This creates `slide-01.jpg`, `slide-02.jpg`, etc. Pass those real files in `artifact_paths`.

To re-render only an affected slide:

```bash
pdftoppm -jpeg -r 150 -f N -l N output.pdf slide-fixed
```

---

## Dependencies

Managed Hermes production provides locked MarkItDown, PptxGenJS, and LibreOffice dependencies inside the immutable executor runtime. Local installations may need:

- `pip install "markitdown[pptx]"` - text extraction
- `pip install Pillow` - thumbnail grids
- `npm install -g pptxgenjs` - creating from scratch
- LibreOffice (`soffice`) - PDF conversion through `${HERMES_SKILL_DIR}/scripts/office/soffice.py`
- Poppler (`pdftoppm`) - opt-in thorough visual QA only
