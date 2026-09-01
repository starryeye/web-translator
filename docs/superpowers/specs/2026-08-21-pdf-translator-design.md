# PDF Translator Design

## Summary

Extend the existing `web-translator` Codex plugin with a second `pdf-translator`
skill. The new workflow accepts exactly one machine-generated PDF from a user-provided
local path, attachment, or public HTTP(S) URL and produces a reviewed, selectable Korean
PDF. The output preserves the source document's logical heading, paragraph, list, table,
figure, caption, footnote, and link structure while reflowing content for Korean text.

The existing HTML workflow remains behaviorally unchanged. PDF acquisition, extraction,
assembly, and QA are separate components. The two workflows share only the segment,
zone, assignment, translation, terminology, validation, and master-review contracts.

Each successful PDF run produces:

```text
translated-pdfs/<source-slug>-<UTC timestamp>/
|-- translated.pdf
|-- manifest.json
`-- review-report.md
```

## Goals

- Accept one readable local PDF, attached PDF, or public PDF URL.
- Reject scanned, encrypted, malformed, oversized, and structurally ambiguous PDFs with
  concrete diagnostics.
- Translate selectable source text into natural Korean through the existing isolated-zone
  and fail-closed master-review workflow.
- Preserve the logical document structure rather than original page coordinates or page
  count.
- Preserve raster images and vector figures visually without translating text inside them.
- Preserve tables as tables and translate each selectable cell independently.
- Generate a selectable, searchable PDF with embedded Korean fonts on Windows and macOS.
- Render and inspect every output page before reporting success.
- Keep enough immutable intermediate data to reproduce and diagnose every failure.

## Non-goals

- OCR or translation of scanned pages.
- Translation of text embedded in images, charts, or diagrams.
- Pixel-identical reproduction of the source page layout.
- Preservation of source page count, source font files, or exact text coordinates.
- Password entry, decryption, permission bypass, or signed-PDF preservation.
- Editing or filling AcroForms.
- Recursive translation of linked documents.
- Multiple input PDFs in one run.
- Publishing or hosting generated PDFs.

## User Experience

The user supplies exactly one PDF as an attachment, readable local path, or public URL:

```text
Translate this PDF into Korean:
/absolute/path/to/report.pdf
```

```text
Translate this PDF into Korean:
https://example.com/reports/report.pdf
```

Codex detects the PDF input and selects `pdf-translator`; it does not ask the user to
choose a platform or workflow. The skill creates a unique run directory, acquires and
extracts the PDF, delegates translation zones, performs master review, assembles a new
PDF, and runs structural and visual QA. On success, Codex links to `translated.pdf` and
`review-report.md` with absolute local paths. On failure, it reports the failed stage and
preserves the run directory without presenting a completion link.

## Plugin Surface

The plugin keeps its current package and command name for compatibility and adds a second
skill:

```text
skills/web-translator/SKILL.md
skills/pdf-translator/SKILL.md
```

- `web-translator` remains limited to one supported public HTML URL.
- `pdf-translator` accepts one local or public PDF.
- The PDF skill reads the existing translator contract and review rubric so the semantic
  quality policy remains identical across output formats.
- The plugin manifest description and README describe both workflows without renaming the
  plugin or Python package.

The PDF CLI stages are:

```text
pdf-acquire <FILE_OR_URL> --run-dir <WORK_DIR>
pdf-extract --run-dir <WORK_DIR>
plan-zones --run-dir <WORK_DIR>
prepare-assignments --run-dir <WORK_DIR>
validate-translations --run-dir <WORK_DIR> [--zone-id <ZONE_ID>]
pdf-review-input --run-dir <WORK_DIR>
pdf-assemble --run-dir <WORK_DIR> --output-dir <OUTPUT_DIR>
pdf-qa prepare --run-dir <WORK_DIR> --output-dir <OUTPUT_DIR>
pdf-qa finalize --run-dir <WORK_DIR> --output-dir <OUTPUT_DIR>
```

The existing commands retain their current arguments and behavior.

Both workflows allocate `.web-translator/runs/<run-id>` and their reserved output as
exact lexical children beneath held workspace roots. Allocation and every consuming
command reject symlink, reparse, dangling, replaced, or moved ancestors and never erase
link evidence through path resolution. Web output remains beneath `translated-pages`;
PDF output remains beneath `translated-pdfs`.

## Architecture

The input kind selects one of two format-specific pipelines:

```text
public HTML URL -> capture -> HTML extract ----+
                                                |
local/public PDF -> PDF acquire -> PDF extract -+-> shared planning, translation,
                                                    validation, and master review
                                                +-> format-specific assembly and QA
```

The format-specific boundary prevents PDF heuristics from changing HTML behavior. Shared
translation components consume `segments.jsonl` and treat `locator` as an opaque string.
The HTML assembler interprets locators as DOM selectors; the PDF assembler interprets
them as PDF block identifiers.

### PDF acquisition

Local acquisition accepts only an explicitly supplied readable regular file. It rejects
directories, links, reparse points, non-PDF signatures, and paths that change identity
between validation and copy. The source is copied to a fresh inode under the run
directory before parsing.

Remote acquisition reuses the current public-target, redirect, DNS, and IP protections.
Every redirect target is revalidated. Downloading stops before exceeding 50 MiB. A
response must have a PDF signature; a misleading content type is never sufficient. A
valid signature with a generic binary content type is accepted and recorded as a warning.

Acquisition writes `source.pdf` and `source.json` atomically. `source.json` records input
kind, requested and final source identifiers, content type, byte length, SHA-256,
acquisition time, and redirect evidence. It does not expose an arbitrary local absolute
path in the final public manifest; the final manifest records the source basename and
hash for local inputs.

### PDF inspection and rejection

`pypdf` opens the copied PDF for structural inspection. The workflow rejects:

- any encrypted PDF, even when an empty password would open it;
- invalid cross-reference tables or page trees that cannot be repaired deterministically;
- zero-page documents;
- documents larger than 50 MiB;
- documents with more than 500 pages; and
- pages with unsupported dimensions or rotations that the renderer cannot reproduce.

`pdfplumber` measures selectable characters and image coverage. A page is a
scan-candidate when it has fewer than 20 non-whitespace selectable characters and a
raster image covers at least 50 percent of its area. The entire input is rejected as a
scanned PDF when either:

- the document contains fewer than 100 non-whitespace selectable characters in total; or
- scan-candidate pages exceed `max(1, floor(page_count * 0.20))`.

This permits an image-only cover or separator page while rejecting image-dominant
documents. The rejection report lists every scan-candidate page, its selectable character
count, and image coverage.

### Logical document extraction

`pdf-extract` builds a deterministic block model in `document.json`. Every block has:

- a stable block ID;
- source page number and source bounding box;
- document reading-order index;
- kind: heading, paragraph, list item, table cell, figure, caption, footnote, header,
  footer, or page number;
- minimal style evidence: font size, weight, alignment, indentation, and spacing;
- an optional translation segment ID;
- table row and column coordinates when applicable;
- a media path and caption relationship for figures; and
- external URI or internal destination evidence when applicable.

Text is grouped from characters into words, lines, and blocks using explicit coordinate
tolerances. Font-size and weight clusters identify heading candidates. Numbering patterns,
indentation, and vertical spacing refine heading and list hierarchy. Repeated text in the
same top or bottom page band is classified as a header or footer. Numeric text in the
page-edge band that follows the page sequence is classified as a page number.

Multi-column pages are processed column by column only when column x-ranges are separated
by a gutter of at least 18 points and non-heading text does not cross that gutter. A page
with conflicting column evidence fails extraction instead of guessing its reading order.

Extraction is accepted only when:

- at least 99 percent of non-whitespace selectable characters belong to exactly one text
  block or an explicitly excluded repeated header, footer, or page number;
- no peer text blocks overlap by more than 10 percent of the smaller block area;
- every character inside a detected table belongs to exactly one cell; and
- every emitted translation target appears exactly once in document reading order.

Failure evidence names the page, conflicting blocks, and unmatched character counts.

### Tables

Table detection uses explicit lines first and aligned text second. `document.json` stores
the fixed row and column grid, merged-cell spans, cell order, header-row status, and each
cell's segment ID. Empty cells remain structural cells and are not translation targets.
Characters may not belong to two cells. A table whose cell assignment is ambiguous fails
extraction.

The PDF assembler constructs a native ReportLab table. It may split a long table across
pages and repeats its header rows. A table wider than the normal content frame first uses
a landscape page template. If it cannot render at a minimum of 9-point body text without
horizontal clipping, assembly fails with the table ID.

### Figures, images, and captions

Figures are non-text graphical regions derived from raster image objects and connected
vector graphics. The source page is rendered with Poppler and each accepted figure region
is cropped to a PNG at a fixed high-resolution scale. This preserves masks, vector charts,
and mixed graphics without decoding source image internals. Selectable caption text remains
outside the crop and becomes a separate translation segment. Text visually embedded inside
the figure remains unchanged.

The assembler preserves aspect ratio, never enlarges a crop above its rendered source
resolution, and tries to keep a figure and its caption together. A valid
standalone uncaptioned figure has `caption_id=None` and is emitted once as standalone media. A
relationship is reciprocal only when a caption exists; orphan captions and ambiguous or
multiple pairings remain required failures. A detected figure that cannot be rendered or
cropped is a required failure.

### Footnotes, links, and navigation

Footnote markers and bodies become linked blocks. Each source annotation persists as
structured evidence containing its source page, source block/span or bounds, visible
label, external URI or internal destination, reconstruction status, and reason. Multiple
unambiguous inline annotations in one block are recreated on their individual translated
spans instead of the whole paragraph. The reflowed document places footnotes
at the end of the containing section or, when the relationship is page-local and clear,
at the bottom of the relevant output page. External URI annotations are recreated on the
translated text. Internal outline and destination links are rebuilt only when both source
and destination map unambiguously to emitted blocks. A noncritical link that cannot be
rebuilt becomes a warning only while its visible label and destination remain in both
final artifacts; missing visible text never becomes a warning.

## Shared Translation Contract

The existing segment schema remains unchanged. PDF locators use stable opaque values:

```text
pdf:page-0003:block-0012
pdf:page-0005:table-0002:row-0003:cell-0002
```

Paragraphs, headings, list items, captions, footnotes, and nonempty table cells become
independent translation targets. Inline URLs, identifiers, code, commands, product names,
and protocol keywords use the existing protected-token mechanism. Heading ancestry and
neighbor context preserve document context across zones.

The existing zone planner, immutable assignment packager, per-zone validator, aggregate
validator, canonical glossary, retry limit, review dimensions, and first-use terminology
normalization apply without format-specific exceptions.

After translations, zones/assignments, segments, and glossary policy/content are final,
`pdf-review-input` writes deterministic `semantic-review-input.json`. Its canonical
`semantic_input_sha256` covers the exact bytes of `segments.jsonl`, every zone and
assignment file, every translation file, and the identified/versioned glossary policy
plus glossary content. PDF `review.json` requires that digest. `pdf-assemble`,
`pdf-qa prepare`, and `pdf-qa finalize` recompute it and fail on any mismatch. This is a
PDF-specific review reader and does not change the webpage `review.json` contract.

## PDF Assembly

`pdf-assemble` uses ReportLab Platypus and format-specific flowables. The plugin vendors
static Noto Sans KR Regular and Bold font files plus their Open Font License and embeds
the fonts in every generated PDF. It does not rely on fonts installed by Windows, macOS,
or a PDF viewer.

Assembly rules are:

- preserve heading hierarchy, paragraph order, list nesting, table grid, figure order,
  captions, and footnote relationships;
- use consistent normalized styles rather than copying source fonts;
- allow Korean reflow to increase page count;
- never shrink body or table text below 9 points to fit a source page count;
- use portrait pages by default and landscape templates only for wide tables or figures;
- scale figures down proportionally and never distort or upscale them;
- repeat normalized headers, footers, and page numbers through page templates; and
- add a short final source-attribution block with source name or URL and generation time.

Assembly writes `translated.pdf` to `staged-output/` under the private run directory and
records the reserved final output path without creating it. `pdf-qa prepare` reopens the
staged PDF, performs contract, structural, and rendering QA, then creates contact sheets.
After master visual review, `pdf-qa finalize` validates the written visual-review contract,
adds `manifest.json` and `review-report.md` to the staged directory, and atomically renames
the complete staged directory to the reserved output path. An existing destination is
never replaced.

## PDF QA

QA is fail-closed and has four layers.

### Contract QA

- Source and translated segment IDs match exactly.
- Every protected token remains exact.
- Every table retains its row, column, and merged-cell contract.
- Every required figure and caption appears once.
- The master review has no unresolved required finding.

### Structural PDF QA

The generated PDF is reopened independently. QA requires:

- a nonzero page count;
- no encryption;
- embedded Regular and Bold Korean fonts with usable Unicode mappings;
- extractable Korean text for every nonempty translated block;
- valid page resource and content streams;
- every emitted external link to have a valid URI annotation; and
- every rebuilt internal link to resolve to an output destination.

### Rendering QA

Poppler renders every output page to PNG. Before launch, source/output page dimensions and
DPI are rejected above 36,000,000 rendered pixels per page or 2,000,000,000 pixels across
one PDF. While and after rendering, any PNG above 64 MiB or complete rendered-page set
above 4 GiB terminates or fails the operation. PNG headers and decoded raster dimensions
are validated against the same per-page pixel limit before full Pillow decode. Each
Poppler subprocess is limited to 600 seconds. Where a platform cannot impose an
OS-level address-space limit,
these deterministic pre/while/post geometry, encoded-byte, decoded-pixel, and timeout
budgets remain mandatory.

The assembler records flowable and frame bounds,
which QA uses to reject content outside a page frame, peer-flowable overlap, body or table
text below 9 points, blank pages without an intentional section break, missing figures,
and glyph rendering failures. Render failure for any page is required.

### Master visual review

QA creates numbered contact sheets containing every rendered page. The master inspects all
contact sheets for heading hierarchy, readable Korean, table legibility, figure-caption
pairing, footnote placement, page transitions, clipping, overlap, blank regions, and broken
glyphs. It writes `pdf-layout-review.json` with exact contact-sheet and page coverage plus
one `pass` or `required-fix` finding with nonempty evidence for each canonical visual
dimension: `heading_hierarchy`, `text_legibility`, `table_legibility`,
`figure_caption_pairing`, `footnote_placement`, `page_transitions`, `clipping_overlap`, and
`glyph_rendering`. `pdf-qa finalize` rejects missing pages, missing dimensions, unresolved
required findings, or stale review evidence whose staged-PDF hash differs from the current
file. A layout fix causes assembly, `pdf-qa prepare`, visual review, and `pdf-qa finalize`
to rerun.

## Run Artifacts

The private run directory contains:

```text
source.pdf
source.json
document.json
segments.jsonl
document-summary.txt
glossary.json
media/
zones/
assignments/
translations/
semantic-review-input.json
review.json
pdf-layout-review.json
staged-output/
qa-pages/
```

The final `manifest.json` records:

- input kind, source basename or public URL, final public URL when applicable, source
  SHA-256, source byte length, and source page count;
- selectable character totals, scan-candidate evidence, layout validation counts, and
  extraction warnings;
- counts for headings, paragraphs, lists, tables, cells, figures, captions, footnotes,
  segments, and zones;
- target language, terminology policy, retries, and master-review result;
- output page count, embedded fonts, links, figures, output SHA-256, and QA status; and
- every warning that did not hide or alter required visible content.

`review-report.md` presents the same provenance, semantic-review evidence, layout findings,
render evidence, contact-sheet coverage, warnings, and final acceptance status in a
human-readable form.

## Error Handling

Required failures include:

- unsafe local source path or nonpublic remote target;
- non-PDF signature, malformed structure, encryption, zero pages, size limit, or page
  limit;
- scanned-PDF threshold reached;
- ambiguous reading order, unmatched selectable text, overlapping peer blocks, or
  ambiguous table cells;
- missing required figure crop, paired caption, translation, protected token, or review evidence;
- Korean font load or embedding failure;
- unreadable table at the minimum font size;
- PDF write, reopen, text extraction, page render, or visual-review failure; and
- any attempt to reuse an existing output directory.

Warnings are limited to noncritical metadata loss, a generic remote content type when the
PDF signature is valid, or a visible link that cannot be reconstructed while its label
and destination remain recorded in the report. Warnings cannot downgrade missing text,
figures, tables, fonts, or pages.

Every command returns nonzero on a required failure. Failed run data remains available for
diagnosis inside the private run directory. The final output directory is not created and
no `translated.pdf` completion link is returned unless `pdf-qa finalize` passes.

## Dependencies and Assets

Add runtime dependencies for `pdfplumber`, `pypdf`, `reportlab`, and Pillow. Poppler's
`pdftoppm` and `pdfinfo` executables provide deterministic rendering and inspection. Setup
documentation covers Poppler installation on Windows and macOS and fails with an actionable
message when it is missing.

Vendor the two Korean font files and their license under a dedicated package asset
directory. Tests confirm the files are shipped in built distributions and that output PDFs
do not depend on host fonts.

The selected library capabilities are documented by the official
[pdfplumber project](https://github.com/jsvine/pdfplumber/blob/stable/README.md),
[ReportLab user guide](https://www.reportlab.com/docs/reportlab-userguide.pdf), and
[pypdf documentation](https://pypdf.readthedocs.io/).

## Testing Strategy

### Unit tests

- Local and remote acquisition, redirects, SSRF defenses, signatures, atomic writes, and
  collision-safe output paths.
- Encrypted, malformed, empty, oversized, over-page-limit, and scanned-PDF rejection.
- Text grouping, heading hierarchy, lists, repeated headers and footers, page numbers,
  multi-column reading order, tables, figures, captions, footnotes, and links.
- Exact block coverage, overlap rejection, table-cell assignment, and stable locators.
- PDF font embedding, styles, page templates, table splitting, landscape fallback, image
  scaling, footnotes, links, attribution, and non-overwrite behavior.
- Structural QA, rendering evidence, contact-sheet coverage, and manifest/report ordering.

### Deterministic fixtures

Generate and commit small, reviewable PDFs covering:

- a technical document with headings, paragraphs, lists, links, and page headers;
- a report with a multi-page table and merged cells;
- a two-column document with footnotes;
- a document with raster images, vector charts, and captions;
- an image-only scanned document;
- an encrypted document;
- a malformed document; and
- a valid document located under a path containing spaces and Korean characters.

Fixtures contain known Korean translations so integration tests do not require model calls.

### Integration tests

- Execute the full local-input pipeline from `source.pdf` to all three final artifacts.
- Execute remote acquisition through a controlled local server while retaining public-target
  validation tests separately.
- Verify exact translation coverage, master-review enforcement, deterministic manifest and
  report content, and no output on required failure.
- Run the same contracts on Windows and macOS.
- Keep changing upstream public PDFs out of the default suite; any live PDF checks are
  explicitly marked and opt-in.

### Visual regression

Render every generated fixture page with Poppler. Assert page count, dimensions, blank-page
policy, layout bounds, embedded Korean glyphs, table readability, and figure presence.
Keep small reference renderings only where pixel comparison is stable; otherwise assert
structural layout metrics and inspect contact sheets.

## Implementation Boundaries

New modules should keep one responsibility each:

- `pdf_acquire.py`: safe local copy and bounded public download;
- `pdf_extract.py`: inspection, scan detection, and logical block extraction;
- `pdf_models.py`: typed PDF document and layout contracts;
- `pdf_media.py`: page rendering, figure detection, and crops;
- `pdf_assemble.py`: ReportLab flowables and atomic output publication;
- `pdf_qa.py`: structural, rendering, and contact-sheet QA; and
- focused CLI adapters in `cli.py`, extracted if command wiring becomes unwieldy.

Do not add PDF branches inside the existing HTML capture, extract, assemble, or QA modules.
Generalize a shared model only when both format paths already need the same behavior and
existing HTML tests prove compatibility.

## Completion Criteria

The PDF feature is complete when all approved text-based fixtures produce a selectable
Korean `translated.pdf`, `manifest.json`, and `review-report.md`, and:

- every source translation target maps to exactly one approved translation;
- all protected content remains exact;
- tables retain their approved cell grids;
- every required figure and caption appears once;
- Korean fonts are embedded and render on Windows and macOS without host fonts;
- all generated pages render and receive master visual-review evidence;
- scanned, encrypted, malformed, oversized, and structurally ambiguous fixtures fail with
  the specified diagnostics;
- no existing HTML behavior or test changes unintentionally; and
- no required finding remains unresolved when success is reported.
