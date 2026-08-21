"""Regression tests for fail-closed PDF structural inspection."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.pdf_fixtures import (
    make_encrypted_pdf,
    make_image_only_pdf,
    make_malformed_pdf,
    make_many_pages_pdf,
    make_mixed_pdf,
    make_oversized_dimension_pdf,
    make_oversized_pdf,
    make_rotated_pdf,
    make_text_pdf,
    make_zero_page_pdf,
)
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
        (make_malformed_pdf, "cannot inspect PDF"),
        (make_zero_page_pdf, "zero pages"),
        (make_many_pages_pdf, "page count 101"),
        (make_oversized_dimension_pdf, "unsupported dimensions"),
        (make_oversized_pdf, "size limit"),
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
