from pathlib import Path
import re

from bs4 import BeautifulSoup
import pytest

from web_translator.extract import extract_segments
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

    assert [segment.semantic_type for segment in segments] == ["heading", "list_item"]
    assert "Nested paragraph" in segments[1].source_text
    assert sum("Nested paragraph" in segment.source_text for segment in segments) == 1
    assert "Do not translate" not in "".join(segment.source_text for segment in segments)


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
