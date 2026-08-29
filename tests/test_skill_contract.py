import json
from pathlib import Path


SKILL = Path("skills/web-translator/SKILL.md")
PDF_SKILL = Path("skills/pdf-translator/SKILL.md")
TRANSLATOR_CONTRACT = Path(
    "skills/web-translator/references/translator-contract.md"
)
REVIEW_RUBRIC = Path("skills/web-translator/references/review-rubric.md")


def test_skill_requires_master_review_and_isolated_subagents() -> None:
    text = SKILL.read_text("utf-8")
    for phrase in (
        "spawn_agent",
        "one zone result file",
        "translator-contract.md",
        "review-rubric.md",
        "maximum of two retries",
        "never report partial output as complete",
        'fork_turns="none"',
        "immutable assignment package",
    ):
        assert phrase in text


def test_skill_frontmatter_has_trigger_description() -> None:
    text = SKILL.read_text("utf-8")
    assert text.startswith("---\nname: web-translator\n")
    frontmatter = text.split("---", 2)[1]
    assert "Use when" in frontmatter
    assert "public" in frontmatter
    assert "URL" in frontmatter


def test_skill_defines_the_fail_closed_master_sequence() -> None:
    text = SKILL.read_text("utf-8")
    for phrase in (
        "exactly one supported public URL",
        "capture",
        "extract",
        "plan-zones",
        "glossary.json",
        "exact target partition",
        "same document summary",
        "read-only neighbor context",
        "deterministic result validation before",
        "semantic review",
        "same agent",
        "Normalize first-use glossary placement",
        "assemble",
        "qa",
        "absolute links",
        "optional-asset warnings",
        "--target-zones 3",
        "prepare-assignments",
        "--zone-id",
        "completed zones while other translators are still running",
    ):
        assert phrase in text


def test_translator_contract_is_strict_and_contextual() -> None:
    text = TRANSLATOR_CONTRACT.read_text("utf-8")
    for phrase in (
        "natural contextual Korean",
        "English technical terms",
        "glossary_observations",
        "exact protected-token preservation",
        "exact assigned IDs",
        "JSON Lines",
        "Do not add",
        "Do not omit",
    ):
        assert phrase in text
    assert '"segment_id"' in text
    assert '"text"' in text


def test_assignment_package_keeps_fresh_agent_prompts_bounded() -> None:
    text = Path(
        "skills/web-translator/references/assignment-package.md"
    ).read_text("utf-8")
    for phrase in (
        '"schema_version"',
        '"zone_id"',
        '"document_summary"',
        '"glossary"',
        '"targets"',
        '"context_before"',
        '"context_after"',
        "Do not include unrelated segments",
        "never modify it afterward",
    ):
        assert phrase in text


def test_review_rubric_requires_evidence_for_every_dimension() -> None:
    text = REVIEW_RUBRIC.read_text("utf-8")
    for phrase in (
        "semantic fidelity",
        "qualification preservation",
        "naturalness",
        "terminology",
        "boundary consistency",
        "protected content",
        "pass",
        "required-fix",
        "written evidence",
    ):
        assert phrase in text


def test_live_samples_are_opt_in_and_cover_both_approved_urls() -> None:
    live = Path("tests/live/test_sample_pages.py").read_text("utf-8")
    project = Path("pyproject.toml").read_text("utf-8")
    assert "pytest.mark.live" in live
    assert "https://docs.spring.io/spring-ai/reference/concepts.html" in live
    assert "https://datatracker.ietf.org/doc/html/rfc8693" in live
    assert "capture_page" in live
    assert "extract_segments" in live
    assert ">= 100" in live
    assert "not live" in project


def test_readme_documents_cross_platform_usage_and_validation() -> None:
    text = Path("README.md").read_text("utf-8")
    for phrase in (
        "Windows",
        "macOS",
        ".venv/bin/python",
        "playwright install chromium",
        "translated-pages",
        "Limitations",
        "pytest -q",
        "pytest -m live",
        "quick_validate.py",
        "validate_plugin.py",
        "live source pages can change",
    ):
        assert phrase in text


def test_skill_resolves_one_interpreter_and_persists_review_evidence() -> None:
    skill = SKILL.read_text("utf-8")
    readme = Path("README.md").read_text("utf-8")
    assert "Detect the active OS and shell yourself" in skill
    assert "Never ask the user to choose a platform" in skill
    assert '$python = (Resolve-Path ".\\.venv\\Scripts\\python.exe").Path' in skill
    assert 'python="$(cd .venv/bin && pwd -P)/python"' in skill
    assert 'PowerShell: `& $python`' in skill
    assert 'POSIX: `"$python"`' in skill
    assert skill.count("<python> -m web_translator") == 8
    assert "section_findings must exactly cover every planned zone" in skill
    assert "integers from 0 through 2" in skill
    assert '"unresolved_required": []' in skill
    assert '"retries": {"zone-001": 1}' in skill
    assert '"section_findings": {' in skill
    for dimension in (
        "semantic_fidelity",
        "qualification_preservation",
        "naturalness",
        "terminology",
        "boundary_consistency",
        "protected_content",
    ):
        assert f'"dimension":"{dimension}","verdict":"pass","evidence":' in skill
    assert "non-empty `evidence`" in skill
    assert "sorted, unique string array" in skill
    assert "`zone-ID:dimension`" in skill
    assert '.\\.venv\\Scripts\\python.exe' in readme
    assert './.venv/bin/python' in readme
    assert "assets/ (when captured)" in readme


def test_skill_preserves_platform_arguments_as_single_values() -> None:
    skill = SKILL.read_text("utf-8")
    readme = Path("README.md").read_text("utf-8")

    for substitution in (
        '`<url>` → `$url`',
        '`<work-dir>` → `$workDir`',
        '`<output-dir>` → `$outputDir`',
        '`<url>` → `"$url"`',
        '`<work-dir>` → `"$work_dir"`',
        '`<output-dir>` → `"$output_dir"`',
    ):
        assert substitution in skill
    assert "Never build or evaluate a command string" in skill
    assert '& $python -m web_translator capture $url --run-dir $workDir' in readme
    assert '"$python" -m web_translator capture "$url" --run-dir "$work_dir"' in readme


def test_public_skills_keep_allocated_paths_lexical_and_under_exact_roots() -> None:
    web = SKILL.read_text("utf-8")
    pdf = PDF_SKILL.read_text("utf-8")

    for text, output_root in (
        (web, "translated-pages"),
        (pdf, "translated-pdfs"),
    ):
        assert ".resolve()" not in text
        assert "Path.cwd().absolute()" in text
        assert 'workspace / ".web-translator" / "runs"' in text
        assert f'workspace / "{output_root}"' in text
        assert "exact child" in text
        assert "symlink or reparse" in text


def test_pdf_skill_is_discoverable_and_fail_closed() -> None:
    text = PDF_SKILL.read_text("utf-8")
    for phrase in (
        "name: pdf-translator",
        "local path",
        "attached file",
        "public HTTP(S) URL",
        "pdf-acquire",
        "pdf-extract",
        "plan-zones",
        "prepare-assignments",
        "validate-translations",
        "pdf-assemble",
        "pdf-qa prepare",
        "pdf-layout-review.json",
        "pdf-qa finalize",
        "50 MiB",
        "100 pages",
        "scans",
        "encryption",
        "malformed",
        "never pass a PDF to the HTML",
        "never report partial output as complete",
    ):
        assert phrase in text
    assert not Path("skills/pdf-translator/references").exists()


def test_pdf_skill_reuses_shared_translation_contracts_and_agents() -> None:
    text = PDF_SKILL.read_text("utf-8")
    for phrase in (
        "../web-translator/references/translator-contract.md",
        "../web-translator/references/assignment-package.md",
        "../web-translator/references/review-rubric.md",
        "Segment",
        "spawn_agent",
        'fork_turns="none"',
        "one zone result file",
        "followup_task",
        "same agent",
        "maximum of two retries",
        "master semantic review",
        "deterministic result validation before",
    ):
        assert phrase in text


def test_pdf_skill_preserves_platform_arguments_and_stage_order() -> None:
    text = PDF_SKILL.read_text("utf-8")
    for phrase in (
        '$python = (Resolve-Path ".\\.venv\\Scripts\\python.exe").Path',
        'python="$(cd .venv/bin && pwd -P)/python"',
        'PowerShell: `& $python`',
        'POSIX: `"$python"`',
        '`<source>` → `$source`',
        '`<work-dir>` → `$workDir`',
        '`<output-dir>` → `$outputDir`',
        '`<source>` → `"$source"`',
        '`<work-dir>` → `"$work_dir"`',
        '`<output-dir>` → `"$output_dir"`',
        "Never execute a placeholder literally",
        "Never build or evaluate a command string",
    ):
        assert phrase in text

    commands = (
        "<python> -m web_translator pdf-acquire <source> --run-dir <work-dir>",
        "<python> -m web_translator pdf-extract --run-dir <work-dir>",
        "<python> -m web_translator plan-zones --run-dir <work-dir> --max-chars 12000 --target-zones 3",
        "<python> -m web_translator prepare-assignments --run-dir <work-dir>",
        "<python> -m web_translator validate-translations --run-dir <work-dir> --zone-id zone-001",
        "<python> -m web_translator validate-translations --run-dir <work-dir>",
        "<python> -m web_translator pdf-review-input --run-dir <work-dir>",
        "<python> -m web_translator pdf-assemble --run-dir <work-dir> --output-dir <output-dir>",
        "<python> -m web_translator pdf-qa prepare --run-dir <work-dir> --output-dir <output-dir>",
        "<python> -m web_translator pdf-qa finalize --run-dir <work-dir> --output-dir <output-dir>",
    )
    lines = [line.strip() for line in text.splitlines()]
    positions = [lines.index(command) for command in commands]
    assert positions == sorted(positions)


def test_pdf_skill_allocates_native_paths_from_strict_json() -> None:
    text = PDF_SKILL.read_text("utf-8")
    for phrase in (
        "create_pdf_run_paths",
        "json.dumps",
        "sort_keys=True",
        'separators=(",", ":")',
        "sys.argv[1]",
        "$allocationJson = & $python -c",
        "$source",
        "ConvertFrom-Json -ErrorAction Stop",
        "$allocation.PSObject.Properties.Name",
        'allocation_json=$("$python" -c',
        '"$source"',
        "json.loads(sys.argv[1])",
        'set(data) != {"work_dir", "output_dir"}',
        "os.path.isabs",
        "os.path.isdir",
        "os.path.lexists",
        "malformed allocation output",
        "Never use `eval`",
    ):
        assert phrase in text


def test_pdf_skill_requires_complete_strict_visual_review() -> None:
    text = PDF_SKILL.read_text("utf-8")
    for phrase in (
        "Inspect every numbered contact sheet",
        "pages_reviewed",
        "contact_sheets_reviewed",
        "staged_pdf_sha256",
        "unresolved_required",
        '"verdict": "pass"',
        '"evidence":',
        "heading_hierarchy",
        "text_legibility",
        "table_legibility",
        "figure_caption_pairing",
        "footnote_placement",
        "page_transitions",
        "clipping_overlap",
        "glyph_rendering",
        "exactly the eight canonical dimensions",
        "`pages_reviewed` is a sorted, unique integer array",
        "`contact_sheets_reviewed` maps each filename string to a sorted, unique integer array",
        "`unresolved_required` is a sorted, unique string array",
        "translated.pdf",
        "manifest.json",
        "review-report.md",
        "Only after `pdf-qa finalize` succeeds",
    ):
        assert phrase in text

    schema_text = text.split("```json\n", 1)[1].split("\n```", 1)[0]
    schema = json.loads(schema_text)
    assert set(schema) == {
        "schema_version",
        "staged_pdf_sha256",
        "pages_reviewed",
        "contact_sheets_reviewed",
        "findings",
        "unresolved_required",
    }
    assert set(schema["findings"]) == {
        "heading_hierarchy",
        "text_legibility",
        "table_legibility",
        "figure_caption_pairing",
        "footnote_placement",
        "page_transitions",
        "clipping_overlap",
        "glyph_rendering",
    }
    assert schema["pages_reviewed"] == [1]
    assert all(type(page) is int for page in schema["pages_reviewed"])
    assert schema["contact_sheets_reviewed"] == {"contact-sheet-001.png": [1]}
    assert all(
        type(page) is int
        for pages in schema["contact_sheets_reviewed"].values()
        for page in pages
    )
    assert schema["unresolved_required"] == []
    assert all(
        set(finding) == {"verdict", "evidence"}
        and finding["verdict"] in {"pass", "required-fix"}
        and finding["evidence"].strip()
        for finding in schema["findings"].values()
    )


def test_pdf_skill_and_spec_bind_review_render_figure_and_link_contracts() -> None:
    skill = PDF_SKILL.read_text("utf-8")
    spec = Path(
        "docs/superpowers/specs/2026-08-21-pdf-translator-design.md"
    ).read_text("utf-8")
    readme = Path("README.md").read_text("utf-8")
    for text in (skill, spec, readme):
        for phrase in (
            "36,000,000",
            "360,000,000",
            "64 MiB",
            "1 GiB",
            "pdf-review-input",
            "semantic_input_sha256",
            "standalone uncaptioned figure",
            "visible label and destination",
        ):
            assert phrase in text
    assert "OS-level address-space limit" in skill
    assert "OS-level address-space limit" in spec


def test_readme_documents_pdf_workflow_and_boundaries() -> None:
    text = Path("README.md").read_text("utf-8")
    for phrase in (
        "pdf-translator",
        "local text-selectable PDF",
        "public HTTP(S) PDF URL",
        "50 MiB",
        "100 pages",
        "scanned",
        "encrypted",
        "malformed",
        "Poppler",
        "pdftoppm",
        "pdfinfo",
        "brew install poppler",
        "Noto Sans KR",
        "PROVENANCE.json",
        "pdf-qa prepare",
        "pdf-qa finalize",
        "pdf-layout-review.json",
        "translated-pdfs",
    ):
        assert phrase in text
