"""Fail-closed structural inspection and logical extraction for PDF sources."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import math
from numbers import Real
import os
from pathlib import Path
import re
import shutil
import tempfile
import warnings

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
)

from web_translator.pdf_acquire import MAX_PDF_BYTES
from web_translator.models import ProtectedToken, Segment, write_segments
from web_translator.pdf_layout import (
    PdfExtractionError,
    PdfExtractionWarning,
    PdfLine,
    TableDetectionResult,
    build_text_blocks,
    classify_document_lines,
    classify_graphic_text_lines,
    classify_semantic_roles,
    detect_footnotes,
    detect_tables,
    extract_link_evidence,
    find_clear_gutter,
    group_words_into_lines,
    order_page_lines,
    pair_figure_captions,
    repair_line_fragments,
    split_list_marker,
    _block_source_text,
    _paragraphs_are_contiguous,
    _VERTICAL_OVERLAP,
)
from web_translator.pdf_media import (
    FigureRegion,
    PdfMediaError,
    crop_figure_regions,
    detect_decorated_text_bboxes,
    figure_owns_character,
    partition_graphic_regions,
)
from web_translator.pdf_models import (
    PdfBlock,
    PdfBlockBoundary,
    PdfBlockStyle,
    PdfBoundaryLine,
    PdfContractError,
    PdfDocument,
    PdfPage,
    PdfPageEvidence,
    PdfTableCell,
    PdfTranslationUnit,
    font_size_bucket,
)
from web_translator.protection import protect_fragment
from web_translator.pdf_units import _isolated_edge_item_ids, build_translation_units


_MIN_PAGE_POINTS = 36.0
_MAX_PAGE_POINTS = 14_400.0
_MAX_PAGE_COUNT = 500
_REFERENCE_ENTRY_MARKER_PATTERN = re.compile(
    r"\s*(?:\[(?:\d+|[A-Za-z]+)\]|\d+[.)])\s+\S"
)


@dataclass(frozen=True, slots=True)
class PdfInspection:
    """Structural and scan-detection evidence for one inspected source PDF."""

    page_count: int
    selectable_characters: int
    scan_candidate_pages: list[int]
    pages: list[PdfPageEvidence]


@dataclass(frozen=True, slots=True)
class _PageMaterial:
    page_width: float
    page_height: float
    lines: list[PdfLine]
    table_blocks: tuple[PdfBlock, ...]
    table_cells: tuple[PdfTableCell, ...]
    table_character_count: int
    figure_regions: tuple[FigureRegion, ...]
    figure_character_count: int
    characters: tuple[dict[str, object], ...]


def _flow_font(line: PdfLine) -> str:
    weights: Counter[str] = Counter()
    for word in line.words:
        weights[word.fontname.split("+")[-1].casefold()] += word.character_count
    return weights.most_common(1)[0][0]


def _boundary_line(line: PdfLine) -> PdfBoundaryLine:
    return PdfBoundaryLine((line.x0, line.top, line.x1, line.bottom),
                           line.size, _flow_font(line), line.text)


def collect_owned_flow_lines(
    blocks: Sequence[PdfBlock], pages: Sequence[PdfPage],
    lines_by_page: Mapping[int, Sequence[PdfLine]], *,
    discretionary_continuations: MutableMapping[str, tuple[bool, ...]] | None = None,
) -> dict[str, tuple[PdfBoundaryLine, ...]]:
    """Retain uniquely reconstructed physical lines independently of frames."""
    result: dict[str, tuple[PdfBoundaryLine, ...]] = {}
    for page in pages:
        page_blocks = [block for block in blocks if block.page_number == page.number]
        lines = repair_line_fragments(lines_by_page.get(page.number, ()))
        owned: dict[str, list[PdfLine]] = {block.id: [] for block in page_blocks}
        for line in lines:
            bbox = (line.x0, line.top, line.x1, line.bottom)
            owners = [block for block in page_blocks if _bbox_inside(bbox, block.bbox)]
            if len(owners) == 1:
                owned[owners[0].id].append(line)
        for block in page_blocks:
            members = owned[block.id]
            if (block.kind not in {"paragraph", "list-item"} or block.semantic_role != "body"
                    or not members or _block_source_text(members) != block.source_text):
                continue
            bbox = (min(line.x0 for line in members), min(line.top for line in members),
                    max(line.x1 for line in members), max(line.bottom for line in members))
            if all(abs(a - b) <= 1e-7 for a, b in zip(bbox, block.bbox)):
                result[block.id] = tuple(_boundary_line(line) for line in members)
                if discretionary_continuations is not None:
                    discretionary_continuations[block.id] = tuple(
                        line.continues_discretionary_hyphen for line in members)
    return result


def _paragraph_owned_single_region(
    page_blocks: Sequence[PdfBlock], lines: Sequence[PdfLine],
    proven: Mapping[str, Sequence[PdfLine]], edge_ids: set[str],
) -> bool:
    """Prove one region from independent full-span owned paragraph anchors.

    The caller first rechecks canonical ownership. Short tails, hanging lists
    and inset typography can be context, never calibration by envelope overlap
    alone. Every fragment must occupy a substantial central part of the region
    and follow a single noninterleaved observed line flow.
    """
    fragments = sorted((block for block in page_blocks if block.id not in edge_ids
                        and block.kind in {"paragraph", "list-item"}
                        and block.semantic_role == "body"), key=lambda block: block.order)
    if len(fragments) < 2 or any(block.id not in proven for block in fragments):
        return False
    context = [line for block in fragments for line in proven[block.id]]
    # No unavailable body line may disappear merely because a block failed
    # reconstruction. Furniture and structural content are not calibration.
    observed = [line for line in lines if line.kind in {None, "paragraph", "list-item"}
                and line.semantic_role == "body"
                and not any(_bbox_inside((line.x0, line.top, line.x1, line.bottom), block.bbox)
                            for block in page_blocks if block.id in edge_ids)]
    if Counter(context) != Counter(observed) or len(set(context)) != len(context):
        return False
    left, right = min(line.x0 for line in context), max(line.x1 for line in context)
    width, middle = right - left, (left + right) / 2
    anchors: list[tuple[PdfLine, float]] = []
    previous: PdfLine | None = None
    for block in fragments:
        members = proven[block.id]
        first, last = members[0], members[-1]
        if previous is not None and (first.top <= previous.top
                                    or first.vertical_overlap_ratio(previous) >= _VERTICAL_OVERLAP):
            return False
        previous = last
        size, font = first.size, _flow_font(first)
        if any(_flow_font(line) != font or abs(line.size - size) > size * .15 + 1e-9
               or line.bold != first.bold for line in members):
            return False
        # A narrow independent column is not an inset body fragment, even when
        # wide spanning paragraphs happen to overlap its bounding envelope.
        x0, x1 = min(line.x0 for line in members), max(line.x1 for line in members)
        # A majority inside edge-measurement uncertainty is not positive proof.
        if not (x0 <= middle <= x1 and x1 - x0 > width * .5 + size * .25 + 1e-9
                and x1 - x0 >= size * 4):
            return False
        intervals = [b.top - a.top for a, b in zip(members, members[1:])]
        pitch = min(intervals) if intervals else None
        if any(b.top < a.bottom - 1e-9 or not _paragraphs_are_contiguous(a, b)
               or abs(interval - pitch) > max(a.size, b.size) * .15 + 1e-9
               for a, b, interval in zip(members, members[1:], intervals)):
            return False
        full = [abs(line.x0 - left) <= line.size * .25 + 1e-9
                and abs(line.x1 - right) <= line.size * .25 + 1e-9 for line in members]
        if (block.kind == "paragraph" and not block.style.bold and len(intervals) >= 2
                and any(a and b for a, b in zip(full, full[1:]))):
            anchors.append((first, pitch))
    # Two independent, compatible regular paragraphs are positive calibration;
    # no-gutter, indentation and short lines themselves supply no such proof.
    if len(anchors) < 2:
        return False
    reference, pitch = anchors[0]
    return all(_flow_font(line) == _flow_font(reference)
               and abs(line.size - reference.size) <= reference.size * .15 + 1e-9
               and abs(value - pitch) <= max(line.size, reference.size) * .15 + 1e-9
               for line, value in anchors[1:])


def collect_flow_boundaries(
    blocks: Sequence[PdfBlock], pages: Sequence[PdfPage],
    lines_by_page: Mapping[int, Sequence[PdfLine]], *,
    owned_lines: Mapping[str, tuple[PdfBoundaryLine, ...]] | None = None,
) -> dict[str, PdfBlockBoundary]:
    """Collect real column frames and pitch from uniquely owned physical lines.

    The writer shares its canonical ownership result. Frames still come from
    observed column lines, never guessed block boxes or absent line context.
    """
    if owned_lines is None:
        owned_lines = collect_owned_flow_lines(blocks, pages, lines_by_page)
    edge_ids = _isolated_edge_item_ids(blocks, pages, owned_lines)
    result: dict[str, PdfBlockBoundary] = {}
    for page in pages:
        page_blocks = [block for block in blocks if block.page_number == page.number]
        lines = repair_line_fragments(lines_by_page.get(page.number, ()))
        indexed = {_boundary_line(line): line for line in lines}
        proven = {block.id: [indexed[line] for line in owned_lines[block.id]]
                  for block in page_blocks if block.id in owned_lines and block.id not in edge_ids
                  and all(line in indexed for line in owned_lines[block.id])}
        context = [line for members in proven.values() for line in members]
        if len(context) < 3:
            continue
        try:
            gutter = find_clear_gutter(context, page.width)
        except PdfExtractionError:
            continue
        if gutter is None:
            # A no-gutter result alone does not prove a single column. Every
            # line must overlap a shared horizontal interval.
            if max(line.x0 for line in context) >= min(line.x1 for line in context):
                # Only this failed sufficient proof gets the paragraph-owned
                # fallback; ordinary common-interval and true-gutter paths stay
                # unchanged. Never trust foreign/incomplete caller ownership.
                canonical = collect_owned_flow_lines(page_blocks, [page], {page.number: lines})
                relevant = [block for block in page_blocks if block.id not in edge_ids
                            and block.kind in {"paragraph", "list-item"}
                            and block.semantic_role == "body"]
                if (len({block.id for block in blocks}) != len(blocks)
                        or not set(owned_lines).issubset({block.id for block in blocks})
                        or any(block.id not in canonical
                               or owned_lines.get(block.id) != canonical[block.id] for block in relevant)
                        or not _paragraph_owned_single_region(page_blocks, lines, proven, edge_ids)):
                    continue
            columns = [context]
        else:
            if any(line.x0 < gutter[1] and line.x1 > gutter[0] for line in context):
                continue
            columns = [[line for line in context if line.x1 <= gutter[0]],
                       [line for line in context if line.x0 >= gutter[1]]]
        for index, column in enumerate(columns):
            if len(column) < 3:
                continue
            frame = (min(line.x0 for line in column), min(line.top for line in column),
                     max(line.x1 for line in column), max(line.bottom for line in column))
            for block in page_blocks:
                members = proven.get(block.id, [])
                if not members or any(line not in column for line in members):
                    continue
                first, last = members[0], members[-1]
                peers = sorted((line for line in column
                                if _flow_font(line) == _flow_font(last)
                                and abs(line.size - last.size) <= last.size * 0.15),
                               key=lambda line: line.top)
                pitches = [right.top - left.top for left, right in zip(peers, peers[1:])
                           if right.top > left.top and _paragraphs_are_contiguous(left, right)]
                # Two agreeing observed intervals are the minimum evidence of
                # regular leading. Paragraph gaps cannot establish pitch alone.
                pitch = min(pitches) if pitches else None
                if pitch is None or sum(abs(value - pitch) <= last.size * 0.15 for value in pitches) < 2:
                    pitch = None
                marker = split_list_marker(first.text)
                if block.kind == "list-item":
                    if marker is None or len(first.words) < 2 or first.words[0].text != marker[0]:
                        continue
                    margin = first.words[1].x0
                else:
                    margin = min(line.x0 for line in members)
                result[block.id] = PdfBlockBoundary(
                    block.id, _boundary_line(first), _boundary_line(last), frame,
                    margin - frame[0], pitch, index, len(columns),
                )
    return result


@contextmanager
def _upright_extraction_source(
    source: Path,
    staging_parent: Path,
) -> Iterator[Path]:
    """Clone one PDF for logical extraction with only page rotation cleared."""
    reader = PdfReader(Path(source), strict=True)
    if all(_normalized_rotation(page.get("/Rotate", 0)) == 0 for page in reader.pages):
        yield Path(source)
        return
    staging_parent = Path(staging_parent)
    remove_empty_parent = not staging_parent.exists()
    staging_root: Path | None = None
    try:
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging_root = Path(
            tempfile.mkdtemp(prefix=".pdf-upright-", dir=str(staging_parent))
        )
        partial = staging_root / "source.partial.pdf"
        staged = staging_root / "source.pdf"
        writer = PdfWriter()
        writer.clone_document_from_reader(reader)
        for page in writer.pages:
            page[NameObject("/Rotate")] = NumberObject(0)
        with partial.open("wb") as destination:
            writer.write(destination)
        os.replace(partial, staged)
        yield staged
    finally:
        if staging_root is not None:
            shutil.rmtree(staging_root, ignore_errors=True)
        if remove_empty_parent:
            try:
                staging_parent.rmdir()
            except OSError:
                pass


def _region_with_source_crop(
    region: FigureRegion,
    rotation: int,
) -> FigureRegion:
    """Keep logical geometry upright while mapping only the rendered crop."""
    crop = region.crop_bbox or region.bbox
    width = region.page_width
    height = region.page_height
    if rotation == 0:
        return region
    if rotation == 90:
        rotated = (height - crop[3], crop[0], height - crop[1], crop[2])
        rendered_width, rendered_height = height, width
    elif rotation == 180:
        rotated = (
            width - crop[2],
            height - crop[3],
            width - crop[0],
            height - crop[1],
        )
        rendered_width, rendered_height = width, height
    elif rotation == 270:
        rotated = (crop[1], width - crop[2], crop[3], width - crop[0])
        rendered_width, rendered_height = height, width
    else:  # inspection rejects this before extraction
        raise PdfExtractionError(f"unsupported rotation {rotation!r}")
    return replace(
        region,
        page_width=rendered_width,
        page_height=rendered_height,
        crop_bbox=rotated,
    )


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


def extract_pdf(
    source_pdf: Path,
    document_path: Path,
    segments_path: Path,
    media_dir: Path,
) -> PdfDocument:
    """Extract deterministic logical blocks and shared translation segments."""
    source = Path(source_pdf)
    inspection = inspect_pdf(source)
    reject_unsupported_pdf(inspection)
    with _upright_extraction_source(source, Path(document_path).parent) as logical_source:
        materials = _extract_page_materials(logical_source, inspection)
        classified_pages = classify_document_lines(
            [(material.lines, material.page_height) for material in materials]
        )
        with warnings.catch_warnings(record=True) as semantic_warning_records:
            warnings.simplefilter("always", PdfExtractionWarning)
            classified_pages = classify_semantic_roles(classified_pages)
        semantic_warnings = tuple(
            str(record.message)
            for record in semantic_warning_records
            if issubclass(record.category, PdfExtractionWarning)
        )

        blocks: list[PdfBlock] = []
        table_cells: list[PdfTableCell] = []
        figure_regions: list[FigureRegion] = []
        assigned_by_page: list[int] = []
        figure_number = 0
        lines_by_page: dict[int, list[PdfLine]] = {}
        for evidence, lines, material in zip(
            inspection.pages, classified_pages, materials, strict=True
        ):
            ordered = order_page_lines(
                lines,
                material.page_width,
                spanning_bboxes=[
                    *_table_region_bboxes(material.table_blocks),
                    *(region.bbox for region in material.figure_regions),
                ],
            )
            lines_by_page[evidence.number] = ordered
            assigned_by_page.append(
                sum(line.character_count for line in ordered)
                + material.table_character_count
                + material.figure_character_count
            )
            page_blocks = build_text_blocks(ordered, evidence.number)
            page_figures: list[PdfBlock] = []
            for region in material.figure_regions:
                figure_number += 1
                page_figures.append(
                    PdfBlock(
                        id=(
                            f"pdf:page-{evidence.number:04d}:"
                            f"block-{len(page_blocks) + len(page_figures) + 1:04d}"
                        ),
                        page_number=evidence.number,
                        order=0,
                        kind="figure",
                        bbox=region.bbox,
                        style=PdfBlockStyle(
                            font_size=10.0,
                            bold=False,
                            alignment="center",
                            indentation=region.bbox[0],
                            space_after=0.0,
                        ),
                        media_path=f"media/figure-{figure_number:04d}.png",
                    )
                )
            page_blocks = _insert_rich_blocks(
                page_blocks,
                material.table_blocks,
                page_figures,
            )
            page_blocks = _order_callout_icons(
                page_blocks, page_figures, material.figure_regions,
            )
            page_blocks = detect_footnotes(
                page_blocks,
                material.characters,
                page_height=material.page_height,
            )
            page_blocks = pair_figure_captions(page_blocks)
            blocks.extend(page_blocks)
            table_cells.extend(material.table_cells)
            figure_regions.extend(material.figure_regions)

        _validate_character_assignment(inspection, assigned_by_page)
        blocks = [replace(block, order=index) for index, block in enumerate(blocks)]
        blocks = _attach_reference_continuations(blocks)
        _validate_peer_overlap(blocks)
        link_evidence = extract_link_evidence(logical_source, blocks)
        blocks = list(link_evidence.blocks)
    extraction_warnings = sorted(
        set((*semantic_warnings, *link_evidence.warnings))
    )
    for message in extraction_warnings:
        warnings.warn(message, PdfExtractionWarning, stacklevel=2)
    pages = [PdfPage(evidence.number, material.page_width, material.page_height, evidence.rotation)
             for evidence, material in zip(inspection.pages, materials, strict=True)]
    owned_line_continuations: dict[str, tuple[bool, ...]] = {}
    owned_lines = collect_owned_flow_lines(
        blocks, pages, lines_by_page, discretionary_continuations=owned_line_continuations)
    boundaries = collect_flow_boundaries(blocks, pages, lines_by_page, owned_lines=owned_lines)
    units, flow_findings = build_translation_units(
        blocks, pages, boundaries, owned_lines=owned_lines,
        owned_line_continuations=owned_line_continuations)
    blocks, units, segments = build_pdf_unit_segments(blocks, units)
    document = PdfDocument(
        schema_version="1.2",
        extracted_schema_version="1.2",
        source_sha256=_sha256(source),
        page_count=inspection.page_count,
        selectable_characters=inspection.selectable_characters,
        scan_candidate_pages=list(inspection.scan_candidate_pages),
        pages=pages,
        blocks=blocks,
        translation_units=units,
        flow_findings=flow_findings,
        table_cells=table_cells,
        links=list(link_evidence.links),
        extraction_warnings=extraction_warnings,
    )
    try:
        PdfDocument.from_dict(document.to_dict())
        Path(document_path).parent.mkdir(parents=True, exist_ok=True)
        Path(segments_path).parent.mkdir(parents=True, exist_ok=True)
        media = Path(media_dir)
        if figure_regions:
            crop_figure_regions(source, figure_regions, media, dpi=144)
        else:
            media.mkdir(parents=True, exist_ok=True)
            if not media.is_dir():
                raise PdfExtractionError(f"PDF media path is not a directory: {media}")
        Path(document_path).write_text(
            json.dumps(document.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        write_segments(Path(segments_path), segments)
    except PdfExtractionError:
        raise
    except PdfMediaError as error:
        raise PdfExtractionError(str(error)) from error
    except (OSError, UnicodeError, ValueError) as error:
        raise PdfExtractionError(f"cannot write PDF extraction outputs: {error}") from error
    return document


def _attach_reference_continuations(blocks: Sequence[PdfBlock]) -> list[PdfBlock]:
    """Expose page-local reference blocks that belong to an earlier entry."""
    result: list[PdfBlock] = []
    owner: PdfBlock | None = None
    for block in blocks:
        if block.semantic_role == "reference-heading":
            owner = None
        elif block.semantic_role == "reference-entry":
            if _REFERENCE_ENTRY_MARKER_PATTERN.match(block.source_text) is not None:
                owner = block
            elif owner is not None:
                block = replace(block, continuation_of=owner.id)
        elif block.kind not in {"header", "footer", "page-number"}:
            owner = None
        result.append(block)
    return result


def _extract_page_materials(
    source: Path, inspection: PdfInspection
) -> list[_PageMaterial]:
    try:
        with pdfplumber.open(source) as document:
            if len(document.pages) != inspection.page_count:
                raise PdfExtractionError("PDF readers disagree on page count")
            result: list[_PageMaterial] = []
            for page, evidence in zip(document.pages, inspection.pages, strict=True):
                try:
                    decorated = detect_decorated_text_bboxes(page)
                    table_page = (
                        page.filter(
                            lambda item: not _mapping_center_in_any_bbox(item, decorated)
                        ) if decorated else page
                    )
                    tables = detect_tables(table_page, page_number=evidence.number)
                    partition = partition_graphic_regions(
                        page,
                        page_number=evidence.number,
                        excluded_bboxes=tables.bboxes,
                    )
                    regions = [
                        _region_with_source_crop(region, evidence.rotation)
                        for region in partition.figures
                    ]
                except PdfMediaError as error:
                    raise PdfExtractionError(str(error)) from error
                characters = tuple(
                    dict(character)
                    for character in page.chars
                    if isinstance(character, Mapping)
                )
                tables = _exclude_tables_inside_figures(
                    tables,
                    regions,
                    characters,
                )
                raw_words = page.extract_words(
                    return_chars=True,
                    extra_attrs=["fontname", "size"],
                )
                if not isinstance(raw_words, list):
                    raise PdfExtractionError(
                        f"page {evidence.number} did not produce word evidence"
                    )
                excluded = [*tables.bboxes, *(region.bbox for region in regions)]
                prose_words = [
                    word for word in raw_words if not _word_in_any_bbox(word, excluded)
                ]
                _validate_graphic_word_ownership(
                    raw_words, prose_words, regions, evidence.number,
                )
                lines = [
                    line.with_page_geometry(float(page.width), float(page.height))
                    for line in group_words_into_lines(prose_words)
                ]
                lines = classify_graphic_text_lines(
                    lines, partition.decorated_text_bboxes,
                )
                figure_character_count = sum(
                    region.owned_selectable_characters for region in regions
                )
                result.append(
                    _PageMaterial(
                        page_width=float(page.width),
                        page_height=float(page.height),
                        lines=lines,
                        table_blocks=tables.blocks,
                        table_cells=tables.cells,
                        table_character_count=tables.owned_character_count,
                        figure_regions=tuple(regions),
                        figure_character_count=figure_character_count,
                        characters=characters,
                    )
                )
            return result
    except PdfExtractionError:
        raise
    except Exception as error:
        raise PdfExtractionError(f"cannot extract PDF words: {error}") from error


def _validate_graphic_word_ownership(
    raw_words: Sequence[Mapping[str, object]],
    prose_words: Sequence[Mapping[str, object]],
    regions: Sequence[FigureRegion],
    page_number: int,
) -> None:
    """Reject words crossing artwork boundaries before losing or double-counting glyphs."""
    prose_ids = {id(word) for word in prose_words}
    for word in raw_words:
        chars = word.get("chars")
        if not isinstance(chars, list):
            raise PdfExtractionError(f"page {page_number}: missing word character evidence")
        for region in regions:
            try:
                owned = [
                    figure_owns_character(char, region.bbox, page_number=page_number)
                    for char in chars if str(char.get("text", "")).strip()
                ]
            except PdfMediaError as error:
                raise PdfExtractionError(str(error)) from error
            word_owned = _word_in_any_bbox(word, [region.bbox])
            if (
                (any(owned) and id(word) in prose_ids)
                or (any(owned) and not all(owned))
                or (word_owned and owned and not all(owned))
            ):
                raise PdfExtractionError(
                    f"page {page_number} graphic bounds {region.bbox}: "
                    "figure and translatable text have conflicting character ownership"
                )


def _order_callout_icons(
    blocks: Sequence[PdfBlock], figures: Sequence[PdfBlock],
    regions: Sequence[FigureRegion],
) -> list[PdfBlock]:
    result = list(blocks)
    for figure, region in zip(figures, regions, strict=True):
        bbox = region.decorated_text_bbox
        if bbox is None:
            continue
        owners = [
            block for block in result
            if block.semantic_role.startswith("callout-")
            and _bbox_inside(block.bbox, bbox)
        ]
        if not owners:
            raise PdfExtractionError(
                f"page {region.page_number} graphic bounds {region.bbox}: missing callout text owner"
            )
        result.remove(figure)
        result.insert(min(result.index(owner) for owner in owners), figure)
    return result


def _exclude_tables_inside_figures(
    tables: TableDetectionResult,
    regions: Sequence[FigureRegion],
    characters: Sequence[Mapping[str, object]],
) -> TableDetectionResult:
    figure_bboxes = [region.bbox for region in regions]
    retained_bboxes = tuple(
        bbox
        for bbox in tables.bboxes
        if not any(_bbox_inside(bbox, figure_bbox) for figure_bbox in figure_bboxes)
    )
    if len(retained_bboxes) == len(tables.bboxes):
        return tables
    table_ids = list(dict.fromkeys(block.table_id for block in tables.blocks))
    if len(table_ids) != len(tables.bboxes) or any(
        table_id is None for table_id in table_ids
    ):
        raise PdfExtractionError("table evidence cannot be mapped to detected bounds")
    retained_ids = {
        table_id
        for table_id, bbox in zip(table_ids, tables.bboxes, strict=True)
        if bbox in retained_bboxes
    }
    return TableDetectionResult(
        blocks=tuple(block for block in tables.blocks if block.table_id in retained_ids),
        cells=tuple(cell for cell in tables.cells if cell.table_id in retained_ids),
        bboxes=retained_bboxes,
        owned_character_count=sum(
            1
            for character in characters
            if str(character.get("text", "")).strip()
            and _mapping_center_in_any_bbox(character, retained_bboxes)
        ),
    )


def _bbox_inside(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> bool:
    return (
        inner[0] >= outer[0] - 1e-6
        and inner[1] >= outer[1] - 1e-6
        and inner[2] <= outer[2] + 1e-6
        and inner[3] <= outer[3] + 1e-6
    )


def _extract_page_lines(
    source: Path, inspection: PdfInspection
) -> list[list[PdfLine]]:
    """Compatibility wrapper for callers that only need non-rich page lines."""
    return [material.lines for material in _extract_page_materials(source, inspection)]


def _word_in_any_bbox(
    word: Mapping[str, object],
    bboxes: Sequence[tuple[float, float, float, float]],
) -> bool:
    return _mapping_center_in_any_bbox(word, bboxes)


def _mapping_center_in_any_bbox(
    value: Mapping[str, object],
    bboxes: Sequence[tuple[float, float, float, float]],
) -> bool:
    try:
        x = (float(value["x0"]) + float(value["x1"])) / 2.0
        y = (float(value["top"]) + float(value["bottom"])) / 2.0
    except (KeyError, TypeError, ValueError):
        return False
    return any(
        bbox[0] - 1e-6 <= x <= bbox[2] + 1e-6
        and bbox[1] - 1e-6 <= y <= bbox[3] + 1e-6
        for bbox in bboxes
    )


def _insert_rich_blocks(
    ordinary: Sequence[PdfBlock],
    table_blocks: Sequence[PdfBlock],
    figure_blocks: Sequence[PdfBlock],
) -> list[PdfBlock]:
    """Insert each rich-layout group without perturbing unaffected text order."""
    result = list(ordinary)
    groups: list[tuple[tuple[float, float, float, float], list[PdfBlock]]] = []
    by_table: dict[str, list[PdfBlock]] = {}
    for block in table_blocks:
        if block.table_id is None:
            raise PdfExtractionError(f"table cell {block.id} has no table ID")
        by_table.setdefault(block.table_id, []).append(block)
    for cells in by_table.values():
        ordered_cells = sorted(
            cells, key=lambda block: (block.row or 0, block.column or 0)
        )
        groups.append((_union_block_bbox(ordered_cells), ordered_cells))
    groups.extend((block.bbox, [block]) for block in figure_blocks)
    for bbox, group in sorted(groups, key=lambda item: (item[0][1], item[0][0])):
        insertion = next(
            (
                index
                for index, block in enumerate(result)
                if block.bbox[1] >= bbox[1]
                and _horizontal_overlap(block.bbox, bbox) > 0
            ),
            len(result),
        )
        result[insertion:insertion] = group
    return result


def _table_region_bboxes(
    table_blocks: Sequence[PdfBlock],
) -> list[tuple[float, float, float, float]]:
    by_table: dict[str, list[PdfBlock]] = {}
    for block in table_blocks:
        if block.table_id is None:
            raise PdfExtractionError(f"table cell {block.id} has no table ID")
        by_table.setdefault(block.table_id, []).append(block)
    return [_union_block_bbox(cells) for cells in by_table.values()]


def _union_block_bbox(
    blocks: Sequence[PdfBlock],
) -> tuple[float, float, float, float]:
    return (
        min(block.bbox[0] for block in blocks),
        min(block.bbox[1] for block in blocks),
        max(block.bbox[2] for block in blocks),
        max(block.bbox[3] for block in blocks),
    )


def _horizontal_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0]))


def _validate_character_assignment(
    inspection: PdfInspection, assigned_by_page: list[int]
) -> None:
    assigned = sum(assigned_by_page)
    selectable = inspection.selectable_characters
    if assigned > selectable:
        raise PdfExtractionError(
            "PDF character assignment exceeds selectable evidence: "
            f"assigned {assigned}, selectable {selectable}"
        )
    if selectable == 0 or assigned / selectable < 0.99:
        details = "; ".join(
            f"page {page.number}: unmatched "
            f"{max(0, page.selectable_characters - page_assigned)}"
            for page, page_assigned in zip(
                inspection.pages, assigned_by_page, strict=True
            )
        )
        raise PdfExtractionError(
            "PDF character assignment below 99 percent: "
            f"assigned {assigned} of {selectable}; {details}"
        )


def _validate_peer_overlap(blocks: list[PdfBlock]) -> None:
    by_page: dict[int, list[PdfBlock]] = {}
    for block in blocks:
        by_page.setdefault(block.page_number, []).append(block)
    for page_number, page_blocks in by_page.items():
        for index, left in enumerate(page_blocks):
            for right in page_blocks[index + 1 :]:
                intersection = _intersection_area(left.bbox, right.bbox)
                if intersection == 0:
                    continue
                smaller_area = min(_bbox_area(left.bbox), _bbox_area(right.bbox))
                if (
                    smaller_area > 0
                    and intersection / smaller_area > 0.10
                    and not _is_tight_leading(left, right)
                ):
                    raise PdfExtractionError(
                        "PDF peer text blocks overlap above 10 percent on page "
                        f"{page_number}: {left.id}, {right.id}"
                    )


def _is_tight_leading(left: PdfBlock, right: PdfBlock) -> bool:
    text_kinds = {"heading", "paragraph", "list-item", "caption", "footnote"}
    overlap_height = max(
        0.0,
        min(left.bbox[3], right.bbox[3]) - max(left.bbox[1], right.bbox[1]),
    )
    return (
        left.kind in text_kinds
        and right.kind in text_kinds
        and right.order == left.order + 1
        and right.bbox[1] > left.bbox[1]
        and overlap_height
        <= max(left.style.font_size, right.style.font_size) * 0.50
    )


def _bbox_area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _intersection_area(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return width * height


def _unquoted_reference_text(text: str) -> str:
    """Mask quoted titles without changing logical-to-physical offsets."""
    return re.sub(r'"[^\"]*"|“[^”]*”', lambda match: "_" * len(match.group()), text)


def _reference_core_lengths(blocks: Sequence[PdfBlock]) -> dict[str, int]:
    """Locate citation cores logically, then project them onto physical fragments."""
    groups: dict[str, list[PdfBlock]] = {}
    for block in blocks:
        if block.semantic_role == "reference-entry":
            groups.setdefault(block.continuation_of or block.id, []).append(block)
    lengths: dict[str, int] = {}
    for fragments in groups.values():
        text = " ".join(block.source_text for block in fragments)
        annotation = re.search(
            r"\s+(?:Note|Annotation):\s*", _unquoted_reference_text(text), re.IGNORECASE
        )
        core_end = annotation.start() if annotation else len(text)
        core = text[:core_end]
        # Recognize ordering evidence, not a universal year-as-end delimiter.
        # An author-(year) entry has a title clause and a publication clause;
        # unknown extra prose still needs an explicit annotation boundary.
        unquoted = _unquoted_reference_text(core)
        # Initials and entry markers are not clause separators. Mask them with
        # equal-length text so all offsets still refer to the exact source.
        clauses = re.sub(r"(?<!\w)([A-Z])\.(?=\s|,)", r"\1_", unquoted)
        clauses = re.sub(
            r"^\s*(?:\[[A-Za-z0-9]+\]|\d+[.)])\s+",
            lambda match: "_" * len(match.group()), clauses,
        )
        # A terminal generational suffix is author evidence, not a sentence.
        # Keep the prefix anchored so unrelated prose cannot introduce a year.
        author_year = re.match(
            r"[^.!?:]*(?:\b(?:Jr|Sr)\.\s*)?\((?:18|19|20)\d{2}\)\.\s*",
            clauses, re.IGNORECASE,
        )
        if author_year is not None:
            # A year-first entry needs exactly its title and publication clauses,
            # even when extra prose happens to end in another year.
            if re.fullmatch(r'(?:(?:"[^"]+"|“[^”]+”)|[^.!?]+)\.\s+[^.!?]+\.?\s*', core[author_year.end():]) is None:
                raise PdfExtractionError(f"ambiguous bibliography annotation: {fragments[0].id}")
        else:
            # In year-last/identifier entries, completion belongs to publication
            # facts after the author and title clauses, not to a year in the title.
            author_clause = re.match(
                r"(?:(?:\bet\s+al\.(?=\s*:))|[^.!?:])+[.:]\s+",
                clauses,
                re.IGNORECASE,
            )
            if author_clause is None:
                raise PdfExtractionError(f"ambiguous bibliography citation core: {fragments[0].id}")
            # Quote closure is title evidence even when its terminal citation
            # punctuation is inside the quote and was masked from clauses.
            quoted_title = re.match(
                r'(?:(?:"[^"]+"|“[^”]+”))[.!?,]?\s+',
                core[author_clause.end():],
            )
            title_clause = quoted_title or re.match(
                r"[^.!?,]+[.!?,]\s+", clauses[author_clause.end():],
            )
            if title_clause is None:
                raise PdfExtractionError(f"ambiguous bibliography citation core: {fragments[0].id}")
            publication_start = author_clause.end() + title_clause.end()
            publication = unquoted[publication_start:]
            completion = re.search(
                r"\b(?:18|19|20)\d{2}\b|https?://\S+|\bDOI:?\s*10\.\d+/\S+",
                publication, re.I,
            )
            if completion is None:
                # A bare domain is publication evidence only when it is the
                # complete terminal clause after a source-confirmed quoted title,
                # never a completion marker in prose.
                if quoted_title is None:
                    raise PdfExtractionError(f"ambiguous bibliography citation core: {fragments[0].id}")
                bare_domain = re.fullmatch(
                    r"\s*(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
                    r"[A-Za-z]{2,63}\.?\s*",
                    publication,
                )
                if bare_domain is None:
                    raise PdfExtractionError(f"ambiguous bibliography citation core: {fragments[0].id}")
            else:
                # Later prose cannot redefine completion just by ending in a year.
                tail = core[publication_start + completion.end():]
                tail = re.sub(r"(?:https?://\S+|(?:DOI|ISBN):?\s*\S+)", "", tail, flags=re.I)
                if re.search(r"[A-Za-z]", tail):
                    raise PdfExtractionError(f"ambiguous bibliography annotation: {fragments[0].id}")
        offset = 0
        for block in fragments:
            lengths[block.id] = max(0, min(len(block.source_text), core_end - offset))
            offset += len(block.source_text) + 1
    return lengths


def _protect_reference_fragment(text: str, core_length: int) -> tuple[str, list[ProtectedToken]]:
    if not core_length:
        return protect_fragment(text)
    suffix, protected = protect_fragment(text[core_length:])
    shifted = {token.token: f"⟦WT:{index + 1:06d}⟧" for index, token in enumerate(protected)}
    suffix = re.sub(r"⟦WT:\d{6}⟧", lambda match: shifted[match.group()], suffix)
    return "⟦WT:000000⟧" + suffix, [
        ProtectedToken(token="⟦WT:000000⟧", kind="bibliography", value=text[:core_length]),
        *(replace(token, token=shifted[token.token]) for token in protected),
    ]


def _build_segments(blocks: list[PdfBlock]) -> tuple[list[PdfBlock], list[Segment]]:
    reference_lengths = _reference_core_lengths(blocks)
    target_kinds = {
        "heading",
        "paragraph",
        "list-item",
        "caption",
        "footnote",
        "table-cell",
    }
    target_blocks = [
        block
        for block in blocks
        if block.kind in target_kinds and block.source_text.strip()
    ]
    identifiers = [
        f"seg-{index:06d}" for index in range(1, len(target_blocks) + 1)
    ]
    heading_sizes = sorted(
        {
            font_size_bucket(block.style.font_size)
            for block in target_blocks
            if block.kind == "heading"
        },
        reverse=True,
    )
    headings: list[str] = []
    drafts: list[tuple[PdfBlock, list[str], str, list[ProtectedToken]]] = []
    segment_by_block: dict[str, str] = {}
    footnote_markers = {
        block.id: marker
        for block in target_blocks
        if block.kind == "footnote"
        and (marker := _pdf_leading_marker_value(block.source_text)) is not None
    }
    for identifier, block in zip(identifiers, target_blocks, strict=True):
        source_text, protected = _protect_reference_fragment(
            block.source_text, reference_lengths.get(block.id, 0)
        )
        source_text, protected = _protect_pdf_numbers_and_markers(
            source_text,
            list(protected),
            leading_marker=block.kind == "footnote",
            owner_marker=footnote_markers.get(block.destination or ""),
        )
        drafts.append((block, list(headings), source_text, list(protected)))
        segment_by_block[block.id] = identifier
        if block.kind == "heading":
            level = _block_heading_level(block, heading_sizes)
            headings = headings[: level - 1]
            while len(headings) < level - 1:
                headings.append("")
            headings.append(block.source_text)

    segments: list[Segment] = []
    for index, (block, heading_path, source_text, protected) in enumerate(drafts):
        identifier = identifiers[index]
        context_ids = identifiers[max(0, index - 1) : index] + identifiers[
            index + 1 : index + 2
        ]
        segments.append(
            Segment(
                id=identifier,
                locator=block.id,
                semantic_type=block.kind,
                heading_path=heading_path,
                source_text=source_text,
                protected=protected,
                context_ids=context_ids,
                target=True,
            )
        )
    return [
        replace(block, segment_id=segment_by_block.get(block.id)) for block in blocks
    ], segments


def build_pdf_unit_segments(
    blocks: Sequence[PdfBlock], units: Sequence[PdfTranslationUnit],
) -> tuple[list[PdfBlock], list[PdfTranslationUnit], list[Segment]]:
    """Assign one shared target per logical unit without selecting the 1.2 writer."""
    from web_translator.pdf_units import (
        _require_supported_unit_members, _unit_protection, project_unit_text,
    )

    by_id = {block.id: block for block in blocks}
    for unit in units:
        _require_supported_unit_members(unit, by_id)
    reference_blocks = [block for block in blocks if block.semantic_role == "reference-entry"]
    reference_lengths = _reference_core_lengths(reference_blocks) if reference_blocks else {}
    heading_sizes = sorted({font_size_bucket(by_id[unit.source_block_ids[0]].style.font_size)
                            for unit in units if unit.kind == "heading"}, reverse=True)
    identifiers = [f"seg-{index:06d}" for index in range(1, len(units) + 1)]
    headings: list[str] = []
    drafts = []
    segment_by_block: dict[str, str] = {}
    assigned_units: list[PdfTranslationUnit] = []
    for identifier, unit in zip(identifiers, units, strict=True):
        first = by_id[unit.source_block_ids[0]]
        source_text, occurrences = _unit_protection(unit, by_id, reference_lengths)
        drafts.append((unit, identifier, list(headings), source_text, occurrences))
        assigned_units.append(replace(unit, segment_id=identifier))
        for block_id in unit.source_block_ids:
            if block_id in segment_by_block:
                raise PdfContractError(f"{unit.id}: duplicate ownership of {block_id}")
            segment_by_block[block_id] = identifier
        if unit.kind == "heading":
            level = _block_heading_level(first, heading_sizes)
            headings = headings[:level - 1]
            while len(headings) < level - 1:
                headings.append("")
            headings.append(project_unit_text(unit, by_id).text)

    segments = [Segment(
        id=identifier,
        locator=unit.source_block_ids[0],
        semantic_type=unit.kind,
        heading_path=heading_path,
        source_text=source_text,
        protected=[ProtectedToken(item.placeholder, item.kind, item.value) for item in occurrences],
        context_ids=identifiers[max(0, index - 1):index] + identifiers[index + 1:index + 2],
        target=True,
    ) for index, (unit, identifier, heading_path, source_text, occurrences) in enumerate(drafts)]
    return ([replace(block, segment_id=segment_by_block.get(block.id)) for block in blocks],
            assigned_units, segments)


_PDF_PLACEHOLDER_PATTERN = re.compile(r"⟦WT:(\d{6})⟧")
_PDF_LEADING_MARKER_PATTERN = re.compile(
    r"^(?P<space>\s*)(?P<marker>(?:\d{1,3}|[*†‡])(?:[.)])?|"
    r"(?:[ivxlcdm]{1,6}[.)]|\([ivxlcdm]{1,6}\)|\[[ivxlcdm]{1,6}\]))(?=\s)",
    re.IGNORECASE,
)
_PDF_NUMBER_PATTERN = re.compile(r"(?<![\w⟦])[-+]?\d+(?:[,.]\d+)*(?:%)?(?![\w⟧])")


def _protect_pdf_numbers_and_markers(
    text: str,
    protected: list[ProtectedToken],
    *,
    leading_marker: bool,
    owner_marker: str | None,
) -> tuple[str, list[ProtectedToken]]:
    next_index = max(
        (int(match.group(1)) for match in _PDF_PLACEHOLDER_PATTERN.finditer(text)),
        default=-1,
    ) + 1

    def replace_match(
        rendered: str,
        match: re.Match[str],
        kind: str,
    ) -> str:
        nonlocal next_index
        placeholder = f"⟦WT:{next_index:06d}⟧"
        next_index += 1
        value = match.group("marker") if "marker" in match.groupdict() else match.group()
        protected.append(ProtectedToken(placeholder, kind, value))
        start, end = match.span("marker") if "marker" in match.groupdict() else match.span()
        return f"{rendered[:start]}{placeholder}{rendered[end:]}"

    if leading_marker and (match := _PDF_LEADING_MARKER_PATTERN.search(text)) is not None:
        text = replace_match(text, match, "footnote-marker")
    if owner_marker is not None:
        owner_pattern = re.compile(
            rf"(?<!\w)(?P<marker>{re.escape(owner_marker)})(?!\w)",
            re.IGNORECASE,
        )
        matches = list(owner_pattern.finditer(text))
        if len(matches) != 1:
            raise PdfExtractionError(
                f"ambiguous owner footnote marker {owner_marker!r}"
            )
        text = replace_match(text, matches[0], "footnote-marker")

    position = 0
    while (match := _PDF_NUMBER_PATTERN.search(text, position)) is not None:
        if any(
            placeholder.start() <= match.start() < placeholder.end()
            for placeholder in _PDF_PLACEHOLDER_PATTERN.finditer(text)
        ):
            position = match.end()
            continue
        text = replace_match(text, match, "number")
        position = match.start() + len(f"⟦WT:{next_index - 1:06d}⟧")
    return text, protected


def _pdf_leading_marker_value(text: str) -> str | None:
    match = _PDF_LEADING_MARKER_PATTERN.search(text)
    if match is None:
        return None
    return re.sub(r"[\s\[\]().]", "", match.group("marker")).casefold()


def _block_heading_level(block: PdfBlock, heading_sizes: list[int]) -> int:
    numbered = re.match(r"^(\d+(?:\.\d+)*)[.)]?\s+", block.source_text)
    if numbered is not None:
        return numbered.group(1).count(".") + 1
    rounded_size = font_size_bucket(block.style.font_size)
    return heading_sizes.index(rounded_size) + 1 if rounded_size in heading_sizes else 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise PdfExtractionError(f"cannot hash PDF source: {error}") from error
    return digest.hexdigest()


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
