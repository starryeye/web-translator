"""Hand-declared TEST ONLY figure ownership over real selectable source PDFs."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pdfplumber
from PIL import Image
from reportlab.pdfgen.canvas import Canvas

from tests.pdf_fixtures import make_pdf_source_record
from web_translator.pdf_media import render_pdf_pages
from web_translator.pdf_models import PdfBlock, PdfBlockStyle, PdfDocument, PdfPage, PdfTranslationUnit

FIGURE_ID = "pdf:page-0001:block-0002"
LABEL_LINES = {
    "diagram": [(90, "API")],
    "sentence-labels": [(90, "Requests arrive before processing."), (90, "Responses leave after processing.")],
    "numeric-prose": [(90, "The 12 workers handle 24 requests in 3 stages")],
    "colon-prose": [(90, "Workers: requests flow through the ordinary process")],
    "indent-20": [(110, "The ordinary paragraph begins here"), (90, "and continues as independent explanatory prose")],
    "indent-36": [(126, "The ordinary paragraph begins here"), (90, "and continues as independent explanatory prose")],
    "empty-figure": [], "figure-free": [],
    "partial-glyph": [(90, "API")], "overlapping-figures": [(90, "API")],
    "body-overlap": [(90, "API")],
}
_RENDERED_PAGES: dict[bytes, bytes] = {}


def make_figure_review_run(tmp_path: Path, *, case: str = "diagram") -> tuple[Path, Path]:
    run = tmp_path / ".web-translator" / "runs" / "그림 검토 with spaces"
    output = tmp_path / "translated-pdfs" / run.name
    run.mkdir(parents=True)
    output.parent.mkdir()
    canvas = Canvas(str(run / "source.pdf"), pagesize=(612, 792), invariant=1)
    canvas.setFont("Helvetica", 12)
    for number, (x, text) in enumerate(LABEL_LINES[case]):
        canvas.drawString(x, 680 - number * 20, text)
    canvas.rect(72, 600, 468, 110)
    canvas.line(80, 610, 520, 690)
    body_line = "Independent selectable body prose remains outside the preserved artwork. "
    body = body_line * 3
    for index in range(3):
        canvas.drawString(72, 750 - index * 14, body_line)
    canvas.save()
    source_bytes = (run / "source.pdf").read_bytes()
    digest = hashlib.sha256(source_bytes).hexdigest()
    source = replace(make_pdf_source_record(), byte_length=len(source_bytes), sha256=digest)
    (run / "source.json").write_text(json.dumps(source.to_dict()) + "\n", encoding="utf-8")
    with pdfplumber.open(run / "source.pdf") as pdf:
        count = sum(bool(c["text"].strip()) for c in pdf.pages[0].chars)
    style = PdfBlockStyle(12.0, False, "left", 0.0, 8.0)
    body_block = PdfBlock("pdf:page-0001:block-0001", 1, 0, "paragraph", (72., 32., 540., 80.), style,
                          source_text=body, segment_id="seg-000001")
    figure = PdfBlock(FIGURE_ID, 1, 1, "figure", (72., 82., 540., 192.), style,
                      source_text="", media_path="media/figure-0001.png")
    blocks = [body_block]
    if case != "figure-free":
        if case == "partial-glyph":
            figure = replace(figure, bbox=(94., 82., 540., 192.))
        if case == "body-overlap":
            blocks[0] = replace(body_block, bbox=(72., 82., 540., 305.))
        blocks.append(figure)
        if case == "overlapping-figures":
            blocks.append(replace(figure, id="pdf:page-0001:block-0003", order=2,
                                  media_path="media/figure-0002.png"))
        if source_bytes not in _RENDERED_PAGES:
            pages = render_pdf_pages(run / "source.pdf", tmp_path / "source-views", dpi=72)
            _RENDERED_PAGES[source_bytes] = pages[0].read_bytes()
        (run / "media").mkdir()
        from io import BytesIO
        with Image.open(BytesIO(_RENDERED_PAGES[source_bytes])) as image:
            for block in blocks[1:]:
                image.crop(block.bbox).save(run / block.media_path)
    document = PdfDocument("1.2", digest, 1, count, [], [PdfPage(1, 612., 792., 0)], blocks,
                           extracted_schema_version="1.2", translation_units=[
                               PdfTranslationUnit("pdf:unit-000001", (body_block.id,), "paragraph", "body", "seg-000001", ())])
    (run / "document.json").write_text(json.dumps(document.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
    return run, output
