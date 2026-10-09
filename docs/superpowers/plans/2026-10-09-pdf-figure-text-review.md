# PDF Figure Text Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (selected by the user) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace unreliable figure-label guesses with direct AI/master decisions bound to exact source characters, without preserving untranslated body text accidentally.

**Architecture:** A focused PDF-only module inventories held source characters/media and validates explicit review coverage. Existing semantic binding, assembly, QA and reporting consume that contract; no shared translation or HTML contract changes. Two sequential implementation/review units precede controller-owned real-PDF acceptance.

**Tech Stack:** Python 3.11+, pdfplumber, existing anchored filesystem utilities, pytest, existing bounded Poppler/Pillow rendering.

**Spec:** `docs/superpowers/specs/2026-10-09-pdf-figure-text-review-design.md` (approved 2026-10-09).

## Global Constraints

- Selectable text only, reject scans/encryption, 500 source pages, 50 MiB.
- Native 1.2 logical translation units, Korean-first terminology, source trim size, original images, macOS/Windows support.
- Exactly `translated.pdf`, `manifest.json`, `review-report.md` after successful finalization; private inventory is not a fourth public artifact.
- Webpage contracts, shared Segment/Translation/assignment formats and source extraction evidence do not change.
- No OCR, machine-translation service, image-text editing or automatic override.
- Reuse `figure_owns_character`; refuse ambiguous/partial/duplicate ownership and translatable-block overlap.
- Source/media/inventory/review files stay identity/content-held during consumption; no overwriting prior evidence or final outputs.
- Every source figure receives direct controller AI inspection with page context; code verifies evidence, not semantic correctness.
- Final publication still requires every rendered page and thirteen canonical visual dimensions.
- Do not claim actual Windows verification from macOS-only tests.
- Commit only verified scoped work and push immediately after every commit.
- No release/version bump, marketplace deployment, merge, worktree cleanup or source-specific allowlist in this plan.

## Review Focus

1. A glyph record contains several Unicode characters or decomposed accents: preserve its exact string and original page-character index, without normalization or offset renumbering (Task 1).
2. Nonfinite/degenerate rectangles or overlapping source figures: refuse ambiguity rather than construct apparently complete approval coverage (Task 1).
3. A replaced file has identical bytes: held identity checks must reject replacement, not rely only on matching hash (Tasks 1 and 2).
4. Review changes after successful QA while the staged PDF stays identical: finalization must reject stale evidence before publishing anything (Task 2).
5. Figure-free and empty-text-figure inputs: preserve optional-empty compatibility without silently approving a figure containing text (Tasks 1 and 2).

## Workspace and execution

Work only in `/Users/starryeye/play/web-translator/.worktrees/pdf-commercial-layout`, branch `codex/pdf-commercial-layout`. Main checkout stays untouched. Base is `34e0fe9` plus the pending controller-owned changes listed below; the plan/spec approval commits do not change that production baseline.

On this host, every Python invocation uses `PYTHONPATH=src /Users/starryeye/play/web-translator/.venv/bin/python` from that worktree. Native Windows invocations resolve their own absolute `.venv\\Scripts\\python.exe`; never copy the macOS executable path there. `P` below denotes the full host-appropriate Python invocation, not a literal executable.

Before implementation, record the exact HEAD and pending tracked diff in the private progress ledger. Preserve the existing seven dirty files: `skills/pdf-translator/SKILL.md`, `src/web_translator/{cli.py,pdf_qa.py,pdf_report.py,pdf_review.py}`, `tests/{test_pdf_qa.py,test_pdf_review.py}`. Their preserved-name/SQL fixes belong in Task 2; their unsuccessful figure heuristics must be superseded, not stacked or blanket reverted. Private `.web-translator/` evidence must never be staged.

Use one implementer and one independent reviewer per sequential development unit, with no child delegation. Reuse the implementer for review corrections. The controller owns pushes, actual AI semantic/figure review and visual acceptance. Reviewers inspect the frozen scoped diff and retained test evidence, without repeating whole suites or operating the real book. Full-suite verification is a completion gate for each implemented unit; do not run unrelated duplicate baselines. Remain in the existing suitable worktree.

## File responsibilities

- New `src/web_translator/pdf_figure_review.py`: exact inventory/review contracts, pure construction/validation and anchored input generation/consumption.
- `src/web_translator/cli.py`: new PDF command, native run routing and PDF-only review parsing.
- `src/web_translator/pdf_review.py`: inventory bytes in held semantic digest; exact optional review fields.
- `src/web_translator/pdf_assemble.py`: source-recomputed figure approval before staged authoring, including direct API use.
- `src/web_translator/pdf_qa.py`: same approval checks in prepare/finalize; remove unreliable figure-text heuristics, retain geometry/media integrity.
- `src/web_translator/pdf_report.py`: exact manifest/report preservation of approvals and inventory binding.
- New `tests/pdf_figure_review_fixtures.py`, `tests/test_pdf_figure_review.py`: real selectable source fixtures and contract/transaction tests.
- Existing PDF/CLI/skill tests and fixed PDF fixture evidence: integrate publication gates without automatic production approval.
- `skills/pdf-translator/SKILL.md`: source-figure AI review stage and strict evidence workflow, using shared references unchanged.

---

### Task 1: Source-bound inventory, approval contract and input command

**Files:** Create `src/web_translator/pdf_figure_review.py`, `tests/pdf_figure_review_fixtures.py`, `tests/test_pdf_figure_review.py`; modify the new-command parts of `src/web_translator/cli.py`, `tests/test_cli_contract.py`.

**Interfaces produced in `pdf_figure_review.py`:**

- `PdfFigureReviewError(ValueError)` for actionable contract/source/identity failures.
- `FIGURE_TEXT_INPUT_NAME = "figure-text-input.json"`.
- `build_pdf_figure_text_input(document: PdfDocument, *, document_bytes: bytes, source_pdf_bytes: bytes, media_payloads: Mapping[str, bytes]) -> dict[str, Any]`: exact spec inventory, no filesystem writes. `media_payloads` is keyed by figure block ID; require exact figure coverage.
- `parse_pdf_figure_text_input(value: Any) -> dict[str, Any]`: validate exact nested fields, stable block/page correspondence, hashes, ordered finite rectangles and unique indexes.
- `canonical_figure_text_input_bytes(value: Mapping[str, Any]) -> bytes`: validate then serialize using the spec's UTF-8/sorted/compact/no-NaN/one-LF policy.
- `parse_pdf_figure_text_review(value: Any) -> dict[str, Any]`: structure-only canonicalization, allowing diagnostic `required-fix` records.
- `validate_pdf_figure_text_review(value: Any, *, inventory_bytes: bytes, require_pass: bool = True) -> dict[str, Any]`: verify inventory file-byte digest, exact figure coverage, disjoint exact label coverage/text and verdicts. `require_pass=False` allows diagnostic required-fix records without requiring complete passing label coverage; all supplied indexes/text must still be valid. Passing figures always require complete coverage.
- `PdfFigureInputSnapshot`: held `run_anchor`, parsed `document`/`source`, `document_bytes`, `source_pdf_bytes`, `media_payloads`, optional existing `inventory_bytes: bytes | None`, and `verify() -> None`; resources remain owned by its context. If inventory exists at entry, hold its identity/content too; its absence does not prevent first generation.
- `hold_pdf_figure_inputs(run: Path | Any) -> Iterator[PdfFigureInputSnapshot]` as a context manager: accept a path or existing directory anchor, hold source/document/media identities and bytes; validate native origin, source length/hash/page evidence. Use runtime-local anchored-helper imports to avoid a cycle with assembly.
- `write_pdf_figure_review_input(run_dir: Path) -> Path`: anchored atomic no-overwrite publication, or reuse an existing identical canonical inventory only after current source verification.
- `validate_current_pdf_figure_review(snapshot: PdfFigureInputSnapshot, *, inventory_bytes: bytes, review_value: Any) -> dict[str, Any]`: recompute canonical inventory, compare exact bytes, require passing approval, and verify held inputs before return. Publication callers must keep the context alive through their mutation and call `verify()` before committing it.

**Fixture interface:** `make_figure_review_run(tmp_path: Path, *, case: str = "diagram") -> tuple[Path, Path]` in `tests/pdf_figure_review_fixtures.py` returns allocated run/output paths. Cases are `diagram`, `sentence-labels`, `numeric-prose`, `colon-prose`, `indent-20`, `indent-36`, `empty-figure`, `figure-free`, `partial-glyph`, `overlapping-figures`, `body-overlap`. Generate real selectable source PDFs/media; unsafe-source cases can use explicitly test-only declared native document ownership so extraction refusal does not prevent exercising the consumer. Use hand-checked fixture label groups/decisions, never an automatic production reviewer. Existing native test utilities supply valid segments/bindings when a publication test needs them. The `diagram` fixture has one figure `pdf:page-0001:block-0002` and one label `API`, drawn before the separate body so its page-character indexes are exactly `[0, 1, 2]`. Add `approved_figure_case` in the new test module, returning `(inventory_bytes, review)` for that hand-specified fixture; derive its three expected glyph rectangles independently from the known source font, not the builder under test.

- [ ] **Step 1: Add failing contract and real-source tests.** Assert `numeric-prose`, `colon-prose`, `indent-20`, `indent-36` inventories contain every visible nonspace glyph, not inferred labels. For an approved known graph, assert exact index/text/bbox sets, exact source/document/media hashes and equality to the fixed UTF-8 JSON bytes including one LF. Assert only whitespace-only records are omitted; mixed/multi-character glyph text and decomposed accents survive exactly. Reject foreign/duplicate indexes, bool indexes, unordered lists, altered label text, empty evidence/reason, extra fields and wrong inventory digest. Passing known label groups cover the exact inventory once; missing one index fails; required-fix never passes publication even with complete coverage.
- [ ] **Step 2: Verify RED.** Run `P -m pytest -q tests/test_pdf_figure_review.py`. Confirm failures identify the missing inventory/approval behavior; fix fixture/import setup errors before recording behavioral RED.
- [ ] **Step 3: Implement the pure APIs.** Use source page `page.chars` indexes and the shared whole-glyph owner. Preserve document figure order and exact finite geometry. Reject cross-figure duplicate ownership, body overlap, partial glyphs and source/document/page/media inconsistency. No word-count, punctuation, numeric or first-line-indent approvals.
- [ ] **Step 4: Add failing anchored command/transaction tests.** `pdf-figure-review-input` routes to `translated-pdfs`; Unicode/space paths remain one native argument. Identical repeat generation leaves file bytes/identity unchanged. Differing existing evidence, linked/replaced input/run/media and injected publication failure produce nonzero/error and preserve old artifacts. Replace a held source/media/inventory with identical bytes: `verify()` refuses changed identity. Empty figures appear with `characters: []`; no-figure generation gives `figures: []`. No PDF authoring/render or source/translation mutation occurs.
- [ ] **Step 5: Verify transaction RED, then implement the context/writer/CLI.** Run `P -m pytest -q tests/test_pdf_figure_review.py tests/test_cli_contract.py -k 'figure_review or figure_input'` before and after. Add parser/handler and `_command_output_root` recognition; translate `PdfFigureReviewError` to the existing contract-failure exit. Use existing exact-child anchors and recoverable atomic publication utilities, not resolved-path or generic overwrite shortcuts.
- [ ] **Step 6: Verify and freeze.** Run `P -m pytest -q tests/test_pdf_figure_review.py tests/test_cli_contract.py tests/test_pdf_media.py`, then `P -m pytest -q -m 'not live'` and `git diff --check`. Retain named RED/GREEN/final commands, outputs/skips and scoped diff in the private ledger. Reviewers must not infer live-network or Windows execution from skips.
- [ ] **Step 7: Scoped commit and immediate push.** After independent review clears Important/Critical findings, commit only the Task 1 files with `feat: add source-bound PDF figure review input`; push `origin codex/pdf-commercial-layout`. Preserve pending controller CLI edits outside the new-command hunks. Record the pushed SHA before Task 2.

Core Step 1 assertions (imports use the Task 1 API; fixture returns a fresh mutable review):

```python
def test_exact_approved_label(approved_figure_case):
    inventory_bytes, review = approved_figure_case
    parsed = validate_pdf_figure_text_review(review, inventory_bytes=inventory_bytes)
    label = parsed["figures"]["pdf:page-0001:block-0002"]["labels"][0]
    assert label["character_indexes"] == [0, 1, 2]
    assert label["text"] == "API"

def test_missing_label_character_refuses(approved_figure_case):
    inventory_bytes, review = approved_figure_case
    label = review["figures"]["pdf:page-0001:block-0002"]["labels"][0]
    label["character_indexes"] = [0, 1]
    label["text"] = "AP"
    with pytest.raises(PdfFigureReviewError):
        validate_pdf_figure_text_review(review, inventory_bytes=inventory_bytes)

def test_required_fix_refuses_complete_coverage(approved_figure_case):
    inventory_bytes, review = approved_figure_case
    review["figures"]["pdf:page-0001:block-0002"]["verdict"] = "required-fix"
    with pytest.raises(PdfFigureReviewError):
        validate_pdf_figure_text_review(review, inventory_bytes=inventory_bytes)
```

### Task 2: Enforce approvals across publication and retain audit evidence

**Files:** Modify `src/web_translator/{pdf_review.py,pdf_assemble.py,pdf_qa.py,pdf_report.py,cli.py}`, `skills/pdf-translator/SKILL.md`, `tests/{test_pdf_figure_review.py,test_pdf_review.py,test_pdf_assemble.py,test_pdf_qa.py,test_pdf_pipeline.py,test_cli_contract.py,test_skill_contract.py,pdf_fixtures.py,pdf_unit_fixtures.py}`. Migrate only figure-bearing test fixture review evidence under `tests/fixtures/pdf/figures-captions-v1/` and any other existing figure-bearing fixture actually consumed by these tests; do not alter their source PDFs or unrelated translations. Scope new fixture approvals to the fixed inspected synthetic artwork.

**Interfaces consumed:** All Task 1 APIs. Existing `hold_pdf_semantic_inputs`, `validate_pdf_semantic_review_snapshot`, `assemble_pdf`, `prepare_pdf_qa`, `finalize_pdf_output`, `_semantic_review_from_value`, `PdfFinalManifest` remain callable with their current public signatures. Private figure-gate consumers may accept already-held snapshots/bytes to avoid rereading unchecked paths.

**Interfaces produced:** Existing publication APIs now require exact passing figure evidence for figure-containing native runs. The existing PDF semantic snapshot holds `figure-text-input.json` when required/present and includes its exact bytes/hash/length in `PdfSemanticReviewInput`. PDF master review supports independent optional `preserved_names` and `figure_text_review` fields; HTML still supports neither. QA/report schemas keep the spec's current versions and exact existing metric fields.

- [ ] **Step 1: Add failing publication-gate tests.** Parameterize stage `assembly`, `prepare`, `finalize` and invalid case `missing-inventory`, `missing-review`, `foreign-figure`, `omitted-character`, `duplicate-character`, `changed-text`, `changed-bbox`, `stale-source`, `stale-document`, `stale-media`, `stale-inventory`, `required-fix`. Assert each stage refuses with no new staged/final output; prepare/finalize retain preexisting staged/QA evidence. Cover direct `assemble_pdf` as well as CLI, so CLI-only validation cannot bypass the gate. For later-stage tests, first create valid staged/QA evidence, then mutate only the tested component.
- [ ] **Step 2: Add failing regression/compatibility tests.** Use real numeric/colon/20pt/36pt fixtures: no approval refuses all; explicit required-fix refuses all. Known sentence-shaped and horizontally separated graph labels pass only with exact approval. A prose-containing figure can never be automatically approved; tests do not assert a deterministic semantic classifier. Assert missing evidence in empty-text figures still refuses, while `labels: []` with source-specific evidence passes. Figure-free legacy semantic records round-trip unchanged; optional canonical empty inventory plus empty review passes and participates in the digest. Inventory without matching optional-empty approval, or a review without its inventory, refuses publication. Approved label/body overlap still refuses geometry checks.
- [ ] **Step 3: Verify RED.** Run `P -m pytest -q tests/test_pdf_figure_review.py tests/test_pdf_review.py tests/test_pdf_qa.py -k 'figure_review or figure_input or indented_or_numbered'`. Retain behavioral failures without accepting obsolete heuristic expectations as proof of semantic correctness.
- [ ] **Step 4: Integrate held semantic review and assembly.** Hold inventory as a root input, require it for any figures, and digest it without digesting `review.json` itself. Require optional PDF fields independently and validate them against bound exact inputs. Recompute source inventory via the Task 1 context and validate master approvals before staged authoring; keep source/media/review identities protected through publication. Reconcile the pending preserved-name/SQL corrections and keep every previously verified regression. Do not restore capitalization-based name masking or whole-SQL-block exemptions.
- [ ] **Step 5: Replace figure-QA heuristics and finalize checks.** Prepare/finalize use Task 1 source recomputation and approved complete coverage. Delete obsolete figure-only `_looks_like_diagram_label`, `_has_aligned_prose_lines`, `_looks_like_complete_prose_line` gating if they have no other consumer; preserve extraction safety and shared `figure_owns_character`. Keep current translatable overlap, media fidelity, fonts, source binding, bounds, rendering limits, stale-PDF and all other QA gates. For figure-review runs, extend the existing `publication.text_image_separation` finding's deterministic evidence with `inventory_sha256=<64 lowercase hex>` and `review_sha256=<SHA-256 of held exact review.json bytes>`, along with figure/character counts. Finalize recomputes and compares both bindings; an unchanged staged PDF with changed review bytes cannot reuse prior QA. Do not expand QA's exact metrics/top-level schema to store these hashes.
- [ ] **Step 6: Add failing report/schema tests, then integrate them.** Assert final manifest master review retains every approved index/text/reason, inventory hash and semantic-input file hash/length. `PdfFinalManifest` round-trips valid evidence and refuses malformed/foreign figure evidence where manifest document/figure evidence permits checking; source recomputation remains a publication-time check, not a parser claim. Report human summary and canonical embedded JSON retain equivalent complete evidence. Existing exact QA metrics do not gain fields; the `publication.text_image_separation` finding includes inventory digest and verified figure/character counts. HTML review reader rejects both PDF-only fields.
- [ ] **Step 7: Exercise identity/content races and stale approvals.** Swap held source/media/inventory/review with identical-byte replacements and inject changes during assembly, prepare and immediately before final rename. Assert refusal/no wrong final files and recoverable evidence. Include an unchanged staged PDF with changed approval verdict/reason; no cached PDF hash alone authorizes publication. Update fixed figure-bearing test fixtures without introducing any auto-pass helper into production.
- [ ] **Step 8: Update the skill workflow.** Add `pdf-figure-review-input` after extraction; require controller viewing every source figure plus source page, exact master label groups and reasons before final `pdf-review-input`. Keep original images, selectable translated captions/body/tables, same-agent translation retries and all-page final visual review. Read the applicable skill-authoring instructions before editing. Use an agent scenario with sentence-shaped labels versus absorbed numeric/colon prose to verify the consuming AI behavior, not just text-string assertions; retain that scenario result with the existing contract tests. No new translation dispatch if source/translations are unchanged.
- [ ] **Step 9: Verify and review the frozen unit.** Run `P -m pytest -q tests/test_pdf_figure_review.py tests/test_pdf_review.py tests/test_pdf_assemble.py tests/test_pdf_qa.py tests/test_pdf_pipeline.py tests/test_cli_contract.py tests/test_skill_contract.py`, then `P -m pytest -q -m 'not live'` and `git diff --check`. Record fresh evidence/limitations; independent reviewer covers semantic-binding, direct API enforcement, stale-review refusal, all original/residual regressions and report parity. Correct Important/Critical findings with the owning implementer before continuing.
- [ ] **Step 10: Commit and push.** Commit verified Task 2 files, including the pending preserved-name/SQL fixes and skill update, with `fix: require AI-reviewed PDF figure labels for publication`; immediately push `origin codex/pdf-commercial-layout`. No private real-book artifacts, release changes or source PDF changes are staged.

Step 7's `prepared_figure_publication` fixture in `tests/test_pdf_figure_review.py` uses Task 1's known diagram, valid test-only Korean translation, literal master approval, successful assembly/prepare and a test-only passing visual review. It returns the existing `PdfQARun` shape (`run_dir`, `output_dir`) so mutation tests invoke the real finalizer:

```python
def test_changed_review_refuses_unchanged_pdf(prepared_figure_publication):
    run = prepared_figure_publication
    staged = run.run_dir / "staged-output" / "translated.pdf"
    original_pdf = staged.read_bytes()
    path = run.run_dir / "review.json"
    review = json.loads(path.read_bytes())
    review["figure_text_review"]["figures"]["pdf:page-0001:block-0002"]["evidence"] += " Changed after QA."
    path.write_text(json.dumps(review, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(PdfQAFailure):
        finalize_pdf_output(run.run_dir, run.output_dir)
    assert staged.read_bytes() == original_pdf
    assert not run.output_dir.exists()
```

### Task 3: Controller-owned real first-50-page acceptance

**Files:** Only private evidence/current run, staged artifacts and the exactly reserved final directory. No production changes without a new scoped RED/fix/review cycle.

**Current run:** `/Users/starryeye/play/web-translator/.worktrees/pdf-commercial-layout/.web-translator/runs/designing-data-intensive-applications-pages-001-050-native-logical-units-post-owned-context-preflight3-20261009-085445`.

**Reserved output:** `/Users/starryeye/play/web-translator/.worktrees/pdf-commercial-layout/translated-pdfs/designing-data-intensive-applications-pages-001-050-native-logical-units-post-owned-context-preflight3-20261009-085445` (must remain absent until finalize).

- [ ] **Step 1: Freeze unchanged evidence.** Record hashes of source PDF/record, document, segments, glossary, zones, assignments/binding and translations. Confirm 50 source pages, 498 logical targets, 511 physical translated members, 13 joins and zero required flow findings. Preserve earlier runs/outputs and both old/current staged PDFs/layouts. Do not rerun acquisition/extraction or the authoring marker; its first-authoring operation already succeeded exactly once.
- [ ] **Step 2: Generate inventory and perform real direct AI review.** Run `P -m web_translator pdf-figure-review-input --run-dir <current-run>`. Inspect EVERY source figure crop plus its original page via `view_image`, using existing source renders where available. Controller records actual label groups/text/indexes and page-specific reasons in `review.json.figure_text_review`; do not copy fixture approvals or infer pass from valid schema. If crop owns genuine body/caption/table text, record required-fix and stop for source-ownership correction rather than granting approval.
- [ ] **Step 3: Renew semantic binding.** Run aggregate `validate-translations` and `pdf-review-input`; master reviews the same six semantic dimensions and incorporates the exact current digest. Preserve valid master name decisions. Assert all frozen source/translation/assignment hashes unchanged. No redundant seven-zone translation dispatch for unchanged text.
- [ ] **Step 4: Reassemble and prepare.** Recoverably rename only exact existing staging/layout targets to unique historical names, then run `pdf-assemble` and `pdf-qa prepare` with the current run/reserved output. Verify all source-bound figure, image integrity and existing automated gates; do not bypass an unexpected failure.
- [ ] **Step 5: Review every output page.** Read current `pdf-qa.json`; inspect every contact sheet with exact page coverage and detailed pages where necessary. Compare figure/caption/body separation to source pages. Write the six-field `pdf-layout-review.json` for all thirteen dimensions, bound to current PDF hash and exact coverage. Report genuine defects as required-fix; a passing suite is not commercial-layout acceptance.
- [ ] **Step 6: Finalize and inspect exact outputs.** Only after all gates pass, run `pdf-qa finalize`. Reopen final PDF and confirm selectable Korean, exact extended-Latin source names and unchanged artwork. Verify exactly three final files, approved figure decisions in manifest/report, current hashes and unchanged old artifacts. Return absolute links with source/output counts and preserved partial-input TOC/link warnings. No completion claim or final PDF link before successful finalize.

## Plan review and handoff

This plan covers the approved spec without adding extraction/translation features. Review Focus items are pinned above, existing name/SQL corrections are preserved, and uncertain semantic image ownership remains an AI judgment with explicit refusal rather than a new heuristic. Written plan approval is still required before implementation. The existing user-selected subagent-driven method is retained; do not ask them to choose it again.
