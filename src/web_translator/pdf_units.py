"""Logical PDF unit membership and reversible source-text projection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Mapping, Sequence

from web_translator.pdf_layout import split_list_marker
from web_translator.pdf_models import (
    PdfBlock, PdfBlockBoundary, PdfContractError, PdfDocument, PdfFlowFinding,
    PdfJoinEvidence, PdfPage, PdfTextJoin, PdfTranslationUnit,
)


TRANSLATABLE_KINDS = frozenset({
    "heading", "paragraph", "list-item", "caption", "footnote", "table-cell",
})


@dataclass(frozen=True, slots=True)
class PdfJoinDecision:
    status: Literal["join", "separate", "ambiguous"]
    join: PdfTextJoin | None = None
    finding: PdfFlowFinding | None = None


def _ambiguous(left: PdfBlock, right: PdfBlock, reason: str) -> PdfJoinDecision:
    return PdfJoinDecision("ambiguous", finding=PdfFlowFinding(
        "ambiguous-page-continuation", left.id, right.id, "required",
        f"Pages {left.page_number}–{right.page_number}: {reason}.",
    ))


def _flow_pair(left: PdfBlock, right: PdfBlock) -> bool:
    return (right.page_number == left.page_number + 1
            and left.kind in {"paragraph", "list-item"} and right.kind == "paragraph"
            and left.semantic_role == right.semantic_role == "body"
            and bool(left.source_text.strip()) and bool(right.source_text.strip())
            and split_list_marker(right.source_text) is None
            and not left.source_text.rstrip().rstrip("\"'”’)]}").endswith((".", "!", "?", ":", ";", "。", "！", "？")))


def decide_page_join(
    left: PdfBlock, right: PdfBlock,
    left_boundary: PdfBlockBoundary, right_boundary: PdfBlockBoundary,
    pages: Mapping[int, PdfPage],
) -> PdfJoinDecision:
    """Decide one candidate pair; the builder checks global order and blockers."""
    separate = PdfJoinDecision("separate")
    if not _flow_pair(left, right):
        return separate
    tail, head = left_boundary.last_line, right_boundary.first_line
    discretionary = left.source_text.endswith(("‐", "\u00ad"))
    if (left_boundary.block_id != left.id or right_boundary.block_id != right.id
            or left.page_number not in pages or right.page_number not in pages):
        return _ambiguous(left, right, "missing or mismatched physical boundary evidence")
    lp, rp = pages[left.page_number], pages[right.page_number]
    lc, rc = left_boundary.column_bbox, right_boundary.column_bbox
    size = max(tail.font_size / lp.width, head.font_size / rp.width)
    left_width, right_width = (lc[2] - lc[0]) / lp.width, (rc[2] - rc[0]) / rp.width
    # Compare column-relative measures so mirrored page margins are immaterial.
    if abs(left_width - right_width) > size * 0.25 + 1e-9:
        return separate
    expected_indent = left_boundary.text_indent / lp.width
    head_indent = (head.bbox[0] - rc[0]) / rp.width
    if abs(head_indent - expected_indent) > size * 0.25 + 1e-9:
        return separate
    if any(b.column_index is None or b.column_count is None or b.line_pitch is None
           for b in (left_boundary, right_boundary)):
        return _ambiguous(left, right, "missing observed line pitch or column context")
    if (left_boundary.column_index != left_boundary.column_count - 1
            or right_boundary.column_index != 0
            or left_boundary.column_count != right_boundary.column_count):
        return separate
    pitch_left = left_boundary.line_pitch / lp.height
    pitch_right = right_boundary.line_pitch / rp.height
    if any(b.column_bbox[3] - b.column_bbox[1] < 4 * b.line_pitch - 1e-9
           for b in (left_boundary, right_boundary)):
        return _ambiguous(left, right, "observed column frame is too short to establish overflow")
    if (tail.bbox[2] > lc[2] + 1e-9 or tail.bbox[0] < lc[0] - 1e-9
            or head.bbox[0] < rc[0] - 1e-9):
        return _ambiguous(left, right, "boundary line exceeds observed column frame")
    if (abs(tail.bbox[3] / lp.height - rc[3] / rp.height) > pitch_left * 0.5 + 1e-9
            or abs(head.bbox[1] / rp.height - lc[1] / lp.height) > pitch_right * 0.5 + 1e-9
            or abs(tail.bbox[3] / lp.height - lc[3] / lp.height) > pitch_left * 0.5 + 1e-9
            or abs(head.bbox[1] / rp.height - rc[1] / rp.height) > pitch_right * 0.5 + 1e-9):
        return _ambiguous(left, right, "tail/head placement conflicts with observed column frame")
    font_left = tail.font_family.split("+")[-1].casefold()
    font_right = head.font_family.split("+")[-1].casefold()
    if (not font_left or not font_right or font_left != font_right
            or abs(tail.font_size / lp.width - head.font_size / rp.width) > size * 0.15 + 1e-9
            or left.style.bold != right.style.bold):
        return _ambiguous(left, right, "incompatible boundary font evidence")
    if (tail.text != left.source_text and not left.source_text.endswith(tail.text)) or not right.source_text.startswith(head.text):
        return _ambiguous(left, right, "boundary line text does not match physical fragments")
    if (lc[2] - tail.bbox[2]) / lp.width > size + 1e-9:
        return _ambiguous(left, right, "tail line is not filled to its observed column edge")
    if not head.text or not head.text[0].islower():
        return _ambiguous(left, right, "head text lacks positive continuation evidence")
    if discretionary and (len(tail.text) < 2 or not tail.text[-2].isalpha()):
        return _ambiguous(left, right, "discretionary mark is not a word break")
    return PdfJoinDecision("join", join=PdfTextJoin(
        left.id, right.id, "remove-discretionary-hyphen" if discretionary else "space",
        PdfJoinEvidence(left_boundary, right_boundary, (lp.width, lp.height), (rp.width, rp.height)),
    ))


def build_translation_units(
    blocks: Sequence[PdfBlock], pages: Sequence[PdfPage],
    boundaries: Mapping[str, PdfBlockBoundary],
) -> tuple[list[PdfTranslationUnit], list[PdfFlowFinding]]:
    """Build unassigned logical ownership without modifying physical blocks."""
    ordered = sorted(blocks, key=lambda block: block.order)
    page_map = {page.number: page for page in pages}
    # Furniture and explicitly linked notes cannot hide structural barriers.
    linked = {block.destination for block in blocks if block.destination}
    notes = {block.id for block in blocks if block.kind == "footnote" and block.id in linked}
    flow = [block for block in ordered
            if block.kind not in {"header", "footer", "page-number"} and block.id not in notes]
    joins: dict[str, PdfTextJoin] = {}
    findings: list[PdfFlowFinding] = []
    for left, right in zip(flow, flow[1:]):
        if not _flow_pair(left, right):
            continue
        if left.id not in boundaries or right.id not in boundaries:
            decision = _ambiguous(left, right, "missing provable boundary ownership or column frame")
        else:
            decision = decide_page_join(left, right, boundaries[left.id], boundaries[right.id], page_map)
        if decision.status == "join":
            competing = [
                candidate for candidate in flow if candidate.id not in {left.id, right.id}
                and ((candidate.page_number == left.page_number
                      and _flow_pair(candidate, right)
                      and candidate.bbox[1] < left.bbox[3] <= candidate.bbox[3])
                     or (candidate.page_number == right.page_number
                         and _flow_pair(left, candidate)
                         and candidate.bbox[1] <= right.bbox[1] < candidate.bbox[3]))
                and max(candidate.bbox[0], left.bbox[0] if candidate.page_number == left.page_number else right.bbox[0])
                < min(candidate.bbox[2], left.bbox[2] if candidate.page_number == left.page_number else right.bbox[2])
            ]
            if competing:
                decision = _ambiguous(left, right, "multiple candidate predecessors or successors")
        if decision.finding is not None:
            findings.append(decision.finding)
        if decision.join is not None:
            joins[left.id] = decision.join
    units: list[PdfTranslationUnit] = []
    pending: dict[str, int] = {}
    for block in ordered:
        if block.kind not in TRANSLATABLE_KINDS or not block.source_text.strip():
            continue
        index = pending.pop(block.id, None)
        previous = units[index] if index is not None else None
        join = joins.get(previous.source_block_ids[-1]) if previous else None
        if join is not None and join.right_block_id == block.id:
            units[index] = replace(previous, source_block_ids=(*previous.source_block_ids, block.id),
                                joins=(*previous.joins, join))
        else:
            index = len(units)
            units.append(PdfTranslationUnit(f"pdf:unit-{len(units) + 1:06d}", (block.id,),
                                            block.kind, block.semantic_role, None, ()))
        if block.id in joins:
            pending[joins[block.id].right_block_id] = index
    return units, findings


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
