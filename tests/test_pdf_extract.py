"""Regression tests for fail-closed PDF structural inspection."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.pdf_fixtures import (
    make_dimension_pdf,
    make_encrypted_pdf,
    make_image_text_pdf,
    make_image_only_pdf,
    make_inconsistent_page_tree_pdf,
    make_malformed_pdf,
    make_many_pages_pdf,
    make_mixed_pdf,
    make_oversized_dimension_pdf,
    make_oversized_pdf,
    make_pdf_at_size,
    make_rotated_pdf,
    make_text_pdf,
    make_truncated_eof_pdf,
    make_zero_page_pdf,
)
from web_translator.pdf_acquire import MAX_PDF_BYTES
from web_translator.pdf_extract import (
    PdfExtractionError,
    PdfInspection,
    inspect_pdf,
    reject_unsupported_pdf,
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
