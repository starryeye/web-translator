# Commercial-Quality PDF Translation Design

## Summary

Upgrade the `pdf-translator` workflow from structurally valid reflow to a
publication-quality Korean edition workflow. The translator will preserve the source
trim size and logical design language while allowing Korean text to repaginate. It will
recognize front matter, tables of contents, part and chapter openers, callouts,
references, figures, captions, and footnotes as distinct semantic structures instead of
flattening them into generic paragraphs and headings.

The design keeps the existing separation between the `web-translator` and
`pdf-translator` skills. Both workflows continue to share planning, translation,
protected-token, terminology, validation, and master-review modules. PDF extraction,
semantic layout, assembly, and visual QA remain format-specific.

## Motivation and Failure Evidence

The 50-page *Designing Data-Intensive Applications* trial produced a technically valid
30-page PDF, but page-by-page review found that it was not commercially acceptable:

- source pages 7 through 12 were flattened from a hierarchical table of contents into
  prose and detached runs of page numbers;
- the dedication and epigraph on source page 13 lost their dedicated-page composition;
- the Part I and Chapter 2 openers on source pages 21 and 47 were merged into preceding
  body content;
- the `Percentiles in Practice` callout on source page 36 was rasterized as an English
  image even though its text was selectable in the source;
- a callout icon on source page 34 was separated from the callout it introduced;
- references on source pages 43 through 45 became dense run-on paragraphs;
- running furniture and footnote fragments leaked into body text; and
- aggressive preservation of English glossary terms produced unnatural Korean prose.

The existing QA passed this output because it verifies extractability, glyphs, bounds,
and basic figure relationships, but does not verify publication semantics.

## Goals

- Preserve source trim dimensions and recognizable editorial structure while allowing
  Korean text to change page count and line breaks.
- Keep all translatable source text selectable and searchable in the output.
- Preserve original images and true graphical artwork without translating text embedded
  in those images.
- Never rasterize selectable body, caption, callout, table, or reference text merely
  because it is surrounded by vector decoration.
- Give front matter, part openers, chapter openers, callouts, references, and footnotes
  dedicated layout policies.
- Rebuild table-of-contents navigation against the translated document when a target is
  present.
- Produce natural Korean terminology while retaining identifiers, code, acronyms, and
  proper product names exactly.
- Reject output that is technically readable but fails the publication-quality contract.
- Re-run and inspect the same 50-page trial before release.

## Non-goals

- Pixel-identical reproduction of source pages.
- Preservation of source page count or source line breaks.
- OCR or translation of scans or text that is genuinely embedded in an image.
- Re-illustration, localization, or recreation of artwork.
- Automated font matching to arbitrary proprietary source fonts.
- Fabricating destinations for table-of-contents entries whose target pages are not part
  of a partial input PDF.

## Considered Approaches

### 1. Patch the current block kinds with document-specific heuristics

This is the smallest change, but it would encode assumptions from one book into generic
paragraph and heading handling. It would remain fragile for other publishers and would
not give QA a stable semantic contract. This approach is rejected.

### 2. Preserve page canvases and overlay translated text

This retains visual similarity, but Korean expansion would overlap artwork, require
aggressive shrinking, or leave large blank regions. It also makes selectable-text
ownership difficult to prove. This approach is rejected.

### 3. Semantic extraction with role-aware reflow

This approach adds semantic roles to the PDF document model, separates real graphics
from decorated text containers, and lets the assembler choose a template for each role.
It requires a contract migration and broader tests, but generalizes across books,
reports, and technical papers. This is the approved approach.

## Document Contract

The PDF document contract advances from schema `1.0` to `1.1`. `PdfBlock.kind` remains
the low-level content type used by the shared translation pipeline. A new
`semantic_role` field carries editorial meaning without multiplying translation kinds.

The initial role vocabulary is:

```text
body
toc-title
toc-part
toc-chapter
toc-entry
dedication
epigraph
epigraph-attribution
part-label
part-title
chapter-label
chapter-title
callout-title
callout-body
reference-heading
reference-entry
```

Headers, footers, page numbers, tables, figures, captions, and footnotes retain their
existing `kind`; their default role is `body` unless one of the specialized roles is
applicable. New runs always write schema `1.1`. The reader accepts schema `1.0`
documents only through an explicit compatibility upgrade that assigns conservative
roles; it never silently claims publication-quality QA for an upgraded legacy run.

Role classification evidence is deterministic and derived from page position, font
clusters, alignment, whitespace, numbering, neighboring lines, and cross-page patterns.
The persisted block record remains the source of truth for assembly and QA.

## Semantic Extraction

### Running furniture

Running headers, footers, and page numbers are detected as complete lines and as
composite lines such as `Chapter title | 27`. Detection uses edge-band position,
neighboring-page sequences, alternating sides, normalized labels, and roman or decimal
page tokens. A single page may inherit the running pattern established by adjacent pages
in the same section. Running furniture is never emitted into the body story.

### Front matter and openers

A sparse page with centered prose, strong surrounding whitespace, and no body-flow
continuation may become a dedication or epigraph. Attribution lines are linked to the
preceding epigraph. Large `PART`, `CHAPTER`, or numbered equivalents create paired label
and title roles. These rules use layout evidence and source text patterns together; when
the evidence conflicts, extraction records an ambiguity and publication QA requires a
review rather than guessing.

### Table of contents

TOC recognition groups title, part, chapter, and entry lines across consecutive pages.
Dot leaders, right-aligned numeric columns, indentation, font weight, and link
destinations become structured entry evidence. The source page-number column is attached
to its corresponding entry rather than emitted as a paragraph. Entry hierarchy and
source order are preserved.

When an entry destination exists in the supplied PDF, the output links and printed page
number refer to the translated output anchor. For a partial input whose TOC refers beyond
the supplied pages, the source reference remains visible without a fabricated link and
is recorded as an unresolved-excerpt warning rather than a required failure.

### References

A references heading starts a reference section. Bracketed or numbered entry markers
split entries even when the extractor originally joins several entries into one line or
paragraph. Wrapped continuations remain attached to their entry across source pages.
Each reference is a separate translation target and preserves names, titles, identifiers,
URLs, DOI values, ISBN values, and publication facts through protected tokens.

### Decorated text and callouts

Figure detection distinguishes true graphical content from a decorated text container.
Before a graphic region is accepted, the extractor measures overlap with selectable
characters and text blocks:

- raster objects and vector artwork with no owned translatable text remain figures;
- small icons next to prose remain figures but are grouped with the adjacent callout;
- borders, rules, or fills surrounding selectable text become callout decoration, while
  the title and body remain selectable translation blocks; and
- a candidate that would consume unclassified selectable text is rejected with page and
  bounds evidence.

Character assignment counts text inside accepted figures only when the text is genuinely
part of the artwork. Selectable text cannot be hidden from translation coverage by a
figure crop.

### Line repair and footnotes

Line merging repairs discretionary hyphens and font-run splits only when geometry proves
that adjacent fragments form one token. It must join examples such as `Post-` plus
`greSQL` and footnote continuations such as `alternating` plus `current` without joining
separate columns or list items.

Footnote bodies are grouped before segmentation, including wrapped lines. Each body is
linked to an owner marker. Ambiguous ownership remains a required finding.

## Terminology Policy

The shared terminology module gains an explicit display policy rather than a PDF-only
fork:

- `english-first`: the existing webpage-compatible behavior;
- `korean-first`: the PDF publication behavior.

For `korean-first`, the first visible occurrence is rendered as
`한국어(English)` and later occurrences use the canonical Korean term. Registered product
names, code, commands, identifiers, URLs, protocol keywords, and acronyms remain exact.
Numbers are protected and are never translated or reformatted by terminology
normalization. Placeholder boundaries remain opaque using the existing protected-token
metadata.

The manifest and review report identify the selected policy. Existing webpage behavior
does not change unless that workflow explicitly selects a different policy in the
future.

## Role-Aware Assembly

The assembler preserves the source trim size and uses embedded Korean fonts. It applies
the following policies:

- dedication, epigraph, part, and chapter opener groups start on a fresh page and are
  vertically composed as a unit;
- labels, titles, epigraph text, and attribution retain their hierarchy, alignment, and
  grouping;
- an opener may share its page with introductory body content only when the source does;
- TOC entries use indentation, dot leaders, right-aligned page references, internal
  anchors, and multi-pass page-number resolution;
- callout icon, title, decoration, and body are kept together when they fit, and continue
  using an explicit callout continuation style when they do not;
- reference entries use hanging indents, one entry per paragraph, and controlled spacing;
- figures and captions remain grouped and retain source aspect ratio without upscaling;
- page-local footnotes stay with the owning page when they fit, otherwise continue with
  an explicit continuation marker; and
- generated provenance is stored in PDF metadata, `manifest.json`, and
  `review-report.md`, not inserted as a visible final body block.

Normal body paragraphs are left-aligned with Korean-appropriate leading and paragraph
spacing. The assembler does not shrink body text below the existing minimum to force a
source page count.

## Publication QA

Existing contract, structural, rendering, and visual QA remain mandatory. The visual
review contract adds these canonical dimensions:

```text
semantic_structure
toc_navigation
reference_formatting
text_image_separation
terminology_readability
```

Automated QA additionally requires:

- every specialized role to appear in an allowed layout template;
- every part and chapter opener to begin on a fresh output page;
- every linked TOC entry to display the actual anchored output page;
- every reference entry to remain separately traceable in output layout records;
- no running header, footer, or source page number to appear as a body flowable;
- no source selectable text classified as translatable to be present only inside a
  figure image;
- no visible final provenance block;
- no unintentional blank page;
- no detached callout icon, title, or caption; and
- Korean prose blocks to remain below the configured Latin-script density threshold
  after protected products, code, identifiers, URLs, acronyms, and references are
  excluded.

The threshold is diagnostic evidence, not a license to replace legitimate protected
terms. A failure names the page, block, observed ratio, and exclusions. Visual review
must inspect every rendered page and provide nonempty evidence for all old and new
dimensions.

## Error Handling

The workflow fails closed when it cannot safely distinguish figure text from decorated
selectable text, cannot resolve a required opener or footnote relationship, loses a TOC
target that exists in the input, emits running furniture as body content, or cannot map a
reference entry through assembly. Partial-input TOC destinations outside the supplied
page range are warnings with explicit evidence.

No failed or superseded run overwrites an existing final output. Diagnostic run artifacts
remain private under `.web-translator/runs`.

## Implementation Boundaries

- `pdf_models.py`: schema 1.1 and semantic-role contracts.
- `pdf_layout.py`: running furniture, TOC, opener, reference, callout, and line-repair
  classification.
- `pdf_media.py`: text-aware graphic-region partitioning.
- `pdf_extract.py`: role persistence, text ownership, segmentation, and compatibility
  migration.
- `terminology.py`: shared configurable English-first and Korean-first normalization.
- `pdf_assemble.py`: role-aware flowables, multi-pass TOC, openers, callouts, references,
  footnotes, and metadata-only provenance.
- `pdf_qa.py` and `pdf_report.py`: publication gates, visual dimensions, and evidence.

HTML capture, HTML extraction, and HTML assembly remain unchanged. Shared terminology
changes require the existing webpage tests to prove compatibility.

## Test Strategy

Development follows test-driven development. Focused failing tests are added before each
behavioral change:

- schema 1.1 serialization, strict validation, and schema 1.0 migration;
- composite and inherited running-furniture detection;
- TOC hierarchy, numeric-column pairing, partial destinations, and output page anchors;
- dedication, epigraph, part opener, and chapter opener detection and pagination;
- references split across paragraphs and source pages;
- selectable text inside vector boxes remaining text while nearby icons remain images;
- line-fragment and footnote-continuation repair;
- Korean-first first-use normalization, later Korean-only use, protected terms, and
  unchanged webpage English-first behavior;
- role-specific ReportLab styles and grouping;
- removal of visible source attribution;
- every new automated and visual QA failure mode; and
- end-to-end fixture generation with exactly `translated.pdf`, `manifest.json`, and
  `review-report.md` in the published directory.

The complete existing test suite must pass before release.

## Real-Document Acceptance

After automated tests pass, create a new run for the existing 50-page trial PDF. Do not
overwrite the previous diagnostic run or its final output. Reuse approved translations
only when their source segment text and protected-token contract match exactly; newly
split or changed segments require validation and review again.

Acceptance requires page-by-page comparison of every source page to its mapped output
pages and explicit confirmation that:

- the TOC hierarchy and page references are readable;
- the dedication, epigraph, Part I, and Chapter 2 openers are intentionally composed;
- the page 34 callout icon remains attached;
- the page 36 callout text is Korean and selectable rather than rasterized;
- references are separated with hanging indents;
- running furniture and footnote fragments do not enter body prose;
- terminology reads naturally in Korean;
- figures remain sharp and captions remain adjacent; and
- no page has clipping, overlap, accidental blank space, broken glyphs, or visible build
  metadata.

The final publication directory must contain exactly the three required artifacts.

## Release

This is a backward-compatible skill surface with a materially expanded PDF contract and
output policy, so the plugin minor version advances to `0.6.0`. Commit and push the
design, implementation, and release changes separately using the repository's existing
commit style. After the plugin release is tagged and pushed, update the external
marketplace repository to reference `0.6.0`, commit, and push that change as well.

## Completion Criteria

The work is complete only when:

- all focused and full-suite tests pass;
- the new 50-page run passes structural, rendering, semantic, and visual QA;
- every output page has been compared with its source mapping;
- the final directory contains exactly the three required files;
- no known publication-quality finding remains unresolved; and
- plugin `0.6.0`, its tag, and the marketplace reference are pushed.
