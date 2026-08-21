"""Small deterministic PDF-contract fixtures shared by PDF tests."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject
from reportlab.pdfgen.canvas import Canvas

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


def make_text_pdf(path: Path, *, pages: int = 1) -> Path:
    """Create a PDF whose pages each contain more than 100 selectable characters."""
    canvas = Canvas(str(path), pagesize=(612, 792))
    text = "Selectable document text. " * 8
    for _ in range(pages):
        canvas.drawString(72, 720, text)
        canvas.showPage()
    canvas.save()
    return path


def make_image_only_pdf(path: Path, *, pages: int = 1) -> Path:
    """Create pages whose dominant content is one full-page raster image."""
    image_path = path.with_suffix(".png")
    Image.new("RGB", (612, 792), "white").save(image_path)
    canvas = Canvas(str(path), pagesize=(612, 792))
    for _ in range(pages):
        canvas.drawImage(str(image_path), 0, 0, width=612, height=792)
        canvas.showPage()
    canvas.save()
    return path


def make_mixed_pdf(path: Path, *, scanned_pages: int, text_pages: int) -> Path:
    """Create a document with image-only pages before selectable-text pages."""
    image_path = path.with_suffix(".png")
    Image.new("RGB", (612, 792), "white").save(image_path)
    canvas = Canvas(str(path), pagesize=(612, 792))
    for _ in range(scanned_pages):
        canvas.drawImage(str(image_path), 0, 0, width=612, height=792)
        canvas.showPage()
    text = "Selectable document text. " * 8
    for _ in range(text_pages):
        canvas.drawString(72, 720, text)
        canvas.showPage()
    canvas.save()
    return path


def make_encrypted_pdf(path: Path) -> Path:
    """Create a password-protected PDF without depending on external tools."""
    clear_path = path.with_name(f"{path.stem}-clear.pdf")
    make_text_pdf(clear_path)
    writer = PdfWriter()
    writer.append(PdfReader(clear_path))
    writer.encrypt("secret")
    with path.open("wb") as destination:
        writer.write(destination)
    return path


def make_malformed_pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.7\nnot a valid PDF")
    return path


def make_zero_page_pdf(path: Path) -> Path:
    writer = PdfWriter()
    with path.open("wb") as destination:
        writer.write(destination)
    return path


def make_rotated_pdf(path: Path, *, rotation: int) -> Path:
    make_text_pdf(path)
    reader = PdfReader(path)
    writer = PdfWriter()
    page = reader.pages[0]
    if rotation % 90 == 0:
        page.rotate(rotation)
    else:
        page[NameObject("/Rotate")] = NumberObject(rotation)
    writer.add_page(page)
    with path.open("wb") as destination:
        writer.write(destination)
    return path


def make_many_pages_pdf(path: Path, *, pages: int = 101) -> Path:
    return make_text_pdf(path, pages=pages)


def make_oversized_dimension_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(14_401, 792))
    canvas.drawString(72, 720, "Selectable document text. " * 8)
    canvas.save()
    return path


def make_oversized_pdf(path: Path) -> Path:
    make_text_pdf(path)
    with path.open("ab") as destination:
        destination.truncate(50 * 1024 * 1024 + 1)
    return path
