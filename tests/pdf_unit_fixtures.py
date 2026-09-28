"""Deterministic logical-unit contracts; not generated translation output."""

from __future__ import annotations

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
