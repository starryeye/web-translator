# PDF Logical Translation Units Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Translate a proved cross-page paragraph or list item as one AI target without losing physical source ownership or publication-quality checks.

**Architecture:** Preserve physical PDF blocks and add a versioned logical-unit map. Build coherent source text and protected-token provenance before shared AI assignment, then render and verify units through PDF-specific adapters. Implement the new reader/adapter path before switching the extraction writer, so intermediate commits retain the existing working workflow.

**Tech Stack:** Python 3.11+, dataclasses, pdfplumber, pypdf, ReportLab, Pillow, Poppler, pytest; no new runtime dependency.

**Spec:** `docs/superpowers/specs/2026-09-25-pdf-logical-translation-units-design.md`

## Global Constraints

- AI must translate the complete logical unit directly; neither machine-translation services nor mechanical substitutions are allowed.
- All existing limits remain: selectable PDFs only, 500 source pages, 50 MiB, source trim dimensions, unchanged original artwork, Korean-first PDF terminology, and exactly `translated.pdf`, `manifest.json`, and `review-report.md` on successful publication.
- Existing source files, diagnostic runs and published output are never overwritten.
- `continuation_of` retains its existing reference-entry meaning.
- Shared translation records and webpage implementations remain unchanged.
- The configured character budget (currently default 12,000) is not silently increased.
- Use the existing thirteen visual-review dimensions.
- Preserve all original physical block IDs, text, page numbers, geometry and relationships. Only declared logical ownership may change target cardinality.
- TDD for every behavior change; no source/page/font-name/vocabulary allowlists, fixture translations as real output, or source-text edits to force acceptance.
- Use the existing isolated `codex/pdf-commercial-layout` branch. After each commit, the implementer immediately hands its SHA to the controller, which immediately pushes that branch to the existing origin under standing user approval. Never push main, merge, tag or release as part of this plan.
- Keep every run private until semantic and visual acceptance. Do not stage PDFs, runtime output, private ledgers or review evidence.

## Review Focus

1. Two joined source pages reuse the same visible footnote marker: preserve both physical owners and render each note once (Tasks 3 and 5).
2. A unit map changes without changing source PDF bytes or visible text: old assignments/reviews must fail, including directory-swap attempts (Task 4).
3. Recto/verso margins or an intervening blank page resemble continuation: normalize legitimate margins but never jump a blank page or unrelated column (Task 2).
4. One long logical paragraph exceeds 12,000 characters: identify the unit and fail before assignment instead of silently splitting it (Tasks 3 and 7).
5. A legacy document or repeated layout split looks fully covered: retain extraction origin and prove member coverage rather than counting representative IDs (Tasks 1 and 6).

## File Structure and Sequencing

- `pdf_models.py`: strict versioned unit/join/finding records and document validation.
- New `pdf_units.py`: boundary decisions, logical grouping, derived text/offset projection; no I/O or rendering.
- New `pdf_unit_bindings.py`: PDF-only immutable assignment binding and safe verification.
- `pdf_extract.py`: boundary-evidence handoff and unit-to-Segment adapter; keep physical extraction intact.
- `pdf_assemble.py`, `pdf_flowables.py`: one render item per unit, member provenance, anchors and member-note callbacks.
- `pdf_review.py`, `pdf_qa.py`, `pdf_report.py`: bound review inputs, unit-aware coverage and truthful final evidence.
- `cli.py`: PDF-only preconditions and binding publication; shared/web payload shapes stay unchanged.
- New `tests/pdf_unit_fixtures.py`, `tests/test_pdf_units.py`, `tests/test_pdf_unit_bindings.py`; extend existing PDF tests at their consumer boundaries.
- `skills/pdf-translator/SKILL.md`, `README.md`: supported behavior and fail-closed limits at activation, not before.

Tasks 1–6 introduce and test explicit 1.2 inputs without changing the current extraction writer. Task 7 switches the writer after its consumers are ready. Legacy readers must remain usable, but legacy evidence never acquires new publication approval merely by being parsed. Task 8 completes the actual-document gate; completing this subplan does not close the parent commercial-layout plan or authorize release/cleanup.

Runtime commands below use `PYTHONPATH=src ../../.venv/bin/python` from the existing worktree. Run focused iterations, then each task's named covering files once after freeze. Chromium/localhost covering requires explicit escalation; retain failed XML before an amended run. Do not repeat unchanged successful covering evidence after an interruption.
At execution setup, the SDD workspace tool creates `.superpowers/sdd/2026-09-28-pdf-logical-translation-units`; keep this plan's reports/XML there, separate from the parent plan's ledger.

### Task 1: Strict logical-unit contracts and legacy reads

**Files:** Modify `src/web_translator/pdf_models.py`, `tests/test_pdf_models_paths.py`; create `src/web_translator/pdf_units.py`, `tests/pdf_unit_fixtures.py`, `tests/test_pdf_units.py`.

**Interfaces:**
- Add frozen records in `pdf_models.py`: `PdfBoundaryLine(bbox, font_size, font_family, text)`, `PdfBlockBoundary(block_id, first_line, last_line, column_bbox, text_indent)`, `PdfJoinEvidence(left, right, left_page_size, right_page_size)`, `PdfTextJoin(left_block_id, right_block_id, operation, evidence)`, `PdfTranslationUnit(id, source_block_ids, kind, semantic_role, segment_id, joins)`, and `PdfFlowFinding(code, left_block_id, right_block_id, severity, message)`. Bboxes are four finite floats; page sizes are positive `(width, height)` float pairs; IDs/text/family are strings; member/join collections are tuples. Join evidence uses typed `PdfBlockBoundary` left/right records. `operation` is exactly `space` or `remove-discretionary-hyphen`; finding severity is `required`.
- Document 1.2 adds `extracted_schema_version: str`, `translation_units: list[PdfTranslationUnit]`, `flow_findings: list[PdfFlowFinding]`. Preserve existing 1.0/1.1 serialization/read routes during rollout; provide explicit `upgrade_pdf_document_to_units(data: Mapping[str, Any]) -> dict[str, Any]` producing 1.2 singleton mappings and the original extraction version.
- `pdf_units.py`: `validate_unit_membership(document: PdfDocument) -> None`, `unit_for_block(document: PdfDocument, block_id: str) -> PdfTranslationUnit`, `project_unit_text(unit: PdfTranslationUnit, blocks: Mapping[str, PdfBlock]) -> PdfUnitProjection`.
- Frozen records in `pdf_units.py`: `PdfUnitProjection(text: str, spans: tuple[PdfSourceSpan, ...])` and `PdfSourceSpan(block_id: str, source_start: int, source_end: int, unit_start: int, unit_end: int)` with half-open character offsets. Inserted spaces and removed discretionary marks have explicit zero-width counterparts, so projection never silently drops evidence.
- Fixture `make_unit_document(texts: tuple[str, ...] = ("A para‐", "graph continues."), *, operation: str = "remove-discretionary-hyphen") -> PdfDocument`: one source block per adjacent page, one assigned unit, stable IDs, no unrelated table metadata.

- [ ] **Step 1: Add contract/projection RED tests.**

```python
def test_unit_roundtrip_preserves_physical_members():
    doc = make_unit_document()
    loaded = PdfDocument.from_dict(doc.to_dict())
    assert loaded.to_dict() == doc.to_dict()
    assert len(loaded.blocks) == 2
    assert len(loaded.translation_units) == 1
    projection = project_unit_text(loaded.translation_units[0], {b.id: b for b in loaded.blocks})
    assert projection.text == "A paragraph continues."
    assert {s.block_id for s in projection.spans} == {b.id for b in doc.blocks}

def test_duplicate_member_is_rejected():
    data = make_unit_document().to_dict()
    data["translation_units"][0]["source_block_ids"].append(data["blocks"][0]["id"])
    with pytest.raises(PdfContractError, match="member"):
        PdfDocument.from_dict(data)
```

Add named tests `test_unit_membership_rejects_missing_reordered_or_foreign_blocks` (all malformed variants raise), `test_legacy_unit_upgrade_preserves_origin_and_locators` (1.0/1.1 origins remain exact; one unit per former target), and `test_projection_preserves_ascii_hyphen` (`space` yields `well- known`, never `wellknown`). Check bad versions, nonfinite evidence, invalid operations and mismatched physical segment IDs.
- [ ] **Step 2:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_units.py tests/test_pdf_models_paths.py -q`; confirm assertions fail for missing behavior, not fixture setup errors.
- [ ] **Step 3:** Implement strict records/validators and deterministic text projection. Unit IDs are `pdf:unit-` plus six digits, ordered by first member. Serialized units require assigned segment IDs. Internal pre-segmentation units may have `segment_id=None` but cannot be published as a completed document. Figures/furniture/empty cells own no units; every eligible text block owns exactly one. List units may contain a first list-item and unmarked body paragraphs; other mixed kinds/roles are invalid. No change to the extraction writer yet.
- [ ] **Step 4:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_units.py tests/test_pdf_models_paths.py -q --junitxml=.superpowers/sdd/2026-09-28-pdf-logical-translation-units/task-1-covering.xml`; expect zero failures/errors.
- [ ] **Step 5:** Commit only named files: `feat: model logical PDF translation units`; immediately hand off for push.

### Task 2: Conservative cross-page continuation and boundary evidence

**Files:** Modify `src/web_translator/pdf_units.py`, `tests/pdf_unit_fixtures.py`, `tests/test_pdf_units.py`; add boundary collection to `src/web_translator/pdf_extract.py` without enabling new output.

**Interfaces:**
- `collect_flow_boundaries(blocks: Sequence[PdfBlock], pages: Sequence[PdfPage], lines_by_page: Mapping[int, Sequence[PdfLine]]) -> dict[str, PdfBlockBoundary]` captures first/last owned lines and observed column frame, not a synthetic block bbox. Unprovable ownership produces no boundary, not guessed geometry.
- `decide_page_join(left: PdfBlock, right: PdfBlock, left_boundary: PdfBlockBoundary, right_boundary: PdfBlockBoundary, pages: Mapping[int, PdfPage]) -> PdfJoinDecision`; decision has `status` (`join`, `separate`, `ambiguous`), optional `join` and optional required `finding`.
- `build_translation_units(blocks: Sequence[PdfBlock], pages: Sequence[PdfPage], boundaries: Mapping[str, PdfBlockBoundary]) -> tuple[list[PdfTranslationUnit], list[PdfFlowFinding]]`; generated units are internal/unassigned until Task 3.
- Fixture `make_flow_case(case: str, *, scale: float = 1.0)` returns `(blocks, pages, boundaries)`. Cases below are defined here; each changes one physical condition from an adjacent-page continuation.

- [ ] **Step 1: Write named producer and decision RED tests.**

```python
@pytest.mark.parametrize("scale", [0.75, 1.0, 1.7])
def test_cross_page_discretionary_word_is_one_unit(scale):
    blocks, pages, boundaries = make_flow_case("discretionary", scale=scale)
    units, findings = build_translation_units(blocks, pages, boundaries)
    assert not findings
    assert len(units) == 1
    assert units[0].source_block_ids == tuple(b.id for b in blocks)
    assert project_unit_text(units[0], {b.id: b for b in blocks}).text == "A paragraph continues."

@pytest.mark.parametrize("case", ["blank-page", "new-indent", "new-list", "nested-list", "heading", "table", "figure", "column-change"])
def test_structural_boundary_is_not_joined(case):
    blocks, pages, boundaries = make_flow_case(case)
    units, findings = build_translation_units(blocks, pages, boundaries)
    assert all(len(u.source_block_ids) == 1 for u in units)
```

Add `test_three_page_chain_preserves_order`, `test_list_continuation_keeps_one_marker`, `test_recto_verso_normalizes_observed_column_margins`, and `test_conflicting_continuation_has_required_finding` with exact one-unit/one-marker, normalized frame and required finding assertions. Exercise real `group_words_into_lines` → classification → ordering → block/boundary collection, not only hand-built boundaries.
- [ ] **Step 2:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_units.py -q`; preserve valid RED separately from malformed-fixture failures.
- [ ] **Step 3:** Implement adjacency plus compatible body role/font family/size, column-relative margins, tail/head placement, no intervening structure, and unique predecessor/successor. Use observed line pitch and column frames; missing frame/evidence cannot count as positive. Strong discretionary evidence permits dehyphenation. A non-hyphen join additionally needs an overflow-like filled tail line, continuation-compatible head indentation and no sentence/paragraph-end contradiction; adjacency/lowercase alone is insufficient. Clear new structure is separate; plausible conflicting flow is ambiguous. Do not skip blank pages or chain through headings/artwork. Reuse existing typography tolerances where applicable and pin new boundaries with exact just-inside/just-outside tests.
- [ ] **Step 4:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_units.py tests/test_pdf_extract.py -q --junitxml=.superpowers/sdd/2026-09-28-pdf-logical-translation-units/task-2-covering.xml` once after freeze; expect zero failures/errors. Existing extraction output remains unchanged at this stage.
- [ ] **Step 5:** Commit `feat: identify cross-page PDF text continuations`; immediate push handoff.

### Task 3: Unit segments, protected occurrences and offset provenance

**Files:** Modify `src/web_translator/pdf_extract.py`, `src/web_translator/pdf_units.py`, `tests/test_pdf_extract.py`, `tests/test_pdf_units.py`, `tests/pdf_unit_fixtures.py`.

**Interfaces:**
- `build_pdf_unit_segments(blocks: Sequence[PdfBlock], units: Sequence[PdfTranslationUnit]) -> tuple[list[PdfBlock], list[PdfTranslationUnit], list[Segment]]` assigns one segment per unit, representative first-member locator and all member mappings.
- Add `PdfProtectedOccurrence` in `pdf_units.py`: `placeholder: str`, `value: str`, `kind: str`, `source_spans: tuple[PdfSourceSpan, ...]`, `owner_block_id: str | None`, `note_id: str | None`. A token spanning a join retains ordered spans from both physical blocks, including any removed mark's zero-width projection; never assign its entire range to only the first block. Footnote occurrences additionally require their exact physical owner and note IDs; other occurrences leave those two fields unset. `project_protected_occurrences(unit: PdfTranslationUnit, blocks: Mapping[str, PdfBlock]) -> tuple[PdfProtectedOccurrence, ...]` derives occurrence identity from existing physical ownership and projection; no marker-text-only lookup across members. Use one canonical token-allocation path shared with unit segment construction so the occurrence placeholder IDs cannot drift.
- `require_pdf_unit_budget(document: PdfDocument, segments: Sequence[Segment], max_chars: int) -> None` in `pdf_units.py` raises `PdfContractError` with unit/member IDs and measured size before invoking shared zone planning when one PDF unit is oversized.
- Reuse shared `protect_fragment`, number/reference protection and translation records; do not change shared schemas. Bindings/assembly consume the same deterministic occurrence map rather than persisting a second editable source string.

- [ ] **Step 1: Add RED adapter tests.**

```python
def test_unit_segments_have_one_target_and_all_member_mappings():
    doc = make_unit_document()
    blocks, units, segments = build_pdf_unit_segments(doc.blocks, doc.translation_units)
    assert len(segments) == 1
    assert segments[0].locator == blocks[0].id
    assert {b.segment_id for b in blocks} == {segments[0].id}
    assert units[0].segment_id == segments[0].id

def test_oversized_logical_unit_is_not_split():
    doc = make_unit_document(("A" * 6001, "B" * 6000), operation="space")
    _, _, segments = build_pdf_unit_segments(doc.blocks, doc.translation_units)
    with pytest.raises(PdfContractError, match="12002") as error:
        require_pdf_unit_budget(doc, segments, max_chars=12000)
    assert doc.translation_units[0].id in str(error.value)
    assert all(b.id in str(error.value) for b in doc.blocks)
    with pytest.raises(ZoneContractError):
        build_zones(segments, max_chars=12000)
```

Add `test_repeated_visible_note_markers_keep_distinct_occurrences`: two `i` markers on different member pages produce different placeholders/note IDs and exact multiplicities. Add `test_joined_product_and_protected_spans_keep_source_offsets` and `test_reference_singletons_keep_existing_core_protection`; assert exact restored identifiers/numbers/URLs and physical offsets after a removed mark. Recognized literal code remains exact; this is not authorization for a new general SQL parser.
- [ ] **Step 2:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_units.py tests/test_pdf_extract.py -q -k 'unit or occurrence or joined_product or reference_singleton'`; confirm RED.
- [ ] **Step 3:** Implement normalization before general protection, source-offset projection and collision-free per-unit placeholders. Preserve heading paths and neighbor context in unit order. Keep the old extraction writer selected until Task 7; test this adapter explicitly with 1.2 fixtures. The budget error must include the unit ID, member IDs and measured character count at the PDF adapter/CLI boundary without changing webpage errors.
- [ ] **Step 4:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_units.py tests/test_pdf_extract.py -q --junitxml=.superpowers/sdd/2026-09-28-pdf-logical-translation-units/task-3-covering.xml`; expect zero failures/errors.
- [ ] **Step 5:** Commit `feat: segment PDF logical units with source provenance`; immediate push handoff.

### Task 4: Immutable PDF assignment binding and stale-review rejection

**Files:** Create `src/web_translator/pdf_unit_bindings.py`, `tests/test_pdf_unit_bindings.py`; modify `src/web_translator/cli.py`, `src/web_translator/pdf_review.py`, `tests/test_cli_contract.py`, `tests/test_pdf_review.py`.

**Interfaces:**
- `PdfUnitBinding` schema `1.0`: SHA-256 of exact document/segments bytes plus maps of zone and assignment filenames to hashes; binding file never hashes itself. `PdfUnitBindingError(ValueError)` is defined in the same module; callers translate it to their existing public error type rather than introducing a module cycle with `pdf_review.py`.
- `build_pdf_unit_binding(document_bytes: bytes, segments_bytes: bytes, zone_payloads: Mapping[str, bytes], assignment_payloads: Mapping[str, bytes]) -> PdfUnitBinding`.
- `hold_pdf_unit_binding(run: Path | Any) -> Iterator[PdfUnitBinding]` opens/holds inputs using existing anchored PDF file primitives, validates exact expected names and bytes, and verifies identity through consumption.
- `require_assignable_pdf(document: PdfDocument) -> None`: reject required `flow_findings` and non-native-1.2 extraction when preparing new publication assignments. Legacy read/diagnostic paths stay explicit.

- [ ] **Step 1: Add RED transaction/binding tests.**

```python
def test_same_text_different_unit_map_invalidates_binding(bound_unit_run):
    run = bound_unit_run
    change_join_evidence_without_changing_text(run)
    with pytest.raises(PdfUnitBindingError, match="binding"):
        with hold_pdf_unit_binding(run):
            pass

def test_required_flow_finding_prevents_assignment(ambiguous_unit_run):
    assert main(["prepare-assignments", "--run-dir", str(ambiguous_unit_run)]) != 0
    assert not (ambiguous_unit_run / "assignments").exists()
```

Define these fixtures in `tests/test_pdf_unit_bindings.py` using Task 1 documents and existing CLI fixture patterns. Add tests for missing/foreign binding entries, malformed digest, changed segments/zones, symlink or directory replacement, mutation during held consumption, failed rename, and unchanged webpage package JSON.
- [ ] **Step 2:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_unit_bindings.py tests/test_pdf_review.py -q`; confirm missing-binding/gating behavior fails before implementation.
- [ ] **Step 3:** For native 1.2 PDFs only, put `.pdf-unit-binding.json` inside the temporary assignments directory before its existing atomic publication. This keeps zone package fields unchanged and publishes binding/packages as one directory transaction. PDF semantic readers accept exactly this one additional known file and include it plus held `document.json` bytes in the semantic digest; arbitrary extra files still fail. Validate the binding before PDF translation validation, semantic review, assembly and QA. Planning checks required findings before creating zones. No generic reader silently guesses PDF from file extension; use the existing validated PDF source/run contract.
- [ ] **Step 4:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_unit_bindings.py tests/test_pdf_review.py tests/test_cli_contract.py -q --junitxml=.superpowers/sdd/2026-09-28-pdf-logical-translation-units/task-4-covering.xml`; expect zero failures/errors. Use explicit escalation for platform/localhost fixtures. Assert unchanged web assignment payload shape and preserve anchored-handle security behavior.
- [ ] **Step 5:** Commit `feat: bind PDF assignments to logical ownership`; immediate push handoff.

### Task 5: Render one unit with member anchors and all member notes

**Files:** Modify `src/web_translator/pdf_assemble.py`, `src/web_translator/pdf_flowables.py`, `tests/test_pdf_assemble.py`, `tests/pdf_unit_fixtures.py`.

**Interfaces:**
- Add `PdfRenderUnit(unit: PdfTranslationUnit, source_blocks: tuple[PdfBlock, ...], segment: Segment, text: str)` in `pdf_assemble.py` and `normalize_pdf_units(document: PdfDocument, segments: Sequence[Segment], translations: Mapping[str, Translation], glossary: Mapping[str, str]) -> list[PdfRenderUnit]`.
- Layout 1.2 text records add `unit_id: str` and `source_block_ids: tuple[str, ...]` while retaining first-member `block_id`, source order and output split-part fields. Non-text figures have no unit. Legacy layout reads retain their original version.
- Extend `TrackedFlowable` with keyword-only optional unit/member metadata and anchor aliases; propagate metadata to every split part. Existing one-block callers remain valid.

- [ ] **Step 1: Add generated-PDF RED tests in `test_pdf_assemble.py`.**

```python
def test_joined_unit_renders_once_with_all_source_members(rendered_unit_case):
    doc, layout, output_text = rendered_unit_case
    unit = doc.translation_units[0]
    records = [r for r in layout.flowables if r.unit_id == unit.id]
    assert records
    assert all(r.source_block_ids == unit.source_block_ids for r in records)
    assert output_text.count("문단 전체를 번역한 검증 문장") == 1
    assert [r.split_part for r in records] == list(range(len(records)))
```

Fixture creates a selectable multi-page source and a fixed test-only translation, not a real-book translation. Add `test_joined_list_draws_one_original_marker`, `test_member_anchors_resolve_to_reported_unit_start`, `test_joined_unit_preserves_two_equal_visible_note_markers`, and `test_split_unit_schedules_each_note_once`; assert distinct owner/note IDs, no duplicates, and unchanged source block serialization.
- [ ] **Step 2:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_assemble.py -q -k 'joined_unit or joined_list or member_anchors or split_unit'`; confirm RED.
- [ ] **Step 3:** Render one logical paragraph/list item with a unit-specific view, never mutate persisted physical blocks or fabricate a union bbox. Reuse role styles and existing layout machinery. Resolve each physical source anchor through its owning unit; record paragraph-start granularity. Build callbacks from protected occurrence identity for every member note, including repeated visible markers; never use only the first member's destination. Preserve existing figures, references, TOC, captions and singleton roles.
- [ ] **Step 4:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_assemble.py tests/test_pdf_models_paths.py -q --junitxml=.superpowers/sdd/2026-09-28-pdf-logical-translation-units/task-5-covering.xml`; expect zero failures/errors.
- [ ] **Step 5:** Visual-inspect the generated joined/list/multiple-note output pages under the PDF skill; this is not actual50 approval.
- [ ] **Step 6:** Commit `feat: reflow PDF logical units with member provenance`; immediate push handoff.

### Task 6: Unit-aware QA, truthful metrics and final report mappings

**Files:** Modify `src/web_translator/pdf_qa.py`, `src/web_translator/pdf_report.py`, `src/web_translator/pdf_review.py`, `tests/test_pdf_qa.py`, `tests/test_pdf_pipeline.py`, `tests/test_pdf_review.py`.

**Interfaces:**
- `validate_pdf_unit_layout(document: PdfDocument, layout: PdfAssemblyLayout) -> None` in `pdf_qa.py`: exact physical membership and unit logical coverage, contiguous split parts, member relationships and no unresolved required findings.
- New QA result and final manifest writes use schema `1.1` for the new metric contract; retain explicit legacy `1.0` read validation. Document/layout versions remain independently `1.2`.
- `translated_block_count` counts covered physical target blocks; new `translation_unit_count` counts unique AI targets. Final manifest adds `translation_units` entries with unit ID, segment ID, member IDs/source pages/source bboxes, output pages, and anchor granularity. No extra final file.

- [ ] **Step 1: Add RED coverage/report tests.**

```python
def test_member_coverage_is_not_representative_coverage(unit_document, unit_layout):
    broken = remove_nonrepresentative_member(unit_layout)
    with pytest.raises(PdfQAFailure, match="member"):
        validate_pdf_unit_layout(unit_document, broken)

def test_unit_metrics_distinguish_physical_and_logical(verified_unit_manifest):
    m = verified_unit_manifest
    assert m["schema_version"] == "1.1"
    assert m["qa"]["automated"]["metrics"]["translated_block_count"] == 2
    assert m["qa"]["automated"]["metrics"]["translation_unit_count"] == 1
    assert len(m["translation_units"][0]["source_block_ids"]) == 2
```

Build fixtures through Tasks 1/5 interfaces, not mocks. Add tests for duplicate flowable logical ownership, missing/reordered split parts, foreign member, orphan note, source-map tampering after semantic approval, and legacy extracted origin falsely claiming new acceptance. Existing thirteen-dimension review and staged-PDF hash checks must still reject stale evidence.
- [ ] **Step 2:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_qa.py tests/test_pdf_pipeline.py -q -k 'unit or legacy_origin'`; confirm RED.
- [ ] **Step 3:** Replace PDF-only one-block/one-target assumptions with validated unit maps. Include map/binding/document bytes in existing held semantic/final snapshots and manifest input evidence. Map every source member to the unit's output pages without inventing separate rectangles. Update count validation and report prose explicitly; do not reduce coverage to a set of representative block IDs. Keep immutable/no-replace publication and rollback contracts unchanged.
- [ ] **Step 4:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_qa.py tests/test_pdf_pipeline.py tests/test_pdf_review.py -q --junitxml=.superpowers/sdd/2026-09-28-pdf-logical-translation-units/task-6-covering.xml`; expect zero failures/errors. Existing webpage QA is not modified. New 1.2 fixtures require native origin; the still-active legacy writer route remains operational until Task 7 switches the publication minimum. Confirm old strict readers reject new serialized versions rather than silently misreading counts.
- [ ] **Step 5:** Commit `feat: verify PDF unit coverage and publication evidence`; immediate push handoff.

### Task 7: Activate native 1.2 extraction and prove the complete workflow

**Files:** Modify `src/web_translator/pdf_extract.py`, `src/web_translator/pdf_models.py`, `src/web_translator/cli.py`, `skills/pdf-translator/SKILL.md`, `README.md`, `tests/test_pdf_extract.py`, `tests/test_pdf_extract_transaction.py`, `tests/test_pdf_pipeline.py`, `tests/test_skill_contract.py`, `tests/pdf_unit_fixtures.py`.

**Interfaces:** Existing `extract_pdf`, CLI entry points and exact successful final directory remain unchanged externally. Native extraction now writes document 1.2/origin 1.2, units/findings/assigned segments. Do not introduce an alternate public command that bypasses unit QA.

- [ ] **Step 1: Add a real generated multi-page source workflow RED test.**

```python
def test_native_unit_pipeline_publishes_exact_artifacts(native_unit_run):
    run, output = native_unit_run
    doc = json.loads((run / "document.json").read_text())
    assert doc["schema_version"] == doc["extracted_schema_version"] == "1.2"
    assert any(len(u["source_block_ids"]) > 1 for u in doc["translation_units"])
    assert {p.name for p in output.iterdir()} == {"translated.pdf", "manifest.json", "review-report.md"}
    assert (run / "assignments" / ".pdf-unit-binding.json").is_file()
```

The fixture executes acquire/extract/plan/prepare/validate/review/assemble/QA/finalize with declared test-only translations. Add explicit ambiguity/oversized-unit refusal before assignment, 500-page/50MiB boundary preservation, scan rejection, Unicode/space paths, and no-overwrite transaction assertions. Keep 501-page/over-limit negatives.
- [ ] **Step 2:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_pipeline.py tests/test_pdf_extract.py -q -k 'native_unit or unit_refusal'`; verify RED under the old writer.
- [ ] **Step 3:** Collect boundary evidence while page-local ordered lines are available, build units only after physical ordering/relationships, and pass them to Task 3 segmentation before transactional extraction publication. Update strict version constants/read routes without erasing original extraction version. Read skill-creator/writing-skills before updating the PDF skill; document full-unit ownership, required findings, binding and oversized-unit limits, while leaving the shared translator result format/web skill unchanged.
- [ ] **Step 4:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_extract.py tests/test_pdf_extract_transaction.py tests/test_pdf_pipeline.py tests/test_skill_contract.py -q --junitxml=.superpowers/sdd/2026-09-28-pdf-logical-translation-units/task-7-covering.xml`; expect zero failures/errors, explaining platform skips.
- [ ] **Step 5:** Run `PYTHONPATH=src ../../.venv/bin/python -m pytest --junitxml=.superpowers/sdd/2026-09-28-pdf-logical-translation-units/full-suite.xml -q` (existing configuration excludes opt-in live tests). Explicitly list Windows-native ABI skips rather than claiming a Windows run on macOS. If a fix invalidates only one surface, preserve prior XML and rerun that amended coverage; no repeated unchanged full suites.
- [ ] **Step 6:** Commit `feat: enable logical-unit PDF translation workflow`; immediate push handoff. Do not bump release versions or deploy the marketplace yet.

### Task 8: Fresh real-document AI and all-page acceptance

**Files:** Private new run only under `.web-translator/runs`; no existing source/run/output overwrite and no actual-book data in tracked fixtures.

**Interfaces:** Use the existing PDF skill and CLI lifecycle with Task 7's native 1.2 document, immutable bound packages, semantic digest and layout evidence. Source is the already approved first-50-page input; use a new unique descriptive run/output name and persist its allocation before acquisition.

- [ ] **Step 1:** Record original/source and prior-output hashes, then acquire/extract/plan once. Review every target and every unit join/required finding before preparing packages. Resolve a genuine defect via its owning task's bounded TDD/review loop; do not hand-edit IDs/text or repeatedly extract unchanged code.
- [ ] **Step 2:** Prepare content-only summary and canonical glossary; explicitly dispatch Korean-first policy. One isolated AI translator per immutable zone, exact assigned IDs, no MT. Old translations may be reused only with identical unit/member/join/source/protected contracts and master contextual approval; conflicting old candidates are not selected arbitrarily.
- [ ] **Step 3:** Validate each zone, perform the existing complete semantic rubric and neighbor checks, verify exact SQL/numbers/products, then bind final master approval to current unit map and exact input bytes. Fixture translations or fabricated approvals cannot satisfy this step.
- [ ] **Step 4:** Assemble and render staged output; compare every one of the 50 source pages with its mapped output pages, including joined boundaries, notes, TOC, images and original blanks. Record evidence in all thirteen dimensions and leave no required finding unresolved. Preserve existing link warnings and declared destination granularity.
- [ ] **Step 5:** Finalize only after all gates pass; verify exactly three final files, immutable evidence hashes and unchanged old artifacts. Report source/output page counts, unit/physical counts and remaining limitations. Return this evidence to the parent commercial-layout Task10 ledger; carry its deferred review findings to the final whole-branch review. No merge, release, tag, marketplace update or worktree/evidence deletion is authorized by this subplan.

## Plan Self-Review and Handoff

Spec coverage: physical/legacy/projection contracts → Task 1; continuation/ambiguity → Task 2; protected one-target input → Task 3; immutable ownership approvals → Task 4; reflow/navigation/member notes → Task 5; coverage/metrics/reports → Task 6; activation/shared-web/platform compatibility → Task 7; actual AI/all-page acceptance → Task 8. Each Review Focus input has a named test in its owning task.

Cross-task contracts: Task 2 emits unassigned units; Task 3 assigns exactly one Segment and member mapping; Task 4 binds exact document/segment/package bytes; Task 5 consumes that ownership and emits member-aware layout; Task 6 verifies physical and logical cardinalities independently. Task 7 is the only writer activation point. Shared record shapes do not change.

Execution method remains the user's previously selected **subagent-driven** workflow: one implementer at a time, fresh scoped spec+quality review after each task, bounded re-review of fixes and final whole-branch review before release. This written plan requires user review/confirmation before execution. Its existence does not authorize implementing the new schema yet.
