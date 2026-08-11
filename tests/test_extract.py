from pathlib import Path
import re

from bs4 import BeautifulSoup
import pytest

from web_translator.extract import (
    TRANSLATABLE_ATTRIBUTES,
    ExtractionError,
    extract_segments,
)
from web_translator.models import read_segments
from web_translator.protection import ProtectionError, protect_fragment, restore_tokens


def write_html(tmp_path: Path, html: str) -> Path:
    source = tmp_path / "source.html"
    source.write_text(html, encoding="utf-8")
    return source


def test_extracts_blocks_with_heading_context_and_inline_markup(tmp_path: Path) -> None:
    source = write_html(
        tmp_path,
        """
          <h1>OAuth Token Exchange</h1>
          <p>Use <strong>security tokens</strong> with <code>grant_type</code>.</p>
          <pre><code>MUST remain exact</code></pre>
        """,
    )

    segments = extract_segments(source, tmp_path / "segments.jsonl")

    assert [segment.semantic_type for segment in segments] == ["heading", "paragraph"]
    assert segments[1].heading_path == ["OAuth Token Exchange"]
    assert "security tokens" in segments[1].source_text
    assert "grant_type" not in segments[1].source_text
    assert any(
        token.kind == "code" and "grant_type" in token.value
        for token in segments[1].protected
    )


def test_protects_inline_structure_but_leaves_nested_prose_translatable() -> None:
    fragment = (
        'Read <em>the <strong>security guide</strong></em> at '
        '<a href="https://example.com/guide">this URL</a>.'
    )

    protected, tokens = protect_fragment(fragment)

    assert "the " in protected
    assert "security guide" in protected
    assert "this URL" in protected
    assert "https://example.com/guide" not in protected
    assert [token.kind for token in tokens].count("tag") == 6
    assert restore_tokens(protected, tokens) == fragment


def test_protects_an_opening_tag_with_a_greater_than_sign_in_an_attribute() -> None:
    fragment = '<span title="1 > 0">visible</span>'

    protected, tokens = protect_fragment(fragment)

    assert tokens[0].value == '<span title="1 > 0">'
    assert protected == f"{tokens[0].token}visible{tokens[1].token}"
    assert restore_tokens(protected, tokens) == fragment


@pytest.mark.parametrize("element", ["code", "kbd", "samp", "var"])
def test_protects_complete_code_like_elements(element: str) -> None:
    fragment = f"Before <{element} class='x'>inner <b>exact</b></{element}> after"

    protected, tokens = protect_fragment(fragment)

    assert "inner" not in protected
    assert len(tokens) == 1
    assert tokens[0].kind == "code"
    assert tokens[0].value == f"<{element} class='x'>inner <b>exact</b></{element}>"
    assert restore_tokens(protected, tokens) == fragment


def test_protects_urls_commands_and_rfc_keywords_with_fixed_width_tokens() -> None:
    fragment = "MUST run npm install web-translator; see https://example.com/docs."

    protected, tokens = protect_fragment(fragment)

    assert all(re.fullmatch(r"⟦WT:\d{6}⟧", token.token) for token in tokens)
    assert {token.kind for token in tokens} == {"keyword", "command", "url"}
    assert restore_tokens(protected, tokens) == fragment


def test_url_detection_keeps_balanced_closing_parentheses() -> None:
    url = "https://en.wikipedia.org/wiki/Function_(mathematics)"

    protected, tokens = protect_fragment(f"See {url}.")

    assert [token.value for token in tokens] == [url]
    assert protected.endswith(f"{tokens[0].token}.")


def test_rfc_keywords_are_uppercase_and_multiword_phrases_stay_whole() -> None:
    fragment = "Clients may retry but SHOULD NOT duplicate the request."

    protected, tokens = protect_fragment(fragment)

    assert "may" in protected
    assert [token.value for token in tokens] == ["SHOULD NOT"]
    assert restore_tokens(protected, tokens) == fragment


def test_command_detection_does_not_hide_ordinary_prose() -> None:
    fragment = "Readers make a careful choice, and Git users should review it."

    protected, tokens = protect_fragment(fragment)

    assert protected == fragment
    assert tokens == []


def test_bare_make_word_at_sentence_start_does_not_hide_prose() -> None:
    fragment = "make a careful choice."

    protected, tokens = protect_fragment(fragment)

    assert protected == fragment
    assert tokens == []


def test_command_detection_stops_before_following_visible_prose() -> None:
    fragment = "Run git status and review the output."

    protected, tokens = protect_fragment(fragment)

    assert [token.value for token in tokens] == ["git status"]
    assert "and review the output." in protected
    assert restore_tokens(protected, tokens) == fragment


@pytest.mark.parametrize("adverb", ["carefully", "please"])
def test_no_argument_command_does_not_consume_a_following_adverb(adverb: str) -> None:
    fragment = f"Run git status {adverb}."

    protected, tokens = protect_fragment(fragment)

    assert [token.value for token in tokens] == ["git status"]
    assert f"{adverb}." in protected


@pytest.mark.parametrize(
    "command",
    [
        "make",
        "make test",
        "go env",
        "go test",
        "dotnet add package X",
        "dotnet build",
        "uv export",
        "uv sync",
    ],
)
def test_command_detection_protects_common_build_commands(command: str) -> None:
    protected, tokens = protect_fragment(f"Run {command}; then review.")

    assert [token.value for token in tokens] == [command]
    assert restore_tokens(protected, tokens) == f"Run {command}; then review."


@pytest.mark.parametrize(
    "command",
    [
        "go test ./...",
        "dotnet build Project.sln",
        "uv export --output-file requirements.txt",
        "git log -n 5",
    ],
)
def test_command_detection_keeps_syntax_bearing_operands(command: str) -> None:
    protected, tokens = protect_fragment(f"Run {command}; then review.")

    assert [token.value for token in tokens] == [command]
    assert restore_tokens(protected, tokens) == f"Run {command}; then review."


def test_path_operand_does_not_make_a_following_adverb_part_of_the_command() -> None:
    fragment = "Run dotnet build Project.sln carefully."

    protected, tokens = protect_fragment(fragment)

    assert [token.value for token in tokens] == ["dotnet build Project.sln"]
    assert "carefully." in protected


@pytest.mark.parametrize(
    "command",
    [
        "go test -run TestFoo ./...",
        "dotnet build -c Release Project.sln",
        "make -j4 test",
        "make test VAR=value",
        "git log --oneline -n 5",
    ],
)
def test_command_detection_keeps_structured_option_values(command: str) -> None:
    protected, tokens = protect_fragment(f"Run {command}; then review.")

    assert [token.value for token in tokens] == [command]
    assert restore_tokens(protected, tokens) == f"Run {command}; then review."


@pytest.mark.parametrize(
    "command",
    [
        "make VAR=value test",
        "git config --global user.name Alice",
        "go test -count 1 ./...",
    ],
)
def test_command_detection_respects_command_specific_argument_order(command: str) -> None:
    protected, tokens = protect_fragment(f"Run {command}; then review.")

    assert [token.value for token in tokens] == [command]
    assert restore_tokens(protected, tokens) == f"Run {command}; then review."


@pytest.mark.parametrize(
    "command",
    [
        "npm install",
        "pnpm install",
        "yarn install",
        "npm publish",
        "uv pip install requests",
    ],
)
def test_command_detection_supports_optional_and_nested_package_commands(command: str) -> None:
    protected, tokens = protect_fragment(f"Run {command}; then review.")

    assert [token.value for token in tokens] == [command]
    assert restore_tokens(protected, tokens) == f"Run {command}; then review."


def test_optional_package_operand_does_not_consume_a_sentence_final_adverb() -> None:
    fragment = "Run npm install carefully."

    protected, tokens = protect_fragment(fragment)

    assert [token.value for token in tokens] == ["npm install"]
    assert "carefully." in protected


@pytest.mark.parametrize(
    "command",
    [
        "make clean test",
        "make -j 4 test",
        "go mod tidy",
        "docker compose -f compose.yml up",
        "docker run --rm ubuntu echo hello",
        "npx vite build",
        "git -C repo status",
    ],
)
def test_command_detection_keeps_supported_nested_and_multi_operand_commands(
    command: str,
) -> None:
    fragment = f"Run {command}; then review."

    protected, tokens = protect_fragment(fragment)

    assert [token.value for token in tokens] == [command]
    assert restore_tokens(protected, tokens) == fragment


@pytest.mark.parametrize(
    ("fragment", "command", "visible_prose"),
    [
        ("Run docker build carefully.", "docker build", "carefully."),
        (
            "The npm install command is useful.",
            "npm install",
            "command is useful.",
        ),
    ],
)
def test_command_detection_stops_at_sentence_role_boundaries(
    fragment: str,
    command: str,
    visible_prose: str,
) -> None:
    protected, tokens = protect_fragment(fragment)

    assert [token.value for token in tokens] == [command]
    assert visible_prose in protected
    assert restore_tokens(protected, tokens) == fragment


@pytest.mark.parametrize(
    ("fragment", "command", "visible_prose"),
    [
        (
            "Run make clean test to verify outputs.",
            "make clean test",
            "to verify outputs.",
        ),
        (
            "Run npx vite build to bundle assets.",
            "npx vite build",
            "to bundle assets.",
        ),
        (
            "Run docker run --rm ubuntu echo hello before reviewing output.",
            "docker run --rm ubuntu echo hello",
            "before reviewing output.",
        ),
    ],
)
def test_command_detection_stops_before_following_prose_clauses(
    fragment: str,
    command: str,
    visible_prose: str,
) -> None:
    protected, tokens = protect_fragment(fragment)

    assert [token.value for token in tokens] == [command]
    assert visible_prose in protected
    assert restore_tokens(protected, tokens) == fragment


@pytest.mark.parametrize(
    ("fragment", "command", "visible_prose"),
    [
        (
            "Run make clean test for a fresh build.",
            "make clean test",
            "for a fresh build.",
        ),
        (
            "Run npx vite build with production settings.",
            "npx vite build",
            "with production settings.",
        ),
        (
            "Run docker run --rm ubuntu echo hello as a smoke test.",
            "docker run --rm ubuntu echo hello",
            "as a smoke test.",
        ),
        (
            "Run docker run --rm ubuntu echo hello without keeping the container.",
            "docker run --rm ubuntu echo hello",
            "without keeping the container.",
        ),
    ],
)
def test_command_detection_stops_before_following_prose_adjuncts(
    fragment: str,
    command: str,
    visible_prose: str,
) -> None:
    protected, tokens = protect_fragment(fragment)

    assert [token.value for token in tokens] == [command]
    assert visible_prose in protected
    assert restore_tokens(protected, tokens) == fragment


@pytest.mark.parametrize(
    ("fragment", "command", "visible_prose"),
    [
        (
            "Run make clean test once the build completes.",
            "make clean test",
            "once the build completes.",
        ),
        (
            "Run npx vite build although the server is busy.",
            "npx vite build",
            "although the server is busy.",
        ),
        (
            "Run docker run --rm ubuntu echo hello despite the warning.",
            "docker run --rm ubuntu echo hello",
            "despite the warning.",
        ),
        (
            "Run npx vite build under CI.",
            "npx vite build",
            "under CI.",
        ),
    ],
)
def test_bounded_command_grammars_do_not_depend_on_every_prose_marker(
    fragment: str,
    command: str,
    visible_prose: str,
) -> None:
    protected, tokens = protect_fragment(fragment)

    assert [token.value for token in tokens] == [command]
    assert visible_prose in protected
    assert restore_tokens(protected, tokens) == fragment


@pytest.mark.parametrize(
    "boundary",
    [
        "and",
        "or",
        "then",
        "although",
        "unless",
        "because",
        "since",
        "whereas",
        "while",
        "when",
        "whenever",
        "after",
        "before",
        "once",
        "despite",
        "as",
        "if",
        "so",
        "that",
        "which",
        "who",
        "whose",
        "where",
        "during",
        "without",
        "with",
        "for",
        "from",
        "into",
        "over",
        "under",
        "through",
        "against",
        "around",
        "near",
    ],
)
def test_closed_class_prose_boundaries_apply_after_command_acceptance(
    boundary: str,
) -> None:
    fragment = f"The npm install {boundary} visible prose."

    protected, tokens = protect_fragment(fragment)

    assert [token.value for token in tokens] == ["npm install"]
    assert f"{boundary} visible prose." in protected
    assert restore_tokens(protected, tokens) == fragment


@pytest.mark.parametrize("operand", ["from", "under", "when"])
def test_closed_class_boundary_words_remain_valid_required_operands(
    operand: str,
) -> None:
    command = f"pip install {operand}"
    fragment = f"Run {command}; then review."

    protected, tokens = protect_fragment(fragment)

    assert [token.value for token in tokens] == [command]
    assert restore_tokens(protected, tokens) == fragment


@pytest.mark.parametrize(
    "command",
    [
        'pip install "package>=2"',
        'git commit -m "fix broken login"',
        "git config user.name Alice",
    ],
)
def test_command_detection_protects_quoted_arguments_and_common_subcommands(
    command: str,
) -> None:
    protected, tokens = protect_fragment(f"Run {command}; then verify.")

    assert [token.value for token in tokens] == [command]
    assert command not in protected


def test_restoration_rejects_missing_duplicate_and_foreign_tokens() -> None:
    protected, tokens = protect_fragment("Use <code>JWT</code> and MUST.")

    with pytest.raises(ProtectionError):
        restore_tokens(protected.replace(tokens[0].token, ""), tokens)
    with pytest.raises(ProtectionError):
        restore_tokens(protected + tokens[0].token, tokens)
    with pytest.raises(ProtectionError):
        restore_tokens(protected + "⟦WT:999999⟧", tokens)


def test_restoration_rejects_raw_model_supplied_markup() -> None:
    protected, tokens = protect_fragment("Use <em>safe text</em>.")

    with pytest.raises(ProtectionError):
        restore_tokens('<img src="x" onerror="alert(1)">' + protected, tokens)


@pytest.mark.parametrize(
    "order",
    [
        [3, 0, 1, 2],
        [0, 1, 3, 2],
    ],
    ids=["close-before-open", "crossed-nesting"],
)
def test_restoration_rejects_invalid_tag_boundary_order(order: list[int]) -> None:
    _, tokens = protect_fragment("<em><strong>text</strong></em>")
    reordered = "".join(tokens[index].token for index in order)

    with pytest.raises(ProtectionError):
        restore_tokens(reordered, tokens)


def test_literal_placeholder_text_does_not_collide_with_generated_tokens() -> None:
    fragment = "Literal ⟦WT:000000⟧ and <code>x</code>."

    protected, tokens = protect_fragment(fragment)

    assert len({token.token for token in tokens}) == len(tokens)
    assert protected.count("⟦WT:000000⟧") == 0
    assert restore_tokens(protected, tokens) == fragment


def test_skips_excluded_regions_and_nested_eligible_candidates(tmp_path: Path) -> None:
    source = write_html(
        tmp_path,
        """
        <h1>Guide</h1>
        <ul><li>Lead <p>Nested paragraph</p> tail</li></ul>
        <div translate="no"><p>Do not translate</p></div>
        <script><p>Not content</p></script>
        <pre>literal text</pre>
        """,
    )

    segments = extract_segments(source, tmp_path / "segments.jsonl")

    assert [segment.semantic_type for segment in segments] == [
        "heading",
        "list_item",
        "paragraph",
    ]
    assert "Nested paragraph" not in segments[1].source_text
    assert "Nested paragraph" in segments[2].source_text
    assert sum("Nested paragraph" in segment.source_text for segment in segments) == 1
    assert "Do not translate" not in "".join(segment.source_text for segment in segments)


def test_nested_list_items_are_independent_non_overlapping_segments(tmp_path: Path) -> None:
    source = write_html(
        tmp_path,
        "<ul><li>Outer before<ul><li>Inner item</li></ul>Outer after</li></ul>",
    )

    segments = extract_segments(source, tmp_path / "segments.jsonl")

    assert [segment.semantic_type for segment in segments] == ["list_item", "list_item"]
    assert "Outer before" in segments[0].source_text
    assert "Outer after" in segments[0].source_text
    assert "Inner item" not in segments[0].source_text
    assert "Inner item" in segments[1].source_text
    assert [segment.context_ids for segment in segments] == [
        ["seg-000002"],
        ["seg-000001"],
    ]
    soup = BeautifulSoup(source.read_text(encoding="utf-8"), "lxml")
    assert [item["data-wt-segment"] for item in soup.find_all("li")] == [
        "seg-000001",
        "seg-000002",
    ]


def test_nested_table_cells_keep_typed_row_groups_and_locators(tmp_path: Path) -> None:
    source = write_html(
        tmp_path,
        """
        <table><tr><td>Outer before
          <table><tr><th>Inner heading</th><td>Inner cell</td></tr></table>
        Outer after</td></tr></table>
        """,
    )

    segments = extract_segments(source, tmp_path / "segments.jsonl")

    assert [segment.semantic_type for segment in segments] == [
        "table_cell:row:000001",
        "table_header:row:000002",
        "table_cell:row:000002",
    ]
    assert "Inner heading" not in segments[0].source_text
    assert "Inner cell" not in segments[0].source_text
    assert "Inner heading" in segments[1].source_text
    assert "Inner cell" in segments[2].source_text
    assert [segment.locator for segment in segments] == [
        "[data-wt-segment='seg-000001']",
        "[data-wt-segment='seg-000002']",
        "[data-wt-segment='seg-000003']",
    ]


def test_protects_the_complete_nested_translate_no_subtree(tmp_path: Path) -> None:
    source = write_html(
        tmp_path,
        '<li>Lead <div translate="no"><div>x</div><p>secret</p></div> tail</li>',
    )

    segments = extract_segments(source, tmp_path / "segments.jsonl")

    assert len(segments) == 1
    assert "Lead " in segments[0].source_text
    assert " tail" in segments[0].source_text
    assert "secret" not in segments[0].source_text
    assert any(
        token.kind == "excluded" and "<p>secret</p>" in token.value
        for token in segments[0].protected
    )


def test_translate_no_text_inside_another_attribute_does_not_exclude_prose() -> None:
    fragment = '<span title="set translate=no now">Visible prose</span>'

    protected, tokens = protect_fragment(fragment)

    assert "Visible prose" in protected
    assert [token.kind for token in tokens] == ["tag", "tag"]
    assert restore_tokens(protected, tokens) == fragment


def test_persists_stable_markers_jsonl_and_neighbor_context(tmp_path: Path) -> None:
    source = write_html(
        tmp_path,
        "<h1>One</h1><p>First</p><h2>Two</h2><p>Second</p>",
    )
    manifest = tmp_path / "segments.jsonl"

    first = extract_segments(source, manifest)
    second = extract_segments(source, manifest)

    assert second == first
    assert read_segments(manifest) == first
    assert [segment.id for segment in first] == [
        "seg-000001",
        "seg-000002",
        "seg-000003",
        "seg-000004",
    ]
    assert first[0].context_ids == ["seg-000002"]
    assert first[1].context_ids == ["seg-000001", "seg-000003"]
    assert first[-1].context_ids == ["seg-000003"]
    assert first[-1].heading_path == ["One", "Two"]
    soup = BeautifulSoup(source.read_text(encoding="utf-8"), "lxml")
    assert [node["data-wt-segment"] for node in soup.select("[data-wt-segment]")] == [
        segment.id for segment in first
    ]


def test_heading_context_discards_same_and_deeper_preceding_levels(tmp_path: Path) -> None:
    source = write_html(
        tmp_path,
        "<h1>One</h1><h3>Three</h3><h2>Two</h2><p>Body</p><h1>Next</h1>",
    )

    segments = extract_segments(source, tmp_path / "segments.jsonl")

    assert segments[2].heading_path == ["One"]
    assert segments[3].heading_path == ["One", "Two"]
    assert segments[4].heading_path == []


def test_protected_only_heading_still_establishes_following_context(tmp_path: Path) -> None:
    source = write_html(tmp_path, "<h2><code>API</code></h2><p>Body</p>")

    segments = extract_segments(source, tmp_path / "segments.jsonl")

    assert len(segments) == 1
    assert segments[0].semantic_type == "paragraph"
    assert segments[0].heading_path == ["API"]


def test_extracts_standalone_links_buttons_and_unwrapped_visible_prose(
    tmp_path: Path,
) -> None:
    source = write_html(
        tmp_path,
        """
        <main>
          <a href="/guide">Read the security guide</a>
          <button type="button">Continue securely</button>
          <div>Before <span>the contextual explanation</span> after.</div>
        </main>
        """,
    )

    segments = extract_segments(source, tmp_path / "segments.jsonl")

    assert [segment.semantic_type for segment in segments] == [
        "link",
        "button",
        "prose",
    ]
    assert "Read the security guide" in segments[0].source_text
    assert "Continue securely" in segments[1].source_text
    assert "Before " in segments[2].source_text
    assert "the contextual explanation" in segments[2].source_text
    assert " after." in segments[2].source_text
    assert any(
        token.kind == "tag" and token.value.startswith("<span")
        for token in segments[2].protected
    )
    assert restore_tokens(segments[2].source_text, segments[2].protected) == (
        "Before <span>the contextual explanation</span> after."
    )


def test_extracts_accessibility_attributes_as_one_location_aware_segment(
    tmp_path: Path,
) -> None:
    source = write_html(
        tmp_path,
        """
        <img src="diagram.png"
             alt="Configure OAuth securely"
             aria-label="OAuth configuration diagram"
             title="Security token exchange">
        """,
    )

    segments = extract_segments(source, tmp_path / "segments.jsonl")

    assert len(segments) == 1
    segment = segments[0]
    assert segment.semantic_type == "located:attributes"
    assert segment.locator == "[data-wt-segment='seg-000001']"
    for phrase in (
        "Configure OAuth securely",
        "OAuth configuration diagram",
        "Security token exchange",
    ):
        assert phrase in segment.source_text
    assert {
        token.value
        for token in segment.protected
        if token.kind == "location"
    } >= {
        "<!--wt-location:attribute:alt-->",
        "<!--wt-location:attribute:aria-label-->",
        "<!--wt-location:attribute:title-->",
    }


def test_translatable_attribute_contract_is_exact_and_excludes_urls() -> None:
    assert TRANSLATABLE_ATTRIBUTES == (
        "alt",
        "aria-label",
        "aria-description",
        "title",
        "placeholder",
    )


def test_extracts_zero_target_eligible_body_text_instead_of_succeeding_empty(
    tmp_path: Path,
) -> None:
    source = write_html(tmp_path, "<html><body>Readable standalone prose.</body></html>")

    segments = extract_segments(source, tmp_path / "segments.jsonl")

    assert len(segments) == 1
    assert segments[0].source_text == "Readable standalone prose."


def test_accessibility_extraction_retains_exclusions_and_protected_values(
    tmp_path: Path,
) -> None:
    source = write_html(
        tmp_path,
        """
        <script>Visible words must not be translated</script>
        <style>.label::after { content: "Not prose"; }</style>
        <code>grant_type MUST stay exact</code>
        <div id="oauth-client" data-state="ready">
          Review MUST at https://example.com/spec with <var>grant_type</var>.
        </div>
        """,
    )

    segments = extract_segments(source, tmp_path / "segments.jsonl")

    assert len(segments) == 1
    assert "Review " in segments[0].source_text
    assert "https://example.com/spec" not in segments[0].source_text
    assert "grant_type" not in segments[0].source_text
    assert {token.kind for token in segments[0].protected} >= {
        "url",
        "keyword",
        "code",
    }
    marked = BeautifulSoup(source.read_text(encoding="utf-8"), "lxml")
    assert marked.div["id"] == "oauth-client"
    assert marked.div["data-state"] == "ready"
    assert not marked.script.has_attr("data-wt-segment")
    assert not marked.style.has_attr("data-wt-segment")
    assert not marked.code.has_attr("data-wt-segment")


def test_authored_reserved_marker_fails_closed_without_mutating_css_hook(
    tmp_path: Path,
) -> None:
    original = (
        '<style>[data-wt-segment="hero"] { display: grid; }</style>'
        '<div data-wt-segment="hero">Readable hero introduction.</div>'
    )
    source = write_html(tmp_path, original)
    manifest = tmp_path / "segments.jsonl"

    with pytest.raises(ExtractionError, match="reserved.*data-wt-segment"):
        extract_segments(source, manifest)

    assert source.read_text(encoding="utf-8") == original
    assert not manifest.exists()


def test_reserved_marker_css_selector_alone_fails_before_markers_activate_it(
    tmp_path: Path,
) -> None:
    original = (
        "<style>[data-wt-segment] { visibility: hidden; }</style>"
        "<p>Readable introduction.</p>"
    )
    source = write_html(tmp_path, original)
    manifest = tmp_path / "segments.jsonl"

    with pytest.raises(ExtractionError, match="reserved.*data-wt-segment"):
        extract_segments(source, manifest)

    assert source.read_text(encoding="utf-8") == original
    assert not manifest.exists()


def test_authored_reserved_marker_attribute_fails_without_stylesheet(
    tmp_path: Path,
) -> None:
    original = '<div data-wt-segment="hero">Readable hero introduction.</div>'
    source = write_html(tmp_path, original)

    with pytest.raises(ExtractionError, match="reserved.*data-wt-segment"):
        extract_segments(source, tmp_path / "segments.jsonl")

    assert source.read_text(encoding="utf-8") == original


def test_table_cells_record_typed_row_atomicity(
    tmp_path: Path,
) -> None:
    source = write_html(
        tmp_path,
        "<table><tr><th>First heading</th><td>First value</td></tr>"
        "<tr><th>Second heading</th><td>Second value</td></tr></table>",
    )

    segments = extract_segments(source, tmp_path / "segments.jsonl")

    assert [segment.semantic_type for segment in segments] == [
        "table_header:row:000001",
        "table_cell:row:000001",
        "table_header:row:000002",
        "table_cell:row:000002",
    ]
