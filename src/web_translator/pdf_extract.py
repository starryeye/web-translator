"""Fail-closed structural inspection for acquired PDF sources."""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from pathlib import Path

import pdfplumber
from pypdf import PdfReader
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
)

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
    _require_final_eof(source)
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


def _require_final_eof(source: Path) -> None:
    try:
        final_content = source.read_bytes().rstrip(b" \t\r\n\f\x00")
    except OSError as error:
        raise PdfExtractionError(f"cannot inspect PDF source: {error}") from error
    if not final_content.endswith(b"%%EOF"):
        raise PdfExtractionError("PDF does not end with a final %%EOF marker")


def _read_structure(source: Path) -> tuple[int, list[int]]:
    try:
        with source.open("rb") as stream:
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted:
                raise PdfExtractionError("encrypted PDF inputs are unsupported")
            tree_page_count = _validated_page_tree_count(reader)
            pages = list(reader.pages)
            if len(pages) != tree_page_count:
                raise PdfExtractionError("page tree count disagrees with flattened pages")
            return len(pages), [_normalized_rotation(page.get("/Rotate", 0)) for page in pages]
    except PdfExtractionError:
        raise
    except Exception as error:
        raise PdfExtractionError(f"cannot inspect PDF structure: {error}") from error


def _validated_page_tree_count(reader: PdfReader) -> int:
    catalog = _dictionary_node(reader.trailer.get("/Root"), "catalog")
    if not _has_name(catalog.get("/Type"), "/Catalog"):
        raise PdfExtractionError("PDF catalog has an unsupported type")
    pages = catalog.get("/Pages")
    if pages is None:
        raise PdfExtractionError("PDF catalog has no page tree")
    return _count_page_tree_leaves(pages, set())


def _count_page_tree_leaves(
    value: object,
    seen_nodes: set[tuple[str, int, int] | tuple[str, int]],
) -> int:
    node, identity = _dictionary_node_with_identity(value, "page tree")
    if identity in seen_nodes:
        raise PdfExtractionError("PDF page tree contains a cycle or repeated node")
    seen_nodes.add(identity)

    node_type = node.get("/Type")
    if not isinstance(node_type, NameObject):
        raise PdfExtractionError("PDF page tree node has an unsupported type")
    if node_type == "/Page":
        if "/Kids" in node:
            raise PdfExtractionError("PDF page leaf must not contain /Kids")
        return 1
    if node_type != "/Pages":
        raise PdfExtractionError("PDF page tree node has an unsupported type")

    children = node.get("/Kids")
    if not isinstance(children, ArrayObject):
        raise PdfExtractionError("PDF /Pages node has invalid /Kids")
    declared_count = node.get("/Count")
    if isinstance(declared_count, bool) or not isinstance(declared_count, NumberObject):
        raise PdfExtractionError("PDF /Pages node has invalid /Count")
    if int(declared_count) < 0:
        raise PdfExtractionError("PDF /Pages node has invalid /Count")

    leaf_count = sum(_count_page_tree_leaves(child, seen_nodes) for child in children)
    if int(declared_count) != leaf_count:
        raise PdfExtractionError(
            "page tree count disagrees with recursively validated leaf pages"
        )
    return leaf_count


def _has_name(value: object, expected: str) -> bool:
    return isinstance(value, NameObject) and value == expected


def _dictionary_node(value: object, context: str) -> DictionaryObject:
    node, _ = _dictionary_node_with_identity(value, context)
    return node


def _dictionary_node_with_identity(
    value: object,
    context: str,
) -> tuple[DictionaryObject, tuple[str, int, int] | tuple[str, int]]:
    if isinstance(value, IndirectObject):
        identity: tuple[str, int, int] | tuple[str, int] = (
            "indirect", value.idnum, value.generation
        )
        try:
            value = value.get_object()
        except Exception as error:
            raise PdfExtractionError(f"cannot resolve {context} node") from error
    else:
        identity = ("direct", id(value))
    if not isinstance(value, DictionaryObject):
        raise PdfExtractionError(f"{context} node must be a dictionary")
    return value, identity


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
    page_bbox = _valid_page_bbox(getattr(page, "bbox"), number)
    selectable = sum(
        1
        for char in getattr(page, "chars")
        if str(char.get("text", "")).strip()
    )
    largest_image_area = max(
        (
            _image_area(image, page_bbox, number)
            for image in getattr(page, "images")
        ),
        default=0.0,
    )
    coverage = largest_image_area / (
        (page_bbox[2] - page_bbox[0]) * (page_bbox[3] - page_bbox[1])
    )
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


def _valid_page_bbox(value: object, number: int) -> tuple[float, float, float, float]:
    if not isinstance(value, tuple) or len(value) != 4:
        raise PdfExtractionError(f"page {number} has invalid bounding box")
    try:
        x0, y0, x1, y1 = (float(coordinate) for coordinate in value)
    except (TypeError, ValueError) as error:
        raise PdfExtractionError(f"page {number} has invalid bounding box") from error
    if not all(math.isfinite(coordinate) for coordinate in (x0, y0, x1, y1)):
        raise PdfExtractionError(f"page {number} has invalid bounding box")
    if x1 <= x0 or y1 <= y0:
        raise PdfExtractionError(f"page {number} has invalid bounding box")
    return x0, y0, x1, y1


def _image_area(
    image: object,
    page_bbox: tuple[float, float, float, float],
    number: int,
) -> float:
    try:
        x0 = float(image["x0"])
        x1 = float(image["x1"])
        y0 = float(image["y0"])
        y1 = float(image["y1"])
    except (KeyError, TypeError, ValueError) as error:
        raise PdfExtractionError(f"page {number} has invalid image dimensions") from error
    coordinates = (x0, x1, y0, y1)
    if not all(math.isfinite(coordinate) for coordinate in coordinates):
        raise PdfExtractionError(f"page {number} has invalid image dimensions")
    if x1 < x0 or y1 < y0:
        raise PdfExtractionError(f"page {number} has invalid image dimensions")
    page_x0, page_y0, page_x1, page_y1 = page_bbox
    visible_width = max(0.0, min(page_x1, x1) - max(page_x0, x0))
    visible_height = max(0.0, min(page_y1, y1) - max(page_y0, y0))
    return visible_width * visible_height


def _format_evidence(pages: list[PdfPageEvidence]) -> str:
    return "; ".join(
        "page "
        f"{page.number}: characters {page.selectable_characters}, "
        f"coverage {page.image_coverage:.3f}"
        for page in pages
    )
