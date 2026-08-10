from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup
import pytest

from web_translator.assemble import AssemblyError, assemble_page
from web_translator.extract import extract_segments
from web_translator.models import ProtectedToken, Segment, Translation
from web_translator.terminology import TerminologyError, normalize_first_use


TOKEN = "⟦WT:000000⟧"


def translation(segment_id: str, text: str) -> Translation:
    return Translation(
        segment_id=segment_id,
        text=text,
        notes="reviewed",
        glossary_observations={"OAuth": "consistent"},
    )


def segment(
    segment_id: str,
    source_text: str,
    *,
    protected: list[ProtectedToken] | None = None,
) -> Segment:
    return Segment(
        id=segment_id,
        locator=f"[data-wt-segment='{segment_id}']",
        semantic_type="paragraph",
        heading_path=[],
        source_text=source_text,
        protected=[] if protected is None else protected,
        context_ids=[],
        target=True,
    )


def write_source(tmp_path: Path, body: str, *, head: str = "") -> Path:
    source = tmp_path / "캡처 작업" / "source.html"
    source.parent.mkdir(parents=True)
    source.write_text(
        f"<!doctype html><html><head>{head}</head><body>{body}</body></html>",
        encoding="utf-8",
    )
    return source


def test_first_use_keeps_english_and_adds_one_korean_gloss() -> None:
    records = [
        translation("a", "OAuth enables exchange."),
        translation("b", "OAuth is reused."),
    ]

    normalized = normalize_first_use(records, {"OAuth": "권한 위임"})

    assert normalized[0].text == "OAuth(권한 위임) enables exchange."
    assert normalized[1].text == "OAuth is reused."
    assert normalized[0].notes == "reviewed"
    assert normalized[0].glossary_observations == {"OAuth": "consistent"}


def test_first_use_is_longest_first_case_sensitive_and_boundary_aware() -> None:
    records = [
        translation("a", "Springboard uses spring; Spring AI extends Spring."),
        translation("b", "spring ai and Spring AI remain."),
    ]

    normalized = normalize_first_use(
        records,
        {"Spring": "스프링", "Spring AI": "스프링 AI"},
    )

    assert normalized[0].text == (
        "Springboard uses spring; Spring AI(스프링 AI) extends Spring(스프링)."
    )
    assert normalized[1].text == "spring ai and Spring AI remain."


def test_first_use_removes_existing_gloss_variants_and_is_idempotent() -> None:
    records = [
        translation("a", "OAuth (권한 위임)(권한 위임) 흐름과 OAuth(권한 위임) 처리"),
        translation("b", "OAuth(권한 위임) 재사용"),
    ]

    once = normalize_first_use(records, {"OAuth": "권한 위임"})
    twice = normalize_first_use(once, {"OAuth": "권한 위임"})

    assert [item.text for item in once] == [
        "OAuth(권한 위임) 흐름과 OAuth 처리",
        "OAuth 재사용",
    ]
    assert twice == once


@pytest.mark.parametrize(
    "qualification",
    [
        "사용자 승인에 따라 동작함",
        "선택 사항",
        "웹 환경에서만",
        "레거시 시스템용",
    ],
)
def test_first_use_preserves_semantic_korean_parenthetical(
    qualification: str,
) -> None:
    records = [
        translation("a", f"OAuth({qualification}) 요청입니다."),
        translation("b", "OAuth 재사용"),
    ]

    once = normalize_first_use(records, {"OAuth": "권한 위임"})
    twice = normalize_first_use(once, {"OAuth": "권한 위임"})

    assert once[0].text == f"OAuth(권한 위임)({qualification}) 요청입니다."
    assert once[1].text == "OAuth 재사용"
    assert twice == once


def test_first_use_matches_terms_across_inline_tag_tokens() -> None:
    open_tag = "⟦WT:000000⟧"
    close_tag = "⟦WT:000001⟧"
    records = [
        translation("a", f"Spring {open_tag}AI{close_tag} 모델"),
        translation("b", "Spring AI 재사용"),
    ]

    normalized = normalize_first_use(
        records,
        {"Spring AI": "스프링 AI"},
        protected_by_segment={
            "a": [
                ProtectedToken(open_tag, "tag", "<em>"),
                ProtectedToken(close_tag, "tag", "</em>"),
            ],
            "b": [],
        },
    )

    assert normalized[0].text == (
        f"Spring {open_tag}AI{close_tag}(스프링 AI) 모델"
    )
    assert normalized[1].text == "Spring AI 재사용"


def test_first_use_does_not_match_across_opaque_code_token() -> None:
    records = [
        translation("a", f"Spring {TOKEN}AI code"),
        translation("b", "Spring AI prose"),
    ]

    normalized = normalize_first_use(
        records,
        {"Spring AI": "스프링 AI"},
        protected_by_segment={
            "a": [ProtectedToken(TOKEN, "code", "<code>x</code>")],
            "b": [],
        },
    )

    assert normalized[0].text == f"Spring {TOKEN}AI code"
    assert normalized[1].text == "Spring AI(스프링 AI) prose"


def test_first_use_is_idempotent_with_overlapping_terms_inside_canonical_gloss() -> None:
    records = [
        translation("a", "Spring AI works."),
        translation("b", "AI models follow."),
    ]
    glossary = {"Spring AI": "스프링 AI", "AI": "인공지능"}

    once = normalize_first_use(records, glossary)
    twice = normalize_first_use(once, glossary)

    assert [item.text for item in once] == [
        "Spring AI(스프링 AI) works.",
        "AI(인공지능) models follow.",
    ]
    assert twice == once


def test_first_use_preserves_unregistered_noncanonical_parenthetical() -> None:
    records = [
        translation("a", "OAuth(기존 번역)(사용자 승인에 따라 동작함) 요청"),
        translation("b", "OAuth(또 다른 번역) 재사용"),
    ]

    normalized = normalize_first_use(records, {"OAuth": "권한 위임"})

    assert [item.text for item in normalized] == [
        "OAuth(권한 위임)(기존 번역)(사용자 승인에 따라 동작함) 요청",
        "OAuth(또 다른 번역) 재사용",
    ]


def test_first_use_does_not_inspect_or_rewrite_protected_tokens() -> None:
    records = [
        translation("a", f"{TOKEN} 이후 OAuth"),
        translation("b", "OAuth 재사용"),
    ]

    normalized = normalize_first_use(records, {"OAuth": "권한 위임"})

    assert normalized[0].text == f"{TOKEN} 이후 OAuth(권한 위임)"
    assert normalized[1].text == "OAuth 재사용"


@pytest.mark.parametrize(
    "glossary",
    [
        {"": "빈 용어"},
        {"OAuth": ""},
        {"OAuth": "gloss without Korean"},
        {"OAuth": "괄호(금지)"},
    ],
)
def test_first_use_rejects_invalid_glossary_entries(glossary: dict[str, str]) -> None:
    with pytest.raises(TerminologyError):
        normalize_first_use([translation("a", "OAuth")], glossary)


def test_assembly_changes_only_marked_content_and_restores_inline_code(
    tmp_path: Path,
) -> None:
    source = write_source(
        tmp_path,
        '<main id="doc"><p class="lead" data-extra="keep" '
        'data-wt-segment="seg-000001">Hello <code>x</code>.</p></main>',
    )
    protected = [ProtectedToken(TOKEN, "code", "<code>x</code>")]
    segments = {
        "seg-000001": segment("seg-000001", f"Hello {TOKEN}.", protected=protected)
    }
    translated = {"seg-000001": translation("seg-000001", f"안녕하세요 {TOKEN}.")}

    output = assemble_page(
        source,
        segments,
        translated,
        {},
        tmp_path / "출력 결과",
        "https://example.com/docs",
    )

    soup = BeautifulSoup(output.read_text("utf-8"), "lxml")
    assert output == tmp_path / "출력 결과" / "index.html"
    assert soup.main["id"] == "doc"
    assert soup.p["class"] == ["lead"]
    assert soup.p["data-extra"] == "keep"
    assert "data-wt-segment" not in soup.p.attrs
    assert soup.p.code.string == "x"
    assert soup.p.get_text() == "안녕하세요 x."


def test_assembly_normalizes_glosses_in_document_order_not_mapping_order(
    tmp_path: Path,
) -> None:
    source = write_source(
        tmp_path,
        '<p data-wt-segment="seg-000001">One</p>'
        '<p data-wt-segment="seg-000002">Two</p>',
    )
    segments = {
        "seg-000002": segment("seg-000002", "Two"),
        "seg-000001": segment("seg-000001", "One"),
    }
    translations = {
        "seg-000002": translation("seg-000002", "OAuth 두 번째"),
        "seg-000001": translation("seg-000001", "OAuth 첫 번째"),
    }

    output = assemble_page(
        source,
        segments,
        translations,
        {"OAuth": "권한 위임"},
        tmp_path / "out",
        "https://example.com/",
    )

    paragraphs = BeautifulSoup(output.read_text("utf-8"), "lxml").find_all("p")
    assert [node.get_text() for node in paragraphs] == [
        "OAuth(권한 위임) 첫 번째",
        "OAuth 두 번째",
    ]


def test_assembly_translates_nested_markers_after_parent_replacement(
    tmp_path: Path,
) -> None:
    source = write_source(
        tmp_path,
        "<ul><li>Outer before<ul><li>Inner item</li></ul>Outer after</li></ul>",
    )
    extracted = extract_segments(source, source.parent / "segments.jsonl")
    segments = {item.id: item for item in extracted}
    translations = {
        item.id: translation(
            item.id,
            item.source_text
            .replace("Outer before", "외부 앞")
            .replace("Outer after", "외부 뒤")
            .replace("Inner item", "내부 항목"),
        )
        for item in extracted
    }

    output = assemble_page(
        source,
        segments,
        translations,
        {},
        tmp_path / "out",
        "https://example.com/",
    )

    items = BeautifulSoup(output.read_text("utf-8"), "lxml").find_all("li")
    assert items[0].get_text(" ", strip=True) == "외부 앞 내부 항목 외부 뒤"
    assert items[1].get_text(" ", strip=True) == "내부 항목"


def test_assembly_copies_assets_and_adds_offline_csp_and_attribution(
    tmp_path: Path,
) -> None:
    source = write_source(
        tmp_path,
        '<main><p data-wt-segment="seg-000001">Hello</p></main>',
        head='<link rel="stylesheet" href="assets/theme.css">',
    )
    assets = source.parent / "assets" / "fonts"
    assets.mkdir(parents=True)
    (source.parent / "assets" / "theme.css").write_text("body { color: black }", "utf-8")
    (assets / "doc.woff2").write_bytes(b"font")

    output = assemble_page(
        source,
        {"seg-000001": segment("seg-000001", "Hello")},
        {"seg-000001": translation("seg-000001", "안녕하세요")},
        {},
        tmp_path / "out",
        "https://example.com/docs",
    )

    soup = BeautifulSoup(output.read_text("utf-8"), "lxml")
    csp = soup.find("meta", attrs={"http-equiv": "Content-Security-Policy"})
    assert csp is not None
    policy = csp["content"]
    assert "default-src 'none'" in policy
    assert "script-src 'none'" in policy
    assert "connect-src 'none'" in policy
    assert "object-src 'none'" in policy
    assert "style-src 'self' 'unsafe-inline'" in policy
    assert "img-src 'self' data:" in policy
    assert "font-src 'self' data:" in policy
    assert (output.parent / "assets" / "theme.css").is_file()
    assert (output.parent / "assets" / "fonts" / "doc.woff2").read_bytes() == b"font"
    attribution = soup.select_one("[data-wt-attribution]")
    assert attribution is not None
    assert attribution.find("a")["href"] == "https://example.com/docs"
    assert soup.body.contents.index(attribution) > soup.body.contents.index(soup.main)


def test_assembly_requires_exact_segment_translation_and_marker_sets(
    tmp_path: Path,
) -> None:
    source = write_source(
        tmp_path,
        '<p data-wt-segment="seg-000001">One</p>'
        '<p data-wt-segment="seg-000001">Duplicate</p>',
    )
    segments = {"seg-000001": segment("seg-000001", "One")}
    translations = {"seg-000001": translation("seg-000001", "하나")}

    with pytest.raises(AssemblyError, match="marker"):
        assemble_page(source, segments, translations, {}, tmp_path / "duplicate", "https://example.com/")

    valid_source = write_source(tmp_path / "valid", '<p data-wt-segment="seg-000001">One</p>')
    with pytest.raises(AssemblyError, match="missing translation"):
        assemble_page(valid_source, segments, {}, {}, tmp_path / "missing", "https://example.com/")
    with pytest.raises(AssemblyError, match="foreign translation"):
        assemble_page(
            valid_source,
            segments,
            {**translations, "seg-999999": translation("seg-999999", "외부")},
            {},
            tmp_path / "foreign",
            "https://example.com/",
        )


def test_assembly_rejects_translation_record_with_mismatched_key(tmp_path: Path) -> None:
    source = write_source(tmp_path, '<p data-wt-segment="seg-000001">One</p>')

    with pytest.raises(AssemblyError, match="translation key"):
        assemble_page(
            source,
            {"seg-000001": segment("seg-000001", "One")},
            {"seg-000001": translation("seg-000002", "둘")},
            {},
            tmp_path / "out",
            "https://example.com/",
        )


def test_assembly_rejects_executable_restored_markup_and_changed_shape(
    tmp_path: Path,
) -> None:
    source = write_source(
        tmp_path,
        '<p data-wt-segment="seg-000001"><em>source</em></p>',
    )

    malicious = segment(
        "seg-000001",
        TOKEN,
        protected=[ProtectedToken(TOKEN, "code", '<script src="https://evil.test/x.js"></script>')],
    )
    with pytest.raises(AssemblyError, match="executable"):
        assemble_page(
            source,
            {"seg-000001": malicious},
            {"seg-000001": translation("seg-000001", TOKEN)},
            {},
            tmp_path / "script",
            "https://example.com/",
        )

    changed = segment(
        "seg-000001",
        TOKEN,
        protected=[ProtectedToken(TOKEN, "code", "<strong>translated</strong>")],
    )
    with pytest.raises(AssemblyError, match="shape"):
        assemble_page(
            source,
            {"seg-000001": changed},
            {"seg-000001": translation("seg-000001", TOKEN)},
            {},
            tmp_path / "shape",
            "https://example.com/",
        )


@pytest.mark.parametrize(
    "html",
    [
        "plain text only",
        "<html><head></head></html>",
        "<!doctype html><html><head></head><body>\x00</body></html>",
    ],
)
def test_assembly_rejects_malformed_source(tmp_path: Path, html: str) -> None:
    source = tmp_path / "source.html"
    source.write_text(html, encoding="utf-8")

    with pytest.raises(AssemblyError, match="source HTML"):
        assemble_page(source, {}, {}, {}, tmp_path / "out", "https://example.com/")


def test_assembly_rejects_existing_output_and_unsafe_source_url(tmp_path: Path) -> None:
    source = write_source(tmp_path, '<p data-wt-segment="seg-000001">One</p>')
    segments = {"seg-000001": segment("seg-000001", "One")}
    translations = {"seg-000001": translation("seg-000001", "하나")}
    existing = tmp_path / "existing"
    existing.mkdir()

    with pytest.raises(AssemblyError, match="output"):
        assemble_page(source, segments, translations, {}, existing, "https://example.com/")
    with pytest.raises(AssemblyError, match="source URL"):
        assemble_page(
            source,
            segments,
            translations,
            {},
            tmp_path / "unsafe-url",
            "javascript:alert(1)",
        )


def test_assembly_rejects_symlinked_asset(tmp_path: Path) -> None:
    source = write_source(tmp_path, '<p data-wt-segment="seg-000001">One</p>')
    assets = source.parent / "assets"
    assets.mkdir()
    target = tmp_path / "outside.css"
    target.write_text("secret", encoding="utf-8")
    link = assets / "theme.css"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable on this Windows host: {error}")

    with pytest.raises(AssemblyError, match="link|reparse"):
        assemble_page(
            source,
            {"seg-000001": segment("seg-000001", "One")},
            {"seg-000001": translation("seg-000001", "하나")},
            {},
            tmp_path / "out",
            "https://example.com/",
        )


def test_assembly_rejects_event_handlers_and_javascript_links_from_tokens(
    tmp_path: Path,
) -> None:
    source = write_source(
        tmp_path,
        '<p data-wt-segment="seg-000001"><a href="/safe">safe</a></p>',
    )
    dangerous = '<a href="javascript:alert(1)" onclick="alert(1)">unsafe</a>'
    unsafe_segment = segment(
        "seg-000001",
        TOKEN,
        protected=[ProtectedToken(TOKEN, "code", dangerous)],
    )

    with pytest.raises(AssemblyError, match="executable"):
        assemble_page(
            source,
            {"seg-000001": unsafe_segment},
            {"seg-000001": translation("seg-000001", TOKEN)},
            {},
            tmp_path / "out",
            "https://example.com/",
        )


@pytest.mark.parametrize(
    "href",
    [
        "data:text/html,<script>alert(1)</script>",
        "java&#x09;script:alert(1)",
        "java&#10;script:alert(1)",
    ],
    ids=["data-html", "tab-obfuscated-javascript", "newline-obfuscated-javascript"],
)
def test_assembly_rejects_obfuscated_executable_urls_even_when_shape_matches(
    tmp_path: Path, href: str
) -> None:
    dangerous = f'<a href="{href}">unsafe</a>'
    source = write_source(
        tmp_path,
        f'<p data-wt-segment="seg-000001">{dangerous}</p>',
    )
    unsafe_segment = segment(
        "seg-000001",
        TOKEN,
        protected=[ProtectedToken(TOKEN, "code", dangerous)],
    )

    with pytest.raises(AssemblyError, match="executable URL"):
        assemble_page(
            source,
            {"seg-000001": unsafe_segment},
            {"seg-000001": translation("seg-000001", TOKEN)},
            {},
            tmp_path / "out",
            "https://example.com/",
        )


def test_assembly_rejects_source_meta_refresh_and_preexisting_csp(tmp_path: Path) -> None:
    refresh = write_source(
        tmp_path / "refresh",
        '<p data-wt-segment="seg-000001">One</p>',
        head='<meta http-equiv="refresh" content="0; url=https://evil.test/">',
    )
    existing_csp = write_source(
        tmp_path / "csp",
        '<p data-wt-segment="seg-000001">One</p>',
        head='<meta http-equiv="Content-Security-Policy" content="style-src https://cdn.test">',
    )
    segments = {"seg-000001": segment("seg-000001", "One")}
    translations = {"seg-000001": translation("seg-000001", "하나")}

    with pytest.raises(AssemblyError, match="refresh"):
        assemble_page(refresh, segments, translations, {}, tmp_path / "refresh-out", "https://example.com/")
    with pytest.raises(AssemblyError, match="Content-Security-Policy"):
        assemble_page(existing_csp, segments, translations, {}, tmp_path / "csp-out", "https://example.com/")


def test_assembly_wraps_non_string_translation_keys_as_contract_errors(tmp_path: Path) -> None:
    source = write_source(tmp_path, '<p data-wt-segment="seg-000001">One</p>')

    with pytest.raises(AssemblyError, match="translation keys"):
        assemble_page(
            source,
            {"seg-000001": segment("seg-000001", "One")},
            {
                "seg-000001": translation("seg-000001", "하나"),
                7: translation("seg-000007", "일곱"),  # type: ignore[dict-item]
            },
            {},
            tmp_path / "out",
            "https://example.com/",
        )


def test_assembly_rejects_broken_asset_symlink(tmp_path: Path) -> None:
    source = write_source(tmp_path, '<p data-wt-segment="seg-000001">One</p>')
    assets = source.parent / "assets"
    assets.mkdir()
    link = assets / "missing.css"
    try:
        link.symlink_to(tmp_path / "does-not-exist.css")
    except OSError as error:
        pytest.skip(f"symlinks unavailable on this Windows host: {error}")

    with pytest.raises(AssemblyError, match="link|reparse"):
        assemble_page(
            source,
            {"seg-000001": segment("seg-000001", "One")},
            {"seg-000001": translation("seg-000001", "하나")},
            {},
            tmp_path / "out",
            "https://example.com/",
        )


def test_assembly_rejects_reparse_output_parent(tmp_path: Path) -> None:
    source = write_source(tmp_path, '<p data-wt-segment="seg-000001">One</p>')
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-output"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable on this Windows host: {error}")

    with pytest.raises(AssemblyError, match="output.*link|output.*reparse"):
        assemble_page(
            source,
            {"seg-000001": segment("seg-000001", "One")},
            {"seg-000001": translation("seg-000001", "하나")},
            {},
            linked_parent / "bundle",
            "https://example.com/",
        )
