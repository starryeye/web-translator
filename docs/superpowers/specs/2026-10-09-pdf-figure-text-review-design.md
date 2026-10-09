# PDF Figure Text Review

Status: written specification approved on 2026-10-09; implementation awaits plan review.

## Intent and boundaries

Translate diverse selectable book/report PDFs directly with AI, without tuning the
plugin to one book. Preserve original artwork, including its intrinsic diagram labels,
while translating body paragraphs, captions and independently extracted table text.
Do not accidentally hide translatable prose inside a preserved figure crop.

The user approved replacing punctuation/indentation guesses with explicit AI/master
review of figure labels. The controller makes the semantic decision after viewing the
source page and artwork. Code verifies the source binding, exact character ownership,
complete approval coverage and unchanged artwork. Code does not certify that an AI
semantic judgment is correct; final visual review remains mandatory.

Existing constraints remain unchanged: selectable text only, reject scans/encryption,
500 source pages, 50 MiB, native 1.2 logical translation units, Korean-first terminology,
source trim size, original images, macOS/Windows support, and exactly `translated.pdf`,
`manifest.json`, `review-report.md` after successful finalization. Webpage contracts,
shared Segment/Translation/assignment formats and source extraction evidence do not
change. No OCR, machine-translation service, image-text editing or automatic override.

## Problem and alternatives

Current QA globally groups source words by vertical position, then uses word count,
numbers, colons, punctuation and left-edge alignment to classify figure text. This
both rejects legitimate separated graph labels and accepts some absorbed prose:
unpunctuated numeric/colon sentences and a conventional 36-point first-line indent.

1. Add more heuristic thresholds. Small change, but neither punctuation nor geometry
   establishes that text is semantically a diagram label; failures alternate.
2. Permit every string inside a figure. Simple, but silently preserves untranslated
   prose when the crop or detector is wrong.
3. Require source-bound AI label decisions and deterministic coverage checks. Adds a
   PDF-only review artifact/field, but makes semantic responsibility explicit and
   does not weaken geometric ownership or artwork integrity.

Option 3 is selected. Existing figure-QA text heuristics are not the approval gate
after this change. Extraction's conservative ownership/partition safety is not relaxed.

## Deterministic review input

Add `pdf-figure-review-input --run-dir <absolute-run-dir>`. After extraction and before
final semantic approval, it creates canonical `figure-text-input.json` in the private
run. It never rewrites document/segments/assignments/translations or authors a PDF.

The inventory has exactly `schema_version` (`1.0`), `source_sha256`,
`document_sha256`, and `figures`. Each figure has exactly `block_id`, `page_number`,
`bbox`, `media_sha256`, and `characters`, ordered by document block order. Include
every source figure, even figures with zero selectable characters.

For each non-whitespace owned source character, record exactly `index`, `text`, and
`bbox`. `index` is its zero-based position in the source page's complete `page.chars`
array, not a counter over labels. Preserve the exact non-normalized character string
and finite top-origin PDF-point rectangle. One glyph record may contain several
Unicode characters. Character records are ordered by index; ownership uses the existing
`figure_owns_character` policy, including refusal of partial-glyph overlap. A character
cannot belong to two figures or overlap a translatable block. Invalid geometry or
ambiguous ownership stops inventory generation, not just QA.

Compute SHA-256 from held exact source/document/media bytes. Verify source length/hash
against `source.json` and `document.json`. Canonical JSON is UTF-8, `ensure_ascii=False`,
sorted object keys, compact separators, `allow_nan=False`, followed by one LF. The
inventory's file-byte SHA-256 is the review binding; it contains no self-digest.

Use anchored file/directory access and atomic, no-overwrite publication. Repeat input
generation may validate and reuse an exactly identical inventory; it must refuse an
existing differing file. It must not silently replace review evidence. Parser/render
versions producing different exact inventories invalidate approval rather than guessing
equivalence. Retain existing source-size, render-budget, timeout and filesystem safety
limits. Avoid repeated whole-document renders: inspect existing source page renders
and source-derived figure media with `view_image`; render only missing review views
through the existing bounded renderer.

## Master AI review contract

Add optional PDF-only `figure_text_review` to `review.json`, alongside the existing
semantic review and optional `preserved_names`. It contains exactly:

```json
{
  "schema_version": "1.0",
  "inventory_sha256": "<SHA-256 of the exact canonical inventory file>",
  "figures": {
    "<source figure block ID>": {
      "verdict": "pass",
      "evidence": "Page-specific visual explanation of artwork and surrounding prose.",
      "labels": [
        {
          "character_indexes": [123, 124, 125],
          "text": "<exact concatenation of those source character strings>",
          "reason": "Why this is an intrinsic diagram label, not body or caption."
        }
      ]
    }
  }
}
```

The example is illustrative, not executable acceptance evidence. The figures map
exactly covers the inventory. Every figure has nonempty page-specific evidence and
`pass` or `required-fix`. Each label has exactly the three shown fields, nonempty
reason, and a sorted unique nonempty integer index array (booleans are not integers).
Its text must exactly match the indexed source character strings. Label bounds are
derived from inventory rectangles; callers cannot approve a broad rectangle containing
unlisted text. Within each figure, label indexes are disjoint. A passing figure's
labels exactly cover all its owned non-whitespace characters. Empty-text figures use
`labels: []` and evidence that the artwork was inspected. Foreign IDs/indexes, duplicate
ownership, missing/extra text and unknown fields are rejected.

The controller inspects EVERY source figure with its source-page context, not just QA
flagged candidates. Titles, axis labels, legends, node labels, arrows' explanations
and table-like data intrinsic to original artwork can be preserved when visually
identified as such. This includes sentence-shaped labels; no punctuation whitelist.
An independent explanatory paragraph, caption or ordinary table mistakenly absorbed
by a crop is `required-fix`, even if it contains numbers or resembles a short label.
Do not approve one all-figure label merely to cover everything. Evidence identifies
the visual role of each actual label group. If the distinction is uncertain, refuse
publication and retain the diagnostic for correction.

Any `required-fix` blocks publication even if all owned characters are listed. The
figure review's verdicts are checked independently of the existing six zone dimensions
and their `unresolved_required` list; that shared dimension contract remains unchanged.
Fix extraction ownership through the established fresh-native-run process when needed;
never edit physical source evidence or reassigned targets to clear a finding.

## Binding and lifecycle

1. Generate inventory; inspect all source figure/page views; record master decisions.
2. Run the existing `pdf-review-input` after translation/glossary and figure inventory
   are final. Include the exact inventory file bytes in the held semantic snapshot and
   canonical semantic digest when the document contains figures. This changes the
   digest of existing figure runs and requires renewed master approval. It does not
   require redispatching unchanged translations to AI.
3. Assembly recomputes the inventory from held source/document/media bytes and validates
   exact equality, review schema and full passing coverage before creating new staged
   output. Its held semantic snapshot includes the inventory; source/media/review files
   receive the existing held-identity/content protection throughout consumption.
4. QA prepare recomputes the inventory from held source/document/media bytes and compares
   it exactly with the approved inventory. It checks passing complete label coverage,
   translatable-block non-overlap and existing output-media fidelity. Remove numeric,
   colon and indent heuristics as a route to either automatic approval or rejection.
5. Finalize repeats these checks against held current evidence. Approval cannot bypass
   geometry ambiguity, replaced files, output image mismatch, stale staged-PDF hashes,
   any other automated gate, or the thirteen-dimension all-page visual review.

For figure-free PDFs, no new inventory or review field is required. An explicitly
generated empty inventory and matching empty review are allowed; they do not become
mandatory. Reject any supplied figure review with foreign figures. Historical
figure-containing runs remain readable
for diagnostics, but new assembly/QA/finalization refuses missing/stale inventory or
review. No silent legacy approval. Missing evidence yields a specific next-step error.
The native extraction schema stays 1.2; semantic input/QA/final manifest schemas retain
their current versions with exact optional review-field parsing, as already used for
`preserved_names`. Update every strict consumer consistently; old figure-free records
round-trip unchanged. HTML readers continue rejecting PDF-only review fields.

## Artifacts and implementation boundaries

Use a small `pdf_figure_review.py` module for inventory construction, exact contracts,
coverage validation and source-bound review consumption. Integrate existing CLI,
semantic snapshot, assembly, QA and report code without moving unrelated responsibilities.
Reuse the existing source-character ownership policy; do not duplicate its geometry rules.

The final manifest retains `figure_text_review`, including exact label indexes/text/
reasons and inventory digest, under master semantic review. The semantic-input file
evidence retains the inventory hash and length. QA's existing text/image-separation
finding reports inspected figure/character counts and approved inventory digest without
expanding its exact metrics schema. The report summarizes preservation decisions and
keeps the complete canonical manifest as it does now. No fourth public artifact.
The private inventory is not a substitute for visual inspection, and neither inventory
generation nor source-image viewing is a new PDF authoring-marker operation.

## Acceptance and remaining work

- Real selectable synthetic PDFs reproduce unpunctuated numeric/colon prose and 20/36pt
  indentation. No evidence means refusal, not automatic label acceptance.
- Multi-line sentence-shaped graph labels and horizontally separated labels pass only
  with exact master approvals. Do not build a book/page/font-name allowlist.
- Reject missing label coverage, duplicate/foreign indexes, changed character text or
  position, stale document/source/media/inventory hash, nonfinite geometry, partial
  glyph overlap, mixed translatable ownership and `required-fix` decisions.
- Preserve approved original artwork while captions/body/table text remain selectable
  Korean. Validate review-schema/report round trips and unchanged HTML contracts.
- Exercise no-figure and empty-text-figure cases, anchored replacement/race refusal,
  atomic failure cleanup, native macOS and existing Windows-path contracts. Do not
  claim actual Windows verification from macOS-only tests.
- Run relevant regression tests and the non-live project suite; independent code review
  must clear Important/Critical findings before actual publication.
- For the current first-50-page run, generate new evidence without altering source,
  physical extraction, assignments or translations. Inspect all original figures,
  renew semantic review/digest, preserve prior staging/layout recoverably, reassemble,
  prepare QA, inspect EVERY new contact sheet and required detail page, then finalize.
- The already clean font correction remains committed/pushed. Pending preserved-name/
  SQL fixes and unsuccessful figure heuristics are uncommitted: integrate the former
  after review, replace the latter rather than accumulating more thresholds. Commit
  only verified scoped work and push immediately after every commit.

This document authorizes no implementation by itself. After user review of this written
specification, write the implementation plan and obtain its review before coding.
