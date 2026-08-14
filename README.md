# web-translator

`web-translator` is a Windows-first Codex plugin that translates one public static HTML
page into natural Korean while preserving the captured DOM, links, styles, and offline
assets. Translator agents work in isolated semantic zones; a master agent reviews every
zone before deterministic assembly and QA.

## Windows setup

From PowerShell, use Python 3.11 or newer and install the package plus test dependencies:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

The final command downloads the browser used for layout, offline, and screenshot QA.

## Versioning

The release version is declared in `.codex-plugin/plugin.json` and mirrored in
`pyproject.toml` and `src/web_translator/__init__.py`. Keep all three synchronized
with the standard-library-only helper:

```powershell
python .\scripts\version.py check
python .\scripts\version.py show
python .\scripts\version.py bump patch
python .\scripts\version.py set 1.0.0
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

The skill normally orchestrates these commands. They are also useful for diagnosis:

```powershell
.\.venv\Scripts\python.exe -m web_translator capture <URL> --run-dir <WORK_DIR>
.\.venv\Scripts\python.exe -m web_translator extract --run-dir <WORK_DIR>
.\.venv\Scripts\python.exe -m web_translator plan-zones --run-dir <WORK_DIR> --max-chars 12000 --target-zones 3
.\.venv\Scripts\python.exe -m web_translator prepare-assignments --run-dir <WORK_DIR>
.\.venv\Scripts\python.exe -m web_translator validate-translations --run-dir <WORK_DIR> --zone-id zone-001
.\.venv\Scripts\python.exe -m web_translator validate-translations --run-dir <WORK_DIR>
.\.venv\Scripts\python.exe -m web_translator assemble --run-dir <WORK_DIR> --output-dir <OUTPUT_DIR>
.\.venv\Scripts\python.exe -m web_translator qa --run-dir <WORK_DIR> --output-dir <OUTPUT_DIR>
```

Each command exits nonzero on a required failure. Existing output directories are never
overwritten.

## Tests and validation

Routine tests are deterministic and exclude network-marked tests by default:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest tests/test_skill_contract.py -q
.\.venv\Scripts\python.exe C:\Users\Elite\.codex\skills\.system\skill-creator\scripts\quick_validate.py skills/web-translator
.\.venv\Scripts\python.exe C:\Users\Elite\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py .
```

With explicit network approval, run the two upstream compatibility checks separately:

```powershell
.\.venv\Scripts\python.exe -m pytest -m live -q
```

These tests are diagnostic because live source pages can change. Upstream drift must not
be treated as a deterministic regression in the default suite.

## Limitations

The MVP accepts one unauthenticated public HTTP(S) HTML page. It does not support private
or authenticated pages, CAPTCHA flows, JavaScript-only applications, recursive site
translation, PDF input/output, publishing, or guaranteed pixel-identical line wrapping.
Missing optional images or fonts may remain origin fallbacks and are reported as warnings;
missing critical HTML or CSS fails the run.
