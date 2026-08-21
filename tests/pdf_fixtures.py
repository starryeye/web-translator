"""Small deterministic PDF-contract fixtures shared by PDF tests."""

from __future__ import annotations

from web_translator.pdf_models import (
    PdfBlock,
    PdfBlockStyle,
    PdfDocument,
    PdfLayoutReview,
    PdfPage,
    PdfPageEvidence,
    PdfSourceRecord,
    PdfTableCell,
)


def make_pdf_source_record() -> PdfSourceRecord:
    return PdfSourceRecord(
        schema_version="1.0",
        input_kind="local",
        requested_source="report.pdf",
        final_source="report.pdf",
        content_type="application/pdf",
        byte_length=42,
        sha256="a" * 64,
        acquired_at="2026-08-21T01:02:03Z",
        redirects=[],
        warnings=[],
    )


def make_pdf_page_evidence() -> PdfPageEvidence:
    return PdfPageEvidence(
        number=1,
        width=612.0,
        height=792.0,
        rotation=0,
        selectable_characters=42,
        image_coverage=0.0,
        scan_candidate=False,
    )


def make_pdf_block_style() -> PdfBlockStyle:
    return PdfBlockStyle(12.0, False, "left", 0.0, 8.0)


def make_pdf_block(*, order: int = 0) -> PdfBlock:
    return PdfBlock(
        id=f"pdf:page-0001:block-{order + 1:04d}",
        page_number=1,
        order=order,
        kind="paragraph",
        bbox=(72.0, 72.0 + order * 24.0, 540.0, 96.0 + order * 24.0),
        style=make_pdf_block_style(),
        source_text="Selectable text",
        segment_id=f"seg-{order + 1:06d}",
    )


def make_pdf_page() -> PdfPage:
    return PdfPage(number=1, width=612.0, height=792.0, rotation=0)


def make_pdf_table_cell() -> PdfTableCell:
    return PdfTableCell(
        id="pdf:page-0001:table-0001:row-0001:cell-0001",
        table_id="pdf:page-0001:table-0001",
        page_number=1,
        row=0,
        column=0,
        row_span=1,
        column_span=1,
        is_header=True,
        block_id="pdf:page-0001:block-0001",
    )


def make_pdf_document() -> PdfDocument:
    return PdfDocument(
        schema_version="1.0",
        source_sha256="a" * 64,
        page_count=1,
        selectable_characters=42,
        scan_candidate_pages=[],
        pages=[make_pdf_page()],
        blocks=[make_pdf_block()],
        table_cells=[make_pdf_table_cell()],
    )


def make_pdf_layout_review() -> PdfLayoutReview:
    return PdfLayoutReview(
        schema_version="1.0",
        staged_pdf_sha256="b" * 64,
        pages_reviewed=[1],
        contact_sheets_reviewed={"contact-sheet-001.png": [1]},
        findings={
            "heading_hierarchy": {"verdict": "pass", "evidence": "Heading levels are clear."}
        },
        unresolved_required=[],
    )
