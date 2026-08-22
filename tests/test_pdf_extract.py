"""Regression tests for fail-closed PDF inspection and logical extraction."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
import pdfplumber
import pytest
from reportlab.pdfgen.canvas import Canvas

from web_translator.models import read_segments

from tests.pdf_fixtures import (
    make_dimension_pdf,
    make_encrypted_pdf,
    make_image_text_pdf,
    make_image_only_pdf,
    make_inconsistent_page_tree_pdf,
    make_malformed_pdf,
    make_many_pages_pdf,
    make_mixed_pdf,
    make_nonzero_origin_image_pdf,
    make_oversized_dimension_pdf,
    make_oversized_pdf,
    make_pdf_at_size,
    make_rotated_pdf,
    make_text_pdf,
    make_truncated_eof_pdf,
    make_string_catalog_type_pdf,
    make_string_pages_type_pdf,
    make_zero_page_pdf,
)
from web_translator.pdf_acquire import MAX_PDF_BYTES
from web_translator.pdf_extract import (
    PdfExtractionError,
    PdfInspection,
    _validate_character_assignment,
    _validate_peer_overlap,
    inspect_pdf,
    reject_unsupported_pdf,
)
from web_translator.pdf_models import PdfBlock, PdfBlockStyle, PdfPageEvidence


def _word(
    text: str,
    *,
    x0: float,
    x1: float,
    top: float,
    bottom: float,
    size: float = 10.0,
    fontname: str = "Helvetica",
) -> dict[str, object]:
    return {
        "text": text,
        "x0": x0,
        "x1": x1,
        "top": top,
        "bottom": bottom,
        "size": size,
        "fontname": fontname,
        "chars": [{"text": character} for character in text],
    }


def _structured_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(612, 792))
    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(72, 720, "Architecture")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(
        72,
        680,
        "This architecture describes a deterministic translation pipeline that MUST",
    )
    canvas.drawString(
        72,
        664,
        "preserve every selectable character and the URL https://example.com/spec.",
    )
    canvas.drawString(72, 630, "1. Acquire and inspect the source document.")
    canvas.drawString(90, 610, "- Extract nested logical content in stable order.")
    canvas.save()
    return path


def _repeated_bands_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(612, 792))
    for page_number in range(1, 4):
        canvas.setFont("Helvetica", 9)
        canvas.drawString(72, 770, "Deterministic Systems Handbook")
        canvas.drawString(72, 25, "Confidential review copy")
        canvas.drawCentredString(306, 10, str(page_number))
        canvas.setFont("Helvetica-Bold", 16)
        canvas.drawString(72, 715, f"Section {page_number}")
        canvas.setFont("Helvetica", 11)
        canvas.drawString(
            72,
            675,
            "Selectable body text remains assigned to exactly one logical text block.",
        )
        canvas.showPage()
    canvas.save()
    return path


def _column_pdf(path: Path, *, columns: int) -> Path:
    canvas = Canvas(str(path), pagesize=(612, 792))
    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(72, 740, "Column Layout")
    canvas.setFont("Helvetica", 10)
    x_positions = [48, 224, 400][:columns]
    for index, x_position in enumerate(x_positions, start=1):
        canvas.drawString(x_position, 700, f"Column {index} first logical sentence.")
        canvas.drawString(x_position, 680, f"Column {index} second logical sentence.")
    canvas.save()
    return path


def _heading_hierarchy_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(612, 792))
    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(72, 720, "1. Architecture")
    canvas.setFont("Helvetica-Bold", 14)
    canvas.drawString(72, 680, "1.1. Layers")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(
        72,
        640,
        "The nested section contains enough selectable body text to validate its exact",
    )
    canvas.drawString(
        72,
        624,
        "heading path while preserving deterministic document and segment ordering.",
    )
    canvas.save()
    return path


def test_group_words_uses_the_sixty_percent_vertical_overlap_boundary() -> None:
    from web_translator.pdf_layout import group_words_into_lines

    at_boundary = group_words_into_lines(
        [
            _word("A", x0=0, x1=5, top=0, bottom=10),
            _word("B", x0=7, x1=12, top=4, bottom=14),
        ]
    )
    below_boundary = group_words_into_lines(
        [
            _word("A", x0=0, x1=5, top=0, bottom=10),
            _word("B", x0=7, x1=12, top=4.01, bottom=14.01),
        ]
    )

    assert [line.text for line in at_boundary] == ["A B"]
    assert [line.text for line in below_boundary] == ["A", "B"]


def test_order_page_lines_uses_an_eighteen_point_gutter_boundary() -> None:
    from web_translator.pdf_layout import group_words_into_lines, order_page_lines

    exact_lines = group_words_into_lines(
        [
            _word("L1", x0=10, x1=90, top=10, bottom=20, size=12),
            _word("R1", x0=108, x1=190, top=10, bottom=20, size=12),
            _word("L2", x0=10, x1=90, top=30, bottom=40, size=12),
            _word("R2", x0=108, x1=190, top=30, bottom=40, size=12),
        ]
    )
    below_lines = group_words_into_lines(
        [
            _word("L1", x0=10, x1=90, top=10, bottom=20),
            _word("R1", x0=107.99, x1=190, top=10, bottom=20),
            _word("L2", x0=10, x1=90, top=30, bottom=40),
            _word("R2", x0=107.99, x1=190, top=30, bottom=40),
        ]
    )

    assert [line.text for line in order_page_lines(exact_lines, 200)] == [
        "L1",
        "L2",
        "R1",
        "R2",
    ]
    assert [line.text for line in order_page_lines(below_lines, 200)] == [
        "L1",
        "R1",
        "L2",
        "R2",
    ]


@pytest.mark.parametrize(
    ("left_bounds", "right_bounds"),
    [
        ((0, 128), (146, 200)),
        ((0, 54), (72, 200)),
    ],
    ids=["seventy-thirty", "thirty-seventy"],
)
def test_order_page_lines_detects_asymmetric_clear_gutters(
    left_bounds: tuple[float, float],
    right_bounds: tuple[float, float],
) -> None:
    from web_translator.pdf_layout import group_words_into_lines, order_page_lines

    lines = group_words_into_lines(
        [
            _word("L1", x0=left_bounds[0], x1=left_bounds[1], top=10, bottom=20),
            _word("R1", x0=right_bounds[0], x1=right_bounds[1], top=10, bottom=20),
            _word("L2", x0=left_bounds[0], x1=left_bounds[1], top=30, bottom=40),
            _word("R2", x0=right_bounds[0], x1=right_bounds[1], top=30, bottom=40),
        ]
    )

    assert [line.text for line in order_page_lines(lines, 200)] == [
        "L1",
        "L2",
        "R1",
        "R2",
    ]


def test_order_page_lines_places_spanning_headings_before_column_regions() -> None:
    from web_translator.pdf_layout import group_words_into_lines, order_page_lines

    lines = group_words_into_lines(
        [
            _word(
                "Heading",
                x0=10,
                x1=190,
                top=0,
                bottom=12,
                size=16,
                fontname="Helvetica-Bold",
            ),
            _word("L1", x0=10, x1=90, top=20, bottom=30),
            _word("R1", x0=110, x1=190, top=20, bottom=30),
            _word("L2", x0=10, x1=90, top=40, bottom=50),
            _word("R2", x0=110, x1=190, top=40, bottom=50),
        ]
    )

    assert [line.text for line in order_page_lines(lines, 200)] == [
        "Heading",
        "L1",
        "L2",
        "R1",
        "R2",
    ]


def test_order_page_lines_sections_columns_around_a_spanning_rich_region() -> None:
    from web_translator.pdf_layout import group_words_into_lines, order_page_lines

    lines = group_words_into_lines(
        [
            _word("L1", x0=10, x1=90, top=10, bottom=20),
            _word("R1", x0=110, x1=190, top=10, bottom=20),
            _word("L2", x0=10, x1=90, top=60, bottom=70),
            _word("R2", x0=110, x1=190, top=60, bottom=70),
        ]
    )

    assert [
        line.text
        for line in order_page_lines(
            lines,
            200,
            spanning_bboxes=[(0.0, 30.0, 200.0, 50.0)],
        )
    ] == ["L1", "R1", "L2", "R2"]


def test_order_page_lines_rejects_crossing_non_heading_evidence() -> None:
    from web_translator.pdf_layout import group_words_into_lines, order_page_lines

    lines = group_words_into_lines(
        [
            _word("L1", x0=10, x1=90, top=10, bottom=20),
            _word("R1", x0=110, x1=190, top=10, bottom=20),
            _word("Crossing prose", x0=10, x1=190, top=25, bottom=35),
            _word("L2", x0=10, x1=90, top=40, bottom=50),
            _word("R2", x0=110, x1=190, top=40, bottom=50),
        ]
    )

    with pytest.raises(PdfExtractionError, match="conflicting column evidence"):
        order_page_lines(lines, 200)


def test_order_page_lines_rejects_ambiguous_three_column_evidence() -> None:
    from web_translator.pdf_layout import group_words_into_lines, order_page_lines

    lines = group_words_into_lines(
        [
            _word("A1", x0=10, x1=50, top=10, bottom=20),
            _word("B1", x0=80, x1=120, top=10, bottom=20),
            _word("C1", x0=150, x1=190, top=10, bottom=20),
            _word("A2", x0=10, x1=50, top=30, bottom=40),
            _word("B2", x0=80, x1=120, top=30, bottom=40),
            _word("C2", x0=150, x1=190, top=30, bottom=40),
        ]
    )

    with pytest.raises(PdfExtractionError, match="ambiguous column evidence"):
        order_page_lines(lines, 200)


@pytest.mark.parametrize(
    "column_counts",
    [(3, 2, 1), (1, 2, 3)],
    ids=["three-two-one", "one-two-three"],
)
def test_order_page_lines_rejects_unbalanced_three_column_evidence(
    column_counts: tuple[int, int, int],
) -> None:
    from web_translator.pdf_layout import group_words_into_lines, order_page_lines

    words = []
    for column, (x0, x1, count) in enumerate(
        zip((10, 80, 150), (50, 120, 190), column_counts, strict=True),
        start=1,
    ):
        words.extend(
            _word(
                f"C{column}-{line_number}",
                x0=x0,
                x1=x1,
                top=10 + (line_number - 1) * 20,
                bottom=20 + (line_number - 1) * 20,
            )
            for line_number in range(1, count + 1)
        )
    lines = group_words_into_lines(words)

    with pytest.raises(PdfExtractionError, match="ambiguous column evidence"):
        order_page_lines(lines, 200)


def test_build_text_blocks_merges_only_contiguous_paragraph_lines() -> None:
    from web_translator.pdf_layout import build_text_blocks, group_words_into_lines

    lines = group_words_into_lines(
        [
            _word("First line", x0=20, x1=100, top=10, bottom=20),
            _word("continues", x0=20, x1=80, top=23, bottom=33),
            _word("- item", x0=20, x1=80, top=38, bottom=48),
        ]
    )

    blocks = build_text_blocks(lines, page_number=1)

    assert [(block.kind, block.source_text) for block in blocks] == [
        ("paragraph", "First line continues"),
        ("list-item", "- item"),
    ]


@pytest.mark.parametrize(
    "marker",
    ["-", "1.", "1.2)", "A.", "A)", "iv)", "‣", "◦", "⁃", "∙"],
)
def test_list_marker_families_share_extraction_classification(marker: str) -> None:
    from web_translator.pdf_layout import build_text_blocks, group_words_into_lines

    source_text = f"{marker} Item"
    lines = group_words_into_lines(
        [_word(source_text, x0=20, x1=100, top=10, bottom=20)]
    )

    blocks = build_text_blocks(lines, page_number=1)

    assert [(block.kind, block.source_text) for block in blocks] == [
        ("list-item", source_text)
    ]


def test_numbered_heading_classification_consumes_style_and_vertical_spacing() -> None:
    from web_translator.pdf_layout import (
        classify_document_lines,
        group_words_into_lines,
    )

    lines = group_words_into_lines(
        [
            _word(
                "1. Architecture",
                x0=20,
                x1=140,
                top=10,
                bottom=26,
                size=16,
                fontname="Helvetica-Bold",
            ),
            _word(
                "1.1. Layers",
                x0=20,
                x1=120,
                top=50,
                bottom=64,
                size=14,
                fontname="Helvetica-Bold",
            ),
            _word(
                "Body text makes the ten point cluster dominant for classification.",
                x0=20,
                x1=180,
                top=88,
                bottom=98,
            ),
            _word(
                "1. Bold ordered item",
                x0=35,
                x1=155,
                top=104,
                bottom=116,
                size=12,
                fontname="Helvetica-Bold",
            ),
            _word(
                "2. Bold ordered item",
                x0=35,
                x1=155,
                top=119,
                bottom=131,
                size=12,
                fontname="Helvetica-Bold",
            ),
            _word(
                "3. Plain ordered item",
                x0=35,
                x1=155,
                top=134,
                bottom=144,
            ),
        ]
    )
    pages = [([line.with_page_geometry(200, 200) for line in lines], 200.0)]

    classified = classify_document_lines(pages)[0]

    assert [(line.kind, line.heading_level) for line in classified[:2]] == [
        ("heading", 1),
        ("heading", 2),
    ]
    assert [line.kind for line in classified[3:]] == [
        "list-item",
        "list-item",
        "list-item",
    ]


@pytest.mark.parametrize("styled_position", ["first", "last"])
def test_styled_ordered_list_edge_uses_tight_same_indent_peer_context(
    styled_position: str,
) -> None:
    from web_translator.pdf_layout import (
        classify_document_lines,
        group_words_into_lines,
    )

    first_style = {
        "size": 12,
        "fontname": "Helvetica-Bold",
    } if styled_position == "first" else {}
    last_style = {
        "size": 12,
        "fontname": "Helvetica-Bold",
    } if styled_position == "last" else {}
    if styled_position == "first":
        first_top, last_top, following_top = 50, 65, 82
    else:
        first_top, last_top, following_top = 50, 63, 110
    lines = group_words_into_lines(
        [
            _word(
                "Surrounding prose makes the ten point body cluster dominant.",
                x0=20,
                x1=180,
                top=10,
                bottom=20,
            ),
            _word(
                "1. First ordered item",
                x0=35,
                x1=155,
                top=first_top,
                bottom=first_top + (12 if styled_position == "first" else 10),
                **first_style,
            ),
            _word(
                "2. Last ordered item",
                x0=35,
                x1=155,
                top=last_top,
                bottom=last_top + (12 if styled_position == "last" else 10),
                **last_style,
            ),
            _word(
                "Following prose stays outside the ordered list sequence.",
                x0=20,
                x1=180,
                top=following_top,
                bottom=following_top + 10,
            ),
        ]
    )
    pages = [([line.with_page_geometry(200, 200) for line in lines], 200.0)]

    classified = classify_document_lines(pages)[0]

    assert [line.kind for line in classified[1:3]] == [
        "list-item",
        "list-item",
    ]


@pytest.mark.parametrize(
    ("styled_position", "list_bounds", "opposite_bounds"),
    [
        ("first", (20, 90), (130, 200)),
        ("last", (20, 90), (130, 200)),
        ("first", (130, 200), (20, 90)),
        ("last", (130, 200), (20, 90)),
    ],
    ids=["left-first", "left-last", "right-first", "right-last"],
)
def test_styled_ordered_list_edge_skips_interleaved_opposite_column_lines(
    styled_position: str,
    list_bounds: tuple[float, float],
    opposite_bounds: tuple[float, float],
) -> None:
    from web_translator.pdf_layout import (
        classify_document_lines,
        group_words_into_lines,
    )

    list_x0, list_x1 = list_bounds
    opposite_x0, opposite_x1 = opposite_bounds
    if styled_position == "first":
        first_top, first_bottom = 50, 62
        opposite_top, opposite_bottom = 63, 73
        last_top, last_bottom = 65, 75
        following_top = 85
    else:
        first_top, first_bottom = 50, 60
        opposite_top, opposite_bottom = 61, 71
        last_top, last_bottom = 63, 75
        following_top = 110
    lines = group_words_into_lines(
        [
            _word(
                "Surrounding prose makes the body font cluster dominant.",
                x0=list_x0,
                x1=list_x1,
                top=10,
                bottom=20,
            ),
            _word(
                "1. First ordered item",
                x0=list_x0,
                x1=list_x1,
                top=first_top,
                bottom=first_bottom,
                size=12 if styled_position == "first" else 10,
                fontname=(
                    "Helvetica-Bold"
                    if styled_position == "first"
                    else "Helvetica"
                ),
            ),
            _word(
                "Opposite column prose interleaves raw line order.",
                x0=opposite_x0,
                x1=opposite_x1,
                top=opposite_top,
                bottom=opposite_bottom,
            ),
            _word(
                "2. Last ordered item",
                x0=list_x0,
                x1=list_x1,
                top=last_top,
                bottom=last_bottom,
                size=12 if styled_position == "last" else 10,
                fontname=(
                    "Helvetica-Bold"
                    if styled_position == "last"
                    else "Helvetica"
                ),
            ),
            _word(
                "Following prose stays outside the ordered list sequence.",
                x0=list_x0,
                x1=list_x1,
                top=following_top,
                bottom=following_top + 10,
            ),
        ]
    )
    pages = [([line.with_page_geometry(220, 200) for line in lines], 200.0)]

    classified = classify_document_lines(pages)[0]
    kinds = {line.text: line.kind for line in classified}

    assert kinds["1. First ordered item"] == "list-item"
    assert kinds["2. Last ordered item"] == "list-item"


def test_distant_same_indent_numbered_line_does_not_suppress_heading() -> None:
    from web_translator.pdf_layout import (
        classify_document_lines,
        group_words_into_lines,
    )

    lines = group_words_into_lines(
        [
            _word(
                "1. Architecture",
                x0=20,
                x1=150,
                top=10,
                bottom=26,
                size=16,
                fontname="Helvetica-Bold",
            ),
            _word(
                "Body prose separates the heading from a later numeric marker.",
                x0=20,
                x1=190,
                top=50,
                bottom=60,
            ),
            _word(
                "2. Later ordered marker",
                x0=20,
                x1=150,
                top=100,
                bottom=110,
            ),
        ]
    )
    pages = [([line.with_page_geometry(220, 200) for line in lines], 200.0)]

    classified = classify_document_lines(pages)[0]

    assert classified[0].kind == "heading"
    assert classified[2].kind == "list-item"


def test_repeated_band_classification_accepts_exactly_sixty_percent() -> None:
    from web_translator.pdf_layout import (
        classify_document_lines,
        group_words_into_lines,
    )

    pages = []
    for page_number in range(1, 6):
        header_text = "Repeated Header" if page_number <= 3 else f"Unique {page_number}"
        lines = group_words_into_lines(
            [
                _word(header_text, x0=20, x1=100, top=5, bottom=15),
                _word(
                    f"Body {page_number}",
                    x0=20,
                    x1=100,
                    top=50,
                    bottom=60,
                ),
            ]
        )
        pages.append(
            ([line.with_page_geometry(200, 200) for line in lines], 200.0)
        )

    classified = classify_document_lines(pages)

    assert [lines[0].kind for lines in classified] == [
        "header",
        "header",
        "header",
        "paragraph",
        "paragraph",
    ]


def test_character_assignment_accepts_exactly_ninety_nine_percent() -> None:
    inspection = PdfInspection(
        page_count=1,
        selectable_characters=100,
        scan_candidate_pages=[],
        pages=[
            PdfPageEvidence(
                number=1,
                width=200,
                height=200,
                rotation=0,
                selectable_characters=100,
                image_coverage=0,
                scan_candidate=False,
            )
        ],
    )

    _validate_character_assignment(inspection, [99])
    with pytest.raises(PdfExtractionError, match="below 99 percent.*unmatched 2"):
        _validate_character_assignment(inspection, [98])


def test_peer_overlap_accepts_exactly_ten_percent_and_rejects_above() -> None:
    style = PdfBlockStyle(10, False, "left", 0, 0)
    left = PdfBlock(
        id="pdf:page-0001:block-0001",
        page_number=1,
        order=0,
        kind="paragraph",
        bbox=(0, 0, 10, 10),
        style=style,
        source_text="left",
    )
    exact = PdfBlock(
        id="pdf:page-0001:block-0002",
        page_number=1,
        order=1,
        kind="paragraph",
        bbox=(9, 0, 19, 10),
        style=style,
        source_text="exact",
    )
    above = PdfBlock(
        id="pdf:page-0001:block-0003",
        page_number=1,
        order=1,
        kind="paragraph",
        bbox=(8.99, 0, 18.99, 10),
        style=style,
        source_text="above",
    )

    _validate_peer_overlap([left, exact])
    with pytest.raises(PdfExtractionError, match="above 10 percent.*block-0001"):
        _validate_peer_overlap([left, above])


def test_extract_pdf_emits_heading_context_and_opaque_locators(tmp_path: Path) -> None:
    from web_translator.pdf_extract import extract_pdf

    source = _structured_pdf(tmp_path / "structured.pdf")

    document = extract_pdf(
        source,
        tmp_path / "document.json",
        tmp_path / "segments.jsonl",
        tmp_path / "media",
    )
    segments = read_segments(tmp_path / "segments.jsonl")

    assert [block.kind for block in document.blocks[:4]] == [
        "heading",
        "paragraph",
        "list-item",
        "list-item",
    ]
    assert [block.order for block in document.blocks] == list(range(len(document.blocks)))
    assert [block.id for block in document.blocks] == [
        f"pdf:page-0001:block-{index:04d}"
        for index in range(1, len(document.blocks) + 1)
    ]
    assert segments[1].heading_path == ["Architecture"]
    assert segments[1].locator == "pdf:page-0001:block-0002"
    assert document.blocks[3].style.indentation > document.blocks[2].style.indentation
    assert [segment.id for segment in segments] == [
        f"seg-{index:06d}" for index in range(1, len(segments) + 1)
    ]
    assert segments[1].protected
    assert sum(
        len(character)
        for block in document.blocks
        for character in block.source_text
        if not character.isspace()
    ) == document.selectable_characters
    assert (tmp_path / "media").is_dir()


def test_extract_pdf_classifies_repeated_bands_and_sequential_page_numbers(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_extract import extract_pdf

    document = extract_pdf(
        _repeated_bands_pdf(tmp_path / "bands.pdf"),
        tmp_path / "document.json",
        tmp_path / "segments.jsonl",
        tmp_path / "media",
    )

    kinds = [block.kind for block in document.blocks]
    assert kinds.count("header") == 3
    assert kinds.count("footer") == 3
    assert kinds.count("page-number") == 3
    segments = read_segments(tmp_path / "segments.jsonl")
    assert all(
        segment.semantic_type
        not in {"header", "footer", "page-number"}
        for segment in segments
    )
    assert len(segments) == 6


def test_extract_pdf_builds_numbered_heading_hierarchy(tmp_path: Path) -> None:
    from web_translator.pdf_extract import extract_pdf

    extract_pdf(
        _heading_hierarchy_pdf(tmp_path / "hierarchy.pdf"),
        tmp_path / "document.json",
        tmp_path / "segments.jsonl",
        tmp_path / "media",
    )

    segments = read_segments(tmp_path / "segments.jsonl")
    assert [segment.semantic_type for segment in segments] == [
        "heading",
        "heading",
        "paragraph",
    ]
    assert segments[2].heading_path == ["1. Architecture", "1.1. Layers"]


def test_extract_pdf_orders_clear_columns_and_rejects_ambiguous_columns(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_extract import extract_pdf

    clear = extract_pdf(
        _column_pdf(tmp_path / "two-columns.pdf", columns=2),
        tmp_path / "clear-document.json",
        tmp_path / "clear-segments.jsonl",
        tmp_path / "clear-media",
    )

    assert [
        block.source_text.partition(" ")[0]
        for block in clear.blocks
        if block.kind == "paragraph" and block.source_text.startswith("Column")
    ] == ["Column", "Column"]
    assert "Column 1 first" in clear.blocks[1].source_text
    assert "Column 2 first" in clear.blocks[2].source_text

    with pytest.raises(PdfExtractionError, match="ambiguous column evidence"):
        extract_pdf(
            _column_pdf(tmp_path / "three-columns.pdf", columns=3),
            tmp_path / "ambiguous-document.json",
            tmp_path / "ambiguous-segments.jsonl",
            tmp_path / "ambiguous-media",
        )


def test_inspect_pdf_records_selectable_text_and_page_evidence(tmp_path: Path) -> None:
    source = make_text_pdf(tmp_path / "text.pdf")

    inspection = inspect_pdf(source)

    assert inspection.page_count == 1
    assert inspection.selectable_characters >= 100
    assert inspection.scan_candidate_pages == []
    assert inspection.pages[0].number == 1
    assert inspection.pages[0].rotation == 0
    assert inspection.pages[0].image_coverage == 0.0


def test_scan_rule_allows_one_image_cover_but_rejects_image_dominant_document(
    tmp_path: Path,
) -> None:
    allowed = make_mixed_pdf(tmp_path / "allowed.pdf", scanned_pages=1, text_pages=4)
    rejected = make_mixed_pdf(tmp_path / "rejected.pdf", scanned_pages=2, text_pages=3)

    assert inspect_pdf(allowed).scan_candidate_pages == [1]
    reject_unsupported_pdf(inspect_pdf(allowed))
    with pytest.raises(PdfExtractionError, match="scanned PDF.*pages 1, 2"):
        reject_unsupported_pdf(inspect_pdf(rejected))


def test_rejects_documents_with_too_little_selectable_text(tmp_path: Path) -> None:
    inspection = inspect_pdf(make_image_only_pdf(tmp_path / "image-only.pdf"))

    with pytest.raises(
        PdfExtractionError,
        match=r"selectable characters 0.*page 1: characters 0, coverage 1\.000",
    ):
        reject_unsupported_pdf(inspection)


@pytest.mark.parametrize(
    ("builder", "message"),
    [
        (make_encrypted_pdf, "encrypted"),
        (make_malformed_pdf, "final %%EOF"),
        (make_zero_page_pdf, "zero pages"),
        (make_many_pages_pdf, "page count 101"),
        (make_oversized_dimension_pdf, "unsupported dimensions"),
        (make_oversized_pdf, "size limit"),
        (make_truncated_eof_pdf, "final %%EOF"),
        (make_inconsistent_page_tree_pdf, "page tree count"),
        (make_string_catalog_type_pdf, "catalog has an unsupported type"),
        (make_string_pages_type_pdf, "page tree node has an unsupported type"),
    ],
)
def test_inspect_pdf_rejects_unsupported_document_structure(
    tmp_path: Path,
    builder: object,
    message: str,
) -> None:
    source = builder(tmp_path / "unsupported.pdf")

    with pytest.raises(PdfExtractionError, match=message):
        inspect_pdf(source)


def test_inspect_pdf_normalizes_supported_rotation(tmp_path: Path) -> None:
    inspection = inspect_pdf(make_rotated_pdf(tmp_path / "rotated.pdf", rotation=450))

    assert inspection.pages[0].rotation == 90


def test_inspect_pdf_rejects_rotation_not_divisible_by_ninety(tmp_path: Path) -> None:
    source = make_rotated_pdf(tmp_path / "unsupported-rotation.pdf", rotation=45)

    with pytest.raises(PdfExtractionError, match="unsupported rotation"):
        inspect_pdf(source)


@pytest.mark.parametrize("page_count", [0, 101])
def test_reject_unsupported_pdf_enforces_page_count(page_count: int) -> None:
    inspection = PdfInspection(
        page_count=page_count,
        selectable_characters=100,
        scan_candidate_pages=[],
        pages=[],
    )

    with pytest.raises(PdfExtractionError, match="page count"):
        reject_unsupported_pdf(inspection)


def test_inspect_pdf_accepts_source_at_exact_size_limit(tmp_path: Path) -> None:
    source = make_pdf_at_size(tmp_path / "at-limit.pdf", MAX_PDF_BYTES)

    assert inspect_pdf(source).page_count == 1


@pytest.mark.parametrize(
    ("width", "height"),
    [(36, 792), (612, 14_400)],
)
def test_inspect_pdf_accepts_exact_dimension_limits(
    tmp_path: Path,
    width: float,
    height: float,
) -> None:
    inspection = inspect_pdf(
        make_dimension_pdf(tmp_path / "at-dimension-limit.pdf", width=width, height=height)
    )

    assert inspection.pages[0].width == width
    assert inspection.pages[0].height == height


@pytest.mark.parametrize(
    ("width", "height"),
    [(35, 792), (612, 14_401)],
)
def test_inspect_pdf_rejects_dimensions_outside_limits(
    tmp_path: Path,
    width: float,
    height: float,
) -> None:
    source = make_dimension_pdf(tmp_path / "outside-dimension-limit.pdf", width=width, height=height)

    with pytest.raises(PdfExtractionError, match="unsupported dimensions"):
        inspect_pdf(source)


def test_inspect_pdf_accepts_exact_page_count_limit(tmp_path: Path) -> None:
    inspection = inspect_pdf(make_many_pages_pdf(tmp_path / "hundred-pages.pdf", pages=100))

    assert inspection.page_count == 100


def test_scan_candidate_character_and_coverage_cutoffs(tmp_path: Path) -> None:
    candidate = inspect_pdf(
        make_image_text_pdf(tmp_path / "candidate.pdf", characters=19, image_width=306)
    )
    text_boundary = inspect_pdf(
        make_image_text_pdf(tmp_path / "text-boundary.pdf", characters=20, image_width=612)
    )
    coverage_below = inspect_pdf(
        make_image_text_pdf(tmp_path / "coverage-below.pdf", characters=19, image_width=305)
    )

    assert candidate.pages[0].selectable_characters == 19
    assert candidate.pages[0].image_coverage == 0.5
    assert candidate.scan_candidate_pages == [1]
    assert text_boundary.pages[0].selectable_characters == 20
    assert text_boundary.scan_candidate_pages == []
    assert coverage_below.pages[0].selectable_characters == 19
    assert coverage_below.pages[0].image_coverage < 0.5
    assert coverage_below.scan_candidate_pages == []


def test_scan_rejection_allows_exact_candidate_threshold(tmp_path: Path) -> None:
    allowed = make_mixed_pdf(tmp_path / "threshold-allowed.pdf", scanned_pages=2, text_pages=8)
    rejected = make_mixed_pdf(tmp_path / "threshold-rejected.pdf", scanned_pages=3, text_pages=7)

    reject_unsupported_pdf(inspect_pdf(allowed))
    with pytest.raises(PdfExtractionError, match="candidate pages 1, 2, 3 exceed 2"):
        reject_unsupported_pdf(inspect_pdf(rejected))


def test_reject_unsupported_pdf_accepts_exact_total_character_limit(tmp_path: Path) -> None:
    inspection = inspect_pdf(
        make_dimension_pdf(tmp_path / "hundred-chars.pdf", width=612, height=792)
    )

    assert inspection.selectable_characters == 100
    reject_unsupported_pdf(inspection)


def test_inspect_pdf_clips_off_page_image_coverage(tmp_path: Path) -> None:
    inspection = inspect_pdf(
        make_image_text_pdf(tmp_path / "off-page.pdf", characters=19, image_x=-306, image_width=918)
    )

    assert inspection.pages[0].image_coverage == 1.0


def test_inspect_pdf_clips_against_nonzero_page_bbox(tmp_path: Path) -> None:
    inspection = inspect_pdf(make_nonzero_origin_image_pdf(tmp_path / "nonzero-origin.pdf"))

    assert inspection.pages[0].image_coverage == 0.5
    assert inspection.scan_candidate_pages == [1]


def _ruled_table_pdf(path: Path, *, crossing_border: bool = False) -> Path:
    canvas = Canvas(str(path), pagesize=(400, 400))
    for x in (50, 150, 250):
        canvas.line(x, 200, x, 300)
    canvas.line(50, 300, 250, 300)
    canvas.line(50, 200, 250, 200)
    if crossing_border:
        canvas.line(50, 250, 250, 250)
        canvas.drawCentredString(150, 270, "W")
        canvas.drawString(60, 220, "A2")
        canvas.drawString(160, 220, "B2")
    else:
        canvas.line(150, 250, 250, 250)
        canvas.drawString(60, 270, "Merged")
        canvas.drawString(160, 270, "B1")
        # The lower-right cell is deliberately empty.
    canvas.save()
    return path


def _text_aligned_table_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(400, 400))
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(50, 300, "Name")
    canvas.drawString(180, 300, "Value")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(50, 280, "Alpha")
    canvas.drawString(180, 280, "10")
    canvas.drawString(50, 260, "Beta")
    canvas.drawString(180, 260, "20")
    canvas.save()
    return path


def _sparse_text_aligned_table_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(400, 400))
    rows = [
        ("Name", "Value", "Unit"),
        ("Alpha", "10", "kg"),
        ("Beta", "", "m"),
    ]
    for row, y in zip(rows, (300, 280, 260), strict=True):
        for value, x in zip(row, (50, 180, 280), strict=True):
            if value:
                canvas.drawString(x, y, value)
    canvas.save()
    return path


def _mixed_table_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(500, 500))
    for x in (300, 375, 450):
        canvas.line(x, 330, x, 430)
    for y in (330, 380, 430):
        canvas.line(300, y, 450, y)
    canvas.drawString(310, 400, "R1")
    canvas.drawString(385, 400, "R2")
    canvas.drawString(310, 350, "R3")
    canvas.drawString(385, 350, "R4")
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(50, 260, "Name")
    canvas.drawString(160, 260, "Value")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(50, 240, "Alpha")
    canvas.drawString(160, 240, "10")
    canvas.drawString(50, 220, "Beta")
    canvas.drawString(160, 220, "20")
    canvas.save()
    return path


def _column_rich_order_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(400, 400))
    canvas.setFont("Helvetica", 10)
    canvas.drawString(30, 340, "Left above the spanning table region")
    canvas.drawString(245, 340, "Right above the table")
    canvas.rect(30, 170, 340, 60, stroke=1, fill=0)
    canvas.line(200, 170, 200, 230)
    canvas.line(30, 200, 370, 200)
    canvas.drawString(40, 210, "A1")
    canvas.drawString(210, 210, "B1")
    canvas.drawString(40, 180, "A2")
    canvas.drawString(210, 180, "B2")
    canvas.drawString(30, 100, "Left below the spanning table region")
    canvas.drawString(245, 100, "Right below the table")
    canvas.save()
    return path


def _rich_layout_pdf(path: Path) -> Path:
    image_path = path.with_suffix(".png")
    Image.new("RGB", (100, 80), (220, 50, 40)).save(image_path)
    canvas = Canvas(str(path), pagesize=(612, 792))
    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(72, 750, "Rich Layout Evidence")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(
        72,
        724,
        "This document contains deterministic selectable content for complete extraction.",
    )
    canvas.drawString(
        72,
        708,
        "Every native table cell, caption, footnote, figure, and link remains evidenced.",
    )

    # A ruled table with one two-column merged header.
    canvas.rect(72, 570, 300, 100, stroke=1, fill=0)
    canvas.line(72, 620, 372, 620)
    canvas.line(222, 570, 222, 620)
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(82, 642, "Merged table heading")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(82, 592, "Left value")

    canvas.drawImage(str(image_path), 72, 390, width=100, height=80)
    canvas.setFont("Helvetica", 9)
    canvas.drawString(72, 374, "Figure 1. Source raster image")

    # Connected vector chart without a closed table grid.
    canvas.line(260, 390, 260, 470)
    canvas.line(260, 390, 410, 390)
    canvas.line(260, 405, 305, 430)
    canvas.line(305, 430, 355, 415)
    canvas.line(355, 415, 410, 455)
    canvas.drawString(260, 374, "Figure 2. Source vector chart")

    canvas.setFont("Helvetica", 11)
    canvas.drawString(72, 330, "External specification link")
    canvas.linkURL("https://example.com/spec", (72, 328, 205, 342), relative=0)
    canvas.drawString(72, 306, "Continue at the internal destination")
    canvas.linkRect("", "internal-target", (72, 304, 245, 318), relative=0)
    canvas.drawString(72, 270, "The deterministic workflow includes a page-local note")
    canvas.setFont("Helvetica", 7)
    canvas.drawString(335, 275, "1")
    canvas.drawString(72, 40, "1 Footnote evidence remains linked to its marker.")

    canvas.showPage()
    canvas.bookmarkPage("internal-target", fit="XYZ", left=100, top=725, zoom=0)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(72, 720, "Internal Destination")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(
        72,
        690,
        "This known emitted block is the unambiguous target of the source navigation link.",
    )
    canvas.save()
    return path


def _ambiguous_caption_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(612, 792))
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(72, 730, "Ambiguous Caption Evidence")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(
        72,
        700,
        "Selectable prose keeps this fixture above the supported text threshold while",
    )
    canvas.drawString(
        72,
        684,
        "two explicit captions compete for one connected graphical region below.",
    )
    canvas.line(72, 520, 72, 620)
    canvas.line(72, 520, 300, 520)
    canvas.line(72, 540, 150, 585)
    canvas.line(150, 585, 230, 550)
    canvas.line(230, 550, 300, 610)
    canvas.setFont("Helvetica", 9)
    canvas.drawString(72, 502, "Figure 1. First possible caption")
    canvas.drawString(90, 484, "Figure 2. Second possible caption")
    canvas.save()
    return path


def _ambiguous_link_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(400, 400))
    canvas.drawString(50, 300, "First visible owner")
    canvas.drawString(50, 280, "Second visible owner")
    canvas.linkURL("https://example.com/ambiguous", (50, 278, 180, 312), relative=0)
    canvas.save()
    return path


def _orphan_visible_link_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(400, 400))
    canvas.drawString(50, 300, "Visible orphan link")
    canvas.linkURL("https://example.com/orphan", (50, 298, 150, 312), relative=0)
    canvas.save()
    return path


def _nonzero_origin_link_pdf(path: Path, *, rotation: int = 0) -> Path:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, NameObject, NumberObject

    canvas = Canvas(str(path), pagesize=(200, 100))
    canvas.drawString(0, 50, "Target link")
    canvas.linkURL("https://example.com/nonzero", (0, 48, 60, 62), relative=0)
    canvas.save()
    reader = PdfReader(path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    if rotation:
        writer.pages[0].rotate(rotation)
    bounds = ArrayObject(
        [NumberObject(-50), NumberObject(-25), NumberObject(150), NumberObject(75)]
    )
    writer.pages[0][NameObject("/MediaBox")] = bounds
    writer.pages[0][NameObject("/CropBox")] = ArrayObject(bounds)
    with path.open("wb") as destination:
        writer.write(destination)
    return path


def _nonzero_rich_coordinates_pdf(path: Path, *, rotation: int = 0) -> Path:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, NameObject, NumberObject

    image_path = path.with_suffix(".png")
    Image.new("RGB", (140, 80), (30, 120, 210)).save(image_path)
    canvas = Canvas(str(path), pagesize=(400, 500))
    canvas.setFont("Helvetica", 11)
    canvas.drawString(-60, 370, "Introductory prose before the rich regions remains ordered.")
    canvas.linkURL(
        "https://example.com/rich",
        (-60, 368, 250, 382),
        relative=0,
    )
    canvas.bookmarkPage("rich-target", fit="XYZ", left=-60, top=370, zoom=0)

    canvas.rect(-60, 220, 140, 80, stroke=1, fill=0)
    canvas.line(10, 220, 10, 300)
    canvas.line(-60, 260, 80, 260)
    canvas.drawString(-50, 275, "A1")
    canvas.drawString(20, 275, "B1")
    canvas.drawString(-50, 235, "A2")
    canvas.drawString(20, 235, "B2")

    canvas.drawImage(str(image_path), 120, 220, width=140, height=80)
    canvas.setFillColorRGB(1, 1, 1)
    canvas.drawString(140, 255, "Embedded figure label")
    canvas.setFillColorRGB(0, 0, 0)
    canvas.setFont("Helvetica", 9)
    canvas.drawString(120, 205, "Figure 1. Nonzero source figure")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(-60, 160, "Following prose after the rich regions remains ordered.")
    canvas.linkRect("", "rich-target", (-60, 158, 240, 172), relative=0)
    canvas.save()

    reader = PdfReader(path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    page = writer.pages[0]
    if rotation:
        page.rotate(rotation)
    bounds = ArrayObject(
        [NumberObject(-100), NumberObject(-100), NumberObject(300), NumberObject(400)]
    )
    page[NameObject("/MediaBox")] = bounds
    page[NameObject("/CropBox")] = ArrayObject(bounds)
    writer.add_named_destination("rich-named-target", 0)
    with path.open("wb") as destination:
        writer.write(destination)
    return path


def test_upright_extraction_staging_preserves_pdf_evidence_and_always_cleans(
    tmp_path: Path,
) -> None:
    from pypdf import PdfReader

    from web_translator import pdf_extract

    normalize = getattr(pdf_extract, "_upright_extraction_source", None)
    assert callable(normalize), "rotated extraction requires isolated upright staging"
    source = _nonzero_rich_coordinates_pdf(
        tmp_path / "staging-source.pdf",
        rotation=90,
    )
    original_bytes = source.read_bytes()
    staging_parent = tmp_path / "run-owned-staging"

    with normalize(source, staging_parent) as staged:
        staging_root = staged.parent
        assert staging_root.parent == staging_parent
        assert staged.parent != source.parent
        assert sorted(staging_root.iterdir()) == [staged]
        source_reader = PdfReader(source, strict=True)
        staged_reader = PdfReader(staged, strict=True)
        source_page = source_reader.pages[0]
        staged_page = staged_reader.pages[0]

        assert int(source_page.get("/Rotate", 0)) == 90
        assert int(staged_page.get("/Rotate", 0)) == 0
        for box_name in ("mediabox", "cropbox"):
            assert list(getattr(staged_page, box_name)) == list(
                getattr(source_page, box_name)
            )
        assert staged_page.get_contents().get_data() == source_page.get_contents().get_data()
        source_resources = source_page["/Resources"].get_object()
        staged_resources = staged_page["/Resources"].get_object()
        assert set(staged_resources) == set(source_resources)
        source_xobjects = source_resources["/XObject"].get_object()
        staged_xobjects = staged_resources["/XObject"].get_object()
        assert set(staged_xobjects) == set(source_xobjects)
        for name in source_xobjects:
            assert staged_xobjects[name].get_object().get_data() == (
                source_xobjects[name].get_object().get_data()
            )
        source_annotations = [
            annotation.get_object() for annotation in source_page.get("/Annots", ())
        ]
        staged_annotations = [
            annotation.get_object() for annotation in staged_page.get("/Annots", ())
        ]
        assert [list(annotation["/Rect"]) for annotation in staged_annotations] == [
            list(annotation["/Rect"]) for annotation in source_annotations
        ]
        assert [
            tuple(str(value) for value in annotation.get("/Dest", ())[1:])
            for annotation in staged_annotations
        ] == [
            tuple(str(value) for value in annotation.get("/Dest", ())[1:])
            for annotation in source_annotations
        ]
        assert set(staged_reader.named_destinations) == set(
            source_reader.named_destinations
        )

    assert not staging_root.exists()
    assert not staging_parent.exists()
    assert source.read_bytes() == original_bytes

    with pytest.raises(KeyboardInterrupt):
        with normalize(source, staging_parent) as staged:
            interrupted_root = staged.parent
            raise KeyboardInterrupt
    assert not interrupted_root.exists()
    assert not staging_parent.exists()
    assert source.read_bytes() == original_bytes


def test_detect_tables_preserves_merged_spans_and_empty_structural_cells(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_layout import detect_tables

    with pdfplumber.open(_ruled_table_pdf(tmp_path / "merged.pdf")) as document:
        result = detect_tables(document.pages[0], page_number=1)

    assert [
        (block.row, block.column, block.row_span, block.column_span, block.source_text)
        for block in result.blocks
    ] == [
        (0, 0, 2, 1, "Merged"),
        (0, 1, 1, 1, "B1"),
        (1, 1, 1, 1, ""),
    ]
    assert [cell.block_id for cell in result.cells] == [
        block.id for block in result.blocks
    ]


def test_detect_tables_falls_back_to_aligned_text_without_synthetic_blank_rows(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_layout import detect_tables

    with pdfplumber.open(_text_aligned_table_pdf(tmp_path / "aligned.pdf")) as document:
        result = detect_tables(document.pages[0], page_number=1)

    assert [(block.row, block.column, block.source_text) for block in result.blocks] == [
        (0, 0, "Name"),
        (0, 1, "Value"),
        (1, 0, "Alpha"),
        (1, 1, "10"),
        (2, 0, "Beta"),
        (2, 1, "20"),
    ]
    assert [cell.is_header for cell in result.cells] == [True, True, False, False, False, False]


def test_detect_tables_preserves_sparse_aligned_text_cells(tmp_path: Path) -> None:
    from web_translator.pdf_layout import detect_tables

    with pdfplumber.open(
        _sparse_text_aligned_table_pdf(tmp_path / "sparse-aligned.pdf")
    ) as document:
        result = detect_tables(document.pages[0], page_number=1)

    assert [
        (block.row, block.column, block.source_text) for block in result.blocks
    ] == [
        (0, 0, "Name"),
        (0, 1, "Value"),
        (0, 2, "Unit"),
        (1, 0, "Alpha"),
        (1, 1, "10"),
        (1, 2, "kg"),
        (2, 0, "Beta"),
        (2, 1, ""),
        (2, 2, "m"),
    ]


def test_detect_tables_keeps_nonoverlapping_text_table_after_ruled_table(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_layout import detect_tables

    with pdfplumber.open(_mixed_table_pdf(tmp_path / "mixed-tables.pdf")) as document:
        result = detect_tables(document.pages[0], page_number=1)

    assert [cell.table_id for cell in result.cells] == (
        ["pdf:page-0001:table-0001"] * 4
        + ["pdf:page-0001:table-0002"] * 6
    )


def test_extract_pdf_orders_both_columns_around_a_full_width_table(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_extract import extract_pdf

    document = extract_pdf(
        _column_rich_order_pdf(tmp_path / "column-rich-order.pdf"),
        tmp_path / "column-rich-document.json",
        tmp_path / "column-rich-segments.jsonl",
        tmp_path / "column-rich-media",
    )
    positions = {
        label: next(
            block.order for block in document.blocks if label in block.source_text
        )
        for label in ("Left above", "Right above", "A1", "Left below", "Right below")
    }

    assert positions["Left above"] < positions["Right above"] < positions["A1"]
    assert positions["A1"] < positions["Left below"] < positions["Right below"]


def test_detect_tables_rejects_character_crossing_an_internal_border(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_layout import detect_tables

    with pdfplumber.open(
        _ruled_table_pdf(tmp_path / "ambiguous.pdf", crossing_border=True)
    ) as document:
        with pytest.raises(PdfExtractionError, match="ambiguous table character ownership"):
            detect_tables(document.pages[0], page_number=1)


def test_extract_pdf_emits_figures_captions_footnotes_tables_and_links(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_extract import extract_pdf

    document = extract_pdf(
        _rich_layout_pdf(tmp_path / "rich.pdf"),
        tmp_path / "document.json",
        tmp_path / "segments.jsonl",
        tmp_path / "media",
    )

    assert [
        (cell.row, cell.column, cell.row_span, cell.column_span)
        for cell in document.table_cells
    ] == [(0, 0, 1, 2), (1, 0, 1, 1), (1, 1, 1, 1)]
    figures = [block for block in document.blocks if block.kind == "figure"]
    captions = [block for block in document.blocks if block.kind == "caption"]
    assert [block.media_path for block in figures] == [
        "media/figure-0001.png",
        "media/figure-0002.png",
    ]
    assert [block.caption_id for block in figures] == [block.id for block in captions]
    assert [block.caption_id for block in captions] == [block.id for block in figures]
    assert [(Image.open(tmp_path / str(block.media_path))).size for block in figures] == [
        (200, 160),
        (300, 160),
    ]

    footnote = next(block for block in document.blocks if block.kind == "footnote")
    marker_owner = next(
        block for block in document.blocks if "page-local note" in block.source_text
    )
    assert marker_owner.destination == footnote.id
    external = next(
        block for block in document.blocks if "External specification link" in block.source_text
    )
    internal = next(
        block
        for block in document.blocks
        if "Continue at the internal destination" in block.source_text
    )
    target = next(
        block for block in document.blocks if block.source_text == "Internal Destination"
    )
    assert external.uri == "https://example.com/spec"
    assert internal.destination == target.id

    segments = read_segments(tmp_path / "segments.jsonl")
    empty_cell = next(
        block
        for block in document.blocks
        if block.kind == "table-cell" and not block.source_text
    )
    assert empty_cell.segment_id is None
    assert empty_cell.id not in {segment.locator for segment in segments}
    assert {"table-cell", "caption", "footnote"} <= {
        segment.semantic_type for segment in segments
    }


def test_extract_pdf_rejects_ambiguous_figure_caption_pairing(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_extract import extract_pdf

    with pytest.raises(PdfExtractionError, match="ambiguous figure-caption pairing"):
        extract_pdf(
            _ambiguous_caption_pdf(tmp_path / "ambiguous-caption.pdf"),
            tmp_path / "document.json",
            tmp_path / "segments.jsonl",
            tmp_path / "media",
        )


def test_detect_footnotes_rejects_two_bodies_claiming_one_marker() -> None:
    from web_translator.pdf_layout import detect_footnotes

    normal = PdfBlockStyle(10.0, False, "left", 50.0, 4.0)
    small = PdfBlockStyle(7.0, False, "left", 50.0, 2.0)
    owner = PdfBlock(
        id="pdf:page-0001:block-0001",
        page_number=1,
        order=0,
        kind="paragraph",
        bbox=(50.0, 100.0, 220.0, 112.0),
        style=normal,
        source_text="One marker has one footnote body",
    )
    context = PdfBlock(
        id="pdf:page-0001:block-0002",
        page_number=1,
        order=1,
        kind="paragraph",
        bbox=(50.0, 140.0, 220.0, 152.0),
        style=normal,
        source_text="Additional body-sized context",
    )
    bodies = [
        PdfBlock(
            id=f"pdf:page-0001:block-{index:04d}",
            page_number=1,
            order=index - 1,
            kind="paragraph",
            bbox=(50.0, top, 250.0, top + 9.0),
            style=small,
            source_text=text,
        )
        for index, top, text in (
            (3, 650.0, "1 First footnote body"),
            (4, 670.0, "1 Duplicate footnote body"),
        )
    ]
    marker = {
        "text": "1",
        "x0": 205.0,
        "x1": 209.0,
        "top": 100.0,
        "bottom": 106.0,
        "size": 7.0,
    }

    with pytest.raises(PdfExtractionError, match="ambiguous footnote"):
        detect_footnotes([owner, context, *bodies], [marker], page_height=792.0)


@pytest.mark.parametrize(
    "destination_kind",
    ["FitH", "FitV", "FitR", "Destination"],
)
def test_internal_destination_uses_available_coordinates(
    tmp_path: Path,
    destination_kind: str,
) -> None:
    from pypdf import PdfReader
    from pypdf.generic import (
        ArrayObject,
        Destination,
        Fit,
        FloatObject,
        NameObject,
    )

    from web_translator.pdf_layout import _map_internal_destination

    reader = PdfReader(
        make_dimension_pdf(
            tmp_path / f"destination-{destination_kind}.pdf",
            width=400,
            height=400,
        )
    )
    page_reference = reader.pages[0].indirect_reference
    if destination_kind == "FitH":
        destination = ArrayObject(
            [page_reference, NameObject("/FitH"), FloatObject(300)]
        )
    elif destination_kind == "FitV":
        destination = ArrayObject(
            [page_reference, NameObject("/FitV"), FloatObject(250)]
        )
    elif destination_kind == "FitR":
        destination = ArrayObject(
            [
                page_reference,
                NameObject("/FitR"),
                FloatObject(200),
                FloatObject(280),
                FloatObject(300),
                FloatObject(320),
            ]
        )
    else:
        destination = Destination(
            "coordinate-target",
            page_reference,
            Fit.xyz(left=250, top=300),
        )
    style = PdfBlockStyle(10.0, False, "left", 20.0, 2.0)
    decoy = PdfBlock(
        id="pdf:page-0001:block-0001",
        page_number=1,
        order=0,
        kind="paragraph",
        bbox=(20.0, 250.0, 120.0, 280.0),
        style=style,
        source_text="Earlier logical block",
    )
    target = PdfBlock(
        id="pdf:page-0001:block-0002",
        page_number=1,
        order=1,
        kind="heading",
        bbox=(200.0, 80.0, 300.0, 120.0),
        style=style,
        source_text="Coordinate destination",
    )

    assert _map_internal_destination(
        reader,
        destination,
        {1: [decoy, target]},
    ) == target


def test_link_coordinates_match_emitted_blocks_with_nonzero_page_origin(
    tmp_path: Path,
) -> None:
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, FloatObject, NameObject

    from web_translator.pdf_layout import (
        _map_internal_destination,
        extract_link_evidence,
    )

    source = _nonzero_origin_link_pdf(tmp_path / "nonzero-link.pdf")
    target = PdfBlock(
        id="pdf:page-0001:block-0001",
        page_number=1,
        order=0,
        kind="paragraph",
        bbox=(0.0, 40.0, 60.0, 53.0),
        style=PdfBlockStyle(12.0, False, "left", 0.0, 0.0),
        source_text="Target link",
    )
    evidence = extract_link_evidence(source, [target])
    reader = PdfReader(source)
    destination = ArrayObject(
        [
            reader.pages[0].indirect_reference,
            NameObject("/XYZ"),
            FloatObject(0),
            FloatObject(60),
            FloatObject(0),
        ]
    )

    assert evidence.blocks[0].uri == "https://example.com/nonzero"
    assert _map_internal_destination(reader, destination, {1: [target]}) == target


def test_extract_pdf_keeps_one_semantic_coordinate_system_for_nonzero_rich_page(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_extract import extract_pdf

    document = extract_pdf(
        _nonzero_rich_coordinates_pdf(tmp_path / "nonzero-rich.pdf"),
        tmp_path / "nonzero-rich-document.json",
        tmp_path / "nonzero-rich-segments.jsonl",
        tmp_path / "nonzero-rich-media",
    )
    figures = [block for block in document.blocks if block.kind == "figure"]
    captions = [block for block in document.blocks if block.kind == "caption"]
    segments = read_segments(tmp_path / "nonzero-rich-segments.jsonl")

    assert len(document.table_cells) == 4
    assert len(figures) == 1
    assert len(captions) == 1
    assert figures[0].caption_id == captions[0].id
    assert captions[0].caption_id == figures[0].id
    assert all("Embedded figure label" not in segment.source_text for segment in segments)
    positions = {
        label: next(
            block.order for block in document.blocks if label in block.source_text
        )
        for label in ("Introductory prose", "A1", "Figure 1.", "Following prose")
    }
    assert positions["Introductory prose"] < positions["A1"]
    assert positions["A1"] < positions["Following prose"]
    assert positions["Figure 1."] < positions["Following prose"]


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_extract_pdf_normalizes_rotated_nonzero_rich_page_end_to_end(
    tmp_path: Path,
    rotation: int,
) -> None:
    from web_translator.pdf_extract import extract_pdf

    source = _nonzero_rich_coordinates_pdf(
        tmp_path / f"nonzero-rich-{rotation}.pdf",
        rotation=rotation,
    )
    document = extract_pdf(
        source,
        tmp_path / f"nonzero-rich-{rotation}-document.json",
        tmp_path / f"nonzero-rich-{rotation}-segments.jsonl",
        tmp_path / f"nonzero-rich-{rotation}-media",
    )
    segments = read_segments(
        tmp_path / f"nonzero-rich-{rotation}-segments.jsonl"
    )
    table_blocks = sorted(
        (block for block in document.blocks if block.kind == "table-cell"),
        key=lambda block: (block.row or 0, block.column or 0),
    )
    figures = [block for block in document.blocks if block.kind == "figure"]
    captions = [block for block in document.blocks if block.kind == "caption"]

    assert [
        (block.row, block.column, block.source_text) for block in table_blocks
    ] == [
        (0, 0, "A1"),
        (0, 1, "B1"),
        (1, 0, "A2"),
        (1, 1, "B2"),
    ]
    assert [(cell.row, cell.column) for cell in document.table_cells] == [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
    ]
    assert len(figures) == 1
    assert len(captions) == 1
    assert captions[0].source_text == "Figure 1. Nonzero source figure"
    assert figures[0].caption_id == captions[0].id
    assert captions[0].caption_id == figures[0].id
    assert all("Embedded figure label" not in segment.source_text for segment in segments)
    assert document.pages[0].width == 400.0
    assert document.pages[0].height == 500.0
    assert document.pages[0].rotation == rotation

    expected_text = (
        "Introductory prose before the rich regions remains ordered.",
        "Following prose after the rich regions remains ordered.",
    )
    prose = [
        block for block in document.blocks if block.source_text in expected_text
    ]
    assert [block.source_text for block in prose] == list(expected_text)
    assert prose[0].uri == "https://example.com/rich"
    assert prose[1].destination == prose[0].id
    rich_orders = [
        *(block.order for block in table_blocks),
        figures[0].order,
        captions[0].order,
    ]
    assert prose[0].order < min(rich_orders)
    assert max(rich_orders) < prose[1].order

    assigned_characters = sum(
        sum(1 for character in block.source_text if not character.isspace())
        for block in document.blocks
    ) + len("Embeddedfigurelabel")
    assert assigned_characters / document.selectable_characters >= 0.99

    expected_crop_size = {
        90: (160, 280),
        180: (280, 160),
        270: (160, 280),
    }
    with Image.open(
        tmp_path / f"nonzero-rich-{rotation}-media" / "figure-0001.png"
    ) as image:
        assert image.size == expected_crop_size[rotation]
        assert image.convert("RGB").getpixel((10, 10)) == (30, 120, 210)


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_external_link_maps_rotated_nonzero_annotation_bbox(
    tmp_path: Path,
    rotation: int,
) -> None:
    from web_translator.pdf_layout import extract_link_evidence

    source = _nonzero_origin_link_pdf(
        tmp_path / f"rotated-uri-{rotation}.pdf",
        rotation=rotation,
    )
    with pdfplumber.open(source) as document:
        words = document.pages[0].extract_words()
    block = PdfBlock(
        id="pdf:page-0001:block-0001",
        page_number=1,
        order=0,
        kind="paragraph",
        bbox=(
            min(float(word["x0"]) for word in words),
            min(float(word["top"]) for word in words),
            max(float(word["x1"]) for word in words),
            max(float(word["bottom"]) for word in words),
        ),
        style=PdfBlockStyle(12.0, False, "left", 0.0, 0.0),
        source_text="Target link",
    )

    evidence = extract_link_evidence(source, [block])

    assert evidence.blocks[0].uri == "https://example.com/nonzero"
    assert evidence.warnings == ()


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_visible_orphan_link_is_fatal_after_rotation_and_nonzero_origin(
    tmp_path: Path,
    rotation: int,
) -> None:
    from web_translator.pdf_layout import extract_link_evidence

    source = _nonzero_origin_link_pdf(
        tmp_path / f"rotated-orphan-{rotation}.pdf",
        rotation=rotation,
    )

    with pytest.raises(PdfExtractionError, match="visible link text has no emitted owner"):
        extract_link_evidence(source, [])


@pytest.mark.parametrize("rotation", [90, 180, 270])
@pytest.mark.parametrize(
    "destination_kind",
    ["XYZ", "FitH", "FitV", "FitR", "Destination"],
)
def test_internal_destination_maps_rotated_nonzero_coordinates(
    tmp_path: Path,
    rotation: int,
    destination_kind: str,
) -> None:
    from pypdf import PdfReader
    from pypdf.generic import (
        ArrayObject,
        Destination,
        Fit,
        FloatObject,
        NameObject,
    )

    from web_translator.pdf_layout import _map_internal_destination

    source = _nonzero_origin_link_pdf(
        tmp_path / f"rotated-destination-{rotation}-{destination_kind}.pdf",
        rotation=rotation,
    )
    reader = PdfReader(source)
    page_reference = reader.pages[0].indirect_reference
    if destination_kind == "XYZ":
        destination = ArrayObject(
            [
                page_reference,
                NameObject("/XYZ"),
                FloatObject(35),
                FloatObject(65),
                FloatObject(0),
            ]
        )
    elif destination_kind == "FitH":
        destination = ArrayObject(
            [page_reference, NameObject("/FitH"), FloatObject(65)]
        )
    elif destination_kind == "FitV":
        destination = ArrayObject(
            [page_reference, NameObject("/FitV"), FloatObject(35)]
        )
    elif destination_kind == "FitR":
        destination = ArrayObject(
            [
                page_reference,
                NameObject("/FitR"),
                FloatObject(20),
                FloatObject(55),
                FloatObject(50),
                FloatObject(75),
            ]
        )
    else:
        destination = Destination(
            "rotated-coordinate-target",
            page_reference,
            Fit.xyz(left=35, top=65),
        )
    target_bboxes = {
        90: (55.0, 120.0, 75.0, 150.0),
        180: (50.0, 105.0, 80.0, 125.0),
        270: (-25.0, 150.0, -5.0, 180.0),
    }
    decoy_bboxes = {
        90: (0.0, 20.0, 20.0, 50.0),
        180: (120.0, 40.0, 150.0, 60.0),
        270: (40.0, 50.0, 60.0, 80.0),
    }
    style = PdfBlockStyle(10.0, False, "left", 0.0, 0.0)
    target = PdfBlock(
        id="pdf:page-0001:block-0001",
        page_number=1,
        order=0,
        kind="paragraph",
        bbox=target_bboxes[rotation],
        style=style,
        source_text="Coordinate target",
    )
    decoy = PdfBlock(
        id="pdf:page-0001:block-0002",
        page_number=1,
        order=1,
        kind="paragraph",
        bbox=decoy_bboxes[rotation],
        style=style,
        source_text="Decoy",
    )

    assert _map_internal_destination(
        reader,
        destination,
        {1: [target, decoy]},
    ) == target


def test_extract_link_evidence_warns_and_fails_closed_for_ambiguous_source(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_layout import extract_link_evidence

    style = PdfBlockStyle(12.0, False, "left", 50.0, 6.0)
    blocks = [
        PdfBlock(
            id=f"pdf:page-0001:block-{index:04d}",
            page_number=1,
            order=index - 1,
            kind="paragraph",
            bbox=bbox,
            style=style,
            source_text=text,
        )
        for index, bbox, text in (
            (1, (50.0, 88.0, 180.0, 102.0), "First visible owner"),
            (2, (50.0, 108.0, 180.0, 122.0), "Second visible owner"),
        )
    ]

    evidence = extract_link_evidence(
        _ambiguous_link_pdf(tmp_path / "ambiguous-link.pdf"), blocks
    )

    assert [block.uri for block in evidence.blocks] == [None, None]
    assert evidence.warnings == (
        "page 1 link 1: unresolved visible link source",
    )


def test_extract_link_evidence_rejects_visible_text_without_an_emitted_owner(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_layout import extract_link_evidence

    with pytest.raises(PdfExtractionError, match="visible link text has no emitted owner"):
        extract_link_evidence(
            _orphan_visible_link_pdf(tmp_path / "orphan-visible-link.pdf"),
            [],
        )
