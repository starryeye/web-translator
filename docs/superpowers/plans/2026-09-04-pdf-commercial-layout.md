# Commercial-Quality PDF Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce publication-quality Korean PDFs that preserve editorial semantics, keep selectable source text out of images, and fail QA when layout quality regresses.

**Architecture:** Extend the PDF block and layout contracts with semantic roles, classify editorial structures before segmentation, and render those roles through dedicated ReportLab flowables. Keep terminology normalization shared and policy-driven, then make automated and visual QA verify the new semantic contract before publication.

**Tech Stack:** Python 3.11+, dataclasses, pdfplumber, pypdf, ReportLab Platypus, Pillow, Poppler, pytest

**Spec:** `docs/superpowers/specs/2026-09-04-pdf-commercial-layout-design.md`

## Global Constraints

- Preserve the source PDF trim dimensions; Korean reflow may change page count and line breaks.
- Support at most 500 pages and 50 MiB per source PDF.
- Support selectable-text PDFs only; keep the existing explicit scanned-PDF rejection.
- Preserve true images and graphical artwork; translate selectable body, caption, callout, table, reference, and footnote text.
- Keep `web-translator` and `pdf-translator` as separate skills while sharing translation and terminology modules.
- A successful final directory contains exactly `translated.pdf`, `manifest.json`, and `review-report.md`.
- Do not overwrite the existing 50-page diagnostic run or final output.
- Existing webpage behavior remains unchanged under the `english-first` terminology policy.
- Every behavior change follows red-green-refactor TDD.
- Run `git push origin main` immediately after every commit, as requested by the user.
- Do not publish version `0.6.0` until focused tests, the full suite, and real-document acceptance all pass.

---

## File Structure

- Modify `src/web_translator/pdf_models.py`: semantic-role vocabulary, document schema 1.1, and schema 1.0 compatibility upgrade.
- Modify `src/web_translator/pdf_layout.py`: line repair, running furniture, TOC, opener, callout, and reference classification.
- Modify `src/web_translator/pdf_media.py`: text-aware graphic-region evidence and decorated-text exclusion.
- Modify `src/web_translator/pdf_extract.py`: propagate roles, enforce selectable-text ownership, and build role-preserving segments.
- Modify `src/web_translator/terminology.py`: shared `english-first` and `korean-first` display policies.
- Modify `src/web_translator/pdf_assemble.py`: source-trim page size, role-specific flowables, grouped content, two-pass TOC, and metadata-only provenance.
- Modify `src/web_translator/pdf_flowables.py`: persist semantic role and output anchor/page evidence.
- Modify `src/web_translator/pdf_qa.py`: automated publication-quality gates and expanded visual-review dimensions.
- Modify `src/web_translator/pdf_report.py`: terminology policy and semantic-layout metrics in the manifest/report.
- Modify `src/web_translator/pdf_review.py`: identify the PDF Korean-first policy in semantic review input.
- Modify `src/web_translator/cli.py`: use the workflow-specific terminology policy identifier.
- Modify `skills/pdf-translator/SKILL.md`: instruct visual review over all thirteen canonical dimensions.
- Modify `tests/pdf_fixtures.py`: semantic fixtures and schema helpers.
- Modify `tests/test_pdf_models_paths.py`: schema and role contract tests.
- Modify `tests/test_pdf_extract.py`: semantic classification, line repair, and extraction ownership tests.
- Modify `tests/test_pdf_media.py`: selectable-text/graphic partition tests.
- Modify `tests/test_assemble.py`: terminology policy tests.
- Modify `tests/test_pdf_assemble.py`: source-trim, opener, callout, reference, TOC, footnote, and provenance tests.
- Modify `tests/test_pdf_qa.py`: new automated QA gate tests.
- Modify `tests/test_pdf_pipeline.py`: end-to-end semantic fixture and exact final-artifact tests.
- Modify `tests/test_skill_contract.py`: thirteen-dimension PDF skill contract.
- Modify `README.md`: publication-quality behavior and limitations.
- Modify `.codex-plugin/plugin.json`, `pyproject.toml`, and `src/web_translator/__init__.py`: release `0.6.0` only after acceptance.

---

### Task 1: Add semantic roles and schema 1.1 compatibility

**Files:**
- Modify: `src/web_translator/pdf_models.py` (`PdfBlock`, `PdfDocument.from_dict`, schema validation helpers)
- Modify: `tests/pdf_fixtures.py` (`make_pdf_block`, `make_pdf_document`)
- Test: `tests/test_pdf_models_paths.py`

**Interfaces:**
- Produces: `PdfSemanticRole`, `PDF_DOCUMENT_SCHEMA_VERSION`, `PdfBlock.semantic_role`, and `upgrade_pdf_document_v1(data: Mapping[str, Any]) -> dict[str, Any]`.
- Consumes: the existing strict `PdfBlock`/`PdfDocument` JSON contracts.

- [ ] **Step 1: Write failing schema and role tests**

```python
def test_pdf_block_requires_a_supported_semantic_role() -> None:
    payload = make_pdf_block().to_dict()
    payload["semantic_role"] = "magazine-sidebar"
    with pytest.raises(PdfContractError, match="semantic_role is not supported"):
        PdfBlock.from_dict(payload)


def test_pdf_document_upgrades_schema_1_blocks_to_body_role() -> None:
    payload = make_pdf_document().to_dict()
    payload["schema_version"] = "1.0"
    for block in payload["blocks"]:
        block.pop("semantic_role", None)
    loaded = PdfDocument.from_dict(payload)
    assert loaded.schema_version == "1.1"
    assert [block.semantic_role for block in loaded.blocks] == ["body"]
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `pytest tests/test_pdf_models_paths.py -q`

Expected: FAIL because `semantic_role` and the schema 1.0 upgrade do not exist.

- [ ] **Step 3: Add the strict role vocabulary and compatibility upgrade**

```python
PdfSemanticRole = Literal[
    "body", "toc-title", "toc-part", "toc-chapter", "toc-entry",
    "dedication", "epigraph", "epigraph-attribution", "part-label",
    "part-title", "chapter-label", "chapter-title", "callout-title",
    "callout-body", "reference-heading", "reference-entry",
]
PDF_DOCUMENT_SCHEMA_VERSION = "1.1"


def upgrade_pdf_document_v1(data: Mapping[str, Any]) -> dict[str, Any]:
    upgraded = dict(data)
    upgraded["schema_version"] = PDF_DOCUMENT_SCHEMA_VERSION
    upgraded["blocks"] = [
        {**dict(block), "semantic_role": dict(block).get("semantic_role", "body")}
        for block in data["blocks"]
    ]
    return upgraded
```

Add `semantic_role: PdfSemanticRole = "body"` to `PdfBlock`, serialize it, validate it
strictly, and make `PdfDocument.from_dict` upgrade only root schema `1.0`. Reject any
other version. New extractors must write `1.1`; source, review, manifest, and unrelated
contracts remain at their current versions.

- [ ] **Step 4: Update fixture constructors and run focused model tests**

Run: `pytest tests/test_pdf_models_paths.py tests/test_pdf_extract.py -q`

Expected: PASS, including unchanged direct `PdfBlock` constructor callers through the `body`
default.

- [ ] **Step 5: Commit and push**

```bash
git add src/web_translator/pdf_models.py tests/pdf_fixtures.py tests/test_pdf_models_paths.py
git commit -m "feat: add PDF semantic role contract"
git push origin main
```

---

### Task 2: Repair line fragments and running furniture

**Files:**
- Modify: `src/web_translator/pdf_layout.py` (`classify_document_lines`, running-band helpers, paragraph merge helpers)
- Test: `tests/test_pdf_extract.py`

**Interfaces:**
- Produces: `repair_line_fragments(lines: Sequence[PdfLine]) -> list[PdfLine]` and complete running-furniture classification before block construction.
- Consumes: `PdfLine` geometry, font evidence, page dimensions, and neighboring page lines.

- [ ] **Step 1: Add failing tests for adjacent-page furniture inheritance and split words**

```python
def test_running_band_inherits_one_missing_composite_page_from_neighbors() -> None:
    pages = _running_pages(
        "26 | Chapter 2: Data Models and Query Languages",
        "Chapter 2: Data Models and Query Languages | 27",
        "28 | Chapter 2: Data Models and Query Languages",
    )
    classified = classify_document_lines(pages)
    assert [page[-1].kind for page in classified] == ["footer", "footer", "footer"]


def test_repair_line_fragments_joins_discretionary_hyphen_and_font_run() -> None:
    lines = [_line("Post‐", x0=90, x1=118), _line("greSQL [6, 7].", x0=90, x1=170)]
    repaired = repair_line_fragments(lines)
    assert [line.text for line in repaired] == ["PostgreSQL [6, 7]."]
```

Also test that separate columns, list markers, and ordinary hyphenated compounds are not
joined.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `pytest tests/test_pdf_extract.py -k 'running_band or repair_line_fragments' -q`

Expected: FAIL on the inherited composite line and both repair cases.

- [ ] **Step 3: Implement geometry-bounded line repair**

```python
_DISCRETIONARY_HYPHENS = "\u00ad\u2010\u2011\u2012\u2013\u2014\u2015"


def repair_line_fragments(lines: Sequence[PdfLine]) -> list[PdfLine]:
    repaired: list[PdfLine] = []
    for line in lines:
        if repaired and _is_wrapped_token_continuation(repaired[-1], line):
            repaired[-1] = _join_wrapped_lines(repaired[-1], line)
        else:
            repaired.append(line)
    return repaired
```

Require matching column x-range, normal leading, compatible font size, a preceding
discretionary hyphen or alphabetic token boundary, and a lowercase continuation. Remove
only the discretionary line-end hyphen. Invoke repair before heading/list classification.

- [ ] **Step 4: Extend running-band grouping**

Normalize composite labels separately from page tokens and permit one missing or
unclassified member only when both neighboring pages establish the same sequential
family and compatible top/bottom band. Mark the whole line as header/footer; do not split
its page token into a body block.

- [ ] **Step 5: Run extraction regressions**

Run: `pytest tests/test_pdf_extract.py -q`

Expected: PASS.

- [ ] **Step 6: Commit and push**

```bash
git add src/web_translator/pdf_layout.py tests/test_pdf_extract.py
git commit -m "fix: repair PDF lines and running furniture"
git push origin main
```

---

### Task 3: Classify TOC, opener, epigraph, and reference structures

**Files:**
- Modify: `src/web_translator/pdf_layout.py` (`PdfLine`, semantic classifiers, `build_text_blocks`)
- Modify: `src/web_translator/pdf_extract.py` (document-wide semantic pass)
- Modify: `tests/pdf_fixtures.py` (publication-layout fixture generator)
- Test: `tests/test_pdf_extract.py`

**Interfaces:**
- Produces: `classify_semantic_roles(pages: Sequence[Sequence[PdfLine]]) -> list[list[PdfLine]]` and role-preserving `PdfBlock` values.
- Consumes: Task 1 roles and Task 2 repaired/furniture-classified lines.

- [ ] **Step 1: Generate a deterministic semantic fixture**

Add `make_publication_structure_pdf(path: Path) -> Path` using ReportLab canvas. It must
contain three TOC entries with a separate right-aligned page-number column, a centered
dedication and epigraph, a sparse `PART I` opener, a `CHAPTER 2` opener, and three
bracketed references spanning two pages.

- [ ] **Step 2: Add failing extraction assertions**

```python
def test_extract_pdf_preserves_publication_semantic_roles(tmp_path: Path) -> None:
    document = extract_pdf(
        make_publication_structure_pdf(tmp_path / "publication.pdf"),
        tmp_path / "document.json",
        tmp_path / "segments.jsonl",
        tmp_path / "media",
    )
    roles = [block.semantic_role for block in document.blocks]
    assert roles.count("toc-entry") == 3
    assert roles.count("dedication") == 1
    assert roles.count("epigraph") == 1
    assert roles.count("epigraph-attribution") == 1
    assert roles.count("part-label") == 1
    assert roles.count("part-title") == 1
    assert roles.count("chapter-label") == 1
    assert roles.count("chapter-title") == 1
    assert roles.count("reference-entry") == 3
```

Assert each TOC number is attached to one entry, references do not merge, and continuation
text attaches to the correct reference across the page boundary.

- [ ] **Step 3: Run the test and verify it fails**

Run: `pytest tests/test_pdf_extract.py::test_extract_pdf_preserves_publication_semantic_roles -q`

Expected: FAIL because all specialized content currently remains generic.

- [ ] **Step 4: Implement deterministic role classification**

Add `semantic_role` to `PdfLine`, defaulting to `body`, and preserve it when lines merge
into a block. Run classifiers in this order:

```python
pages = _classify_page_numbers(pages)
pages = _classify_running_bands(pages)
pages = _classify_repeated_bands(pages)
pages = _classify_toc_structure(pages)
pages = _classify_sparse_openers(pages)
pages = _classify_epigraphs(pages)
pages = _classify_reference_sections(pages)
```

TOC matching must pair entry and numeric columns by vertical row overlap and indentation.
Reference merging must stop at every new bracketed/numbered marker. Sparse opener rules
must require large type, page whitespace, and recognized label/title proximity.

- [ ] **Step 5: Preserve roles through blocks and segments**

`build_text_blocks` must reject a merge across distinct roles. `_build_segments` keeps the
existing semantic type derived from `kind`; `semantic_role` remains PDF metadata so the
shared segment schema is unchanged.

- [ ] **Step 6: Run focused and complete extraction tests**

Run: `pytest tests/test_pdf_extract.py tests/test_pdf_models_paths.py -q`

Expected: PASS.

- [ ] **Step 7: Commit and push**

```bash
git add src/web_translator/pdf_layout.py src/web_translator/pdf_extract.py tests/pdf_fixtures.py tests/test_pdf_extract.py
git commit -m "feat: classify PDF publication structures"
git push origin main
```

---

### Task 4: Keep selectable callout text out of figure images

**Files:**
- Modify: `src/web_translator/pdf_media.py` (`FigureRegion`, `detect_figure_regions`)
- Modify: `src/web_translator/pdf_extract.py` (`_extract_page_materials`, character ownership)
- Modify: `src/web_translator/pdf_layout.py` (callout role classification)
- Test: `tests/test_pdf_media.py`
- Test: `tests/test_pdf_extract.py`

**Interfaces:**
- Produces: `FigureRegion.owned_selectable_characters`, `partition_graphic_regions` returning `GraphicPartition`, and `callout-title`/`callout-body` blocks.
- Consumes: source raster/vector boxes, raw character boxes, and table exclusions.

- [ ] **Step 1: Add a decorated-callout fixture and failing media test**

```python
def test_detect_figure_regions_excludes_selectable_text_inside_vector_box() -> None:
    page = _page_with_box_icon_and_selectable_callout()
    partition = partition_graphic_regions(page, page_number=1)
    assert partition.figures == [FigureRegion(1, ICON_BBOX, 612.0, 792.0)]
    assert partition.decorated_text_bboxes == [CALLOUT_BBOX]
```

The fixture must draw a border/fill, a small vector icon, a bold callout title, and
selectable body text. Add a companion raster-chart fixture proving genuine chart labels
that are part of a raster image stay in the image.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `pytest tests/test_pdf_media.py -k 'selectable_text_inside_vector_box or raster_chart' -q`

Expected: FAIL because the complete vector box is currently one figure crop.

- [ ] **Step 3: Partition graphic candidates with text ownership evidence**

```python
@dataclass(frozen=True, slots=True)
class GraphicPartition:
    figures: Sequence[FigureRegion]
    decorated_text_bboxes: Sequence[BBox]


def partition_graphic_regions(page: object, *, page_number: int,
                              excluded_bboxes: Sequence[BBox] = ()) -> GraphicPartition:
    characters = _selectable_character_bboxes(page)
    candidates = _graphic_candidates(page, excluded_bboxes)
    # Vector containers that enclose translatable character runs become decoration.
    # Independent raster/vector art remains a figure.
```

Use overlap, enclosure, object type, text density, and icon size. Fail when a candidate
cannot be partitioned without consuming translatable characters. Do not infer from color
or publisher-specific artwork.

- [ ] **Step 4: Propagate decorated text as callout roles**

Pass decorated-text bounds into line classification. Use bold/size evidence to assign
`callout-title`; remaining enclosed prose becomes `callout-body`. Keep any adjacent small
icon as a figure ordered immediately before the callout group.

- [ ] **Step 5: Enforce character ownership**

Count characters owned by a figure only when the accepted figure itself owns them.
Selectable characters in decorated text must pass through normal line/block extraction.
Raise `PdfExtractionError` with page and bounds when any character is simultaneously
owned by figure and translatable text.

- [ ] **Step 6: Run media and extraction suites**

Run: `pytest tests/test_pdf_media.py tests/test_pdf_extract.py -q`

Expected: PASS.

- [ ] **Step 7: Commit and push**

```bash
git add src/web_translator/pdf_media.py src/web_translator/pdf_extract.py src/web_translator/pdf_layout.py tests/test_pdf_media.py tests/test_pdf_extract.py
git commit -m "fix: preserve selectable PDF callout text"
git push origin main
```

---

### Task 5: Add shared Korean-first terminology rendering

**Files:**
- Modify: `src/web_translator/terminology.py`
- Modify: `src/web_translator/pdf_assemble.py`
- Modify: `src/web_translator/pdf_review.py`
- Modify: `src/web_translator/pdf_report.py`
- Modify: `src/web_translator/cli.py`
- Test: `tests/test_assemble.py`
- Test: `tests/test_pdf_assemble.py`
- Test: `tests/test_pdf_review.py`

**Interfaces:**
- Produces: `TerminologyDisplayPolicy = Literal["english-first", "korean-first"]` and `normalize_terminology` returning `list[Translation]` for an explicit policy.
- Consumes: ordered translations, canonical glossary, and protected-token metadata.

- [ ] **Step 1: Add failing policy tests**

```python
def test_korean_first_uses_bilingual_first_occurrence_then_korean() -> None:
    records = [
        translation("a", "OAuth 기능을 사용한다."),
        translation("b", "OAuth 기능이 작동한다."),
    ]
    actual = normalize_terminology(records, {"OAuth": "권한 위임"}, policy="korean-first")
    assert [item.text for item in actual] == [
        "권한 위임(OAuth) 기능을 사용한다.",
        "권한 위임 기능이 작동한다.",
    ]


def test_english_first_remains_backward_compatible() -> None:
    actual = normalize_terminology(
        [translation("a", "OAuth")], {"OAuth": "권한 위임"}, policy="english-first"
    )
    assert actual[0].text == "OAuth(권한 위임)"
```

Use grammatically complete Korean test sentences rather than expecting the normalizer to
invent particles. Add protected product, acronym, identifier, URL, code, and number cases.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `pytest tests/test_assemble.py -k 'korean_first or english_first' -q`

Expected: FAIL because only `normalize_first_use` exists.

- [ ] **Step 3: Implement one configurable normalizer**

```python
TerminologyDisplayPolicy = Literal["english-first", "korean-first"]


def normalize_terminology(
    ordered: Sequence[Translation],
    glossary: Mapping[str, str],
    *,
    policy: TerminologyDisplayPolicy,
    protected_by_segment: Mapping[str, Sequence[ProtectedToken]] | None = None,
) -> list[Translation]:
    if policy not in {"english-first", "korean-first"}:
        raise TerminologyError(f"unsupported terminology display policy: {policy}")
    first_format = (
        (lambda term, gloss: f"{term}({gloss})")
        if policy == "english-first"
        else (lambda term, gloss: f"{gloss}({term})")
    )
    later_format = (
        (lambda term, _gloss: term)
        if policy == "english-first"
        else (lambda _term, gloss: gloss)
    )
    return _normalize_policy_records(
        ordered,
        glossary,
        protected_by_segment=protected_by_segment,
        first_format=first_format,
        later_format=later_format,
    )
```

Rename the current record-walking implementation to `_normalize_policy_records`, add the
`first_format` and `later_format` callables shown above where replacements are built, and
keep `normalize_first_use` as a compatibility wrapper selecting `english-first`.
Replacement operates only on exact glossary term boundaries outside opaque placeholders.
Korean-first replaces later visible term occurrences with the Korean gloss; it leaves
protected values and numbers byte-for-byte unchanged.

- [ ] **Step 4: Wire workflow-specific policy identifiers**

PDF assembly/review/report must use `korean-first-technical-terms` version `2.0`. Webpage
commands keep `english-technical-first-use-ko-gloss` version `1.0`. The semantic review
digest must include the selected PDF policy exactly as it does today.

- [ ] **Step 5: Run shared and PDF terminology tests**

Run: `pytest tests/test_assemble.py tests/test_pdf_assemble.py tests/test_pdf_review.py tests/test_pipeline.py -q`

Expected: PASS with unchanged webpage snapshots.

- [ ] **Step 6: Commit and push**

```bash
git add src/web_translator/terminology.py src/web_translator/pdf_assemble.py src/web_translator/pdf_review.py src/web_translator/pdf_report.py src/web_translator/cli.py tests/test_assemble.py tests/test_pdf_assemble.py tests/test_pdf_review.py
git commit -m "feat: add Korean-first terminology policy"
git push origin main
```

---

### Task 6: Preserve source trim size and render semantic roles

**Files:**
- Modify: `src/web_translator/pdf_flowables.py` (`PdfPageSize`, `PdfFlowableLayout`)
- Modify: `src/web_translator/pdf_assemble.py` (`_select_page_size`, role styles, story construction)
- Test: `tests/test_pdf_assemble.py`

**Interfaces:**
- Produces: `PdfPageSize.name == "SOURCE"`, `PdfFlowableLayout.semantic_role`, and role-aware flowable builders.
- Consumes: Task 1 semantic blocks and Task 5 normalized translations.

- [ ] **Step 1: Add failing source-trim and role-layout tests**

```python
def test_assembly_preserves_source_trim_and_starts_openers_on_fresh_pages(tmp_path: Path) -> None:
    run_dir, translations, glossary = _publication_assembly_run(tmp_path, width=504, height=661.5)
    output = assemble_pdf(run_dir, tmp_path / "out", translations, glossary)
    reader = PdfReader(output)
    assert float(reader.pages[0].mediabox.width) == pytest.approx(504)
    assert float(reader.pages[0].mediabox.height) == pytest.approx(661.5)
    layout = read_pdf_layout(run_dir / "layout.json")
    opener_pages = {
        item.page_number for item in layout.flowables
        if item.semantic_role in {"part-label", "part-title", "chapter-label", "chapter-title"}
    }
    assert len(opener_pages) == 2
```

Add assertions for dedication/epigraph grouping, callout icon/title/body adjacency,
reference hanging indent, and absence of a visible `Source:`/`Generated:` block.

- [ ] **Step 2: Run focused assembly tests and verify they fail**

Run: `pytest tests/test_pdf_assemble.py -k 'source_trim or opener or callout or reference or visible_provenance' -q`

Expected: FAIL because assembly currently chooses A4/LETTER and uses generic styles.

- [ ] **Step 3: Extend layout evidence**

Allow page-size name `SOURCE`, add `semantic_role` to each tracked flowable, and advance
layout evidence to schema `1.1`. Read schema `1.0` by assigning `body` only for legacy
diagnostics; new assembly always writes `1.1`.

- [ ] **Step 4: Select the median source trim directly**

```python
def _select_page_size(document: PdfDocument) -> PdfPageSize:
    width = statistics.median(page.width for page in document.pages)
    height = statistics.median(page.height for page in document.pages)
    return PdfPageSize(name="SOURCE", width=width, height=height)
```

Use the dominant portrait trim as the reflow canvas and retain the existing explicit
landscape template for wide tables/figures; do not silently normalize the document to A4
or Letter.

- [ ] **Step 5: Add dedicated role flowables**

Create focused helpers named `_append_opener_group`, `_append_epigraph_group`,
`_append_callout_group`, and `_reference_style`. Each append helper receives the story,
the exact block sequence, translated text mapping, link mapping, frame, layout records,
and part counters; `_reference_style` receives one `PdfBlock` and returns a
`ParagraphStyle`.

```python
if block.semantic_role in _OPENER_ROLES:
    opener = _consume_role_group(document.blocks, block.order, _OPENER_ROLES)
    _append_opener_group(
        story, opener, translated, links_by_block, portrait_frame, records, part_counters
    )
    emitted_block_ids.update(item.id for item in opener)
```

Use `PageBreak`, `KeepTogether`, `KeepWithNext`, hanging indents, and Korean-appropriate
leading. Preserve source opener behavior: introductory prose remains on the opener only
when its source page shares that structure. Remove `_source_attribution` from the visible
story and retain provenance in PDF metadata.

- [ ] **Step 6: Run assembly and layout-contract tests**

Run: `pytest tests/test_pdf_assemble.py tests/test_pdf_models_paths.py -q`

Expected: PASS.

- [ ] **Step 7: Commit and push**

```bash
git add src/web_translator/pdf_flowables.py src/web_translator/pdf_assemble.py tests/test_pdf_assemble.py
git commit -m "feat: render semantic PDF layouts"
git push origin main
```

---

### Task 7: Resolve translated TOC pages and footnote continuations

**Files:**
- Modify: `src/web_translator/pdf_assemble.py` (indexing flowables, document template, footnotes)
- Modify: `src/web_translator/pdf_flowables.py` (anchor and continuation evidence)
- Test: `tests/test_pdf_assemble.py`

**Interfaces:**
- Produces: `ResolvedTocEntry`, `PublicationDocTemplate`, output anchor-page mappings, and explicit footnote continuation records.
- Consumes: TOC roles/destinations from Task 3 and source-trim templates from Task 6.

- [ ] **Step 1: Add failing two-pass TOC tests**

```python
def test_toc_prints_output_anchor_page_after_korean_reflow(tmp_path: Path) -> None:
    run_dir, translations, glossary = _toc_assembly_run(tmp_path, long_korean_body=True)
    output = assemble_pdf(run_dir, tmp_path / "out", translations, glossary)
    layout = read_pdf_layout(run_dir / "layout.json")
    chapter = next(item for item in layout.flowables if item.semantic_role == "chapter-title")
    with pdfplumber.open(output) as pdf:
        toc_text = pdf.pages[0].extract_text()
    assert f"제2장{'.' * 3}{chapter.page_number}" in _normalize_dot_leader(toc_text)
```

Add a partial-input TOC test where an absent destination retains its source number and
records one `toc-target-outside-input` warning. Add a long footnote test asserting an
explicit `각주 계속` label and no overlap.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `pytest tests/test_pdf_assemble.py -k 'toc_prints_output or outside_input or footnote_continuation' -q`

Expected: FAIL because TOC numeric columns and continuation flowables do not exist.

- [ ] **Step 3: Add an indexing document template**

```python
class PublicationDocTemplate(BaseDocTemplate):
    def afterFlowable(self, flowable: Flowable) -> None:
        if isinstance(flowable, TrackedFlowable) and flowable.anchor_name:
            self.anchor_pages[flowable.anchor_name] = self.page


class ResolvedTocEntry(IndexingFlowable):
    def isSatisfied(self) -> bool:
        return self._rendered_page == self.anchor_pages.get(self.destination)
```

Build with `multiBuild`, cap passes using the existing retry/fail-closed conventions, and
reset transient layout records at the start of each pass so only final-pass evidence is
persisted. Link matched entries to the output anchor. Keep unmatched out-of-input entries
unlinked with their source reference.

- [ ] **Step 4: Add split footnote continuation flowables**

Measure footnote frame capacity before drawing. When a note cannot fit, split at paragraph
boundaries or ReportLab-supported line boundaries, draw the first part with its owner,
and emit remaining parts on the next page under `각주 계속`. Record a monotonically
increasing `split_part` for QA.

- [ ] **Step 5: Run assembly tests**

Run: `pytest tests/test_pdf_assemble.py -q`

Expected: PASS.

- [ ] **Step 6: Commit and push**

```bash
git add src/web_translator/pdf_assemble.py src/web_translator/pdf_flowables.py tests/test_pdf_assemble.py
git commit -m "feat: resolve PDF contents and footnotes"
git push origin main
```

---

### Task 8: Enforce publication-quality automated and visual QA

**Files:**
- Modify: `src/web_translator/pdf_qa.py`
- Modify: `src/web_translator/pdf_report.py`
- Modify: `skills/pdf-translator/SKILL.md`
- Modify: `tests/pdf_fixtures.py`
- Test: `tests/test_pdf_qa.py`
- Test: `tests/test_skill_contract.py`

**Interfaces:**
- Produces: thirteen canonical visual dimensions and deterministic semantic QA findings.
- Consumes: schema 1.1 document/layout evidence, staged PDF text, translations, media, and contact sheets.

- [ ] **Step 1: Add failing QA tests for each new gate**

```python
NEW_VISUAL_DIMENSIONS = {
    "semantic_structure", "toc_navigation", "reference_formatting",
    "text_image_separation", "terminology_readability",
}


def test_prepare_pdf_qa_rejects_opener_not_starting_a_page(pdf_run: PdfQARun) -> None:
    _move_role_to_existing_body_page(pdf_run, "chapter-title")
    with pytest.raises(PdfQAFailure, match="chapter opener must start a fresh page"):
        prepare_pdf_qa(pdf_run.run_dir, pdf_run.output_dir)
```

Add one focused test for TOC page mismatch, merged reference entries, running-furniture
body flowable, translatable text represented only by a figure, visible provenance,
detached callout content, and excessive Latin density after exclusions.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `pytest tests/test_pdf_qa.py -k 'opener or toc_page or reference_entry or running_furniture or figure_only or provenance or callout or latin_density' -q`

Expected: FAIL because the gates do not exist.

- [ ] **Step 3: Implement semantic layout gates**

Add pure checks named `_validate_opener_pages`, `_validate_toc_pages`,
`_validate_reference_layout`, `_validate_text_image_separation`,
`_validate_running_furniture`, and `_validate_latin_density`. The first, third, fourth,
and fifth receive `PdfDocument` plus `PdfAssemblyLayout`; TOC validation also receives the
opened `PdfReader`; Latin-density validation receives the document plus the translation
mapping.

```python
_validate_opener_pages(document, layout)
_validate_toc_pages(document, layout, staged_reader)
_validate_reference_layout(document, layout)
_validate_text_image_separation(document, layout)
_validate_running_furniture(document, layout)
_validate_latin_density(document, translations_by_segment)
```

The prose Latin-density limit is 35 percent after excluding protected products, code,
identifiers, URLs, acronyms, and `reference-entry` blocks. Evidence names page, block,
ratio, and exclusions. Do not apply the ratio to captions containing source figure labels
or to bibliographic records.

- [ ] **Step 4: Expand the visual review contract and skill instructions**

Keep the existing eight dimensions and add the five new dimensions exactly. Update JSON
examples, fixture reviews, canonical field validation, and instructions requiring every
rendered page to be checked. A stale eight-dimension review must fail under the new run
contract; legacy runs remain diagnostics only.

- [ ] **Step 5: Add report evidence**

Include counts by semantic role, terminology policy/version, TOC resolution warnings,
and all thirteen visual findings in `manifest.json` and `review-report.md`. Preserve exact
final output names and deterministic ordering.

- [ ] **Step 6: Run QA, report, skill, and pipeline tests**

Run: `pytest tests/test_pdf_qa.py tests/test_skill_contract.py tests/test_pdf_pipeline.py -q`

Expected: PASS.

- [ ] **Step 7: Commit and push**

```bash
git add src/web_translator/pdf_qa.py src/web_translator/pdf_report.py skills/pdf-translator/SKILL.md tests/pdf_fixtures.py tests/test_pdf_qa.py tests/test_skill_contract.py tests/test_pdf_pipeline.py
git commit -m "feat: enforce publication-quality PDF QA"
git push origin main
```

---

### Task 9: Add end-to-end publication fixture and documentation

**Files:**
- Modify: `tests/pdf_fixtures.py`
- Modify: `tests/test_pdf_pipeline.py`
- Modify: `README.md`
- Modify: `skills/pdf-translator/SKILL.md`

**Interfaces:**
- Produces: a deterministic end-to-end publication fixture and documented user contract.
- Consumes: all implementation tasks above.

- [ ] **Step 1: Add a failing end-to-end fixture test**

```python
def test_publication_fixture_publishes_exact_artifacts_with_semantics(tmp_path: Path) -> None:
    result = run_publication_fixture_pipeline(tmp_path)
    assert sorted(path.name for path in result.output_dir.iterdir()) == [
        "manifest.json", "review-report.md", "translated.pdf"
    ]
    manifest = json.loads((result.output_dir / "manifest.json").read_text())
    assert manifest["layout"]["semantic_roles"]["toc-entry"] == 3
    assert manifest["layout"]["semantic_roles"]["reference-entry"] == 3
    assert manifest["translation"]["terminology"]["policy_id"] == "korean-first-technical-terms"
```

The fixture must exercise a TOC, dedication, epigraph, part/chapter opener, selectable
callout, independent icon, figure/caption, references, footnote continuation, and Korean
term reuse without network or model calls.

- [ ] **Step 2: Run the test and verify it fails**

Run: `pytest tests/test_pdf_pipeline.py::test_publication_fixture_publishes_exact_artifacts_with_semantics -q`

Expected: FAIL until all fixture evidence and report fields are wired.

- [ ] **Step 3: Complete fixture evidence and user documentation**

Update README and the PDF skill to state source-trim preservation, semantic structures,
Korean-first terms, selectable callout text, 500-page limit, scanned-PDF rejection, and
the three-file output contract. Do not change webpage command selection.

- [ ] **Step 4: Run the complete deterministic suite**

Run: `pytest -q`

Expected: `0 failed`; existing environment-dependent skips/deselections remain documented
by pytest output.

- [ ] **Step 5: Commit and push**

```bash
git add tests/pdf_fixtures.py tests/test_pdf_pipeline.py README.md skills/pdf-translator/SKILL.md
git commit -m "test: cover publication-quality PDF workflow"
git push origin main
```

---

### Task 10: Re-run and inspect the real 50-page PDF

**Files:**
- Runtime input: `.web-translator/runs/designing-data-intensive-applications-pages-001-050-parserfix6/source.pdf`
- Runtime output: a new child of `.web-translator/runs/`
- Runtime publication: a new child of `translated-pdfs/`
- Do not commit: `.web-translator/`, `temp/`, or `translated-pdfs/`

**Interfaces:**
- Produces: one new private run, one new three-file final output, and page-by-page acceptance evidence.
- Consumes: the implemented PDF CLI, exact source text/protected-token matches, and reviewed translations.

- [ ] **Step 1: Create a fresh run and re-extract all 50 source pages**

Run:

```bash
PDF_PUBLICATION_RUN=.web-translator/runs/designing-data-intensive-applications-pages-001-050-publication-v1
PDF_PUBLICATION_OUT=translated-pdfs/designing-data-intensive-applications-pages-001-050-publication-v1
.venv/bin/python -m web_translator pdf-acquire .web-translator/runs/designing-data-intensive-applications-pages-001-050-parserfix6/source.pdf --run-dir "$PDF_PUBLICATION_RUN"
.venv/bin/python -m web_translator pdf-extract --run-dir "$PDF_PUBLICATION_RUN"
.venv/bin/python -m web_translator plan-zones --run-dir "$PDF_PUBLICATION_RUN" --max-chars 12000 --target-zones 8
```

Confirm `document.json` is schema `1.1`, page count is 50, and no selectable callout text
is owned only by a figure.

- [ ] **Step 2: Reuse translations only on exact contracts**

Map old translations by the tuple `(source_text, protected tokens, semantic type)`. Copy
only exact matches into new zone files. Translate and validate every unmatched or newly
split segment using the PDF skill's existing zone workflow. Numbers remain unchanged.

- [ ] **Step 3: Run semantic review, assembly, and automated QA**

Run:

```bash
.venv/bin/python -m web_translator prepare-assignments --run-dir "$PDF_PUBLICATION_RUN"
.venv/bin/python -m web_translator validate-translations --run-dir "$PDF_PUBLICATION_RUN"
.venv/bin/python -m web_translator pdf-review-input --run-dir "$PDF_PUBLICATION_RUN"
.venv/bin/python -m web_translator pdf-assemble --run-dir "$PDF_PUBLICATION_RUN" --output-dir "$PDF_PUBLICATION_OUT"
.venv/bin/python -m web_translator pdf-qa prepare --run-dir "$PDF_PUBLICATION_RUN" --output-dir "$PDF_PUBLICATION_OUT"
```

Complete the exact `review.json` before assembly. Expected: no required semantic or
automated finding and contact sheets cover every output page.

- [ ] **Step 4: Compare every source page with mapped output pages**

Render source and output at the same DPI and create numbered comparison sheets. Inspect
all 50 source pages, recording evidence for the thirteen canonical dimensions. Pay
special attention to source pages 7–13, 21, 34, 36, 43–45, 47, and 50.

- [ ] **Step 5: Iterate until no known finding remains**

For each required finding, add or tighten a deterministic regression test before the
fix, rerun the focused suite, rebuild into a new run/publication directory, and repeat
the complete page review. Do not modify a published output directory in place.

- [ ] **Step 6: Finalize and verify final artifacts**

Write `pdf-layout-review.json` from the inspected current render, run:

```bash
.venv/bin/python -m web_translator pdf-qa finalize --run-dir "$PDF_PUBLICATION_RUN" --output-dir "$PDF_PUBLICATION_OUT"
find "$PDF_PUBLICATION_OUT" -maxdepth 1 -type f -print | sort
```

Expected basenames: `manifest.json`, `review-report.md`, `translated.pdf` only. Open the
PDF independently, extract Korean text from the page-36 callout, and verify images and
captions remain sharp and adjacent.

- [ ] **Step 7: Commit any acceptance-driven code fixes and push**

Stage only source/tests/docs changed by acceptance findings. Do not stage runtime
artifacts.

```bash
git commit -m "fix: address real PDF publication findings"
git push origin main
```

Skip this commit only when `git status --short` shows no tracked changes.

---

### Task 11: Verify, review, release 0.6.0, and update the marketplace

**Files:**
- Modify: `.codex-plugin/plugin.json`
- Modify: `pyproject.toml`
- Modify: `src/web_translator/__init__.py`
- Test: `tests/test_versioning.py`
- External modify after plugin push: `/Users/starryeye/play/plugins/.claude-plugin/marketplace.json`

**Interfaces:**
- Produces: plugin tag `v0.6.0` and marketplace reference `0.6.0`.
- Consumes: all passing tests and approved real-document evidence.

- [ ] **Step 1: Run pre-release verification**

Run:

```bash
git diff --check
pytest -q
python scripts/version.py check
git status --short --branch
```

Expected: no diff errors, `0 failed`, version sources agree at `0.5.2`, and only the
intentional untracked runtime directories remain.

- [ ] **Step 2: Request final code review**

Use `superpowers:requesting-code-review` over the range `358a842..HEAD`. Resolve every
valid required finding using `superpowers:receiving-code-review`, with a failing test
before each behavioral fix. Commit and push each review-fix commit.

- [ ] **Step 3: Set version 0.6.0 and run release tests**

Run:

```bash
python scripts/version.py set 0.6.0
pytest tests/test_versioning.py tests/test_plugin_layout.py tests/test_skill_contract.py -q
python scripts/version.py check
git diff --check
```

Expected: all tests pass and all three version sources equal `0.6.0`.

- [ ] **Step 4: Commit, push, and tag the plugin release**

```bash
git add .codex-plugin/plugin.json pyproject.toml src/web_translator/__init__.py
git commit -m "chore: release 0.6.0"
git push origin main
git tag -a v0.6.0 -m "v0.6.0"
git push origin v0.6.0
```

- [ ] **Step 5: Update and verify the external marketplace**

In `/Users/starryeye/play/plugins`, change only the web-translator entry's version/ref to
`0.6.0`. Run that repository's marketplace validation commands, inspect the diff, then:

```bash
git add .claude-plugin/marketplace.json
git commit -m "Release Web Translator 0.6.0"
git push origin main
```

- [ ] **Step 6: Report final evidence**

Report plugin and marketplace commit hashes, tag `v0.6.0`, full-suite counts, new output
paths, exact three-file listing, and the completed page-by-page acceptance verdict.

---

## Self-Review Record

- Spec coverage: document roles, extraction, graphics/text separation, terminology,
  source trim, role-aware assembly, TOC, footnotes, QA, real-document acceptance, release,
  and marketplace publication are each mapped to a task.
- Placeholder scan: the plan contains no deferred implementation markers.
- Type consistency: `PdfSemanticRole`, `semantic_role`, schema `1.1`,
  `TerminologyDisplayPolicy`, `PdfPageSize(name="SOURCE")`, and the five new visual
  dimensions use the same names across producer and consumer tasks.
