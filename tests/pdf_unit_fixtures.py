"""Deterministic logical-unit contracts; not generated translation output."""

from __future__ import annotations

from dataclasses import replace

from web_translator.pdf_models import (
    PdfBlock, PdfBlockBoundary, PdfBlockStyle, PdfBoundaryLine, PdfDocument,
    PdfJoinEvidence, PdfPage, PdfTextJoin, PdfTranslationUnit,
)


def make_unit_document(
    texts: tuple[str, ...] = ("A para‐", "graph continues."),
    *, operation: str = "remove-discretionary-hyphen",
) -> PdfDocument:
    blocks = [
        PdfBlock(
            id=f"pdf:page-{index + 1:04d}:block-0001",
            page_number=index + 1,
            order=index,
            kind="paragraph",
            bbox=(72.0, 72.0, 540.0, 96.0),
            style=PdfBlockStyle(12.0, False, "left", 0.0, 8.0),
            source_text=text,
            segment_id="seg-000001",
        )
        for index, text in enumerate(texts)
    ]

    def boundary(block: PdfBlock) -> PdfBlockBoundary:
        line = PdfBoundaryLine(block.bbox, 12.0, "Helvetica", block.source_text)
        return PdfBlockBoundary(block.id, line, line, (72.0, 72.0, 540.0, 720.0), 0.0)

    joins = tuple(
        PdfTextJoin(
            left.id, right.id, operation,
            PdfJoinEvidence(boundary(left), boundary(right), (612.0, 792.0), (612.0, 792.0)),
        )
        for left, right in zip(blocks, blocks[1:])
    )
    return PdfDocument(
        schema_version="1.2",
        extracted_schema_version="1.1",
        source_sha256="a" * 64,
        page_count=len(blocks),
        selectable_characters=sum(map(len, texts)),
        scan_candidate_pages=[],
        pages=[PdfPage(index + 1, 612.0, 792.0, 0) for index in range(len(blocks))],
        blocks=blocks,
        translation_units=[PdfTranslationUnit(
            "pdf:unit-000001", tuple(block.id for block in blocks),
            "paragraph", "body", "seg-000001", joins,
        )],
    )


def make_flow_case(case: str, *, scale: float = 1.0):
    """Hand-measured adjacent-page flows; each variant changes one condition."""
    def box(values):
        return tuple(value * scale for value in values)

    texts = ("A para‐", "graph continues.")
    if case in {"space", "short-tail", "sentence-end"}:
        texts = ("A paragraph", "continues.")
    if case == "sentence-end":
        texts = ("A paragraph.", "continues.")
    if case == "list":
        texts = ("• A para‐", "graph continues.")
    if case in {"new-list", "nested-list"}:
        texts = (texts[0], "• graph continues.")
    count = 3 if case == "three-page" else 2
    if count == 3:
        texts = ("A para‐", "graph contin‐", "ues.")
    pages = [PdfPage(n, 612 * scale, 792 * scale, 0) for n in range(1, count + 1)]
    blocks = []
    boundaries = {}
    for index, text in enumerate(texts):
        first = index == 0
        x0 = 90 if case == "recto-verso" and not first else 72
        indent = 18 if case == "list" and not first else 0
        if case in {"new-indent", "nested-list"} and not first:
            indent = 24
        y = 708 if first else 72
        kind = "list-item" if (case == "list" and first) or (case in {"new-list", "nested-list"} and not first) else "paragraph"
        if not first and case in {"heading", "table", "figure"}:
            kind = case
        block = PdfBlock(
            id=f"pdf:page-{index + 1:04d}:block-0001", page_number=index + 1,
            order=index, kind=kind, bbox=box((x0 + indent, y, x0 + 468, y + 12)),
            style=PdfBlockStyle(12 * scale, False, "left", indent * scale, 0),
            source_text=text,
        )
        line = PdfBoundaryLine(block.bbox, 12 * scale, "Helvetica", text)
        last = line
        if case == "three-page" and index == 1:
            last = replace(line, bbox=box((x0, 708, x0 + 468, 720)))
            block = replace(block, bbox=box((x0, 72, x0 + 468, 720)))
        if case == "short-tail" and first:
            line = last = replace(line, bbox=box((x0, y, x0 + 200, y + 12)))
        if case == "conflict" and not first:
            line = last = replace(line, font_family="Courier")
        frame = box((x0, 72, x0 + 468, 720))
        if case == "column-change" and not first:
            frame = box((300, 72, 540, 720))
        boundary = PdfBlockBoundary(block.id, line, last, frame,
                                    (18 if case == "list" else 0) * scale,
                                    line_pitch=18 * scale, column_index=0, column_count=1)
        blocks.append(block)
        boundaries[block.id] = boundary
    if case == "blank-page":
        pages.append(PdfPage(3, 612 * scale, 792 * scale, 0))
        blocks[-1] = replace(blocks[-1], page_number=3)
    return blocks, pages, boundaries


def make_observed_flow(*, columns: int = 1, scale: float = 1.0, list_item: bool = False):
    """Synthetic words run through the real page-local extraction pipeline."""
    from web_translator.pdf_layout import (
        build_text_blocks, classify_document_lines, classify_semantic_roles,
        group_words_into_lines, order_page_lines,
    )
    pages = [PdfPage(n, 612 * scale, 792 * scale, 0) for n in (1, 2)]
    raw = []
    for page in pages:
        words = []
        for column in range(columns):
            x0, x1 = (72, 540) if columns == 1 else ((72, 288) if column == 0 else (324, 540))
            for index, top in enumerate((72, 90, 108, 672, 690, 708)):
                text = f"Observed prose {page.number} {column} {index}."
                if page.number == 1 and column == columns - 1 and index == 5:
                    text = "A para‐"
                if page.number == 2 and column == 0 and index == 0:
                    text = "graph continues."
                start = x0
                if list_item and ((page.number == 1 and index >= 3) or (page.number == 2 and index <= 2)):
                    start += 18
                if list_item and page.number == 1 and index == 3:
                    words.append(dict(text="•", x0=x0 * scale, x1=(x0 + 8) * scale,
                                      top=top * scale, bottom=(top + 12) * scale,
                                      size=12 * scale, fontname="ABCDEF+Helvetica", chars=[{"text": "•"}]))
                words.append(dict(text=text, x0=start * scale, x1=x1 * scale,
                                  top=top * scale, bottom=(top + 12) * scale,
                                  size=12 * scale, fontname="ABCDEF+Helvetica",
                                  chars=[{"text": char} for char in text if not char.isspace()]))
        raw.append((group_words_into_lines(words), page.height))
    classified = classify_semantic_roles(classify_document_lines(raw))
    lines_by_page = {}
    blocks = []
    for page, lines in zip(pages, classified):
        ordered = order_page_lines(lines, page.width)
        lines_by_page[page.number] = ordered
        blocks.extend(build_text_blocks(ordered, page.number))
    blocks = [replace(block, order=index) for index, block in enumerate(blocks)]
    return blocks, pages, lines_by_page
