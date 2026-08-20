from pathlib import Path


SKILL = Path("skills/web-translator/SKILL.md")
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
