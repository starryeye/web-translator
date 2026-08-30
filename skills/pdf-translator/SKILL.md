---
name: pdf-translator
description: Use when translating exactly one text-selectable local path, attached file, or public HTTP(S) PDF URL into a reviewed Korean PDF. Do not use for HTML pages, scans, encrypted PDFs, or multiple inputs.
---

# PDF Translator

Translate one supported PDF through format-specific acquisition, extraction, assembly,
and visual QA while reusing the package's shared `Segment`, zone, assignment, translation,
and review contracts. Never route PDF input into `web-translator`; that skill remains only
for one supported public static HTML URL.

Before dispatch, read the shared
[translator contract](../web-translator/references/translator-contract.md) and
[assignment package](../web-translator/references/assignment-package.md) completely.
Before semantic review, read the shared
[review rubric](../web-translator/references/review-rubric.md) completely. These are the
same contracts used by HTML translation; do not invent a separate PDF translation
contract or copy the reference files into this skill.

## Platform execution contract

Detect the active OS and shell yourself. Never ask the user to choose a platform.
Resolve the repository-local virtual-environment interpreter once, keep its absolute
path, and do not depend on environment activation:

- Windows PowerShell:

  ```powershell
  $python = (Resolve-Path ".\.venv\Scripts\python.exe").Path
  ```

- macOS or Linux with a POSIX shell:

  ```sh
  python="$(cd .venv/bin && pwd -P)/python"
  ```

Every `<python>` token below means the native invocation prefix for that resolved path:
PowerShell: `& $python`; POSIX: `"$python"`. Replace every remaining token with a native
variable before executing a command:

- PowerShell: `<source>` → `$source`, `<work-dir>` → `$workDir`, and
  `<output-dir>` → `$outputDir`.
- POSIX: `<source>` → `"$source"`, `<work-dir>` → `"$work_dir"`, and
  `<output-dir>` → `"$output_dir"`.

Never execute a placeholder literally. Never build or evaluate a command string. Invoke
the resolved interpreter directly. Pass the source and every filesystem path as one
argument so spaces and non-ASCII characters such as Korean remain intact. In PowerShell,
resolve local sources with `Resolve-Path -LiteralPath`; never interpolate them into an
executable string.

## Supported input and paths

Accept exactly one text-selectable PDF supplied as a readable local path, an attached
file exposed through a readable local path, or a public HTTP(S) URL. For an attachment,
use the platform-provided local path as the source value. Reject zero or multiple inputs,
HTML input, directories, private or authenticated URLs, scans, encryption, malformed
PDFs, files larger than 50 MiB, and documents over 100 pages. `pdf-acquire` and
`pdf-extract` enforce these limits and must stop the run on a nonzero exit. Never bypass
a rejection and never pass a PDF to the HTML `capture` or `extract` commands.

Create unique paths with
`web_translator.paths.create_pdf_run_paths(web_translator.paths.lexical_workspace(), source_label,
datetime.now(UTC))`.
Keep its returned `work_dir` and reserved, unused `output_dir` absolute. The final output
directory must not exist before finalization. Never resolve either result: link resolution
erases the evidence needed to reject a symlink or reparse point. Verify that
`work_dir.parent == workspace / ".web-translator" / "runs"` and
`output_dir.parent == workspace / "translated-pdfs"`, where
`workspace = web_translator.paths.lexical_workspace()`. Each returned path must be an exact child of the
intended held root. Abort on a linked, dangling, replaced, or moved run/root/ancestor.

Bind exactly one source value, then allocate the paths through the resolved interpreter.
For a local or attached file, use its absolute native path; for a public URL, assign the
URL directly. These examples show the two source forms—execute only the applicable
assignment:

- Windows PowerShell:

  ```powershell
  # For a local or attached file:
  $source = (Resolve-Path -LiteralPath 'C:\자료 폴더\분기 보고서.pdf').Path
  # For a public URL instead, replace the line above with:
  # $source = 'https://example.com/reports/분기-보고서.pdf'

  $allocationJson = & $python -c 'import json, sys; from datetime import UTC, datetime; from web_translator.paths import create_pdf_run_paths, lexical_workspace; workspace = lexical_workspace(); paths = create_pdf_run_paths(workspace, sys.argv[1], datetime.now(UTC)); expected_run_root = workspace / ".web-translator" / "runs"; expected_output_root = workspace / "translated-pdfs"; (paths.work_dir.parent == expected_run_root and paths.output_dir.parent == expected_output_root) or (_ for _ in ()).throw(RuntimeError("allocated paths are not exact children of intended roots")); print(json.dumps({"work_dir": str(paths.work_dir), "output_dir": str(paths.output_dir)}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))' $source
  if ($LASTEXITCODE -ne 0) { throw 'PDF run-path allocation failed.' }
  try {
      $allocation = $allocationJson | ConvertFrom-Json -ErrorAction Stop
  } catch {
      throw 'malformed allocation output: invalid JSON'
  }
  if ($null -eq $allocation) {
      throw 'malformed allocation output: expected a JSON object'
  }
  $allocationFields = @($allocation.PSObject.Properties.Name)
  if ($allocationFields.Count -ne 2 -or
      $allocationFields -notcontains 'work_dir' -or
      $allocationFields -notcontains 'output_dir' -or
      $allocation.work_dir -isnot [string] -or
      $allocation.output_dir -isnot [string] -or
      -not [IO.Path]::IsPathRooted($allocation.work_dir) -or
      -not [IO.Path]::IsPathRooted($allocation.output_dir) -or
      -not (Test-Path -LiteralPath $allocation.work_dir -PathType Container) -or
      (Test-Path -LiteralPath $allocation.output_dir)) {
      throw 'malformed allocation output: expected one existing absolute work_dir and one unused absolute output_dir'
  }
  $workDir = $allocation.work_dir
  $outputDir = $allocation.output_dir
  ```

- macOS or Linux with a POSIX shell:

  ```sh
  # For a local or attached file:
  source="/tmp/자료 폴더/분기 보고서.pdf"
  # For a public URL instead, replace the line above with:
  # source="https://example.com/reports/분기-보고서.pdf"

  allocation_json=$("$python" -c 'import json, sys; from datetime import UTC, datetime; from web_translator.paths import create_pdf_run_paths, lexical_workspace; workspace = lexical_workspace(); paths = create_pdf_run_paths(workspace, sys.argv[1], datetime.now(UTC)); expected_run_root = workspace / ".web-translator" / "runs"; expected_output_root = workspace / "translated-pdfs"; (paths.work_dir.parent == expected_run_root and paths.output_dir.parent == expected_output_root) or (_ for _ in ()).throw(RuntimeError("allocated paths are not exact children of intended roots")); print(json.dumps({"work_dir": str(paths.work_dir), "output_dir": str(paths.output_dir)}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))' "$source") || exit 1
  allocation_field() {
      "$python" -c '
import json, os, sys
from web_translator.paths import lexical_workspace
try:
    data = json.loads(sys.argv[1])
    if type(data) is not dict or set(data) != {"work_dir", "output_dir"}:
        raise ValueError
    if any(type(data[name]) is not str or not data[name] or not os.path.isabs(data[name]) for name in data):
        raise ValueError
    workspace = os.fspath(lexical_workspace())
    if os.path.dirname(data["work_dir"]) != os.path.join(workspace, ".web-translator", "runs"):
        raise ValueError
    if os.path.dirname(data["output_dir"]) != os.path.join(workspace, "translated-pdfs"):
        raise ValueError
    if os.path.islink(data["work_dir"]) or not os.path.isdir(data["work_dir"]):
        raise ValueError
    if os.path.lexists(data["output_dir"]):
        raise ValueError
    key = sys.argv[2]
    if key not in data:
        raise ValueError
except (json.JSONDecodeError, IndexError, KeyError, TypeError, ValueError):
    raise SystemExit("malformed allocation output")
print(data[key], end="")
' "$1" "$2"
  }
  work_dir=$(allocation_field "$allocation_json" work_dir) || exit 1
  output_dir=$(allocation_field "$allocation_json" output_dir) || exit 1
  ```

The fixed Python program receives the complete source as `sys.argv[1]`, serializes only
the two paths as deterministic JSON, and never interpolates the source into code. Never use `eval`,
shell command-string construction, or text splitting to recover paths. Stop
if allocation or strict JSON parsing fails; do not guess either directory.

## Master workflow

Run the following commands in this exact order. Substitute the completed zone ID for
`zone-001` and run that per-zone command once for every zone:

```text
<python> -m web_translator pdf-acquire <source> --run-dir <work-dir>
<python> -m web_translator pdf-extract --run-dir <work-dir>
<python> -m web_translator plan-zones --run-dir <work-dir> --max-chars 12000 --target-zones 3
<python> -m web_translator prepare-assignments --run-dir <work-dir>
<python> -m web_translator validate-translations --run-dir <work-dir> --zone-id zone-001
<python> -m web_translator validate-translations --run-dir <work-dir>
<python> -m web_translator pdf-review-input --run-dir <work-dir>
<python> -m web_translator pdf-assemble --run-dir <work-dir> --output-dir <output-dir>
<python> -m web_translator pdf-qa prepare --run-dir <work-dir> --output-dir <output-dir>
<python> -m web_translator pdf-qa finalize --run-dir <work-dir> --output-dir <output-dir>
```

Apply these requirements at each stage:

1. After `pdf-extract`, read `document.json`, `segments.jsonl`, and every `zones/*.json`
   after zone planning. Treat each PDF locator as opaque. Build one concise document
   outline and the same document summary for every translator. Write that summary to
   `document-summary.txt` and write `glossary.json` as the canonical mapping of retained
   English technical terms to Korean glosses. Preserve the exact target partition:
   every target `Segment` ID appears once, no context ID becomes a target, and no target
   is added or removed.

2. Run `prepare-assignments` only after the shared summary and glossary exist. Its
   immutable packages contain the same summary and glossary, exactly one zone's target
   records, and bounded read-only neighbor context. Do not paste source records into
   agent prompts or modify a package after dispatch.

3. Create `translations/`. Schedule one zone per available agent slot using `spawn_agent`
   with `fork_turns="none"` and `reasoning_effort="medium"`; queue remaining zones. Each
   fresh agent receives only the absolute immutable assignment-package path, the absolute
   shared translator-contract path, one absolute destination
   `translations/<zone-id>.jsonl`, and a request to return that path plus a short ambiguity
   note. Require one zone result file per fresh agent identity. Translators must not edit
   source evidence, shared context, another zone, or aggregate files.

4. As soon as a zone result exists, run deterministic result validation before master
   semantic review, including while other translators are still running. On schema,
   coverage, ID, or protected-token failure, send concrete findings with `followup_task`
   to the same agent that owns the zone, then revalidate its replacement. Never move a
   failed zone to the first free agent.

5. The controller is the master semantic reviewer. Review every zone and both neighbor
   boundaries against all six dimensions in the shared rubric. Record nonempty written
   evidence for semantic fidelity, qualification preservation, naturalness, terminology,
   boundary consistency, and protected content. For `required-fix`, send only affected
   IDs, source, output, and correction to the same agent with `followup_task`. Allow a
   maximum of two retries after the initial attempt and revalidate every replacement.
   This is the required master semantic review; deterministic validation cannot replace
   it.

6. After every zone is valid, run aggregate `validate-translations`. Normalize first-use
   glossary placement only after master judgment. Once `segments.jsonl`, every zone and
   assignment, every translation file, and the glossary policy/content are final, run
   `pdf-review-input`. Read `semantic-review-input.json` and copy its exact
   `semantic_input_sha256` into the PDF-only `review.json`; this canonical digest binds
   the exact reviewed bytes and policy. Write the remaining review fields using the
   package's existing review contract: `retries` and `section_findings` exactly cover all
   planned zones, each zone has all six canonical dimensions once with `pass` or
   `required-fix` plus nonempty evidence, and `unresolved_required` is the sorted unique
   set of every `zone-ID:dimension` marked `required-fix`. Assembly, QA preparation, and
   finalization each reject any post-review mutation. The webpage `review.json` contract
   stays unchanged. Do not assemble until unresolved findings are empty.

7. Run `pdf-assemble`, then `pdf-qa prepare`. Assembly creates only the private staged
   PDF; prepare performs automated contract, structure, font, rendering, bounds, and page
   checks and creates `pdf-qa.json` plus numbered PNGs under `qa-pages/`. It does not
   publish the reserved output directory.

Rendering is bounded before, during, and after Poppler/Pillow work: at most 36,000,000
pixels per page, 360,000,000 rendered pixels for the complete source/output PDF,
64 MiB encoded bytes for one PNG, and 1 GiB encoded bytes for the complete rendered-page
set. Decoded dimensions and pixels are checked before full Pillow decode. Output growth
is monitored and the timed subprocess is terminated on a limit breach. Platforms that
cannot impose an OS-level address-space limit still enforce all deterministic geometry,
encoded-byte, decoded-pixel, and timeout limits.

A valid detected standalone uncaptioned figure is preserved once with `caption_id=None`;
only an existing figure-caption relationship must be reciprocal and unambiguous. Every
unreconstructed visible link remains a warning only when its visible label and destination
are retained in both `manifest.json` and `review-report.md`. Missing visible link text is
a required failure.

## Required visual review

After `pdf-qa prepare` succeeds, read `pdf-qa.json`. Inspect every numbered contact sheet
in `qa-pages/` with `view_image`; do not infer visual quality from automated metrics or
inspect only a sample. Confirm the sheets collectively cover every rendered page exactly
once, then inspect pages at higher detail when a contact sheet exposes a possible defect.

Judge exactly the eight canonical dimensions: `heading_hierarchy`, `text_legibility`,
`table_legibility`, `figure_caption_pairing`, `footnote_placement`, `page_transitions`,
`clipping_overlap`, and `glyph_rendering`. Write `pdf-layout-review.json` with exactly the
six top-level fields shown below. Copy `staged_pdf_sha256`, page coverage, and contact-sheet
coverage exactly from the current `pdf-qa.json`; do not reuse the sample values. Every
finding has exactly `verdict` and nonempty `evidence`. A verdict is only `pass` or
`required-fix`. `pages_reviewed` is a sorted, unique integer array such as `[1]`.
`contact_sheets_reviewed` maps each filename string to a sorted, unique integer array such
as `{"contact-sheet-001.png":[1]}`. `unresolved_required` is a sorted, unique string array
containing exactly the dimensions currently marked `required-fix`. Page numbers are JSON
integers without quotation marks; never emit string-valued page arrays.

```json
{
  "schema_version": "1.0",
  "staged_pdf_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "pages_reviewed": [1],
  "contact_sheets_reviewed": {"contact-sheet-001.png": [1]},
  "findings": {
    "heading_hierarchy": {"verdict": "pass", "evidence": "Heading levels remain visually distinct on page 1."},
    "text_legibility": {"verdict": "pass", "evidence": "Korean body text is readable at contact-sheet and page detail."},
    "table_legibility": {"verdict": "pass", "evidence": "Tables remain readable with intact rows, columns, and cell content."},
    "figure_caption_pairing": {"verdict": "pass", "evidence": "Each figure remains adjacent to its matching caption."},
    "footnote_placement": {"verdict": "pass", "evidence": "Footnotes remain legible and associated with their page content."},
    "page_transitions": {"verdict": "pass", "evidence": "Paragraphs, lists, and sections transition coherently across pages."},
    "clipping_overlap": {"verdict": "pass", "evidence": "No text, table, figure, or footer is clipped or overlaps peer content."},
    "glyph_rendering": {"verdict": "pass", "evidence": "Korean glyphs render without replacement boxes or corruption."}
  },
  "unresolved_required": []
}
```

If a visual dimension requires a fix, do not run finalize. Correct the responsible
translation or layout, then rerun every affected upstream validation plus `pdf-assemble`,
`pdf-qa prepare`, inspection of all newly generated contact sheets, and creation of a new
review bound to the new staged-PDF hash. Stale review evidence is invalid.

Only after `pdf-qa finalize` succeeds may the run be called complete. Return absolute
local links to all three published artifacts: `translated.pdf`, `manifest.json`, and
`review-report.md`. Treat any nonzero exit, missing artifact, unresolved semantic or visual
finding, incomplete contact-sheet coverage, or failed/stale QA as incomplete. Preserve
the private run diagnostics, state the blocker, and never report partial output as complete
or present a `translated.pdf` completion link before finalization.

## Pressure guardrails

| Shortcut | Required response |
|---|---|
| "It looks like a scan, but OCR may work." | Reject it; this workflow requires selectable text and has no OCR path. |
| "The HTML skill can probably ingest the PDF URL." | Reject routing; PDF input always uses this separate workflow. |
| "A free agent can repair the failed zone faster." | Retry with the same agent so ownership and review context remain intact. |
| "Automated PDF QA passed." | Inspect all contact sheets and write strict visual evidence before finalize. |
| "The staged PDF is useful enough." | Keep it private and incomplete; final links require successful finalize. |
