# Web Translator Codex Plugin Design

## Summary

`web-translator` is a Codex plugin for translating a public static web page into Korean while preserving the source page's structure, layout, styles, images, links, and offline readability. A master agent captures and partitions the page, delegates context-rich sections to translator subagents, reviews their work, and rebuilds the original DOM with approved translations.

The Windows-first MVP accepts one public `http` or `https` HTML URL and produces one offline HTML bundle. It does not support authenticated pages, CAPTCHA-protected pages, JavaScript-only applications, recursive site translation, PDF input, or PDF output.

Representative acceptance pages:

- <https://docs.spring.io/spring-ai/reference/concepts.html>
- <https://datatracker.ietf.org/doc/html/rfc8693>

## Goals

- Preserve the source DOM, layout, styling, images, tables, code blocks, anchors, and link behavior as closely as practical.
- Translate by document context and meaning rather than mechanical sentence substitution.
- Keep technical terms in English and add a Korean gloss in parentheses only at the term's first document-wide occurrence.
- Preserve code, URLs, identifiers, product names, protocol tokens, and RFC normative keywords such as `MUST`, `SHOULD`, and `MAY`.
- Use translator subagents for independent semantic zones and a master agent for planning, terminology, review, selective retry, assembly, and final acceptance.
- Produce an offline bundle that can be opened locally on Windows.
- Record enough structured intermediate data and review evidence to diagnose failures.

## Non-goals

- Authenticated, private, intranet, CAPTCHA-protected, or JavaScript-only pages.
- Translating linked pages or recursively mirroring a site.
- PDF input or output.
- Publishing or hosting generated pages.
- Replacing the page with a shared reader theme or redesigning its content.
- Pixel-identical screenshots after text reflow; Korean text length may legitimately alter line wrapping.
- A standalone translation service, hosted backend, or external translation API.

## User Experience

The user invokes the plugin by giving Codex a URL, for example:

```text
이 페이지를 번역해줘:
https://docs.spring.io/spring-ai/reference/concepts.html
```

The plugin detects the source language and starts a Korean translation without asking routine follow-up questions. Codex reports concise progress for capture, segmentation, translation, review, assembly, and QA. Only access failures or requirements outside the MVP stop for user input.

Each run writes to a new directory under the active workspace:

```text
translated-pages/<host>-<slug>-<timestamp>/
├── index.html
├── assets/
├── manifest.json
└── review-report.md
```

The completion response links to `index.html` and `review-report.md` using absolute local paths.

## Plugin Package

The repository is the plugin package and contains:

```text
.codex-plugin/plugin.json
skills/web-translator/SKILL.md
scripts/
tests/
docs/superpowers/specs/
```

The skill is the orchestration entry point. Local scripts perform deterministic capture, extraction, validation, assembly, and QA. Translation and semantic review remain agent tasks. The MVP does not require an MCP server or a separate API key.

## Architecture

The processing pipeline has six stages:

1. Capture the source page and its required assets.
2. Extract translatable semantic segments while protecting non-translatable content.
3. Have the master agent build a document outline, glossary, and translation zones.
4. Translate zones concurrently with shared terminology and overlapping context.
5. Have the master review all translations and selectively retry deficient zones.
6. Reassemble the original DOM and run structural, offline, and visual QA.

Deterministic scripts own source acquisition and DOM mutation. Agents exchange structured records and never independently edit the shared DOM. This boundary prevents concurrent file conflicts and makes every translation traceable to a source segment.

## Components

### Plugin entry skill

The skill validates the requested URL and output scope, creates a run workspace, invokes deterministic scripts, prepares master-agent context, dispatches translator subagents, reviews results, and reports the final artifacts. It adapts the number of zones to document size and available subagent concurrency rather than assuming a fixed agent count.

### Capture engine

The capture engine:

- accepts one public `http` or `https` URL;
- follows a bounded redirect chain;
- requires an HTML response;
- saves the final HTML, CSS, images, and fonts needed for offline rendering;
- rewrites captured asset references to local relative paths;
- keeps links to other pages as absolute source-site URLs; and
- keeps same-page fragment links local.

Failure to capture HTML or a critical stylesheet aborts the run. Missing optional images or fonts are recorded and retain their absolute source URL as a fallback.

### Semantic extractor

The extractor walks the captured DOM and emits stable segment records for headings, paragraphs, list items, table cells, captions, labels, alternative text, and other human-readable prose. It does not split inside a paragraph, table row, code block, or other indivisible semantic structure.

The extractor excludes or protects:

- `script`, `style`, code, and preformatted content;
- URLs and link targets;
- HTML tag and attribute syntax;
- commands, identifiers, variable names, and protocol tokens; and
- RFC normative keywords.

Inline markup inside prose is represented with protected placeholders so emphasis, links, and inline code return to their original DOM positions.

### Master planner

The master agent reads the document outline and segment metadata, then produces:

- a concise document-level context summary;
- a canonical glossary of English technical terms and Korean first-use glosses;
- protected terminology rules;
- zones aligned to semantic section boundaries; and
- overlapping neighbor context for each zone.

Zones contain independent target segments, but their prompts include the preceding and following section context when available. Context segments are read-only and cannot be returned as translated targets.

### Translator subagents

Each translator subagent receives the document summary, the same glossary, one target zone, neighboring context, and the structured output contract. It returns translations keyed by segment ID and may propose glossary observations separately.

Translator requirements are:

- understand the section's argument before translating;
- write natural Korean that preserves the source meaning and technical precision;
- keep technical terms in English;
- avoid adding explanations or omitting qualifications;
- preserve all protected placeholders exactly; and
- return only assigned target segment IDs.

Subagents write to separate zone result files. They do not edit the source DOM, shared glossary, or another zone's result.

### Master review gate

The master merges results and checks:

- segment completeness and uniqueness;
- meaning, qualifications, references, and logical relationships;
- natural Korean style rather than literal word substitution;
- terminology and tone across zone boundaries;
- preservation of protected content; and
- document-wide first-use gloss placement.

The canonical English term remains visible everywhere. Its Korean gloss is normalized to `English(한글)` at only the earliest eligible document occurrence. Because this is a document-wide rule, the master applies it after all zones are merged rather than trusting independent subagents to coordinate first use.

A deficient zone is returned to its assigned translator with concrete review findings. A zone receives at most two translation retries. Any unresolved required finding fails the run.

### Assembler

The assembler validates every translation record against the segment manifest, restores protected inline markup, and replaces only approved translatable DOM content. It preserves element hierarchy, IDs, classes, anchors, link targets, accessibility relationships, and non-translatable text.

The assembler adds unobtrusive source attribution outside the captured content boundary. The attribution links to the final source URL and does not alter the internal source-content hierarchy used for structural comparison.

### QA engine

The QA engine performs deterministic coverage, protection, DOM, link, asset, offline-load, overflow, and screenshot checks. It creates `review-report.md` from automated evidence and the master's semantic review. A run is complete only when every required check passes.

## Data Contracts

Each run has a private work directory containing:

```text
source.html
segments.jsonl
glossary.json
zones/
translations/
review.json
```

### Segment record

Every JSON Lines record includes:

- a stable segment ID;
- a DOM locator scoped to the captured content;
- semantic type and heading ancestry;
- source text with protected placeholders;
- protected placeholder values and types;
- neighboring context IDs; and
- whether the record is a translation target or read-only context.

### Translation record

Every translation result includes:

- the assigned segment ID;
- Korean text with every protected placeholder intact;
- translator notes only when ambiguity affects review; and
- glossary observations separated from the translation.

The schema rejects missing IDs, duplicate IDs, unassigned IDs, changed placeholders, and non-string translations.

### Manifest

`manifest.json` records:

- requested and final source URLs;
- capture timestamp and detected source language;
- target language and terminology policy;
- document and asset fingerprints;
- missing optional assets and fallback URLs;
- segment and zone counts; and
- final QA status.

## Link and Offline Asset Policy

- Same-page fragments continue to target local element IDs.
- Links to other pages remain absolute source URLs and are not translated.
- Captured assets use relative paths under `assets/`.
- Missing non-critical assets retain absolute origin fallbacks and produce warnings.
- The offline acceptance check blocks network access. Critical layout and content must remain usable; declared optional fallbacks may be unavailable during that check without hiding or corrupting required text.

## Error Handling

- Reject non-HTTP(S) schemes before network access.
- Reject non-HTML responses and unsupported authenticated or interactive pages with a clear reason.
- Bound redirect traversal and fail redirect loops.
- Abort on missing source HTML, critical CSS, invalid segment manifests, unresolved required translation findings, or failed assembly invariants.
- Continue with warnings for missing optional images or fonts when layout and content remain usable.
- Retry a failed translation zone no more than twice and preserve findings from every attempt.
- Never overwrite an existing output directory.
- Never present a partially translated page as a successful result.
- Preserve failed run data for diagnosis while omitting a successful completion marker.

## Testing Strategy

### Unit and fixture tests

- Extract headings, paragraphs, lists, tables, captions, nested inline markup, and accessibility text.
- Exclude scripts, styles, and code while protecting URLs, identifiers, commands, and RFC keywords.
- Reject missing, duplicate, foreign, or placeholder-corrupting translation records.
- Normalize English technical terms and exactly one first-use Korean gloss.
- Preserve DOM hierarchy, IDs, classes, links, and anchors during assembly.
- Create unique outputs without overwriting prior runs.
- Operate from Windows paths containing spaces and Korean characters.

### Integration tests

- Use the Spring AI Concepts page as the document-site fixture for side navigation, images, tables, code, and theme assets.
- Use RFC 8693 as the long-form standards fixture for numbered sections, cross-references, anchors, protocol identifiers, and normative language.
- Verify output loading with network access blocked.
- Verify different outcomes for critical and optional asset failures.
- Exercise retry exhaustion and ensure incomplete translations never receive success status.

Live source pages may change, so reproducible automated tests use versioned local snapshots. A separate opt-in live smoke test detects compatibility drift without making routine tests dependent on the network.

### Visual and structural QA

Compare source and output at desktop and narrow viewport sizes. Korean line wrapping may differ, but the output must have:

- no clipped or overlapping required content;
- no unintended horizontal page overflow;
- no broken critical images or styles;
- the same major layout regions and typographic hierarchy; and
- preserved table and code-block presentation.

Structural comparison ignores translated text nodes and the isolated attribution element, but requires the captured content's tag hierarchy, IDs, classes, and link structure to match.

### Semantic quality review

Translation quality is not reduced to a single mechanical similarity score. The master records section-level findings for semantic fidelity, natural Korean, terminology consistency, qualification preservation, and compliance with the English-term policy. `review-report.md` lists retries, unresolved warnings, automated checks, and final acceptance evidence.

## MVP Completion Criteria

The MVP is complete when both representative pages can produce offline HTML bundles on Windows and:

- every source segment maps to exactly one approved translation;
- all protected content remains intact;
- every required automated check passes;
- no required semantic review finding remains unresolved;
- the page loads with critical layout and content available offline; and
- `index.html`, `manifest.json`, and `review-report.md` are linked from Codex's completion response.

## Future Extensions

Future design cycles may add PDF input and output, authenticated browser capture, recursive site translation, selectable target languages, user-provided glossaries, and publishing. These features are explicitly outside this MVP and require separate specifications.
