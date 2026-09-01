# PDF Translator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-closed PDF translation workflow that accepts one local or public text-based PDF and produces a reviewed, selectable Korean PDF plus manifest and review report.

**Architecture:** Keep HTML acquisition, extraction, assembly, and QA unchanged. Add format-specific PDF modules that emit the existing `Segment` contract, reuse zone planning and translation review, stage a ReportLab-generated PDF, render every page with Poppler, and publish only after strict automated and master visual review.

**Tech Stack:** Python 3.11+, httpx/httpcore, pdfplumber, pypdf, ReportLab Platypus, Pillow, fontTools for vendoring, Poppler (`pdfinfo`, `pdftoppm`), pytest.

**Spec:** `docs/superpowers/specs/2026-08-21-pdf-translator-design.md`

## Global Constraints

- Preserve all current HTML commands and the full existing test suite.
- Accept exactly one readable local PDF, attached PDF, or public HTTP(S) PDF URL.
- Reject files over 50 MiB, files over 500 pages, every encrypted PDF, malformed PDFs, and scanned PDFs using the exact thresholds in the spec.
- Emit no successful final output directory before `pdf-qa finalize` passes.
- Preserve the existing `Segment`, zone, assignment, translation, glossary, retry, and semantic-review contracts.
- Keep Windows PowerShell and macOS/POSIX invocation behavior equivalent; paths with spaces and Korean characters are required cases.
- Use bundled static Noto Sans KR Regular and Bold fonts and never depend on host fonts.
- Treat missing text, tables, figures, fonts, rendered pages, or visual-review evidence as required failures.
- Use TDD for every behavior change: write one focused test, observe the expected failure, implement the minimum behavior, and rerun the focused and neighboring suites.
- In commands below, `<PYTHON>` means `& $python` on PowerShell and `"$python"` on POSIX, using the repository-local interpreter resolved by the existing platform contract.
- Every task ends with its own review and commit; do not combine task commits.

## File Structure

### New production files

- `src/web_translator/network.py`: shared bounded, DNS-pinned public HTTP primitives.
- `src/web_translator/pdf_models.py`: strict PDF source, page, block, table, inspection, layout-review, and QA contracts.
- `src/web_translator/pdf_acquire.py`: safe local copy and public PDF download.
- `src/web_translator/pdf_extract.py`: structural inspection, scan rejection, and logical extraction orchestration.
- `src/web_translator/pdf_layout.py`: word grouping, columns, headings, lists, repeated bands, tables, footnotes, and links.
- `src/web_translator/pdf_media.py`: Poppler discovery/rendering, graphical-region crops, and contact sheets.
- `src/web_translator/pdf_flowables.py`: tracked ReportLab paragraphs, tables, figures, captions, and footnotes.
- `src/web_translator/pdf_assemble.py`: translation restoration, normalized styling, staging, and layout evidence.
- `src/web_translator/pdf_qa.py`: prepare/finalize gates and strict visual-review parsing.
- `src/web_translator/pdf_report.py`: deterministic PDF manifest and review-report rendering.
- `src/web_translator/font_assets/NotoSansKR-Regular.ttf`: vendored static Korean font.
- `src/web_translator/font_assets/NotoSansKR-Bold.ttf`: vendored static Korean font.
- `src/web_translator/font_assets/OFL.txt`: font license.
- `src/web_translator/font_assets/PROVENANCE.json`: pinned source URL and SHA-256 evidence.
- `scripts/vendor_pdf_fonts.py`: reproducibly instantiate and subset the pinned variable source font.
- `skills/pdf-translator/SKILL.md`: PDF-specific master orchestration.

### New test files

- `tests/pdf_fixtures.py`: deterministic in-test PDF builders.
- `tests/test_network.py`
- `tests/test_pdf_models_paths.py`
- `tests/test_pdf_acquire.py`
- `tests/test_pdf_extract.py`
- `tests/test_pdf_media.py`
- `tests/test_pdf_assemble.py`
- `tests/test_pdf_qa.py`
- `tests/test_pdf_pipeline.py`
- `tests/fixtures/pdf/`: generated committed acceptance PDFs and expected metadata.
- `.github/workflows/pdf-cross-platform.yml`: required macOS and Windows package/PDF smoke matrix.

### Existing files modified

- `src/web_translator/capture.py`: consume shared network primitives without behavior change.
- `src/web_translator/paths.py`: add PDF run-path allocation.
- `src/web_translator/cli.py`: wire four PDF stages and stable exit-code mapping.
- `pyproject.toml`: dependencies, font package data, and test tooling.
- `.codex-plugin/plugin.json`: broaden plugin description while retaining plugin name.
- `README.md`: PDF setup, usage, limits, outputs, and Poppler requirements.
- `tests/test_plugin_layout.py`: two-skill discovery and packaged font assertions.
- `tests/test_skill_contract.py`: PDF skill orchestration and platform command contract.
- `tests/test_cli_contract.py`: PDF command help/status/error mapping.
- `tests/test_versioning.py`: retain synchronized version contract.
- `.gitignore`: ignore PDF run and generated test scratch directories, not committed fixtures or fonts.

---

### Task 1: PDF dependencies, typed contracts, and run paths

**Files:**
- Create: `src/web_translator/pdf_models.py`
- Create: `tests/pdf_fixtures.py`
- Create: `tests/test_pdf_models_paths.py`
- Modify: `src/web_translator/paths.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `PdfSourceRecord`, `PdfPageEvidence`, `PdfBlockStyle`, `PdfTableCell`, `PdfBlock`, `PdfPage`, `PdfDocument`, `PdfLayoutReview`, and `create_pdf_run_paths(workspace, source_label, now) -> RunPaths`.
- Consumes: existing `RunPaths`, `Segment`, JSON conventions, and collision-safe directory rules.

- [ ] **Step 1: Add failing model and path tests**

Create tests that import the exact public names, round-trip each record through
`to_dict()`/`from_dict()`, reject unknown or mistyped fields, and assert PDF outputs use
`translated-pdfs/` without overwriting an existing output.

```python
def test_pdf_document_round_trip_rejects_unknown_fields() -> None:
    document = PdfDocument(
        schema_version="1.0",
        source_sha256="a" * 64,
        page_count=1,
        selectable_characters=42,
        scan_candidate_pages=[],
        pages=[PdfPage(number=1, width=612.0, height=792.0, rotation=0)],
        blocks=[
            PdfBlock(
                id="pdf:page-0001:block-0001",
                page_number=1,
                order=0,
                kind="paragraph",
                bbox=(72.0, 72.0, 540.0, 96.0),
                style=PdfBlockStyle(12.0, False, "left", 0.0, 8.0),
                source_text="Selectable text",
                segment_id="seg-000001",
            )
        ],
    )
    assert PdfDocument.from_dict(document.to_dict()) == document
    payload = document.to_dict()
    payload["unknown"] = True
    with pytest.raises(PdfContractError, match="fields must be exactly"):
        PdfDocument.from_dict(payload)
```

```python
def test_pdf_run_paths_use_separate_collision_safe_output_root(tmp_path: Path) -> None:
    now = datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC)
    existing = tmp_path / "translated-pdfs" / "report-20260821-010203"
    existing.mkdir(parents=True)
    paths = create_pdf_run_paths(tmp_path, "report.pdf", now)
    assert paths.work_dir.name == "report-20260821-010203-2"
    assert paths.output_dir == tmp_path / "translated-pdfs" / paths.run_id
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `<PYTHON> -m pytest tests/test_pdf_models_paths.py -q`

Expected: collection fails because `web_translator.pdf_models` and
`create_pdf_run_paths` do not exist.

- [ ] **Step 3: Add dependencies and strict dataclasses**

Add runtime dependencies with bounded major versions and fontTools only to the test/dev
extra:

```toml
dependencies = [
    "httpx>=0.28,<0.29",
    "httpcore>=1.0,<1.1",
    "beautifulsoup4",
    "lxml",
    "tinycss2",
    "langdetect",
    "playwright",
    "pdfplumber>=0.11,<0.12",
    "pypdf>=6,<7",
    "reportlab>=4,<5",
    "Pillow>=11,<13",
]

[project.optional-dependencies]
test = [
    "pytest",
    "pytest-httpx",
    "fonttools>=4,<5",
]
```

Define exact literals and public records in `pdf_models.py`:

```python
PdfBlockKind = Literal[
    "heading", "paragraph", "list-item", "table-cell", "figure",
    "caption", "footnote", "header", "footer", "page-number",
]
PdfVerdict = Literal["pass", "required-fix"]
BBox = tuple[float, float, float, float]

@dataclass(frozen=True, slots=True)
class PdfBlockStyle:
    font_size: float
    bold: bool
    alignment: Literal["left", "center", "right", "justify"]
    indentation: float
    space_after: float

@dataclass(frozen=True, slots=True)
class PdfBlock:
    id: str
    page_number: int
    order: int
    kind: PdfBlockKind
    bbox: BBox
    style: PdfBlockStyle
    source_text: str = ""
    segment_id: str | None = None
    table_id: str | None = None
    row: int | None = None
    column: int | None = None
    row_span: int = 1
    column_span: int = 1
    media_path: str | None = None
    caption_id: str | None = None
    uri: str | None = None
    destination: str | None = None
```

Implement strict field equality, numeric finiteness, positive page dimensions, stable ID
patterns, sorted unique scan pages, exact block ordering, and source SHA-256 validation.

- [ ] **Step 4: Add PDF path allocation**

Refactor the current slug/timestamp collision loop into a private helper used by both
`create_run_paths` and this new function without changing HTML paths:

```python
def create_pdf_run_paths(
    workspace: Path, source_label: str, now: datetime
) -> RunPaths:
    source_name = Path(source_label).name
    stem = Path(source_name).stem or "document"
    base_run_id = "-".join(
        part for part in (_slugify(stem), _utc_timestamp(now)) if part
    )
    return _allocate_run_paths(
        Path(workspace), base_run_id, Path("translated-pdfs")
    )
```

- [ ] **Step 5: Run focused and existing path tests**

Run: `<PYTHON> -m pytest tests/test_pdf_models_paths.py tests/test_models_paths.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/web_translator/pdf_models.py src/web_translator/paths.py tests/pdf_fixtures.py tests/test_pdf_models_paths.py
git commit -m "feat: add PDF translation contracts"
```

### Task 2: Shared bounded public HTTP transport

**Files:**
- Create: `src/web_translator/network.py`
- Create: `tests/test_network.py`
- Modify: `src/web_translator/capture.py`
- Test: `tests/test_capture.py`

**Interfaces:**
- Produces: `NetworkBudget`, `build_public_client`, `fetch_limited`,
  `PinnedHTTPTransport`, and `NetworkError`.
- Consumes: `validate_public_url`, httpx/httpcore, injected `httpx.MockTransport` in tests.

- [ ] **Step 1: Characterize current capture security and specify shared primitives**

Add tests proving a request is rejected before connection when DNS resolves to private
space, redirect bodies count toward the byte budget, environment proxies are ignored, and
only `MockTransport` can be injected.

```python
def test_public_client_blocks_private_dns_before_mocked_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []
    transport = httpx.MockTransport(
        lambda request: requested.append(str(request.url))
        or httpx.Response(200, content=b"%PDF-1.7\n")
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    budget = NetworkBudget(max_bytes=1024, max_redirects=5, deadline_seconds=10.0)
    with build_public_client(budget=budget, transport=transport) as client:
        with pytest.raises(NetworkError, match="non-public DNS"):
            fetch_limited(client, "https://example.com/report.pdf", 1024, "PDF")
    assert requested == []
```

- [ ] **Step 2: Run RED**

Run: `<PYTHON> -m pytest tests/test_network.py -q`

Expected: collection fails because `web_translator.network` does not exist.

- [ ] **Step 3: Move general network mechanics without changing policy**

Move and rename the current budgeted stream, DNS validation, pinned backend/transport,
compatibility check, and limited reader into `network.py`. Expose this construction API:

```python
def build_public_client(
    *,
    budget: NetworkBudget,
    transport: httpx.BaseTransport | None = None,
    user_agent: str = USER_AGENT,
) -> httpx.Client:
    if transport is not None and type(transport) is not httpx.MockTransport:
        raise NetworkError(
            "transport injection accepts only non-network httpx.MockTransport instances"
        )
    return httpx.Client(
        follow_redirects=True,
        max_redirects=budget.max_redirects,
        timeout=30.0,
        transport=transport if transport is not None else PinnedHTTPTransport(),
        trust_env=False,
        headers={"user-agent": user_agent, "accept-encoding": "identity"},
        event_hooks={
            "request": [validate_network_boundary, budget.before_request],
            "response": [budget.after_response],
        },
    )
```

Keep capture-specific emitted-byte, asset-count, and CSS budgets in `capture.py`; use the
shared client and reader for request safety and downloaded bytes.

- [ ] **Step 4: Run security and capture regression tests**

Run: `<PYTHON> -m pytest tests/test_network.py tests/test_capture.py -q`

Expected: all tests pass with the same HTML capture results and error text.

- [ ] **Step 5: Run the full existing suite before PDF consumers depend on the refactor**

Run: `<PYTHON> -m pytest -q`

Expected: the existing suite passes with no PDF test failures introduced by collection.

- [ ] **Step 6: Commit**

```bash
git add src/web_translator/network.py src/web_translator/capture.py tests/test_network.py tests/test_capture.py
git commit -m "refactor: share bounded public downloads"
```

### Task 3: Safe local and public PDF acquisition

**Files:**
- Create: `src/web_translator/pdf_acquire.py`
- Create: `tests/test_pdf_acquire.py`
- Modify: `src/web_translator/cli.py`
- Modify: `tests/test_cli_contract.py`

**Interfaces:**
- Produces: `acquire_pdf(source, run_dir, transport=None, now=None) -> PdfSourceRecord`
  and CLI `pdf-acquire`.
- Consumes: `NetworkBudget`, `build_public_client`, `fetch_limited`, `PdfSourceRecord`,
  `atomic_write`, and CLI atomic JSON helpers.

- [ ] **Step 1: Add failing acquisition tests**

Cover a regular local file under a Korean/space path, local links/reparse points,
non-PDF signatures, a file that changes identity during copy, public URL redirects,
generic binary content type warnings, private redirect targets, content-length lies,
streaming overflow at `50 * 1024 * 1024`, nonempty run directories, and no overwrite.

```python
def test_acquire_local_pdf_copies_to_fresh_inode_and_records_private_provenance(
    tmp_path: Path,
) -> None:
    source = make_text_pdf(tmp_path / "입력 자료" / "보고서.pdf", ["Hello PDF"])
    run_dir = tmp_path / "실행 공간"
    record = acquire_pdf(str(source), run_dir, now=FIXED_TIME)
    copied = run_dir / "source.pdf"
    assert copied.read_bytes() == source.read_bytes()
    assert not os.path.samefile(copied, source)
    assert record.input_kind == "local"
    assert record.source_name == "보고서.pdf"
    assert record.requested_url is None
    assert record.final_url is None
```

- [ ] **Step 2: Run RED**

Run: `<PYTHON> -m pytest tests/test_pdf_acquire.py -q`

Expected: collection fails because `acquire_pdf` does not exist.

- [ ] **Step 3: Implement local acquisition**

Use `lstat`, the Windows reparse attribute, `os.open`, `os.fstat`, and an opened file
descriptor so validation and copying address the same file. Hash while copying and abort
before publishing above 50 MiB.

```python
MAX_PDF_BYTES = 50 * 1024 * 1024
PDF_SIGNATURE = b"%PDF-"

def acquire_pdf(
    source: str,
    run_dir: Path,
    *,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> PdfSourceRecord:
    parsed = urlsplit(source)
    if parsed.scheme in {"http", "https"}:
        return _acquire_public_pdf(source, run_dir, transport=transport, now=now)
    if parsed.scheme:
        raise PdfAcquireError("PDF source must be a local path or public HTTP(S) URL")
    return _acquire_local_pdf(Path(source), run_dir, now=now)
```

- [ ] **Step 4: Implement public acquisition and source metadata**

Use the shared client with one 50 MiB budget, validate the final URL, require the PDF
signature, accept `application/pdf` or generic binary media types, and return a sorted
warning list. Write only `source.pdf`; let the CLI serialize the returned record to
`source.json` atomically.

- [ ] **Step 5: Wire `pdf-acquire` into the CLI**

Add parser and handler without changing existing command names:

```python
pdf_acquire = subparsers.add_parser(
    "pdf-acquire", help="Acquire one local or public PDF."
)
pdf_acquire.add_argument("source")
_add_run_dir(pdf_acquire)
pdf_acquire.set_defaults(handler=_pdf_acquire_command)
```

Map `PdfAcquireError` to `EXIT_CAPTURE_FAILURE`. The handler requires an empty run
directory and writes strict `source.json` after `source.pdf` succeeds.

- [ ] **Step 6: Run focused tests**

Run: `<PYTHON> -m pytest tests/test_pdf_acquire.py tests/test_cli_contract.py -q`

Expected: all tests pass; CLI stdout remains one stable status object.

- [ ] **Step 7: Commit**

```bash
git add src/web_translator/pdf_acquire.py src/web_translator/cli.py tests/test_pdf_acquire.py tests/test_cli_contract.py
git commit -m "feat: acquire local and public PDFs"
```

### Task 4: PDF inspection, limits, and scanned-document rejection

**Files:**
- Modify: `src/web_translator/pdf_extract.py`
- Modify: `tests/pdf_fixtures.py`
- Create: `tests/test_pdf_extract.py`

**Interfaces:**
- Produces: `inspect_pdf(source_pdf) -> PdfInspection` and
  `reject_unsupported_pdf(inspection) -> None`.
- Consumes: `PdfPageEvidence`, pypdf page/encryption metadata, pdfplumber characters and
  image boxes.

- [ ] **Step 1: Add fixture builders and failing inspection tests**

Add deterministic builders for text, image-only, mixed cover/text, encrypted, malformed,
zero-page, rotated, 501-page, and oversized-dimension PDFs. Assert the exact scan rule:

```python
def test_scan_rule_allows_one_image_cover_but_rejects_image_dominant_document(
    tmp_path: Path,
) -> None:
    allowed = make_mixed_pdf(tmp_path / "allowed.pdf", scanned_pages=1, text_pages=4)
    rejected = make_mixed_pdf(tmp_path / "rejected.pdf", scanned_pages=2, text_pages=3)
    assert inspect_pdf(allowed).scan_candidate_pages == [1]
    reject_unsupported_pdf(inspect_pdf(allowed))
    with pytest.raises(PdfExtractionError, match="scanned PDF.*pages 1, 2"):
        reject_unsupported_pdf(inspect_pdf(rejected))
```

- [ ] **Step 2: Run RED**

Run: `<PYTHON> -m pytest tests/test_pdf_extract.py -q`

Expected: import fails because inspection functions do not exist.

- [ ] **Step 3: Implement structural inspection**

Open with `PdfReader(strict=True)` and reject `reader.is_encrypted` before page access.
Open separately with pdfplumber. For each page calculate:

```python
selectable = sum(1 for char in page.chars if str(char.get("text", "")).strip())
largest_image_area = max(
    (float(image["width"]) * float(image["height"]) for image in page.images),
    default=0.0,
)
coverage = largest_image_area / (float(page.width) * float(page.height))
scan_candidate = selectable < 20 and coverage >= 0.50
```

Normalize rotations to `0`, `90`, `180`, or `270`; reject other values, nonfinite sizes,
and dimensions outside 36 through 14,400 points.

- [ ] **Step 4: Implement exact document rejection**

Reject when page count is zero or above 500, total selectable non-whitespace characters
are below 100, or scan candidates exceed `max(1, floor(page_count * 0.20))`. Include page
numbers, character counts, and coverages in the exception evidence.

- [ ] **Step 5: Run inspection tests**

Run: `<PYTHON> -m pytest tests/test_pdf_extract.py -q`

Expected: all inspection and scan tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/web_translator/pdf_extract.py tests/pdf_fixtures.py tests/test_pdf_extract.py
git commit -m "feat: reject unsupported PDF inputs"
```

### Task 5: Logical text blocks and shared segments

**Files:**
- Create: `src/web_translator/pdf_layout.py`
- Modify: `src/web_translator/pdf_extract.py`
- Modify: `src/web_translator/cli.py`
- Modify: `tests/test_pdf_extract.py`
- Modify: `tests/test_cli_contract.py`

**Interfaces:**
- Produces: `extract_pdf(source_pdf, document_path, segments_path, media_dir) -> PdfDocument`
  and CLI `pdf-extract`.
- Consumes: `inspect_pdf`, `protect_fragment`, `write_segments`, strict PDF models.

- [ ] **Step 1: Add failing logical-extraction tests**

Generate fixtures with heading font clusters, paragraphs, nested numbered/bulleted lists,
repeated headers/footers, page numbers, single columns, clear two columns, and an ambiguous
column gutter. Assert stable block/segment order and exact coverage.

```python
def test_extract_pdf_emits_heading_context_and_opaque_locators(tmp_path: Path) -> None:
    source = make_structured_pdf(tmp_path / "structured.pdf")
    document = extract_pdf(
        source,
        tmp_path / "document.json",
        tmp_path / "segments.jsonl",
        tmp_path / "media",
    )
    segments = read_segments(tmp_path / "segments.jsonl")
    assert [block.kind for block in document.blocks[:3]] == [
        "heading", "paragraph", "list-item"
    ]
    assert segments[1].heading_path == ["Architecture"]
    assert segments[1].locator == "pdf:page-0001:block-0002"
```

- [ ] **Step 2: Run RED**

Run: `<PYTHON> -m pytest tests/test_pdf_extract.py -q`

Expected: fails because logical block extraction is absent.

- [ ] **Step 3: Implement word, line, and column ordering**

Use `page.extract_words(return_chars=True, extra_attrs=["fontname", "size"])`. Group words
into lines by overlapping vertical centers and a dynamic gap derived from font size. Detect
two columns only when a gap of at least 18 points separates x-ranges and no non-heading
line crosses it. Reject conflicting column evidence.

Expose focused pure helpers:

```python
def group_words_into_lines(words: Sequence[Mapping[str, object]]) -> list[PdfLine]:
    candidates = [PdfWord.from_pdfplumber(word) for word in words]
    lines: list[PdfLine] = []
    for word in sorted(candidates, key=lambda item: (item.top, item.x0)):
        matching = [
            (index, line.vertical_overlap_ratio(word))
            for index, line in enumerate(lines)
            if line.vertical_overlap_ratio(word) >= 0.60
        ]
        if not matching:
            lines.append(PdfLine.from_word(word))
            continue
        index = max(matching, key=lambda item: item[1])[0]
        lines[index] = lines[index].with_word(word)
    return [line.normalized() for line in sorted(lines, key=lambda item: item.top)]

def order_page_lines(lines: Sequence[PdfLine], page_width: float) -> list[PdfLine]:
    gutter = find_clear_gutter(lines, page_width, minimum_width=18.0)
    if gutter is None:
        return sorted(lines, key=lambda item: (item.top, item.x0))
    if any(line.crosses(gutter) and not line.is_heading for line in lines):
        raise PdfExtractionError("conflicting column evidence")
    return order_column_regions(lines, gutter)

def build_text_blocks(lines: Sequence[PdfLine], page_number: int) -> list[PdfBlock]:
    classified = [classify_line(line) for line in lines]
    merged = merge_contiguous_paragraph_lines(classified)
    return [
        PdfBlock.from_lines(
            block_lines,
            page_number=page_number,
            order=index,
            kind=kind,
        )
        for index, (kind, block_lines) in enumerate(merged)
    ]
```

Implement `PdfWord`/`PdfLine` constructors and the named helpers in the same module. Tests
must directly cover the 60-percent vertical-overlap rule, 18-point gutter threshold,
spanning-heading regions, conflicting non-heading spans, and paragraph merging.

- [ ] **Step 4: Classify headings, lists, repeated bands, and page numbers**

Cluster rounded font sizes document-wide. A bold line or a line in a larger cluster may
be a heading; numbering patterns and vertical spacing determine level. Lines starting with
bullets or ordered-list markers become list items. Repeated normalized text in the same
top/bottom 10 percent band on at least 60 percent of eligible pages becomes header/footer.
Sequential numeric edge-band text becomes page numbers.

- [ ] **Step 5: Emit protected shared segments and validate coverage**

Create one target segment for headings, paragraphs, list items, captions, footnotes, and
nonempty table cells. Use `protect_fragment` on plain text and assign fixed-width IDs in
document order. Require 99 percent character assignment and reject peer overlap above 10
percent of the smaller area.

- [ ] **Step 6: Wire atomic `pdf-extract` publication**

Add a CLI parser/handler that requires safe `source.pdf` and `source.json`, writes
`document.json`, `segments.jsonl`, and `media/` inside a temporary directory, then replaces
all three only after extraction succeeds. Map `PdfExtractionError` to
`EXIT_CONTRACT_FAILURE`.

- [ ] **Step 7: Run extraction, zone, and CLI tests**

Run: `<PYTHON> -m pytest tests/test_pdf_extract.py tests/test_zones_translations.py tests/test_cli_contract.py -q`

Expected: PDF segments work with the existing zone planner and all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/web_translator/pdf_layout.py src/web_translator/pdf_extract.py src/web_translator/cli.py tests/test_pdf_extract.py tests/test_cli_contract.py
git commit -m "feat: extract PDF translation segments"
```

### Task 6: Tables, figures, captions, footnotes, and links

**Files:**
- Create: `src/web_translator/pdf_media.py`
- Modify: `src/web_translator/pdf_layout.py`
- Modify: `src/web_translator/pdf_extract.py`
- Create: `tests/test_pdf_media.py`
- Modify: `tests/test_pdf_extract.py`

**Interfaces:**
- Produces: `find_poppler() -> PopplerTools`, `render_pdf_pages`, `crop_figure_regions`,
  `detect_tables`, `detect_footnotes`, and `extract_link_evidence`.
- Consumes: `PdfBlock`, `PdfTableCell`, pdfplumber tables/objects/annotations, Pillow, and
  Poppler command output.

- [ ] **Step 1: Add failing media and rich-layout tests**

Build PDFs containing a ruled table with merged cells, text-aligned table, raster image,
vector chart, caption, footnote marker/body, external URI, internal destination, and a
deliberately ambiguous table. Assert exact row/column ownership, PNG dimensions, caption
relationships, and link evidence.

- [ ] **Step 2: Run RED**

Run: `<PYTHON> -m pytest tests/test_pdf_media.py tests/test_pdf_extract.py -q`

Expected: media and rich-layout imports or assertions fail.

- [ ] **Step 3: Implement Poppler discovery and deterministic rendering**

Use `shutil.which("pdfinfo")` and `shutil.which("pdftoppm")`; return absolute executables or
raise one actionable `PdfMediaError`. Run subprocesses with argv lists, `shell=False`,
captured stderr, timeouts, and explicit output prefixes.

```python
@dataclass(frozen=True, slots=True)
class PopplerTools:
    pdfinfo: Path
    pdftoppm: Path

def render_pdf_pages(
    source_pdf: Path, destination: Path, *, dpi: int = 144
) -> list[Path]:
    tools = find_poppler()
    destination.mkdir(parents=True, exist_ok=False)
    prefix = destination / "page"
    command = [
        str(tools.pdftoppm), "-png", "-r", str(dpi),
        str(source_pdf), str(prefix),
    ]
    _run_poppler(command, "render PDF pages")
    return sorted(destination.glob("page-*.png"))
```

- [ ] **Step 4: Implement table ownership and ambiguity rejection**

Use explicit line tables before text-alignment tables. Convert every table to fixed cell
coordinates, calculate spans from missing internal borders, and require every selectable
character inside the table bbox to belong to exactly one cell. Empty cells remain blocks
without segments.

- [ ] **Step 5: Implement figure crops and caption relationships**

Group raster objects and connected vector graphics into non-text regions, render the page,
convert PDF coordinates to rendered pixels, crop at 144 DPI, and write deterministic names
`media/figure-0001.png`. Exclude selectable caption text from crops. A region that cannot be
cropped is fatal.

- [ ] **Step 6: Implement footnote and link evidence**

Match superscript markers to smaller-font page-edge bodies by normalized marker text.
Store external URI annotations directly. Store internal destinations only when pypdf maps
both ends to known emitted blocks. Record unresolved visible links as warnings.

- [ ] **Step 7: Run rich extraction tests**

Run: `<PYTHON> -m pytest tests/test_pdf_media.py tests/test_pdf_extract.py -q`

Expected: all tests pass, including missing-Poppler and malformed crop errors.

- [ ] **Step 8: Commit**

```bash
git add src/web_translator/pdf_media.py src/web_translator/pdf_layout.py src/web_translator/pdf_extract.py tests/test_pdf_media.py tests/test_pdf_extract.py
git commit -m "feat: preserve PDF tables and figures"
```

### Task 7: Vendored Korean fonts and basic PDF assembly

**Files:**
- Create: `scripts/vendor_pdf_fonts.py`
- Create: `src/web_translator/font_assets/NotoSansKR-Regular.ttf`
- Create: `src/web_translator/font_assets/NotoSansKR-Bold.ttf`
- Create: `src/web_translator/font_assets/OFL.txt`
- Create: `src/web_translator/font_assets/PROVENANCE.json`
- Create: `src/web_translator/pdf_flowables.py`
- Create: `src/web_translator/pdf_assemble.py`
- Create: `tests/test_pdf_assemble.py`
- Modify: `src/web_translator/cli.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `assemble_pdf(run_dir, translations, glossary, output_dir) -> Path`, tracked
  paragraph/list flowables, staged `translated.pdf`, `layout.json`, and CLI `pdf-assemble`.
- Consumes: `PdfDocument`, `Segment`, `Translation`, `normalize_first_use`,
  `restore_tokens`, ReportLab, and bundled font assets.

- [ ] **Step 1: Add failing font packaging and basic assembly tests**

Assert package-resource lookup finds both fonts, PDFs embed both font programs, Korean
text extracts after reopening, headings/paragraphs/lists retain order, source attribution
exists, and only `run_dir/staged-output/translated.pdf` is created.

```python
def test_assemble_pdf_stages_selectable_korean_without_publishing(
    reviewed_pdf_run: PdfRunFixture,
) -> None:
    final_output = reviewed_pdf_run.root / "translated-pdfs" / "result"
    staged = assemble_pdf(
        reviewed_pdf_run.run_dir,
        reviewed_pdf_run.translations,
        reviewed_pdf_run.glossary,
        final_output,
    )
    assert staged == reviewed_pdf_run.run_dir / "staged-output" / "translated.pdf"
    assert not final_output.exists()
    assert "한국어 본문" in "".join(
        page.extract_text() or "" for page in PdfReader(staged).pages
    )
```

- [ ] **Step 2: Run RED**

Run: `<PYTHON> -m pytest tests/test_pdf_assemble.py tests/test_plugin_layout.py -q`

Expected: assembly module and font resources are missing.

- [ ] **Step 3: Add reproducible font vendoring**

Pin these exact official Noto CJK source constants in the script:

```python
FONT_SOURCE_URL = (
    "https://raw.githubusercontent.com/notofonts/noto-cjk/"
    "f8d157532fbfaeda587e826d4cd5b21a49186f7c/"
    "Sans/Variable/TTF/NotoSansCJKkr-VF.ttf"
)
FONT_SOURCE_SHA256 = "7715af52f5fe77153ce5678546258993982d2da61abea8d25fb89eb5aaec5ca6"
FONT_LICENSE_URL = (
    "https://raw.githubusercontent.com/notofonts/noto-cjk/"
    "f8d157532fbfaeda587e826d4cd5b21a49186f7c/LICENSE"
)
```

Use `fontTools.varLib.instancer` to create weights 400 and 700, subset to ASCII, Latin-1,
general punctuation, currency, arrows, CJK punctuation, Hangul Jamo, compatibility Jamo,
and all Hangul syllables, then save deterministic static TTFs. Copy the upstream OFL and
write source URL, source hash, output hashes, axis values, and Unicode ranges to
`PROVENANCE.json`. The script must refuse a source hash mismatch.

- [ ] **Step 4: Package and register fonts**

Add package data:

```toml
[tool.setuptools.package-data]
web_translator = [
    "font_assets/*.ttf",
    "font_assets/OFL.txt",
    "font_assets/PROVENANCE.json",
]
```

Resolve with `importlib.resources.files("web_translator") / "font_assets"` and register
`WT-NotoSansKR` and `WT-NotoSansKR-Bold` with ReportLab `TTFont`.

- [ ] **Step 5: Implement basic flowables and staging**

Normalize translations in document order, restore tokens, escape ReportLab paragraph
markup, and map heading/paragraph/list blocks to tracked flowables. Use 11-point body,
9-point minimum, normalized heading levels, portrait A4/Letter chosen from source median
aspect ratio, and a final source-attribution block.

```python
def assemble_pdf(
    run_dir: Path,
    translations: Mapping[str, Translation],
    glossary: Mapping[str, str],
    output_dir: Path,
) -> Path:
    document = read_pdf_document(run_dir / "document.json")
    segments = {item.id: item for item in read_segments(run_dir / "segments.jsonl")}
    ordered = _normalize_pdf_translations(document, segments, translations, glossary)
    staging = _prepare_staging_directory(run_dir, output_dir)
    translated_pdf = staging / "translated.pdf"
    layout = _build_basic_document(document, ordered, translated_pdf)
    write_pdf_layout(run_dir / "layout.json", layout)
    return translated_pdf
```

- [ ] **Step 6: Wire `pdf-assemble`**

Require aggregate translation validation and semantic `review.json` before calling
`assemble_pdf`. Map `PdfAssemblyError` to `EXIT_ASSEMBLY_FAILURE`.

- [ ] **Step 7: Run basic assembly and package tests**

Run: `<PYTHON> -m pytest tests/test_pdf_assemble.py tests/test_plugin_layout.py tests/test_cli_contract.py -q`

Expected: all tests pass and no final output directory appears.

- [ ] **Step 8: Commit**

```bash
git add scripts/vendor_pdf_fonts.py src/web_translator/font_assets src/web_translator/pdf_flowables.py src/web_translator/pdf_assemble.py src/web_translator/cli.py pyproject.toml tests/test_pdf_assemble.py tests/test_plugin_layout.py tests/test_cli_contract.py
git commit -m "feat: assemble selectable Korean PDFs"
```

### Task 8: Advanced reflow for tables, figures, footnotes, and links

**Files:**
- Modify: `src/web_translator/pdf_flowables.py`
- Modify: `src/web_translator/pdf_assemble.py`
- Modify: `tests/test_pdf_assemble.py`

**Interfaces:**
- Produces: native split tables, landscape fallback, figure/caption pairs, footnotes,
  URI/internal links, repeated headers/footers, and complete tracked layout evidence.
- Consumes: rich `PdfDocument` blocks and staged media.

- [ ] **Step 1: Add failing rich-assembly tests**

Assert multi-page tables repeat header rows, merged spans remain, wide tables switch to
landscape, unreadable tables fail below 9 points, figures retain aspect ratio and are not
upscaled, captions stay adjacent, links reopen correctly, and every flowable part records
page/frame bounds.

- [ ] **Step 2: Run RED**

Run: `<PYTHON> -m pytest tests/test_pdf_assemble.py -q`

Expected: failures identify missing table, figure, link, and layout behavior.

- [ ] **Step 3: Implement tracked flowables**

Wrap each flowable so `drawOn` records page, x/y, width/height, block ID, and split-part
index. Validate every bound is finite and inside its frame before writing `layout.json`.

- [ ] **Step 4: Implement tables and landscape page templates**

Build ReportLab `Table` data from fixed cells, apply spans, use `repeatRows`, calculate
required column widths, and select a landscape template when portrait width fails. Reject
when 9-point wrapped cells still exceed landscape width.

- [ ] **Step 5: Implement figures, captions, footnotes, and navigation**

Use `KeepTogether` when a figure/caption pair fits; otherwise place the figure then caption
without changing order. Add external links through safe escaped `<link href>` markup and
internal destinations through named anchors. Place clear page-local footnotes in the page
footnote frame; place section footnotes before the next heading.

- [ ] **Step 6: Run assembly tests and inspect rendered fixtures**

Run: `<PYTHON> -m pytest tests/test_pdf_assemble.py -q`

Render the generated rich fixture with:

```bash
pdftoppm -png <STAGED_TRANSLATED_PDF> <TEMP_OUTPUT_PREFIX>
```

Inspect every generated page image and record any fixture adjustment in the test commit.

- [ ] **Step 7: Commit**

```bash
git add src/web_translator/pdf_flowables.py src/web_translator/pdf_assemble.py tests/test_pdf_assemble.py
git commit -m "feat: reflow rich PDF content"
```

### Task 9: Automated PDF QA preparation and contact sheets

**Files:**
- Create: `src/web_translator/pdf_qa.py`
- Create: `tests/test_pdf_qa.py`
- Modify: `src/web_translator/pdf_media.py`
- Modify: `src/web_translator/cli.py`
- Modify: `tests/test_cli_contract.py`

**Interfaces:**
- Produces: `prepare_pdf_qa(run_dir, output_dir) -> PdfQAResult`, strict
  `pdf-qa prepare`, `pdf-qa.json`, `qa-pages/`, and numbered contact sheets.
- Consumes: staged PDF, layout evidence, source/document/segments/translations/review,
  Poppler, pypdf, pdfplumber, and Pillow.

- [ ] **Step 1: Add failing QA preparation tests**

Cover translation/token coverage, missing figures, table grid mismatch, unresolved semantic
review, encryption, missing fonts, missing Unicode maps, unreadable Korean blocks, invalid
links, layout overflow/overlap, sub-9-point text, blank pages, render failure, and contact
sheet coverage.

```python
def test_prepare_pdf_qa_renders_every_page_and_covers_contact_sheets(
    assembled_pdf_run: PdfRunFixture,
) -> None:
    result = prepare_pdf_qa(
        assembled_pdf_run.run_dir,
        assembled_pdf_run.output_dir,
    )
    assert result.passed is True
    assert [path.name for path in result.rendered_pages] == [
        "page-001.png", "page-002.png"
    ]
    assert result.contact_sheet_pages == {"contact-sheet-001.png": [1, 2]}
```

- [ ] **Step 2: Run RED**

Run: `<PYTHON> -m pytest tests/test_pdf_qa.py -q`

Expected: `prepare_pdf_qa` is missing.

- [ ] **Step 3: Implement contract and structural QA**

Validate exact source/translated IDs, protected tokens, tables, figures/captions, and
semantic review. Reopen with pypdf and pdfplumber, require nonzero unencrypted pages,
embedded Regular/Bold fonts with `/ToUnicode`, Korean extraction for every translated
block, valid streams, and link annotations.

- [ ] **Step 4: Implement render/layout QA and contact sheets**

Render every page through `render_pdf_pages`. Cross-check `layout.json` bounds against page
frames, reject peer overlap, font size below 9, unintended blank pages, missing crops, and
glyph replacement boxes. Build contact sheets of at most 12 pages each with visible page
numbers and stable names.

- [ ] **Step 5: Persist immutable automated evidence**

Write `pdf-qa.json` atomically with staged PDF SHA-256, all finding codes/evidence,
rendered page hashes, contact-sheet page coverage, structure metrics, and `passed`.
Regeneration replaces only the prior safe regular `qa-pages/` and `pdf-qa.json` inside the
run directory; never follow links.

- [ ] **Step 6: Wire only `pdf-qa prepare`**

Add nested QA action parsing with the prepare action. Task 10 adds finalize only when its
implementation exists:

```python
pdf_qa = subparsers.add_parser("pdf-qa", help="Prepare or finalize PDF QA.")
pdf_qa_actions = pdf_qa.add_subparsers(dest="pdf_qa_action", required=True)
prepare = pdf_qa_actions.add_parser("prepare")
_add_run_dir(prepare)
_add_output_dir(prepare)
prepare.set_defaults(handler=_pdf_qa_prepare_command)
```

- [ ] **Step 7: Run QA and CLI tests**

Run: `<PYTHON> -m pytest tests/test_pdf_qa.py tests/test_cli_contract.py -q`

Expected: prepare cases pass; no finalize command is advertised before Task 10.

- [ ] **Step 8: Commit**

```bash
git add src/web_translator/pdf_qa.py src/web_translator/pdf_media.py src/web_translator/cli.py tests/test_pdf_qa.py tests/test_cli_contract.py
git commit -m "feat: prepare rendered PDF QA evidence"
```

### Task 10: Visual-review finalization, reports, and atomic publication

**Files:**
- Create: `src/web_translator/pdf_report.py`
- Modify: `src/web_translator/pdf_qa.py`
- Modify: `src/web_translator/cli.py`
- Modify: `tests/test_pdf_qa.py`
- Create: `tests/test_pdf_pipeline.py`

**Interfaces:**
- Produces: `read_pdf_layout_review`, `finalize_pdf_output`, deterministic PDF manifest
  and report writers, operational `pdf-qa finalize`, and atomic final output publication.
- Consumes: `pdf-qa.json`, `pdf-layout-review.json`, staged PDF and its hash, source and
  extraction provenance, semantic review, and reserved output path.

- [ ] **Step 1: Add failing strict-review and publication tests**

Cover exact page/contact-sheet coverage, all eight canonical dimensions, duplicate/missing
dimensions, empty evidence, invalid verdicts, unresolved required findings, stale staged
hash, output collision, publish rollback, manifest determinism, and successful atomic
rename.

```python
VISUAL_DIMENSIONS = (
    "heading_hierarchy", "text_legibility", "table_legibility",
    "figure_caption_pairing", "footnote_placement", "page_transitions",
    "clipping_overlap", "glyph_rendering",
)

def test_finalize_rejects_stale_visual_review(
    prepared_pdf_run: PdfRunFixture,
) -> None:
    write_passing_layout_review(prepared_pdf_run.run_dir, staged_sha256="0" * 64)
    with pytest.raises(PdfQAFailure, match="staged PDF hash"):
        finalize_pdf_output(
            prepared_pdf_run.run_dir,
            prepared_pdf_run.output_dir,
        )
    assert not prepared_pdf_run.output_dir.exists()
```

- [ ] **Step 2: Run RED**

Run: `<PYTHON> -m pytest tests/test_pdf_qa.py tests/test_pdf_pipeline.py -q`

Expected: strict review and finalization functions are missing.

- [ ] **Step 3: Implement strict layout-review parsing**

Require top-level fields `schema_version`, `staged_pdf_sha256`, `pages_reviewed`,
`contact_sheets_reviewed`, `findings`, and `unresolved_required`. Require sorted unique
coverage equal to `pdf-qa.json`, exactly one finding per canonical dimension, and exact
agreement between `required-fix` findings and `unresolved_required`.

- [ ] **Step 4: Implement deterministic manifest and report rendering**

Build a PDF-specific manifest with source, inspection, block counts, languages,
terminology, zones/retries, output page/hash/font/link/figure metrics, automated QA,
visual-review coverage, warnings, tool version, and schema version. Render stable sorted
JSON plus a Markdown report with semantic and visual evidence.

- [ ] **Step 5: Implement atomic finalization**

Require a passing automated QA record and layout review for the current staged hash.
Write manifest/report inside `staged-output`, fsync files and directory, reject an existing
or linked output path, then `os.replace(staged_output, output_dir)`. If report generation or
rename fails, keep staging inside the run directory and do not create a partial final path.

- [ ] **Step 6: Add the finalize CLI action and run tests**

Add `pdf-qa finalize` beside prepare, map `PdfQAFailure` to `EXIT_QA_FAILURE`, and emit
`ok` only after rename completes.

Run: `<PYTHON> -m pytest tests/test_pdf_qa.py tests/test_pdf_pipeline.py tests/test_cli_contract.py -q`

Expected: all tests pass and final output contains exactly `translated.pdf`,
`manifest.json`, and `review-report.md`.

- [ ] **Step 7: Commit**

```bash
git add src/web_translator/pdf_report.py src/web_translator/pdf_qa.py src/web_translator/cli.py tests/test_pdf_qa.py tests/test_pdf_pipeline.py tests/test_cli_contract.py
git commit -m "feat: finalize reviewed PDF outputs"
```

### Task 11: PDF translator skill and plugin documentation

**Files:**
- Create: `skills/pdf-translator/SKILL.md`
- Modify: `.codex-plugin/plugin.json`
- Modify: `README.md`
- Modify: `tests/test_skill_contract.py`
- Modify: `tests/test_plugin_layout.py`

**Interfaces:**
- Produces: discoverable `pdf-translator` skill and cross-platform seven-stage orchestration
  with prepare/review/finalize QA.
- Consumes: existing translator contract, review rubric, assignment package, all PDF CLI
  commands, collaboration tools, and platform interpreter contract.

- [ ] **Step 1: Use the required skill-authoring workflow and capture baseline failure**

Read `superpowers:writing-skills` completely. Run a fresh-context agent scenario against
the plugin before adding `pdf-translator`: ask it to translate a local text-based PDF and
record that no supported PDF workflow can be derived. Do not edit the skill before this
baseline.

- [ ] **Step 2: Add failing static plugin contracts**

Require both skill directories, PDF trigger text, local/public source support, platform
interpreter mappings, every PDF CLI stage, same-agent retry, strict visual dimensions,
contact-sheet inspection, no literal placeholders, and final links only after finalize.

```python
def test_pdf_skill_is_discoverable_and_fail_closed() -> None:
    text = Path("skills/pdf-translator/SKILL.md").read_text("utf-8")
    for phrase in (
        "name: pdf-translator",
        "local path",
        "public HTTP(S) URL",
        "pdf-acquire",
        "pdf-extract",
        "plan-zones",
        "prepare-assignments",
        "validate-translations",
        "pdf-assemble",
        "pdf-qa prepare",
        "pdf-layout-review.json",
        "pdf-qa finalize",
        "never report partial output as complete",
    ):
        assert phrase in text
```

- [ ] **Step 3: Run RED**

Run: `<PYTHON> -m pytest tests/test_skill_contract.py tests/test_plugin_layout.py -q`

Expected: tests fail because the PDF skill and broadened plugin metadata are absent.

- [ ] **Step 4: Write the minimal PDF skill**

Reuse the established platform execution section, source/run path creation, shared
summary/glossary/assignment workflow, isolated translators, per-zone validation/review,
aggregate validation, and retry limits. Add PDF-specific acquire/extract/assemble, render
contact-sheet inspection, exact `pdf-layout-review.json` schema, prepare/finalize commands,
and three final artifact links.

- [ ] **Step 5: Update plugin metadata and README**

Keep manifest name `web-translator`; broaden descriptions to public pages and text-based
PDFs. Document Windows/macOS Poppler installation, local/public examples, 50 MiB/500-page
limits, scan/encryption rejection, stage commands, output tree, font provenance, and visual
review.

- [ ] **Step 6: Pressure-test the completed skill**

Run fresh-context Windows and macOS scenarios with paths containing spaces and Korean
characters. Require exact native command invocations, every stage in order, all contact
sheets reviewed, strict review JSON, and no completion link before finalize. Fix only
observed guidance gaps and rerun until both scenarios comply.

- [ ] **Step 7: Run skill and plugin validators**

Run:

```text
<PYTHON> -m pytest tests/test_skill_contract.py tests/test_plugin_layout.py -q
<PYTHON> <SKILL_CREATOR_DIR>/scripts/quick_validate.py skills/pdf-translator
<PYTHON> <PLUGIN_CREATOR_DIR>/scripts/validate_plugin.py .
```

Expected: tests and both validators pass.

- [ ] **Step 8: Commit**

```bash
git add skills/pdf-translator/SKILL.md .codex-plugin/plugin.json README.md tests/test_skill_contract.py tests/test_plugin_layout.py
git commit -m "feat: add PDF translator workflow"
```

### Task 12: Deterministic acceptance fixtures, full regression, and release

**Files:**
- Create: `tests/fixtures/pdf/technical-document-v1/*`
- Create: `tests/fixtures/pdf/table-report-v1/*`
- Create: `tests/fixtures/pdf/two-column-footnotes-v1/*`
- Create: `tests/fixtures/pdf/figures-captions-v1/*`
- Create: `tests/fixtures/pdf/rejections-v1/*`
- Create: `.github/workflows/pdf-cross-platform.yml`
- Modify: `tests/pdf_fixtures.py`
- Modify: `tests/test_pdf_pipeline.py`
- Modify: `tests/test_pipeline.py`
- Modify: `.gitignore`
- Modify: `.codex-plugin/plugin.json`
- Modify: `pyproject.toml`
- Modify: `src/web_translator/__init__.py`

**Interfaces:**
- Produces: reproducible end-to-end acceptance evidence and release `0.3.0`.
- Consumes: every prior PDF and HTML interface.

- [ ] **Step 1: Generate committed deterministic fixtures and expected records**

Use `tests/pdf_fixtures.py` to generate text-based source PDFs and compact JSON expected
metadata. Include technical prose, multi-page/merged tables, two columns/footnotes,
raster/vector figures/captions, an image-only scan, encrypted input, malformed input, and
a copy under a Korean/space path. Store known Korean translation JSONL, glossary, semantic
review, and visual review for successful fixtures.

- [ ] **Step 2: Add complete local-input pipeline tests**

For every accepted fixture run acquire, extract, shared planning, assignment preparation,
translation validation, assembly, QA prepare, copy reviewed visual evidence for the current
staged hash, and QA finalize. Assert selectable Korean, exact first-use glossary placement,
table/figure counts, all-page contact coverage, three final artifacts, deterministic
manifest fields, and no staging directory after success.

- [ ] **Step 3: Add controlled remote-input and rejection pipeline tests**

Use a local fixture server only for transport behavior and keep public-target validation
unit-tested with mocked DNS. Assert scanned, encrypted, malformed, size, page, ambiguous
column, ambiguous table, missing Poppler, stale review, and output collision cases return
the documented nonzero code and never create final output.

- [ ] **Step 4: Prove HTML compatibility**

Run:

```text
<PYTHON> -m pytest tests/test_capture.py tests/test_extract.py tests/test_assemble.py tests/test_qa.py tests/test_pipeline.py -q
```

Expected: every existing HTML test passes unchanged except deliberate plugin-description
assertions updated in Task 11.

- [ ] **Step 5: Run the complete suite and inspect every acceptance page**

Run: `<PYTHON> -m pytest -q`

Render all accepted final fixture PDFs with Poppler and inspect all contact sheets. Require
zero required findings, no black-square glyphs, no clipping or overlap, readable tables,
and correct figure-caption pairing.

- [ ] **Step 6: Run packaging and platform validation**

Build wheel and source distribution, install the wheel into a fresh temporary environment,
assert both font resources exist through `importlib.resources`, run CLI help, and execute
one small PDF pipeline on macOS. Add `.github/workflows/pdf-cross-platform.yml` with a
required two-OS matrix and the exact platform setup below:

```yaml
name: PDF cross-platform
on: [pull_request, push]
jobs:
  package-smoke:
    strategy:
      fail-fast: false
      matrix:
        os: [macos-15, windows-2025]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - if: runner.os == 'macOS'
        run: brew install poppler
      - if: runner.os == 'Windows'
        run: choco install poppler -y
      - run: python -m pip install --upgrade pip build
      - run: python -m build
      - shell: bash
        run: python -m pip install "$(find dist -name '*.whl' -print -quit)[test]"
        if: runner.os == 'macOS'
      - shell: pwsh
        run: |
          $wheel = Get-ChildItem dist -Filter *.whl | Select-Object -First 1
          python -m pip install "$($wheel.FullName)[test]"
        if: runner.os == 'Windows'
      - run: python -m pytest tests/test_pdf_pipeline.py tests/test_plugin_layout.py -q
```

Protect the release branch so `package-smoke (macos-15)` and
`package-smoke (windows-2025)` must both pass before release. The plan is not complete if
either operating system result is absent, skipped, or failing.

- [ ] **Step 7: Bump the compatible feature release**

Run: `<PYTHON> scripts/version.py set 0.3.0`

Then run:

```text
<PYTHON> scripts/version.py check
<PYTHON> <SKILL_CREATOR_DIR>/scripts/quick_validate.py skills/web-translator
<PYTHON> <SKILL_CREATOR_DIR>/scripts/quick_validate.py skills/pdf-translator
<PYTHON> <PLUGIN_CREATOR_DIR>/scripts/validate_plugin.py .
git diff --check
```

Expected: all versions report `0.3.0`, both skills and the plugin validate, and the diff
check is clean.

- [ ] **Step 8: Request final code review and resolve every Critical/Important finding**

Use `superpowers:requesting-code-review` with the design, this plan, task commits, full test
evidence, package smoke evidence, and rendered contact-sheet evidence. Apply feedback via
`superpowers:receiving-code-review`, rerun affected tests, and request re-review.

- [ ] **Step 9: Commit the release**

```bash
git add .codex-plugin/plugin.json pyproject.toml src/web_translator/__init__.py .gitignore .github/workflows/pdf-cross-platform.yml tests/fixtures/pdf tests/pdf_fixtures.py tests/test_pdf_pipeline.py tests/test_pipeline.py
git commit -m "Release web-translator 0.3.0"
```

- [ ] **Step 10: Final verification before integration**

Use `superpowers:verification-before-completion`. Freshly rerun the full suite, both skill
validators, plugin validator, version check, package build/install smoke, and `git status`.
Do not claim completion or push until every required command exits zero and the worktree
contains only intended commits.
