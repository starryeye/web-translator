# web-translator

`web-translator` is a cross-platform Codex plugin for Windows and macOS with two clearly
selectable workflows:

- `web-translator` translates one supported public static HTML URL into an offline Korean
  bundle while preserving its DOM, links, styles, and captured assets.
- `pdf-translator` translates one local text-selectable PDF, attached text-selectable PDF,
  or public HTTP(S) PDF URL into a reviewed Korean PDF.

Both workflows use the same Python package, immutable semantic zones, translation
contract, glossary, validation, and master-review rubric. Format-specific commands handle
HTML or PDF acquisition, extraction, assembly, and QA.

## Setup

Use Python 3.11 or newer. On Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m playwright install chromium
# Install Poppler, then open a new PowerShell so pdfinfo.exe and pdftoppm.exe are on PATH.
winget install -e --id oschwartz10612.Poppler
```

On macOS with a POSIX shell:

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[test]"
./.venv/bin/python -m playwright install chromium
brew install poppler
```

Chromium supports HTML layout, offline, and screenshot QA. Poppler provides `pdfinfo` and
`pdftoppm` for PDF inspection and rendering. If the Windows package manager is unavailable,
install a trusted Poppler distribution and add its `Library\bin` directory to `PATH`.
Confirm both executables are available before starting a PDF run.

## Versioning

The release version is declared in `.codex-plugin/plugin.json` and mirrored in
`pyproject.toml` and `src/web_translator/__init__.py`. Keep all three synchronized
with the standard-library-only helper. Replace `<PYTHON>` with the repository-local
interpreter for your platform:

```text
<PYTHON> scripts/version.py check
<PYTHON> scripts/version.py show
<PYTHON> scripts/version.py bump patch
<PYTHON> scripts/version.py set 1.0.0
```

Use `patch` for compatible fixes, `minor` for backward-compatible features, and
`major` for breaking changes. Marketplace releases reference an immutable upstream
tag and commit SHA; Codex materializes the pinned Git source declared by the marketplace.

## Use from Codex

### Public HTML

Ask Codex to translate exactly one public URL, for example:

```text
Translate this page into an offline Korean HTML bundle:
https://docs.spring.io/spring-ai/reference/concepts.html
```

The skill creates a unique work directory under `.web-translator/runs/`, captures and
extracts the page, delegates translation zones, performs master review, assembles the
page, and runs QA. A successful output has this layout:

```text
translated-pages/<host>-<path>-<UTC timestamp>/
|-- index.html
|-- assets/ (when captured)
|-- manifest.json
`-- review-report.md
```

The completion response links to `index.html` and `review-report.md` with absolute local
paths and reports optional asset warnings.

Allocation keeps absolute lexical paths and requires the run and reserved output to be
exact children of the held `.web-translator/runs` and `translated-pages` roots. A symlink
or reparse ancestor, dangling link, or replacement race fails closed.

### Local, attached, or public PDF

Ask Codex to translate exactly one supported PDF. Local paths containing spaces and
non-ASCII characters are passed as one native shell argument:

```text
Translate this local text-selectable PDF into Korean:
/Users/me/자료 폴더/분기 보고서.pdf
```

```text
Translate this public HTTP(S) PDF URL into Korean:
https://example.com/reports/quarterly-report.pdf
```

An attachment is supported when Codex exposes it as a readable local path. The PDF skill
creates a unique private run directory, acquires and extracts the source, uses the shared
zone translation and semantic review, assembles a staged PDF with embedded Noto Sans KR
Regular and Bold fonts, prepares automated/rendered QA, and requires master inspection of
every numbered contact sheet. Font licensing and source provenance are stored beside the
package fonts in `src/web_translator/font_assets/OFL.txt` and `PROVENANCE.json`.

Only `pdf-qa finalize` publishes a successful output:

```text
translated-pdfs/<source>-<UTC timestamp>/
|-- translated.pdf
|-- manifest.json
`-- review-report.md
```

The completion response links to all three files using absolute local paths. Staged or
partially reviewed PDFs are never presented as completed output.

PDF allocation applies the same held-root contract with `translated-pdfs`. The workflow
accepts a standalone uncaptioned figure and preserves it once, while orphan or ambiguous
captions fail. An unreconstructed visible link is only a warning when its
visible label and destination remain in both final artifacts; missing visible text fails.

After translations and glossary content are final, `pdf-review-input` creates canonical
semantic-review evidence. PDF `review.json` carries its `semantic_input_sha256`, binding
the exact segments, zones, assignments, translations, and glossary policy/content before
assembly and both QA stages.

Poppler/Pillow rendering is limited to 36,000,000 pixels per page, 360,000,000 pixels per
PDF, 64 MiB per encoded PNG, and 1 GiB for the rendered set. Decoded dimensions are
checked before full decode. These deterministic budgets remain enforced on platforms
without an OS-level address-space limit.

### Performance-oriented orchestration

The default skill targets three size-balanced zones, starts each translator with no
inherited chat history, and passes document input through immutable assignment files.
The master validates and reviews each completed zone while the other translators are
still running, then performs one final aggregate validation. This keeps the complete
master-review and fail-closed guarantees while reducing duplicated context and idle time.

## Direct CLI stages

The skill detects the active OS and uses the repository-local interpreter automatically.
For manual diagnosis, replace `<PYTHON>` below with `.\.venv\Scripts\python.exe` on
Windows PowerShell or `./.venv/bin/python` on macOS. Keep every URL and path as one
native shell argument. For example:

```powershell
$python = (Resolve-Path ".\.venv\Scripts\python.exe").Path
& $python -m web_translator capture $url --run-dir $workDir
& $python -m web_translator assemble --run-dir $workDir --output-dir $outputDir
```

```sh
python="$(cd .venv/bin && pwd -P)/python"
"$python" -m web_translator capture "$url" --run-dir "$work_dir"
"$python" -m web_translator assemble --run-dir "$work_dir" --output-dir "$output_dir"
```

The complete stage sequence is:

```text
<PYTHON> -m web_translator capture <URL> --run-dir <WORK_DIR>
<PYTHON> -m web_translator extract --run-dir <WORK_DIR>
<PYTHON> -m web_translator plan-zones --run-dir <WORK_DIR> --max-chars 12000 --target-zones 3
<PYTHON> -m web_translator prepare-assignments --run-dir <WORK_DIR>
<PYTHON> -m web_translator validate-translations --run-dir <WORK_DIR> --zone-id zone-001
<PYTHON> -m web_translator validate-translations --run-dir <WORK_DIR>
<PYTHON> -m web_translator assemble --run-dir <WORK_DIR> --output-dir <OUTPUT_DIR>
<PYTHON> -m web_translator qa --run-dir <WORK_DIR> --output-dir <OUTPUT_DIR>
```

For a local or public PDF, bind the complete source path or URL to one shell variable and
run the PDF stages in order:

```text
<PYTHON> -m web_translator pdf-acquire <FILE_OR_URL> --run-dir <WORK_DIR>
<PYTHON> -m web_translator pdf-extract --run-dir <WORK_DIR>
<PYTHON> -m web_translator plan-zones --run-dir <WORK_DIR> --max-chars 12000 --target-zones 3
<PYTHON> -m web_translator prepare-assignments --run-dir <WORK_DIR>
<PYTHON> -m web_translator validate-translations --run-dir <WORK_DIR> --zone-id <ZONE_ID>
<PYTHON> -m web_translator validate-translations --run-dir <WORK_DIR>
<PYTHON> -m web_translator pdf-review-input --run-dir <WORK_DIR>
<PYTHON> -m web_translator pdf-assemble --run-dir <WORK_DIR> --output-dir <OUTPUT_DIR>
<PYTHON> -m web_translator pdf-qa prepare --run-dir <WORK_DIR> --output-dir <OUTPUT_DIR>
<MASTER> inspect every contact sheet and write strict pdf-layout-review.json
<PYTHON> -m web_translator pdf-qa finalize --run-dir <WORK_DIR> --output-dir <OUTPUT_DIR>
```

`pdf-qa prepare` writes automated evidence and numbered contact sheets under the private
run directory. The master review records exact page/contact-sheet coverage and all eight
visual dimensions before `pdf-qa finalize` checks the evidence and atomically publishes
the three final artifacts.

Each command exits nonzero on a required failure. Existing output directories are never
overwritten.

## Tests and validation

Routine tests are deterministic and exclude network-marked tests by default. Replace
`<PYTHON>` with the platform-specific interpreter path described above and resolve the
installed skill/plugin validator directories for your Codex installation:

```text
<PYTHON> -m pytest -q
<PYTHON> -m pytest tests/test_skill_contract.py -q
<PYTHON> <SKILL_CREATOR_DIR>/scripts/quick_validate.py skills/web-translator
<PYTHON> <SKILL_CREATOR_DIR>/scripts/quick_validate.py skills/pdf-translator
<PYTHON> <PLUGIN_CREATOR_DIR>/scripts/validate_plugin.py .
```

With explicit network approval, run the two upstream compatibility checks separately:

```text
<PYTHON> -m pytest -m live -q
```

These tests are diagnostic because live source pages can change. Upstream drift must not
be treated as a deterministic regression in the default suite.

## Limitations

The HTML workflow accepts one unauthenticated public HTTP(S) static page. It does not
support private or authenticated pages, CAPTCHA flows, JavaScript-only applications,
recursive site translation, publishing, or guaranteed pixel-identical line wrapping.
Missing optional images or fonts may remain origin fallbacks and are reported as warnings;
missing critical HTML or CSS fails the run.

The PDF workflow accepts one readable local/attached PDF or public HTTP(S) PDF, up to
50 MiB and 100 pages, only when its required text is selectable. It rejects scanned,
encrypted, malformed, oversized, over-page-limit, non-PDF, private-network, authenticated,
or structurally ambiguous inputs. It does not OCR scans or preserve source line wrapping
pixel-for-pixel. Missing required text, tables, figures, captions, embedded fonts, pages,
or semantic/visual review evidence is a failure rather than a warning.
