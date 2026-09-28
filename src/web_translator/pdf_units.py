"""Logical PDF unit membership and reversible source-text projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from web_translator.pdf_models import PdfBlock, PdfContractError, PdfDocument, PdfTranslationUnit


TRANSLATABLE_KINDS = frozenset({
    "heading", "paragraph", "list-item", "caption", "footnote", "table-cell",
})


@dataclass(frozen=True, slots=True)
class PdfSourceSpan:
    block_id: str
    source_start: int
    source_end: int
    unit_start: int
    unit_end: int


@dataclass(frozen=True, slots=True)
class PdfUnitProjection:
    text: str
    spans: tuple[PdfSourceSpan, ...]


def validate_unit_membership(document: PdfDocument) -> None:
    """Check physical ownership and ordered joins, allowing pre-segmentation units."""
    by_id = {block.id: block for block in document.blocks}
    pages = {page.number: page for page in document.pages}
    eligible = {block.id for block in document.blocks
                if block.kind in TRANSLATABLE_KINDS and block.source_text.strip()}
    seen: set[str] = set()
    first_orders: list[int] = []
    segment_ids: set[str] = set()
    for index, unit in enumerate(document.translation_units, 1):
        if unit.id != f"pdf:unit-{index:06d}":
            raise PdfContractError("PdfDocument.translation_units unit IDs must be sequential")
        if not unit.source_block_ids or len(set(unit.source_block_ids)) != len(unit.source_block_ids):
            raise PdfContractError("PdfDocument.translation_units must have unique members")
        try:
            members = [by_id[block_id] for block_id in unit.source_block_ids]
        except KeyError as error:
            raise PdfContractError("PdfDocument.translation_units member is not a physical block") from error
        if any(block.id not in eligible for block in members):
            raise PdfContractError("PdfDocument.translation_units member is not translatable")
        if any(block.id in seen for block in members):
            raise PdfContractError("PdfDocument.translation_units member has duplicate ownership")
        seen.update(unit.source_block_ids)
        orders = [block.order for block in members]
        if orders != sorted(orders):
            raise PdfContractError("PdfDocument.translation_units members must follow physical order")
        first_orders.append(orders[0])
        first = members[0]
        if unit.kind != first.kind or unit.semantic_role != first.semantic_role:
            raise PdfContractError("PdfDocument.translation_units kind and role must match first member")
        for member in members[1:]:
            if unit.kind == "list-item" and member.kind == "list-item":
                raise PdfContractError("PdfDocument.translation_units cannot contain a second list-item")
            if (member.kind, member.semantic_role) != (unit.kind, unit.semantic_role):
                if not (unit.kind == "list-item" and member.kind == "paragraph" and member.semantic_role == "body"):
                    raise PdfContractError("PdfDocument.translation_units cannot mix member kinds or roles")
        if unit.segment_id is not None:
            if unit.segment_id in segment_ids:
                raise PdfContractError("PdfDocument.translation_units segment IDs must be unique")
            segment_ids.add(unit.segment_id)
        if any(block.segment_id != unit.segment_id for block in members):
            raise PdfContractError("PdfDocument.translation_units physical segment IDs must match unit")
        if len(unit.joins) != len(members) - 1:
            raise PdfContractError("PdfDocument.translation_units must join each adjacent member")
        for left, right, join in zip(members[:-1], members[1:], unit.joins, strict=True):
            if (join.left_block_id, join.right_block_id) != (left.id, right.id):
                raise PdfContractError("PdfDocument.translation_units joins must follow member order")
            if (join.evidence.left.block_id, join.evidence.right.block_id) != (left.id, right.id):
                raise PdfContractError("PdfDocument.translation_units join evidence must match members")
            for block, size in ((left, join.evidence.left_page_size), (right, join.evidence.right_page_size)):
                page = pages[block.page_number]
                if size != (page.width, page.height):
                    raise PdfContractError("PdfDocument.translation_units join page size must match physical page")
    if first_orders != sorted(first_orders):
        raise PdfContractError("PdfDocument.translation_units must be ordered by first member")
    if seen != eligible:
        raise PdfContractError("PdfDocument.translation_units must own every eligible member exactly once")
    for finding in document.flow_findings:
        if finding.left_block_id not in by_id or finding.right_block_id not in by_id:
            raise PdfContractError("PdfDocument.flow_findings must refer to physical blocks")


def unit_for_block(document: PdfDocument, block_id: str) -> PdfTranslationUnit:
    for unit in document.translation_units:
        if block_id in unit.source_block_ids:
            return unit
    raise PdfContractError(f"No translation unit owns block {block_id}")


def project_unit_text(unit: PdfTranslationUnit, blocks: Mapping[str, PdfBlock]) -> PdfUnitProjection:
    if len(unit.joins) != len(unit.source_block_ids) - 1:
        raise PdfContractError("PdfTranslationUnit joins must match member boundaries")
    chunks: list[str] = []
    spans: list[PdfSourceSpan] = []
    cursor = 0
    for index, block_id in enumerate(unit.source_block_ids):
        try:
            source = blocks[block_id].source_text
        except KeyError as error:
            raise PdfContractError(f"PdfTranslationUnit member {block_id} is missing") from error
        join = unit.joins[index] if index < len(unit.joins) else None
        if join is not None and (join.left_block_id, join.right_block_id) != (
            block_id, unit.source_block_ids[index + 1]
        ):
            raise PdfContractError("PdfTranslationUnit join does not match member boundary")
        if join is not None and join.operation == "remove-discretionary-hyphen":
            if not source.endswith(("‐", "\u00ad")):
                raise PdfContractError("PdfTranslationUnit discretionary hyphen is absent")
            visible = source[:-1]
        else:
            visible = source
        chunks.append(visible)
        spans.append(PdfSourceSpan(block_id, 0, len(visible), cursor, cursor + len(visible)))
        cursor += len(visible)
        if join is not None:
            if join.operation == "remove-discretionary-hyphen":
                spans.append(PdfSourceSpan(block_id, len(visible), len(source), cursor, cursor))
            elif join.operation == "space":
                chunks.append(" ")
                spans.append(PdfSourceSpan(block_id, len(source), len(source), cursor, cursor + 1))
                cursor += 1
            else:
                raise PdfContractError("PdfTranslationUnit join operation is not supported")
    return PdfUnitProjection("".join(chunks), tuple(spans))
