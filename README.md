# web-translator

`web-translator` is a cross-platform Codex plugin for Windows and macOS that translates one public static HTML
page into natural Korean while preserving the captured DOM, links, styles, and offline
assets. Translator agents work in isolated semantic zones; a master agent reviews every
zone before deterministic assembly and QA.

## Setup

Use Python 3.11 or newer. On Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

On macOS with a POSIX shell:

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[test]"
./.venv/bin/python -m playwright install chromium
```

The final command downloads the browser used for layout, offline, and screenshot QA.

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
`major` for breaking changes. Marketplace releases must vendor a committed plugin
snapshot; Codex displays the vendored `.codex-plugin/plugin.json` version.

## Use from Codex

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
<PYTHON> <PLUGIN_CREATOR_DIR>/scripts/validate_plugin.py .
```

With explicit network approval, run the two upstream compatibility checks separately:

```text
<PYTHON> -m pytest -m live -q
```

These tests are diagnostic because live source pages can change. Upstream drift must not
be treated as a deterministic regression in the default suite.

## Limitations

The MVP accepts one unauthenticated public HTTP(S) HTML page. It does not support private
or authenticated pages, CAPTCHA flows, JavaScript-only applications, recursive site
translation, PDF input/output, publishing, or guaranteed pixel-identical line wrapping.
Missing optional images or fonts may remain origin fallbacks and are reported as warnings;
missing critical HTML or CSS fails the run.
