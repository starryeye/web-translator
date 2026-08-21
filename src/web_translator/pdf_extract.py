"""Fail-closed structural inspection for acquired PDF sources."""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from pathlib import Path

import pdfplumber
from pypdf import PdfReader

from web_translator.pdf_acquire import MAX_PDF_BYTES
from web_translator.pdf_models import PdfPageEvidence


_MIN_PAGE_POINTS = 36.0
_MAX_PAGE_POINTS = 14_400.0
_MAX_PAGE_COUNT = 100


class PdfExtractionError(RuntimeError):
    """A PDF cannot safely enter the translation extraction workflow."""


@dataclass(frozen=True, slots=True)
class PdfInspection:
    """Structural and scan-detection evidence for one inspected source PDF."""

    page_count: int
    selectable_characters: int
    scan_candidate_pages: list[int]
    pages: list[PdfPageEvidence]


def inspect_pdf(source_pdf: Path) -> PdfInspection:
    """Inspect *source_pdf* without attempting logical document extraction."""
    source = Path(source_pdf)
    _reject_oversized_source(source)
    page_count, rotations = _read_structure(source)
    if page_count == 0:
        raise PdfExtractionError("PDF has zero pages")
    if page_count > _MAX_PAGE_COUNT:
        raise PdfExtractionError(f"PDF page count {page_count} exceeds {_MAX_PAGE_COUNT}")

    pages = _read_page_evidence(source, page_count, rotations)
    return PdfInspection(
        page_count=page_count,
        selectable_characters=sum(page.selectable_characters for page in pages),
        scan_candidate_pages=[page.number for page in pages if page.scan_candidate],
        pages=pages,
    )


def reject_unsupported_pdf(inspection: PdfInspection) -> None:
    """Reject scan-dominant PDFs using the inspection's recorded evidence."""
    if inspection.page_count == 0:
        raise PdfExtractionError("PDF page count is zero")
    if inspection.page_count > _MAX_PAGE_COUNT:
        raise PdfExtractionError(
            f"PDF page count {inspection.page_count} exceeds {_MAX_PAGE_COUNT}"
        )
    evidence = _format_evidence(inspection.pages)
    if inspection.selectable_characters < 100:
        raise PdfExtractionError(
            "scanned PDF: selectable characters "
            f"{inspection.selectable_characters} is below 100; {evidence}"
        )

    maximum_candidates = max(1, math.floor(inspection.page_count * 0.20))
    if len(inspection.scan_candidate_pages) > maximum_candidates:
        page_numbers = ", ".join(str(page) for page in inspection.scan_candidate_pages)
        raise PdfExtractionError(
            "scanned PDF: candidate pages "
            f"{page_numbers} exceed {maximum_candidates}; {evidence}"
        )


def _reject_oversized_source(source: Path) -> None:
    try:
        byte_length = source.stat().st_size
    except OSError as error:
        raise PdfExtractionError(f"cannot inspect PDF source: {error}") from error
    if byte_length > MAX_PDF_BYTES:
        raise PdfExtractionError(
            f"PDF size limit exceeded: {byte_length} bytes is above {MAX_PDF_BYTES}"
        )


def _read_structure(source: Path) -> tuple[int, list[int]]:
    try:
        with source.open("rb") as stream:
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted:
                raise PdfExtractionError("encrypted PDF inputs are unsupported")
            pages = list(reader.pages)
            return len(pages), [_normalized_rotation(page.get("/Rotate", 0)) for page in pages]
    except PdfExtractionError:
        raise
    except Exception as error:
        raise PdfExtractionError(f"cannot inspect PDF structure: {error}") from error


def _normalized_rotation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise PdfExtractionError(f"unsupported rotation {value!r}")
    rotation = float(value)
    if not math.isfinite(rotation) or not rotation.is_integer():
        raise PdfExtractionError(f"unsupported rotation {value!r}")
    normalized = int(rotation) % 360
    if normalized not in {0, 90, 180, 270}:
        raise PdfExtractionError(f"unsupported rotation {value!r}")
    return normalized


def _read_page_evidence(
    source: Path,
    page_count: int,
    rotations: list[int],
) -> list[PdfPageEvidence]:
    try:
        with pdfplumber.open(source) as document:
            if len(document.pages) != page_count:
                raise PdfExtractionError("PDF readers disagree on page count")
            return [
                _inspect_page(number, page, rotations[number - 1])
                for number, page in enumerate(document.pages, start=1)
            ]
    except PdfExtractionError:
        raise
    except Exception as error:
        raise PdfExtractionError(f"cannot inspect PDF content: {error}") from error


def _inspect_page(number: int, page: object, rotation: int) -> PdfPageEvidence:
    width = _valid_dimension(getattr(page, "width"), number, "width")
    height = _valid_dimension(getattr(page, "height"), number, "height")
    selectable = sum(
        1
        for char in getattr(page, "chars")
        if str(char.get("text", "")).strip()
    )
    largest_image_area = max(
        (
            _image_area(image, number)
            for image in getattr(page, "images")
        ),
        default=0.0,
    )
    coverage = largest_image_area / (width * height)
    if not math.isfinite(coverage):
        raise PdfExtractionError(f"page {number} has nonfinite image coverage")
    return PdfPageEvidence(
        number=number,
        width=width,
        height=height,
        rotation=rotation,
        selectable_characters=selectable,
        image_coverage=coverage,
        scan_candidate=selectable < 20 and coverage >= 0.50,
    )


def _valid_dimension(value: object, number: int, name: str) -> float:
    try:
        dimension = float(value)
    except (TypeError, ValueError) as error:
        raise PdfExtractionError(f"page {number} has unsupported {name} {value!r}") from error
    if not math.isfinite(dimension) or not _MIN_PAGE_POINTS <= dimension <= _MAX_PAGE_POINTS:
        raise PdfExtractionError(f"page {number} has unsupported dimensions {name}={value!r}")
    return dimension


def _image_area(image: object, number: int) -> float:
    try:
        width = float(image["width"])
        height = float(image["height"])
    except (KeyError, TypeError, ValueError) as error:
        raise PdfExtractionError(f"page {number} has invalid image dimensions") from error
    area = width * height
    if not math.isfinite(area) or area < 0:
        raise PdfExtractionError(f"page {number} has invalid image dimensions")
    return area


def _format_evidence(pages: list[PdfPageEvidence]) -> str:
    return "; ".join(
        "page "
        f"{page.number}: characters {page.selectable_characters}, "
        f"coverage {page.image_coverage:.3f}"
        for page in pages
    )
