"""Regression tests for fail-closed PDF inspection and logical extraction."""

from __future__ import annotations

from pathlib import Path

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
