# Web Translator MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows-first Codex plugin that converts one public static HTML page into a contextually translated, reviewed, offline Korean HTML bundle while preserving the source DOM and presentation.

**Architecture:** A Codex skill orchestrates deterministic Python commands for capture, semantic extraction, contract validation, assembly, and QA. The master agent owns document context, glossary, zone assignment, semantic review, and acceptance; translator subagents return isolated structured results and never edit the shared DOM.

**Tech Stack:** Codex plugin manifest and skill, Python 3.11+, `httpx`, `beautifulsoup4`, `lxml`, `tinycss2`, `langdetect`, `playwright`, `pytest`, and `pytest-httpx`.

## Global Constraints

- MVP input is one public `http` or `https` HTML URL with no login, CAPTCHA, intranet access, or JavaScript-only rendering requirement.
- Output is one offline HTML bundle; PDF input, PDF output, recursive crawling, hosting, and publishing are excluded.
- Source language is detected automatically and target language is Korean.
- Technical terms remain in English; only the first eligible document occurrence uses `English(한글)`.
- Code, URLs, commands, identifiers, product names, protocol tokens, and RFC `MUST`, `SHOULD`, and `MAY` remain unchanged.
- Translation must be based on section and document context, not mechanical sentence substitution.
- Translator subagents write separate zone result files and never modify shared source, glossary, or DOM files.
- The master performs semantic review and allows at most two retries per deficient zone.
- A partial translation can never be reported as successful.
- Runtime outputs are unique timestamped directories beneath `translated-pages/`; existing output is never overwritten.
- Windows paths containing spaces and Korean characters must work.
- Each successful task commit is pushed immediately to `origin/main` before the next task begins.
- The opt-in live compatibility pages are `https://docs.spring.io/spring-ai/reference/concepts.html` and `https://datatracker.ietf.org/doc/html/rfc8693`.

---

## Planned File Structure

```text
.codex-plugin/plugin.json                 Plugin metadata and skill discovery
skills/web-translator/SKILL.md            Master orchestration workflow
skills/web-translator/references/
  translator-contract.md                  Subagent input/output and translation rules
  review-rubric.md                        Master semantic review rubric
pyproject.toml                            Runtime and test dependencies
src/web_translator/
  __init__.py                             Package version
  __main__.py                             `python -m web_translator` entry
  cli.py                                  Subcommand parsing and exit codes
  models.py                               JSON contracts and serialization
  paths.py                                Safe URL and run/output paths
  assets.py                               Asset URL resolution and local naming
  capture.py                              HTML/CSS/image/font acquisition
  protection.py                           Protected-token encoding and restoration
  extract.py                              DOM-to-segment extraction
  zones.py                                Zone creation and validation
  translations.py                         Translation result validation and merge
  terminology.py                          Document-wide first-use gloss normalization
  assemble.py                             Approved translations back into source DOM
  qa.py                                   Structural, asset, offline, and visual checks
  report.py                               Manifest and Markdown review report
tests/
  fixtures/site/                          Reproducible local static source fixture
  fixtures/translated/                    Reviewed synthetic translation records
  test_plugin_layout.py                   Manifest and package checks
  test_models_paths.py                    Contracts, URL validation, and unique paths
  test_capture.py                         HTTP capture and asset rewriting
  test_extract.py                         Semantic extraction and protected content
  test_zones_translations.py              Zone/result contract tests
  test_assemble.py                        Terminology and DOM preservation
  test_qa.py                              Offline, structural, overflow, report checks
  test_pipeline.py                        End-to-end deterministic pipeline
  test_skill_contract.py                  Agent workflow requirements
  live/test_sample_pages.py               Opt-in source compatibility smoke tests
README.md                                 Windows setup, use, limitations, and testing
```

## Task 1: Scaffold the Plugin and Python Test Harness

**Files:**

- Create: `.codex-plugin/plugin.json`
- Create: `skills/web-translator/`
- Create: `pyproject.toml`
- Create: `src/web_translator/__init__.py`
- Create: `src/web_translator/__main__.py`
- Create: `src/web_translator/cli.py`
- Create: `tests/test_plugin_layout.py`

**Interfaces:**

- Consumes: the repository root and the plugin-creator scaffold script.
- Produces: `web_translator.cli.main(argv: Sequence[str] | None = None) -> int` and a validation-ready Codex manifest named `web-translator`.

- [ ] **Step 1: Write the failing plugin-layout and CLI tests**

```python
# tests/test_plugin_layout.py
import json
from pathlib import Path

from web_translator.cli import main


ROOT = Path(__file__).parents[1]


def test_manifest_discovers_web_translator_skill() -> None:
    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text("utf-8"))
    assert manifest["name"] == "web-translator"
    assert manifest["version"] == "0.1.0"
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["displayName"] == "Web Translator"
    assert (ROOT / "skills/web-translator").is_dir()


def test_cli_help_returns_success(capsys) -> None:
    assert main(["--help"]) == 0
    assert "capture" in capsys.readouterr().out
```

- [ ] **Step 2: Run the tests and verify the scaffold is missing**

Run: `python -m pytest tests/test_plugin_layout.py -q`

Expected: collection fails because `web_translator` and `.codex-plugin/plugin.json` do not exist.

- [ ] **Step 3: Generate the canonical scaffold and add the minimal Python package**

Run the plugin-creator scaffold from its installed skill directory, using the parent of this repository as `--path`, and do not create a marketplace entry:

```powershell
python C:\Users\Elite\.codex\skills\.system\plugin-creator\scripts\create_basic_plugin.py web-translator `
  --path C:\Users\Elite\Desktop\develop\git `
  --with-skills --with-scripts
```

Set `.codex-plugin/plugin.json` to validated metadata with `skills: "./skills/"`, no `apps`, no `mcpServers`, and these user-facing prompts:

```json
{
  "name": "web-translator",
  "version": "0.1.0",
  "description": "Translate public static web pages into reviewed offline Korean HTML bundles.",
  "author": {"name": "starryeye"},
  "repository": "https://github.com/starryeye/web-translator",
  "skills": "./skills/",
  "interface": {
    "displayName": "Web Translator",
    "shortDescription": "Translate a public web page into reviewed Korean HTML.",
    "longDescription": "Preserves a public page's structure and assets while Codex agents translate it contextually, review it, and create an offline bundle.",
    "developerName": "starryeye",
    "category": "Productivity",
    "capabilities": ["Write"],
    "defaultPrompt": ["Translate this public web page into an offline Korean HTML bundle."]
  }
}
```

Create `pyproject.toml` with Python 3.11, a `src` layout, the runtime dependencies from the plan header, `pytest` and `pytest-httpx` test dependencies, and a `web-translator = "web_translator.cli:console_main"` script. Implement `main()` with `argparse`, a `capture` parser stub, and `console_main()` that raises `SystemExit(main())`.

- [ ] **Step 4: Install editable dependencies and make the tests pass**

Run:

```powershell
python -m pip install -e ".[test]"
python -m pytest tests/test_plugin_layout.py -q
python C:\Users\Elite\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py .
```

Expected: `2 passed` and plugin validation succeeds.

- [ ] **Step 5: Commit and immediately push**

```powershell
git add .codex-plugin pyproject.toml src tests/test_plugin_layout.py
git commit -m "build: scaffold web translator plugin"
git push origin main
```

## Task 2: Define Run Paths and JSON Contracts

**Files:**

- Create: `src/web_translator/models.py`
- Create: `src/web_translator/paths.py`
- Create: `tests/test_models_paths.py`

**Interfaces:**

- Consumes: Python package from Task 1.
- Produces: `validate_public_url(url: str) -> httpx.URL`, `create_run_paths(workspace: Path, url: str, now: datetime) -> RunPaths`, `read_segments(path: Path) -> list[Segment]`, and `write_segments(path: Path, segments: Iterable[Segment]) -> None`.

- [ ] **Step 1: Write failing contract and path tests**

```python
from datetime import UTC, datetime
from pathlib import Path

import pytest

from web_translator.models import ProtectedToken, Segment, read_segments, write_segments
from web_translator.paths import create_run_paths, validate_public_url


def test_only_public_http_urls_are_accepted() -> None:
    assert str(validate_public_url("https://example.com/docs?a=1")) == "https://example.com/docs?a=1"
    for value in ("file:///C:/secret", "ftp://example.com/a", "http://localhost/a", "http://127.0.0.1/a"):
        with pytest.raises(ValueError):
            validate_public_url(value)


def test_run_paths_are_unique_and_windows_safe(tmp_path: Path) -> None:
    workspace = tmp_path / "한글 workspace"
    now = datetime(2026, 8, 10, 12, 34, 56, tzinfo=UTC)
    paths = create_run_paths(workspace, "https://docs.example.com/a page", now)
    assert paths.output_dir.name == "docs.example.com-a-page-20260810-123456"
    assert paths.work_dir.is_relative_to(workspace / ".web-translator/runs")


def test_segment_jsonl_round_trip(tmp_path: Path) -> None:
    segment = Segment(
        id="seg-000001", locator="[data-wt-segment='seg-000001']",
        semantic_type="paragraph", heading_path=["Overview"],
        source_text="Use ⟦WT:0⟧.",
        protected=[ProtectedToken(token="⟦WT:0⟧", kind="code", value="<code>JWT</code>")],
        context_ids=[], target=True,
    )
    path = tmp_path / "segments.jsonl"
    write_segments(path, [segment])
    assert read_segments(path) == [segment]
```

- [ ] **Step 2: Run the tests and verify missing models fail**

Run: `python -m pytest tests/test_models_paths.py -q`

Expected: import failure for `web_translator.models` and `web_translator.paths`.

- [ ] **Step 3: Implement immutable contracts and safe paths**

Use frozen dataclasses with explicit `to_dict`/`from_dict` methods:

```python
@dataclass(frozen=True, slots=True)
class ProtectedToken:
    token: str
    kind: str
    value: str


@dataclass(frozen=True, slots=True)
class Segment:
    id: str
    locator: str
    semantic_type: str
    heading_path: list[str]
    source_text: str
    protected: list[ProtectedToken]
    context_ids: list[str]
    target: bool


@dataclass(frozen=True, slots=True)
class Translation:
    segment_id: str
    text: str
    notes: str | None = None
    glossary_observations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunPaths:
    run_id: str
    work_dir: Path
    output_dir: Path
```

`validate_public_url` must reject non-HTTP(S), missing hosts, localhost names, loopback, private, link-local, multicast, and unspecified IP literals. `create_run_paths` must slugify the final host and URL path, add a UTC timestamp, create `.web-translator/runs/<run-id>` and reserve but not overwrite `translated-pages/<run-id>`.

- [ ] **Step 4: Run focused and full tests**

Run:

```powershell
python -m pytest tests/test_models_paths.py -q
python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit and immediately push**

```powershell
git add src/web_translator/models.py src/web_translator/paths.py tests/test_models_paths.py
git commit -m "feat: add run paths and data contracts"
git push origin main
```

## Task 3: Capture HTML and Offline Assets

**Files:**

- Create: `src/web_translator/assets.py`
- Create: `src/web_translator/capture.py`
- Create: `tests/fixtures/site/index.html`
- Create: `tests/fixtures/site/theme.css`
- Create: `tests/fixtures/site/logo.svg`
- Create: `tests/test_capture.py`

**Interfaces:**

- Consumes: validated `httpx.URL` and `RunPaths` from Task 2.
- Produces: `capture_page(url: str, run_dir: Path, transport: httpx.BaseTransport | None = None) -> CaptureResult`, including final URL, local `source.html`, asset URL map, warnings, and SHA-256 fingerprints.

- [ ] **Step 1: Write failing capture tests with an in-memory HTTP transport**

```python
def test_capture_rewrites_assets_and_preserves_links(tmp_path, httpx_mock) -> None:
    httpx_mock.add_response(url="https://example.com/docs/", text=FIXTURE_HTML)
    httpx_mock.add_response(url="https://example.com/docs/theme.css", text=".hero{background:url(img/bg.svg)}")
    httpx_mock.add_response(url="https://example.com/docs/img/bg.svg", content=b"<svg/>")
    result = capture_page("https://example.com/docs/", tmp_path)
    html = result.source_html.read_text("utf-8")
    assert 'href="assets/' in html
    assert 'href="https://example.com/next"' in html
    assert 'href="#section-1"' in html
    assert result.missing_optional_assets == []


def test_missing_stylesheet_is_fatal_but_image_is_warning(tmp_path, httpx_mock) -> None:
    httpx_mock.add_response(url="https://example.com/", text=FIXTURE_WITH_CSS_AND_IMAGE)
    httpx_mock.add_response(url="https://example.com/main.css", status_code=404)
    with pytest.raises(CaptureError, match="critical stylesheet"):
        capture_page("https://example.com/", tmp_path)
```

- [ ] **Step 2: Run capture tests and verify they fail**

Run: `python -m pytest tests/test_capture.py -q`

Expected: import failure for `capture_page`.

- [ ] **Step 3: Implement bounded HTTP capture and recursive CSS URL rewriting**

Implement these boundaries:

```python
MAX_REDIRECTS = 5
MAX_HTML_BYTES = 10 * 1024 * 1024
MAX_ASSET_BYTES = 25 * 1024 * 1024
MAX_CSS_IMPORT_DEPTH = 5
USER_AGENT = "web-translator/0.1 (+https://github.com/starryeye/web-translator)"


@dataclass(frozen=True, slots=True)
class CaptureResult:
    requested_url: str
    final_url: str
    source_html: Path
    asset_map: dict[str, str]
    fingerprints: dict[str, str]
    missing_optional_assets: list[str]


def local_asset_name(url: httpx.URL, content_type: str | None) -> Path:
    digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:16]
    suffix = safe_suffix(url.path, content_type)
    return Path("assets") / f"{digest}{suffix}"
```

Use `httpx.Client(follow_redirects=True, max_redirects=5, timeout=30.0)`. Require a final `text/html` response. Resolve and validate every initial, redirected, and asset host before connecting; reject any DNS result in loopback, private, link-local, multicast, reserved, or unspecified ranges. Resolve `<base>`, stylesheet links, image sources, `srcset`, and CSS `url(...)`/`@import` values with `urljoin`. Rewrite same-page fragments unchanged, other anchors to absolute source URLs, and captured assets to relative local paths. Store each URL once, use atomic temporary-file replacement, and record optional failures without swallowing critical stylesheet failures.

- [ ] **Step 4: Run focused and full tests**

Run:

```powershell
python -m pytest tests/test_capture.py -q
python -m pytest -q
```

Expected: all tests pass with no network access.

- [ ] **Step 5: Commit and immediately push**

```powershell
git add src/web_translator/assets.py src/web_translator/capture.py tests/fixtures/site tests/test_capture.py
git commit -m "feat: capture static pages and offline assets"
git push origin main
```

## Task 4: Extract Semantic Segments and Protect Source Tokens

**Files:**

- Create: `src/web_translator/protection.py`
- Create: `src/web_translator/extract.py`
- Create: `tests/test_extract.py`

**Interfaces:**

- Consumes: captured `source.html` from Task 3.
- Produces: `extract_segments(source_html: Path, segments_path: Path) -> list[Segment]`, `protect_fragment(html: str) -> tuple[str, list[ProtectedToken]]`, and `restore_tokens(text: str, tokens: Sequence[ProtectedToken]) -> str`.

- [ ] **Step 1: Write failing semantic extraction tests**

```python
def test_extracts_blocks_with_heading_context_and_inline_markup(tmp_path: Path) -> None:
    source = write_html(tmp_path, """
      <h1>OAuth Token Exchange</h1>
      <p>Use <strong>security tokens</strong> with <code>grant_type</code>.</p>
      <pre><code>MUST remain exact</code></pre>
    """)
    segments = extract_segments(source, tmp_path / "segments.jsonl")
    assert [s.semantic_type for s in segments] == ["heading", "paragraph"]
    assert segments[1].heading_path == ["OAuth Token Exchange"]
    assert "security tokens" in segments[1].source_text
    assert "grant_type" not in segments[1].source_text
    assert any(token.kind == "code" and "grant_type" in token.value for token in segments[1].protected)


def test_restoration_rejects_changed_or_duplicate_tokens() -> None:
    protected, tokens = protect_fragment("Use <code>JWT</code> and MUST.")
    with pytest.raises(ProtectionError):
        restore_tokens(protected.replace(tokens[0].token, ""), tokens)
    with pytest.raises(ProtectionError):
        restore_tokens(protected + tokens[0].token, tokens)
```

- [ ] **Step 2: Run extraction tests and verify they fail**

Run: `python -m pytest tests/test_extract.py -q`

Expected: import failure for extraction and protection modules.

- [ ] **Step 3: Implement deterministic DOM marking and token protection**

Use eligible blocks `h1`-`h6`, `p`, `li`, `dt`, `dd`, `th`, `td`, `caption`, `figcaption`, `label`, and `summary`. Skip any candidate inside `script`, `style`, `noscript`, `pre`, `code`, `svg`, `math`, or `[translate=no]`. Assign `data-wt-segment="seg-000001"` in document order and use that marker as the locator.

Protect complete `code`, `kbd`, `samp`, and `var` elements; URLs; commands; RFC normative keywords; and opening/closing inline tags. Use tokens matching `⟦WT:<six-digit-index>⟧`, verify exact single occurrence on restoration, and persist the marked DOM back to `source.html`. Derive heading ancestry from the nearest preceding heading levels and attach previous/next segment IDs as read-only context.

- [ ] **Step 4: Run focused and full tests**

Run:

```powershell
python -m pytest tests/test_extract.py -q
python -m pytest -q
```

Expected: all tests pass and protected source text is byte-for-byte recoverable.

- [ ] **Step 5: Commit and immediately push**

```powershell
git add src/web_translator/protection.py src/web_translator/extract.py tests/test_extract.py
git commit -m "feat: extract context-rich translation segments"
git push origin main
```

## Task 5: Create Zones and Validate Translator Results

**Files:**

- Create: `src/web_translator/zones.py`
- Create: `src/web_translator/translations.py`
- Create: `tests/test_zones_translations.py`

**Interfaces:**

- Consumes: ordered segments from Task 4 and master-authored `glossary.json`.
- Produces: `build_zones(segments: Sequence[Segment], max_chars: int = 12000) -> list[Zone]`, `validate_zone_results(zone: Zone, records: Sequence[Translation]) -> None`, and `merge_translations(segments, zones, result_dir) -> dict[str, Translation]`.

- [ ] **Step 1: Write failing boundary and result-validation tests**

```python
def test_zones_split_at_sections_and_include_read_only_neighbors() -> None:
    segments = sample_segments(section_sizes=[7000, 7000, 2000])
    zones = build_zones(segments, max_chars=12000)
    assert len(zones) == 3
    assert set(zones[0].target_ids).isdisjoint(zones[1].target_ids)
    assert zones[1].context_before_ids[-1] == zones[0].target_ids[-1]


def test_translation_contract_rejects_missing_foreign_and_changed_tokens() -> None:
    zone = sample_zone(["seg-000001", "seg-000002"])
    with pytest.raises(TranslationContractError, match="missing"):
        validate_zone_results(zone, [translation("seg-000001", "번역")])
    with pytest.raises(TranslationContractError, match="unassigned"):
        validate_zone_results(zone, [translation("seg-999999", "번역")])
```

- [ ] **Step 2: Run zone tests and verify they fail**

Run: `python -m pytest tests/test_zones_translations.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement section-aware zones and strict merge rules**

Define:

```python
@dataclass(frozen=True, slots=True)
class Zone:
    id: str
    heading_path: list[str]
    target_ids: list[str]
    context_before_ids: list[str]
    context_after_ids: list[str]
    attempt: int = 0
```

Accumulate complete heading sections up to `max_chars`; place an oversized section alone rather than splitting a table or block. Include at most two neighboring segments on each side as context. Validate that target IDs across zones form an exact partition of all target segments. For each zone result, require exact assigned IDs, one record per ID, strings only, and exact protected-token multisets. Merge in source order and reject any missing or duplicate final ID.

- [ ] **Step 4: Run focused and full tests**

Run:

```powershell
python -m pytest tests/test_zones_translations.py -q
python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit and immediately push**

```powershell
git add src/web_translator/zones.py src/web_translator/translations.py tests/test_zones_translations.py
git commit -m "feat: partition and validate translation work"
git push origin main
```

## Task 6: Normalize Terminology and Reassemble the DOM

**Files:**

- Create: `src/web_translator/terminology.py`
- Create: `src/web_translator/assemble.py`
- Create: `tests/test_assemble.py`

**Interfaces:**

- Consumes: marked source DOM, complete reviewed translation map, and canonical glossary.
- Produces: `normalize_first_use(ordered: Sequence[Translation], glossary: Mapping[str, str]) -> list[Translation]` and `assemble_page(source_html: Path, segments: Mapping[str, Segment], translations: Mapping[str, Translation], glossary: Mapping[str, str], output_dir: Path, source_url: str) -> Path`.

- [ ] **Step 1: Write failing first-use and DOM-preservation tests**

```python
def test_first_use_keeps_english_and_adds_one_korean_gloss() -> None:
    records = [translation("a", "OAuth enables exchange."), translation("b", "OAuth is reused.")]
    normalized = normalize_first_use(records, {"OAuth": "개방형 인가"})
    assert normalized[0].text == "OAuth(개방형 인가) enables exchange."
    assert normalized[1].text == "OAuth is reused."


def test_assembly_changes_only_marked_content(tmp_path: Path) -> None:
    source = write_html(tmp_path, '<main id="doc"><p class="lead" data-wt-segment="seg-000001">Hello <code>x</code>.</p></main>')
    segments = {"seg-000001": segment("seg-000001", protected_code="<code>x</code>")}
    translated = {"seg-000001": translation("seg-000001", "안녕하세요 ⟦WT:000000⟧.")}
    output = assemble_page(source, segments, translated, {}, tmp_path / "out", "https://example.com/")
    soup = BeautifulSoup(output.read_text("utf-8"), "lxml")
    assert soup.main["id"] == "doc"
    assert soup.p["class"] == ["lead"]
    assert "data-wt-segment" not in soup.p.attrs
    assert soup.code.string == "x"
```

- [ ] **Step 2: Run assembly tests and verify they fail**

Run: `python -m pytest tests/test_assemble.py -q`

Expected: missing terminology and assembly modules.

- [ ] **Step 3: Implement boundary-aware terminology and safe fragment insertion**

Sort glossary terms longest-first, match case-sensitive English term boundaries outside protected tokens, remove duplicate existing Korean glosses, and add one canonical gloss at the first eligible document occurrence. Do not alter glossary terms inside code or protected token values.

For each marked element, restore tokens, parse the result as an HTML fragment, reject executable tags or a changed top-level fragment shape, replace the element contents, and remove only `data-wt-segment`. Copy captured assets, add a plugin-owned attribution element after the captured content boundary, and insert a CSP meta tag that blocks network and script execution while allowing local styles, images, and fonts.

- [ ] **Step 4: Run focused and full tests**

Run:

```powershell
python -m pytest tests/test_assemble.py -q
python -m pytest -q
```

Expected: all tests pass; tag hierarchy, IDs, classes, and link targets remain unchanged.

- [ ] **Step 5: Commit and immediately push**

```powershell
git add src/web_translator/terminology.py src/web_translator/assemble.py tests/test_assemble.py
git commit -m "feat: reassemble reviewed translations"
git push origin main
```

## Task 7: Add Structural, Offline, and Visual QA Reports

**Files:**

- Modify: `src/web_translator/models.py`
- Create: `src/web_translator/qa.py`
- Create: `src/web_translator/report.py`
- Create: `tests/test_qa.py`

**Interfaces:**

- Consumes: marked source HTML, final `index.html`, capture metadata, segment/translation maps, and master review JSON.
- Produces: `run_qa(inputs: QAInputs) -> QAResult`, `write_manifest(result: QAResult, path: Path) -> None`, and `write_review_report(result: QAResult, review: MasterReview, path: Path) -> None`.

- [ ] **Step 1: Write failing QA and report tests**

```python
def test_qa_fails_incomplete_translation_and_broken_critical_asset(tmp_path: Path) -> None:
    inputs = qa_inputs(tmp_path, source_ids={"a", "b"}, translated_ids={"a"}, critical_assets=[Path("assets/main.css")])
    result = run_qa(inputs)
    assert result.passed is False
    assert {finding.code for finding in result.required_findings} == {"translation-coverage", "critical-asset-missing"}


def test_report_records_warnings_retries_and_source(tmp_path: Path) -> None:
    path = tmp_path / "review-report.md"
    write_review_report(passing_result(optional_warnings=1), master_review(retries={"zone-002": 1}), path)
    text = path.read_text("utf-8")
    assert "https://example.com/docs" in text
    assert "zone-002" in text
    assert "PASS" in text
```

- [ ] **Step 2: Run QA tests and verify they fail**

Run: `python -m pytest tests/test_qa.py -q`

Expected: missing QA and report modules.

- [ ] **Step 3: Implement required findings, browser checks, and evidence output**

Define stable finding codes and severities. Required checks are exact translation coverage, protected-token integrity, captured-content structural signature, critical asset existence, internal anchor resolution, and absence of external network dependencies for critical layout/content. Optional missing images/fonts remain warnings.

Add the exact QA contracts:

```python
@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: Literal["required", "warning"]
    message: str
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MasterReview:
    unresolved_required: list[str]
    retries: dict[str, int]
    section_findings: dict[str, list[str]]


@dataclass(frozen=True, slots=True)
class QAInputs:
    source_html: Path
    output_html: Path
    source_url: str
    source_segment_ids: set[str]
    translated_segment_ids: set[str]
    critical_assets: list[Path]
    optional_assets: list[Path]
    screenshot_dir: Path
    master_review: MasterReview


@dataclass(frozen=True, slots=True)
class QAResult:
    passed: bool
    required_findings: list[Finding]
    warnings: list[Finding]
    screenshots: list[Path]
```

Use Playwright Chromium for desktop `1440x900` and narrow `390x844` checks. Serve the output from a loopback ephemeral HTTP server, abort every non-loopback request, capture screenshots under the run work directory, and evaluate:

```javascript
({
  horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  brokenImages: [...document.images].filter(img => !img.complete || img.naturalWidth === 0).map(img => img.src),
  clippedText: [...document.querySelectorAll('main *')].filter(el => el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).overflowX === 'hidden').length
})
```

Write deterministic `manifest.json` and `review-report.md`; mark `passed` only when required findings are empty and the master review contains no unresolved required item.

- [ ] **Step 4: Run focused and full tests**

Run:

```powershell
python -m playwright install chromium
python -m pytest tests/test_qa.py -q
python -m pytest -q
```

Expected: all tests pass and two viewport screenshots are recorded for the fixture.

- [ ] **Step 5: Commit and immediately push**

```powershell
git add src/web_translator/models.py src/web_translator/qa.py src/web_translator/report.py tests/test_qa.py
git commit -m "feat: verify offline output and write reports"
git push origin main
```

## Task 8: Wire the Deterministic CLI and End-to-End Fixture Pipeline

**Files:**

- Modify: `src/web_translator/cli.py`
- Modify: `src/web_translator/__main__.py`
- Create: `tests/fixtures/translated/glossary.json`
- Create: `tests/fixtures/translated/zone-001.jsonl`
- Create: `tests/test_pipeline.py`

**Interfaces:**

- Consumes: all deterministic components from Tasks 2-7.
- Produces CLI subcommands `capture`, `extract`, `plan-zones`, `validate-translations`, `assemble`, and `qa`, each returning `0` on success and a documented nonzero exit code on failure.

- [ ] **Step 1: Write a failing end-to-end fixture test**

```python
def test_fixture_pipeline_builds_complete_offline_bundle(tmp_path: Path, fixture_server) -> None:
    run_dir = tmp_path / "작업 공간" / "run"
    output_dir = tmp_path / "작업 공간" / "translated-pages" / "fixture"
    assert main(["capture", fixture_server.url, "--run-dir", str(run_dir)]) == 0
    assert main(["extract", "--run-dir", str(run_dir)]) == 0
    assert main(["plan-zones", "--run-dir", str(run_dir), "--max-chars", "12000"]) == 0
    copy_reviewed_fixture_translations(run_dir)
    assert main(["validate-translations", "--run-dir", str(run_dir)]) == 0
    assert main(["assemble", "--run-dir", str(run_dir), "--output-dir", str(output_dir)]) == 0
    assert main(["qa", "--run-dir", str(run_dir), "--output-dir", str(output_dir)]) == 0
    assert (output_dir / "index.html").exists()
    assert json.loads((output_dir / "manifest.json").read_text("utf-8"))["qa_status"] == "passed"
```

- [ ] **Step 2: Run the pipeline test and verify command wiring fails**

Run: `python -m pytest tests/test_pipeline.py -q`

Expected: argparse rejects at least one unimplemented subcommand.

- [ ] **Step 3: Implement explicit subcommands and stable exit codes**

Add handlers that read and write only documented run files. Use these exit codes: `0` success, `2` invalid arguments/URL, `3` capture failure, `4` contract failure, `5` assembly failure, and `6` QA failure. Print one JSON status object to stdout per command and human-readable diagnostics to stderr. Catch only expected domain exceptions; allow programming errors to retain tracebacks during development.

`__main__.py` must contain:

```python
from web_translator.cli import console_main

if __name__ == "__main__":
    console_main()
```

- [ ] **Step 4: Run the end-to-end and full suites**

Run:

```powershell
python -m pytest tests/test_pipeline.py -q
python -m pytest -q
```

Expected: the local fixture builds a passing offline bundle and all tests pass.

- [ ] **Step 5: Commit and immediately push**

```powershell
git add src/web_translator/cli.py src/web_translator/__main__.py tests/fixtures/translated tests/test_pipeline.py
git commit -m "feat: connect the translation pipeline CLI"
git push origin main
```

## Task 9: Implement the Codex Master/Subagent Workflow and Release Validation

**Files:**

- Create: `skills/web-translator/SKILL.md`
- Create: `skills/web-translator/references/translator-contract.md`
- Create: `skills/web-translator/references/review-rubric.md`
- Create: `tests/test_skill_contract.py`
- Create: `tests/live/test_sample_pages.py`
- Modify: `README.md`

**Interfaces:**

- Consumes: deterministic CLI commands and schemas from Tasks 1-8.
- Produces: the user-facing plugin workflow, structured translator prompt contract, master review rubric, opt-in sample compatibility tests, and Windows documentation.

- [ ] **Step 1: Write failing skill-contract tests**

```python
def test_skill_requires_master_review_and_isolated_subagents() -> None:
    text = Path("skills/web-translator/SKILL.md").read_text("utf-8")
    for phrase in (
        "spawn_agent", "one zone result file", "translator-contract.md",
        "review-rubric.md", "maximum of two retries", "never report partial output as complete",
    ):
        assert phrase in text


def test_skill_frontmatter_has_trigger_description() -> None:
    text = Path("skills/web-translator/SKILL.md").read_text("utf-8")
    assert text.startswith("---\nname: web-translator\n")
    assert "public" in text.split("---", 2)[1]
    assert "URL" in text.split("---", 2)[1]
```

- [ ] **Step 2: Run the skill tests and verify the workflow is absent**

Run: `python -m pytest tests/test_skill_contract.py -q`

Expected: failure because the skill and references are empty or missing.

- [ ] **Step 3: Write the exact master workflow and review loop**

The skill must direct the master to:

1. Validate that exactly one supported public URL is in scope.
2. Create unique run/output paths and run `capture`, `extract`, and `plan-zones`.
3. Read the outline, build `glossary.json`, and refine zones without changing the exact target partition.
4. Spawn one translator subagent per available zone slot, giving every agent the same document summary, glossary, contract, and only its assigned targets plus read-only neighbor context.
5. Require each agent to write one zone result file and return its path.
6. Run deterministic result validation before semantic review.
7. Review every zone with `review-rubric.md`, send concrete findings back to the same agent, and enforce a maximum of two retries.
8. Normalize first-use glossary placement, run assembly and QA, and refuse completion when any required check remains.
9. Return absolute links to `index.html` and `review-report.md`, plus optional-asset warnings.

`translator-contract.md` must include natural contextual Korean, English technical terms with a separate glossary observation channel, exact protected-token preservation, exact assigned IDs, JSON Lines examples, and prohibitions on additions or omissions. `review-rubric.md` must score semantic fidelity, qualification preservation, naturalness, terminology, boundary consistency, and protected content as pass/required-fix with written evidence.

Add opt-in live tests marked `@pytest.mark.live` that run capture and extraction against the two approved sample URLs, assert HTML content and at least one hundred target segments for RFC 8693, and never run in the default suite.

Document Windows setup, Playwright installation, invocation examples, output layout, limitations, default tests, `pytest -m live`, plugin validation, and the fact that live source pages can change.

- [ ] **Step 4: Run every validation command with fresh evidence**

Run:

```powershell
python -m pytest -q
python -m pytest tests/test_skill_contract.py -q
python C:\Users\Elite\.codex\skills\.system\skill-creator\scripts\quick_validate.py skills/web-translator
python C:\Users\Elite\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py .
git diff --check
```

Expected: all tests and both validators pass, with no whitespace errors. Run `python -m pytest -m live -q` only when network access is approved; record its result separately because upstream page changes must not make the default suite nondeterministic.

- [ ] **Step 5: Commit and immediately push**

```powershell
git add skills tests/live tests/test_skill_contract.py README.md
git commit -m "feat: orchestrate reviewed agent translation"
git push origin main
```

## Final Acceptance Check

- [ ] Run `python -m pytest -q` and record the exact pass count.
- [ ] Run the skill and plugin validators and record both successful outputs.
- [ ] Run `git diff --check` and confirm no output.
- [ ] Generate the local fixture bundle in a Windows path containing spaces and Korean characters.
- [ ] Confirm its `index.html`, `manifest.json`, and `review-report.md` exist and QA status is `passed`.
- [ ] With network approval, run both opt-in live sample smoke tests and record upstream drift separately from deterministic failures.
- [ ] Confirm `git status --short --branch` reports `main...origin/main` with no pending changes.
