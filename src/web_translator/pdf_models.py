"""Strict JSON contracts for the PDF translation workflow."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal


PdfBlockKind = Literal[
    "heading", "paragraph", "list-item", "table-cell", "figure",
    "caption", "footnote", "header", "footer", "page-number",
]
PdfVerdict = Literal["pass", "required-fix"]
BBox = tuple[float, float, float, float]


class PdfContractError(ValueError):
    """A persisted PDF workflow record violates its JSON contract."""


_SCHEMA_VERSION = "1.0"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BLOCK_ID = re.compile(
    r"pdf:page-(?P<page>\d{4}):(?:block-\d{4}|table-\d{4}:row-\d{4}:cell-\d{4})\Z"
)
_TABLE_ID = re.compile(r"pdf:page-(?P<page>\d{4}):table-\d{4}\Z")
_TABLE_CELL_ID = re.compile(r"pdf:page-\d{4}:table-\d{4}:row-\d{4}:cell-\d{4}\Z")
_SEGMENT_ID = re.compile(r"seg-\d{6}\Z")
_BLOCK_KINDS = {
    "heading", "paragraph", "list-item", "table-cell", "figure",
    "caption", "footnote", "header", "footer", "page-number",
}
_ALIGNMENTS = {"left", "center", "right", "justify"}
_VERDICTS = {"pass", "required-fix"}


@dataclass(frozen=True, slots=True)
class PdfSourceRecord:
    """Immutable provenance for a copied PDF source."""

    schema_version: str
    input_kind: Literal["local", "public"]
    requested_source: str
    final_source: str
    content_type: str
    byte_length: int
    sha256: str
    acquired_at: str
    redirects: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "input_kind": self.input_kind,
            "requested_source": self.requested_source,
            "final_source": self.final_source,
            "content_type": self.content_type,
            "byte_length": self.byte_length,
            "sha256": self.sha256,
            "acquired_at": self.acquired_at,
            "redirects": list(self.redirects),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PdfSourceRecord:
        context = "PdfSourceRecord"
        data = _require_exact_fields(
            data, context,
            {"schema_version", "input_kind", "requested_source", "final_source", "content_type",
             "byte_length", "sha256", "acquired_at", "redirects"},
        )
        input_kind = _require_string(data, "input_kind", context)
        if input_kind not in {"local", "public"}:
            raise PdfContractError(f"{context}.input_kind must be local or public")
        return cls(
            schema_version=_require_schema_version(data, context),
            input_kind=input_kind,
            requested_source=_require_string(data, "requested_source", context),
            final_source=_require_string(data, "final_source", context),
            content_type=_require_string(data, "content_type", context),
            byte_length=_require_nonnegative_int(data, "byte_length", context),
            sha256=_require_sha256(data, "sha256", context),
            acquired_at=_require_string(data, "acquired_at", context),
            redirects=_require_string_list(data, "redirects", context),
        )


@dataclass(frozen=True, slots=True)
class PdfPageEvidence:
    """Inspection evidence used to classify a source PDF page."""

    number: int
    width: float
    height: float
    rotation: int
    selectable_characters: int
    image_coverage: float
    scan_candidate: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "selectable_characters": self.selectable_characters,
            "image_coverage": self.image_coverage,
            "scan_candidate": self.scan_candidate,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PdfPageEvidence:
        context = "PdfPageEvidence"
        data = _require_exact_fields(
            data, context,
            {"number", "width", "height", "rotation", "selectable_characters", "image_coverage", "scan_candidate"},
        )
        width = _require_positive_float(data, "width", context)
        height = _require_positive_float(data, "height", context)
        coverage = _require_finite_float(data, "image_coverage", context)
        if not 0.0 <= coverage <= 1.0:
            raise PdfContractError(f"{context}.image_coverage must be between 0 and 1")
        return cls(
            number=_require_positive_int(data, "number", context),
            width=width,
            height=height,
            rotation=_require_rotation(data, context),
            selectable_characters=_require_nonnegative_int(data, "selectable_characters", context),
            image_coverage=coverage,
            scan_candidate=_require_bool(data, "scan_candidate", context),
        )


@dataclass(frozen=True, slots=True)
class PdfBlockStyle:
    font_size: float
    bold: bool
    alignment: Literal["left", "center", "right", "justify"]
    indentation: float
    space_after: float

    def to_dict(self) -> dict[str, object]:
        return {
            "font_size": self.font_size,
            "bold": self.bold,
            "alignment": self.alignment,
            "indentation": self.indentation,
            "space_after": self.space_after,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PdfBlockStyle:
        context = "PdfBlockStyle"
        data = _require_exact_fields(data, context, {"font_size", "bold", "alignment", "indentation", "space_after"})
        alignment = _require_string(data, "alignment", context)
        if alignment not in _ALIGNMENTS:
            raise PdfContractError(f"{context}.alignment must be left, center, right, or justify")
        return cls(
            font_size=_require_positive_float(data, "font_size", context),
            bold=_require_bool(data, "bold", context),
            alignment=alignment,
            indentation=_require_finite_float(data, "indentation", context),
            space_after=_require_finite_float(data, "space_after", context),
        )


@dataclass(frozen=True, slots=True)
class PdfTableCell:
    """One fixed table-grid cell and its corresponding logical block."""

    id: str
    table_id: str
    page_number: int
    row: int
    column: int
    row_span: int
    column_span: int
    is_header: bool
    block_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "table_id": self.table_id,
            "page_number": self.page_number,
            "row": self.row,
            "column": self.column,
            "row_span": self.row_span,
            "column_span": self.column_span,
            "is_header": self.is_header,
            "block_id": self.block_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PdfTableCell:
        context = "PdfTableCell"
        data = _require_exact_fields(
            data, context,
            {"id", "table_id", "page_number", "row", "column", "row_span", "column_span", "is_header", "block_id"},
        )
        identifier = _require_string(data, "id", context)
        if not _TABLE_CELL_ID.fullmatch(identifier):
            raise PdfContractError(f"{context}.id must be a stable table-cell ID")
        table_id = _require_string(data, "table_id", context)
        table_match = _TABLE_ID.fullmatch(table_id)
        if table_match is None:
            raise PdfContractError(f"{context}.table_id must be a stable table ID")
        page_number = _require_positive_int(data, "page_number", context)
        if int(table_match.group("page")) != page_number:
            raise PdfContractError(f"{context}.table_id page must match page_number")
        block_id = _require_string(data, "block_id", context)
        if not _BLOCK_ID.fullmatch(block_id):
            raise PdfContractError(f"{context}.block_id must be a stable block ID")
        return cls(
            id=identifier,
            table_id=table_id,
            page_number=page_number,
            row=_require_nonnegative_int(data, "row", context),
            column=_require_nonnegative_int(data, "column", context),
            row_span=_require_positive_int(data, "row_span", context),
            column_span=_require_positive_int(data, "column_span", context),
            is_header=_require_bool(data, "is_header", context),
            block_id=block_id,
        )


@dataclass(frozen=True, slots=True)
class PdfBlock:
    id: str
    page_number: int
    order: int
    kind: PdfBlockKind
    bbox: BBox
    style: PdfBlockStyle
    source_text: str = ""
    segment_id: str | None = None
    table_id: str | None = None
    row: int | None = None
    column: int | None = None
    row_span: int = 1
    column_span: int = 1
    media_path: str | None = None
    caption_id: str | None = None
    uri: str | None = None
    destination: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "page_number": self.page_number,
            "order": self.order,
            "kind": self.kind,
            "bbox": list(self.bbox),
            "style": self.style.to_dict(),
            "source_text": self.source_text,
            "segment_id": self.segment_id,
            "table_id": self.table_id,
            "row": self.row,
            "column": self.column,
            "row_span": self.row_span,
            "column_span": self.column_span,
            "media_path": self.media_path,
            "caption_id": self.caption_id,
            "uri": self.uri,
            "destination": self.destination,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PdfBlock:
        context = "PdfBlock"
        data = _require_exact_fields(
            data, context,
            {"id", "page_number", "order", "kind", "bbox", "style", "source_text", "segment_id",
             "table_id", "row", "column", "row_span", "column_span", "media_path", "caption_id", "uri", "destination"},
        )
        identifier = _require_string(data, "id", context)
        match = _BLOCK_ID.fullmatch(identifier)
        if match is None:
            raise PdfContractError(f"{context}.id must be a stable block ID")
        page_number = _require_positive_int(data, "page_number", context)
        if int(match.group("page")) != page_number:
            raise PdfContractError(f"{context}.id page must match page_number")
        kind = _require_string(data, "kind", context)
        if kind not in _BLOCK_KINDS:
            raise PdfContractError(f"{context}.kind is not supported")
        segment_id = _require_optional_string(data, "segment_id", context)
        if segment_id is not None and not _SEGMENT_ID.fullmatch(segment_id):
            raise PdfContractError(f"{context}.segment_id must be a stable segment ID")
        table_id = _require_optional_string(data, "table_id", context)
        if table_id is not None and not _TABLE_ID.fullmatch(table_id):
            raise PdfContractError(f"{context}.table_id must be a stable table ID")
        caption_id = _require_optional_string(data, "caption_id", context)
        if caption_id is not None and not _BLOCK_ID.fullmatch(caption_id):
            raise PdfContractError(f"{context}.caption_id must be a stable block ID")
        return cls(
            id=identifier,
            page_number=page_number,
            order=_require_nonnegative_int(data, "order", context),
            kind=kind,
            bbox=_require_bbox(data, context),
            style=PdfBlockStyle.from_dict(_require_mapping_value(data, "style", context)),
            source_text=_require_string(data, "source_text", context),
            segment_id=segment_id,
            table_id=table_id,
            row=_require_optional_nonnegative_int(data, "row", context),
            column=_require_optional_nonnegative_int(data, "column", context),
            row_span=_require_positive_int(data, "row_span", context),
            column_span=_require_positive_int(data, "column_span", context),
            media_path=_require_optional_relative_path(data, "media_path", context),
            caption_id=caption_id,
            uri=_require_optional_string(data, "uri", context),
            destination=_require_optional_string(data, "destination", context),
        )


@dataclass(frozen=True, slots=True)
class PdfPage:
    number: int
    width: float
    height: float
    rotation: int

    def to_dict(self) -> dict[str, object]:
        return {"number": self.number, "width": self.width, "height": self.height, "rotation": self.rotation}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PdfPage:
        context = "PdfPage"
        data = _require_exact_fields(data, context, {"number", "width", "height", "rotation"})
        return cls(
            number=_require_positive_int(data, "number", context),
            width=_require_positive_float(data, "width", context),
            height=_require_positive_float(data, "height", context),
            rotation=_require_rotation(data, context),
        )


@dataclass(frozen=True, slots=True)
class PdfDocument:
    schema_version: str
    source_sha256: str
    page_count: int
    selectable_characters: int
    scan_candidate_pages: list[int]
    pages: list[PdfPage]
    blocks: list[PdfBlock]
    table_cells: list[PdfTableCell] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
            "page_count": self.page_count,
            "selectable_characters": self.selectable_characters,
            "scan_candidate_pages": list(self.scan_candidate_pages),
            "pages": [page.to_dict() for page in self.pages],
            "blocks": [block.to_dict() for block in self.blocks],
            "table_cells": [cell.to_dict() for cell in self.table_cells],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PdfDocument:
        context = "PdfDocument"
        data = _require_exact_fields(
            data, context,
            {"schema_version", "source_sha256", "page_count", "selectable_characters", "scan_candidate_pages", "pages", "blocks", "table_cells"},
        )
        pages = [PdfPage.from_dict(_require_mapping(item, f"{context}.pages[{index}]")) for index, item in enumerate(_require_list(data, "pages", context))]
        page_count = _require_positive_int(data, "page_count", context)
        if [page.number for page in pages] != list(range(1, page_count + 1)):
            raise PdfContractError(f"{context}.pages must exactly cover page numbers 1 through page_count")
        scan_candidate_pages = _require_positive_int_list(data, "scan_candidate_pages", context)
        if scan_candidate_pages != sorted(set(scan_candidate_pages)):
            raise PdfContractError(f"{context}.scan_candidate_pages must be sorted and unique")
        if any(page > page_count for page in scan_candidate_pages):
            raise PdfContractError(f"{context}.scan_candidate_pages must refer to source pages")
        blocks = [PdfBlock.from_dict(_require_mapping(item, f"{context}.blocks[{index}]")) for index, item in enumerate(_require_list(data, "blocks", context))]
        if [block.order for block in blocks] != list(range(len(blocks))):
            raise PdfContractError(f"{context}.blocks must be in exact document order")
        if any(block.page_number > page_count for block in blocks):
            raise PdfContractError(f"{context}.blocks must refer to source pages")
        table_cells = [PdfTableCell.from_dict(_require_mapping(item, f"{context}.table_cells[{index}]")) for index, item in enumerate(_require_list(data, "table_cells", context))]
        if table_cells != sorted(table_cells, key=lambda cell: (cell.table_id, cell.row, cell.column)):
            raise PdfContractError(f"{context}.table_cells must be in exact table order")
        block_ids = {block.id for block in blocks}
        if any(cell.block_id not in block_ids for cell in table_cells):
            raise PdfContractError(f"{context}.table_cells must refer to emitted blocks")
        return cls(
            schema_version=_require_schema_version(data, context),
            source_sha256=_require_sha256(data, "source_sha256", context),
            page_count=page_count,
            selectable_characters=_require_nonnegative_int(data, "selectable_characters", context),
            scan_candidate_pages=scan_candidate_pages,
            pages=pages,
            blocks=blocks,
            table_cells=table_cells,
        )


@dataclass(frozen=True, slots=True)
class PdfLayoutReview:
    schema_version: str
    staged_pdf_sha256: str
    pages_reviewed: list[int]
    contact_sheets_reviewed: dict[str, list[int]]
    findings: dict[str, dict[str, str]]
    unresolved_required: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "staged_pdf_sha256": self.staged_pdf_sha256,
            "pages_reviewed": list(self.pages_reviewed),
            "contact_sheets_reviewed": {name: list(pages) for name, pages in self.contact_sheets_reviewed.items()},
            "findings": {dimension: dict(finding) for dimension, finding in self.findings.items()},
            "unresolved_required": list(self.unresolved_required),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PdfLayoutReview:
        context = "PdfLayoutReview"
        data = _require_exact_fields(
            data, context,
            {"schema_version", "staged_pdf_sha256", "pages_reviewed", "contact_sheets_reviewed", "findings", "unresolved_required"},
        )
        pages_reviewed = _require_positive_int_list(data, "pages_reviewed", context)
        if pages_reviewed != sorted(set(pages_reviewed)):
            raise PdfContractError(f"{context}.pages_reviewed must be sorted and unique")
        contacts = _require_mapping_value(data, "contact_sheets_reviewed", context)
        contact_sheets: dict[str, list[int]] = {}
        for name, pages in contacts.items():
            if not isinstance(name, str) or not name:
                raise PdfContractError(f"{context}.contact_sheets_reviewed keys must be nonempty strings")
            if not isinstance(pages, list):
                raise PdfContractError(f"{context}.contact_sheets_reviewed[{name!r}] must be an array")
            parsed_pages = _require_positive_int_values(pages, f"{context}.contact_sheets_reviewed[{name!r}]")
            if parsed_pages != sorted(set(parsed_pages)):
                raise PdfContractError(f"{context}.contact_sheets_reviewed[{name!r}] must be sorted and unique")
            contact_sheets[name] = parsed_pages
        findings_data = _require_mapping_value(data, "findings", context)
        findings: dict[str, dict[str, str]] = {}
        for dimension, finding in findings_data.items():
            if not isinstance(dimension, str) or not dimension:
                raise PdfContractError(f"{context}.findings keys must be nonempty strings")
            finding = _require_exact_fields(finding, f"{context}.findings[{dimension!r}]", {"verdict", "evidence"})
            verdict = _require_string(finding, "verdict", f"{context}.findings[{dimension!r}]")
            if verdict not in _VERDICTS:
                raise PdfContractError(f"{context}.findings[{dimension!r}].verdict is not supported")
            evidence = _require_string(finding, "evidence", f"{context}.findings[{dimension!r}]")
            if not evidence.strip():
                raise PdfContractError(f"{context}.findings[{dimension!r}].evidence must be nonempty")
            findings[dimension] = {"verdict": verdict, "evidence": evidence}
        unresolved_required = _require_string_list(data, "unresolved_required", context)
        if unresolved_required != sorted(set(unresolved_required)):
            raise PdfContractError(f"{context}.unresolved_required must be sorted and unique")
        return cls(
            schema_version=_require_schema_version(data, context),
            staged_pdf_sha256=_require_sha256(data, "staged_pdf_sha256", context),
            pages_reviewed=pages_reviewed,
            contact_sheets_reviewed=contact_sheets,
            findings=findings,
            unresolved_required=unresolved_required,
        )


def _require_exact_fields(data: object, context: str, fields: set[str]) -> Mapping[str, Any]:
    data = _require_mapping(data, context)
    if set(data) != fields:
        raise PdfContractError(f"{context} fields must be exactly {sorted(fields)}")
    return data


def _require_mapping(data: object, context: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise PdfContractError(f"{context} record must be an object")
    return data


def _require_mapping_value(data: Mapping[str, Any], field: str, context: str) -> Mapping[str, Any]:
    value = data[field]
    if not isinstance(value, Mapping):
        raise PdfContractError(f"{context}.{field} must be an object")
    return value


def _require_string(data: Mapping[str, Any], field: str, context: str) -> str:
    value = data[field]
    if not isinstance(value, str):
        raise PdfContractError(f"{context}.{field} must be a string")
    return value


def _require_optional_string(data: Mapping[str, Any], field: str, context: str) -> str | None:
    value = data[field]
    if value is not None and not isinstance(value, str):
        raise PdfContractError(f"{context}.{field} must be a string or null")
    return value


def _require_bool(data: Mapping[str, Any], field: str, context: str) -> bool:
    value = data[field]
    if type(value) is not bool:
        raise PdfContractError(f"{context}.{field} must be a boolean")
    return value


def _require_int_value(value: object, context: str) -> int:
    if type(value) is not int:
        raise PdfContractError(f"{context} must be an integer")
    return value


def _require_positive_int(data: Mapping[str, Any], field: str, context: str) -> int:
    value = _require_int_value(data[field], f"{context}.{field}")
    if value <= 0:
        raise PdfContractError(f"{context}.{field} must be positive")
    return value


def _require_nonnegative_int(data: Mapping[str, Any], field: str, context: str) -> int:
    value = _require_int_value(data[field], f"{context}.{field}")
    if value < 0:
        raise PdfContractError(f"{context}.{field} must be nonnegative")
    return value


def _require_optional_nonnegative_int(data: Mapping[str, Any], field: str, context: str) -> int | None:
    value = data[field]
    if value is None:
        return None
    value = _require_int_value(value, f"{context}.{field}")
    if value < 0:
        raise PdfContractError(f"{context}.{field} must be nonnegative or null")
    return value


def _require_finite_float(data: Mapping[str, Any], field: str, context: str) -> float:
    value = data[field]
    if type(value) not in {int, float}:
        raise PdfContractError(f"{context}.{field} must be a number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise PdfContractError(f"{context}.{field} must be finite")
    return numeric


def _require_positive_float(data: Mapping[str, Any], field: str, context: str) -> float:
    value = _require_finite_float(data, field, context)
    if value <= 0:
        raise PdfContractError(f"{context}.{field} must be positive")
    return value


def _require_list(data: Mapping[str, Any], field: str, context: str) -> list[Any]:
    value = data[field]
    if not isinstance(value, list):
        raise PdfContractError(f"{context}.{field} must be an array")
    return value


def _require_string_list(data: Mapping[str, Any], field: str, context: str) -> list[str]:
    values = _require_list(data, field, context)
    result: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise PdfContractError(f"{context}.{field}[{index}] must be a string")
        result.append(value)
    return result


def _require_positive_int_list(data: Mapping[str, Any], field: str, context: str) -> list[int]:
    return _require_positive_int_values(_require_list(data, field, context), f"{context}.{field}")


def _require_positive_int_values(values: list[object], context: str) -> list[int]:
    result: list[int] = []
    for index, value in enumerate(values):
        parsed = _require_int_value(value, f"{context}[{index}]")
        if parsed <= 0:
            raise PdfContractError(f"{context}[{index}] must be positive")
        result.append(parsed)
    return result


def _require_sha256(data: Mapping[str, Any], field: str, context: str) -> str:
    value = _require_string(data, field, context)
    if not _SHA256.fullmatch(value):
        raise PdfContractError(f"{context}.{field} must be a lowercase SHA-256 value")
    return value


def _require_schema_version(data: Mapping[str, Any], context: str) -> str:
    value = _require_string(data, "schema_version", context)
    if value != _SCHEMA_VERSION:
        raise PdfContractError(f"{context}.schema_version must be {_SCHEMA_VERSION}")
    return value


def _require_rotation(data: Mapping[str, Any], context: str) -> int:
    rotation = _require_int_value(data["rotation"], f"{context}.rotation")
    if rotation not in {0, 90, 180, 270}:
        raise PdfContractError(f"{context}.rotation must be 0, 90, 180, or 270")
    return rotation


def _require_bbox(data: Mapping[str, Any], context: str) -> BBox:
    value = data["bbox"]
    if not isinstance(value, list) or len(value) != 4:
        raise PdfContractError(f"{context}.bbox must be an array of four numbers")
    coordinates: list[float] = []
    for index, coordinate in enumerate(value):
        if type(coordinate) not in {int, float} or not math.isfinite(float(coordinate)):
            raise PdfContractError(f"{context}.bbox[{index}] must be a finite number")
        coordinates.append(float(coordinate))
    x0, top, x1, bottom = coordinates
    if x1 <= x0 or bottom <= top:
        raise PdfContractError(f"{context}.bbox must have positive width and height")
    return (x0, top, x1, bottom)


def _require_optional_relative_path(data: Mapping[str, Any], field: str, context: str) -> str | None:
    value = _require_optional_string(data, field, context)
    if value is not None and (value.startswith("/") or ".." in value.split("/")):
        raise PdfContractError(f"{context}.{field} must be a relative path")
    return value
