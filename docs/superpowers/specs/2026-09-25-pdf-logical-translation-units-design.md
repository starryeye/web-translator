# PDF Logical Translation Units

Status: design approved on 2026-09-28; implementation awaits written-plan approval.

## Intent and boundaries

The user approved designing logical-paragraph translation across source pages while
preserving original page and position evidence. AI must translate the complete logical
unit directly; neither machine-translation services nor mechanical substitutions are
allowed. This extends the publication-quality PDF design, not the webpage workflow.

The current PDF pipeline assigns one translation target to each physical block. A
paragraph or word split by a source page boundary consequently becomes separate AI
targets. Read-only neighbor context cannot repair this ownership model: translators
must not move, duplicate, or omit text between assigned IDs.

This design covers adjacent-page body paragraphs and the continuation of one existing
list item. It does not introduce OCR, general table reconstruction, cross-page table
merging, new reference parsing, or a new footnote-body detector. Existing reference and
footnote handling remains explicit and must continue working. The separate bounded
local-column correction is a prerequisite, not part of this implementation.

All existing limits remain: selectable PDFs only, 500 source pages, 50 MiB, source trim
dimensions, unchanged original artwork, Korean-first PDF terminology, and exactly
`translated.pdf`, `manifest.json`, and `review-report.md` on successful publication.
Existing source files, diagnostic runs and published output are never overwritten.

## Options and selected direction

1. Keep physical targets and ask translators to coordinate fragments. This is small
   but violates target ownership and makes omission/duplication difficult to audit.
2. Replace physical blocks with synthetic cross-page blocks. This gives AI coherent
   text but invents a single page/bounding box and weakens links and provenance.
3. Keep physical blocks and add logical translation units. This requires PDF-specific
   contract and consumer changes, but preserves evidence and gives AI coherent input.

Option 3 is selected for the proposed design. It does not require changing the shared
Segment, Translation, zone-assignment or translator-result formats.

## Physical evidence and logical ownership

PDF document schema advances to `1.2`. `extracted_schema_version` records whether the
physical extraction was produced under 1.0, 1.1 or 1.2; a compatibility read must not
change that origin. Physical blocks keep their existing IDs, exact
source text, source page, bounding box, kind, semantic role and relationships. They
are not hidden as figures or ignored blocks, and no cross-page bounding box is created.

A new ordered `translation_units` collection owns all translatable text. Every eligible
physical block belongs to exactly one unit; figures, running furniture and empty table
cells belong to none. Most units contain one block. A multi-block unit contains only
one proved paragraph or list item in source reading order.

Each unit records:

- a stable `pdf:unit-000001`-style ID, ordered by its first member;
- ordered `source_block_ids`, with no duplicates or overlapping ownership;
- the unit's rendering kind and semantic role;
- its single `segment_id`; and
- one explicit join record for each successive pair of members.

Join records identify both blocks, the join operation (`space` or
`remove-discretionary-hyphen`), and the source-boundary evidence used to decide it.
Evidence includes the boundary line geometry, page dimensions, compatible typography,
column alignment, source order and structural-boundary checks. It is diagnostic
evidence, not permission for a caller to bypass the join rules.

The unit source text is derived from members and validated join operations; it is not
a second independently editable copy of the source. Physical `segment_id` mappings
must agree with their owning unit. Repeated mappings are valid only for members of
that same declared unit; undeclared duplicates remain contract failures.

`continuation_of` retains its existing reference-entry meaning. It is not repurposed
as general paragraph ownership. Unknown schema versions and malformed memberships
fail explicitly. Schema 1.0/1.1 compatibility creates singleton units, preserving
existing IDs and source text without inventing cross-page joins or quality approval.
New publication-quality acceptance requires fresh 1.2 extraction, not merely a reader
upgrade of an old run.

## Conservative continuation detection

Build logical ownership after page-local classification and reading order, before
segments or immutable assignments. Consider only the final eligible body/list fragment
of one page and the first eligible continuation on the immediately following page.
Recognized running furniture and linked page-edge notes are not continuation content;
they retain their own relationships. Do not skip an intervening blank source page.

A join requires all of the following:

- compatible body roles, typography and normalized column/text margins;
- page-tail and page-head line geometry consistent with a continued text flow;
- no intervening heading, opener, figure, table, caption, callout or reference boundary;
- no new list marker or paragraph-indent/spacing evidence that contradicts continuation;
- a single candidate predecessor and successor; and
- positive continuation evidence, rather than adjacency or lowercase text alone.

For a list item, the unmarked continuation must match the item's established text
margin; a new or nested marker starts another item. Render the original marker once.
A logical unit may span more than two adjacent pages by applying the same pairwise
checks at every boundary. Do not concatenate entire page bodies or sections.

Only a proven discretionary break may be removed. Preserve ordinary ASCII hyphens,
minus signs, identifiers and intentional punctuation. Geometry must support the two
fragments as one word; no book title, page number, font-name or vocabulary allowlist.
Otherwise a proved paragraph continuation uses a single separating space.

The detector distinguishes `join`, `separate` and `ambiguous`. An evident new paragraph
stays separate. A plausible but conflicting continuation creates a required finding
with source block/page evidence and prevents assignment and publication; it is not
silently called separate. Persist these in `flow_findings`, with a stable finding code,
required severity, both source block IDs and an evidence-backed explanation. Planning
and assignment preparation reject unresolved required findings before any AI dispatch.
This iteration does not add manual source-text editing or a generic override
API. Source review may reveal detector defects, which must be fixed and re-extracted
into a new run before assignments. It must not rewrite already assigned target IDs.

## AI assignment and protected content

Generate one ordinary shared Segment per logical unit. Keep its locator equal to the
first source member's existing block ID, including in new runs. The explicit unit map
resolves that representative to all members; it must not infer ownership from a shared
ID prefix. This preserves existing singleton locators while permitting one target to
own several declared physical members.

Join source fragments before general protected-token extraction so a product name or
identifier split at a discretionary break is handled as a complete token. Retain an
offset map back to physical fragments. Number, footnote-marker, URL and code protection
must retain exact values and multiplicities, with collision-free placeholders per unit.
Identical visible footnote markers on different source pages remain distinct by their
physical owner/note IDs and occurrences, not by marker text alone.

Shared zone planning treats a unit as indivisible. The configured character budget
(currently default 12,000) is not silently increased. An oversized unit fails with its
unit/member IDs and measured size; it is not split midword or sent beyond the limit.
Each isolated AI worker returns its assigned unit targets once. Master semantic review
checks all members, neighboring context, code/product preservation and join correctness.
No MT, fallback dictionary translation, or automatic approval of source joins by the
translating worker is introduced.

Assignment and review fingerprints must bind the unit membership and join map as well
as source/segment text. Changing a join invalidates downstream assignments, translation
reuse decisions, semantic approval and layout review. Reusing the same source PDF hash
or segment ID does not authorize reuse of an approval from a different unit map.

## Assembly, navigation and provenance

The assembler renders a logical paragraph or list item once, using the first member's
compatible semantic style. ReportLab may split the resulting Korean flow across output
pages. Original source page breaks do not impose output breaks within that unit.
Singleton specialized roles keep their existing layout behavior.

Output layout schema also advances to `1.2`. Each tracked text flowable identifies its
unit and the complete source-member list. Retain a representative first-member
`block_id` for compatible consumers, but QA must not mistake it for complete coverage.
Do not duplicate a flowable or fabricate separate output rectangles for each member.

Source block anchors resolve through the owning unit. A source destination inside a
merged unit may resolve to that paragraph's output start; report that granularity
instead of inventing a word-level coordinate. Keep explicit source ranges and URLs
for inline links; if transformed Korean text cannot support an exact range, preserve
the destination and report the limitation using the existing warning policy.

Footnotes belonging to any member must remain linked and render once. Assembly and
QA must support multiple distinct source owners within a unit, including repeated
visible markers, or fail explicitly before publication. Grouping is not permission to
drop an owner, choose the first note, or draw every note at the first source page.

The manifest records unit-to-source-member and unit-to-output-page mappings. Source
pages have a truthful many-to-many relationship with translated pages. Review reports
distinguish physical text-block count from AI translation-unit count. Keep
`translated_block_count` as physical translated-block coverage and add
`translation_unit_count` for the number of AI targets. Checks that previously equated
block count with target count must explicitly validate the new ownership map. Persist
the new metric contract under a version that older strict readers reject explicitly;
do not silently change an existing field's meaning.

## QA and compatibility gates

Publication additionally requires:

- exact one-time physical text coverage by valid, source-ordered units;
- exact one-time translation coverage by unit targets;
- deterministic normalized text and valid offset/protected-token mappings;
- each unit represented once logically, with contiguous output split-part evidence;
- no unowned, omitted, duplicated or independently rendered continuation fragment;
- preserved figures, tables, TOC/reference anchors and all member footnote owners;
- no unresolved required continuation findings; and
- semantic and all-page visual review of every joined boundary in the real trial.

Use the existing thirteen visual-review dimensions, especially semantic structure and
terminology readability; a new dimension is not needed just to rename this feature.

Tests cover two- and three-page paragraphs, an existing list item crossing a page,
discretionary word breaks, intentional hyphens, numeric/protected content, multiple
notes, shifted columns, unrelated paragraphs, new/nested lists, blank pages, headings,
tables, artwork, ambiguous evidence, oversized units and legacy schema reads. Include
generated selectable PDFs through extraction, immutable assignment, assembly and QA,
not only helper tests. Scaled and name-independent fixtures prevent source tuning.

Run the focused suites, full regression suite and a fresh real 50-page trial before
release. Validate old-output preservation, unchanged webpage English-first behavior,
Windows/macOS-compatible paths and the exact three-file publication contract. Prior
427-test evidence proves the completed note correction only, not this new design.

## Implementation ownership and approval gate

Keep logical-unit construction and validation in a focused PDF module rather than
adding an unrelated general parser to the already large layout file. Expected owners
are PDF models, extraction/segmentation, assembly/flowable evidence, QA/report/review
adapters and their tests. Shared translation records and webpage implementations remain
unchanged. PDF skill instructions must describe unit ownership and the new acceptance
gate when implementation is approved; that skill change requires its own contract tests.

This is an architectural addition. User review of this written design precedes an
implementation plan; review of that plan and execution choice precedes product-code
changes. The current local-column subtask may finish independently. No release is
claimed, and no existing run is upgraded or mutated merely by approving this design.
