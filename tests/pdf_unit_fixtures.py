"""Deterministic logical-unit contracts; not generated translation output."""

from __future__ import annotations

from dataclasses import replace

from web_translator.pdf_models import (
    PdfBlock, PdfBlockBoundary, PdfBlockStyle, PdfBoundaryLine, PdfDocument,
    PdfJoinEvidence, PdfLinkEvidence, PdfPage, PdfTextJoin, PdfTranslationUnit,
)


def make_native_flow_pdf(path, *, ambiguous=False, oversized=False):
    """Invented selectable source with measured cross-page flow; never real AI output."""
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.pdfbase.pdfmetrics import stringWidth
    canvas = Canvas(str(path), pagesize=(612, 792), invariant=1)
    lines = [
        ["The observers record the first measured conditions",
         "and retain the complete account for later comparison",
         "while the opening account remains separate.",
         "A second account starts near the end of the sheet",
         "and records every qualification in its original order",
         "while the account continues across the physical boundary"],
        ["without losing the relationship between its source parts",
         "or assigning separate translations to incomplete clauses",
         "until the entire account reaches its conclusion.",
         "The final account describes a separate observation",
         "and preserves the evidence for independent inspection",
         "before the generated example comes to an end."],
    ]
    if ambiguous:
        lines[0][-1] += "."
    if oversized:
        # A genuine long paragraph, not a raised planner budget or split unit.
        lines = [["Selectable words " * 800]]
    for page_lines in lines:
        for top, text in zip((72, 90, 108, 672, 690, 708), page_lines):
            obj = canvas.beginText(72, 792 - top - 9.516)
            obj.setFont("Helvetica", 12)
            obj.setHorizScale(468 / stringWidth(text, "Helvetica", 12) * 100)
            obj.textLine(text)
            canvas.drawText(obj)
        canvas.showPage()
    canvas.save()
    return path


def run_native_unit_pipeline(root):
    """Public workflow with explicitly test-only translations and approvals."""
    import json
    from web_translator.cli import main
    from web_translator.models import read_segments
    from tests.test_pdf_qa import _write_review, _write_passing_layout_review
    run = root / ".web-translator" / "runs" / "논리 단위"
    output = root / "translated-pdfs" / "논리 단위"
    run.mkdir(parents=True)
    output.parent.mkdir()
    source = make_native_flow_pdf(root / "두 페이지 source.pdf")
    def command(*args):
        assert main([*args, "--run-dir", str(run)]) == 0
    command("pdf-acquire", str(source))
    command("pdf-extract")
    command("plan-zones")
    (run / "glossary.json").write_text("{}\n", encoding="utf-8")
    (run / "document-summary.txt").write_text("TEST ONLY generated flow fixture", encoding="utf-8")
    command("prepare-assignments")
    translations = ("첫 번째 관찰 기록을 보존합니다.", "물리적 페이지 경계를 넘는 하나의 완전한 기록입니다.", "마지막 관찰을 검토합니다.")
    targets = [s for s in read_segments(run / "segments.jsonl") if s.target]
    assert len(targets) == 3
    (run / "translations").mkdir()
    (run / "translations/zone-001.jsonl").write_text("".join(
        json.dumps({"segment_id": segment.id, "text": text, "notes": "TEST ONLY", "glossary_observations": {}}, ensure_ascii=False) + "\n"
        for segment, text in zip(targets, translations, strict=True)), encoding="utf-8")
    command("validate-translations", "--zone-id", "zone-001")
    command("validate-translations")
    command("pdf-review-input")
    _write_review(run)  # Test-only simulated approval, not human/AI acceptance.
    command("pdf-assemble", "--output-dir", str(output))
    command("pdf-qa", "prepare", "--output-dir", str(output))
    _write_passing_layout_review(run)  # Test-only simulated all-page approval.
    command("pdf-qa", "finalize", "--output-dir", str(output))
    return run, output


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


def make_observed_flow(*, columns: int = 1, scale: float = 1.0, list_item: bool = False,
                       head_discretionary: bool = False, sentence_boundary: bool = False,
                       tops_by_page=None, page_scales=None, space_continuation: bool = False):
    """Synthetic words run through the real page-local extraction pipeline."""
    from web_translator.pdf_layout import (
        build_text_blocks, classify_document_lines, classify_semantic_roles,
        group_words_into_lines, order_page_lines,
    )
    scales = page_scales if page_scales is not None else (scale, scale)
    tops = tops_by_page if tops_by_page is not None else ((72, 90, 108, 672, 690, 708),) * 2
    pages = [PdfPage(n, 612 * factor, 792 * factor, 0)
             for n, factor in enumerate(scales, start=1)]
    raw = []
    for page in pages:
        factor = scales[page.number - 1]
        words = []
        for column in range(columns):
            x0, x1 = (72, 540) if columns == 1 else ((72, 288) if column == 0 else (324, 540))
            for index, top in enumerate(tops[page.number - 1]):
                text = f"Observed prose {page.number} {column} {index}."
                if page.number == 1 and column == columns - 1 and index == 5:
                    text = ("A sentence ends." if sentence_boundary else
                            "A paragraph" if space_continuation else "A para‐")
                if page.number == 2 and column == 0 and index == 0:
                    text = ("graph contin‐" if head_discretionary else
                            "continues." if space_continuation else "graph continues.")
                    if sentence_boundary:
                        text = "Another sentence follows."
                if head_discretionary and page.number == 2 and column == 0 and index == 1:
                    text = "ues within the page."
                start = x0
                if list_item and ((page.number == 1 and index >= 3) or (page.number == 2 and index <= 2)):
                    start += 18
                if list_item and page.number == 1 and index == 3:
                    words.append(dict(text="•", x0=x0 * factor, x1=(x0 + 8) * factor,
                                      top=top * factor, bottom=(top + 12) * factor,
                                      size=12 * factor, fontname="ABCDEF+Helvetica", chars=[{"text": "•"}]))
                words.append(dict(text=text, x0=start * factor, x1=x1 * factor,
                                  top=top * factor, bottom=(top + 12) * factor,
                                  size=12 * factor, fontname="ABCDEF+Helvetica",
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


TERMINAL_PARAGRAPHS = (
    ("Opaque observations retain an ordinary multiword account",
     "while independent measurements preserve the complete sequence",
     "and describe the relationships among several recorded events",
     "before the evidence receives its final careful consideration",
     "The account ends."),
    ("Another independent account begins with measured evidence",
     "and preserves its own sequence of several unrelated events",
     "while the observers consider the complete physical record",
     "before preparing an independent explanation for inspection",
     "This account also ends."),
)


REGION_PARAGRAPHS = (
    (("Measured records preserve an ordinary opening account",
      "while observers retain the sequence of recorded events",
      "before preparing the evidence for careful inspection", "Done."),
     ("1. Indented observations describe a different measured interval",
      "and preserve their original geometric relationships",
      "while retaining the complete selectable source sequence",
      "before this independent account reaches its conclusion."),
     ("A later account begins with independently measured evidence",
      "and records every qualification in its original order",
      "while the account continues across the physical boundary")),
    (("without losing the relationship between its source parts",
      "or assigning separate translations to incomplete clauses",
      "until the entire account reaches its conclusion", "Finished."),
     ("Different observations describe another measured interval",
      "and retain their independently recorded relationships",
      "while preserving all original selectable source evidence",
      "before the independent explanation reaches its conclusion."),
     ("The final account describes a separate observation",
      "and preserves the evidence for independent inspection",
      "before the generated example comes to an end.")),
)

REGION_INSET = (
    "Inset records retain a separately measured sequence",
    "with exact selectable text and independent typography",
    "before the inset account reaches its conclusion.",
)


def region_source_lines(*, margin=72, discretionary=False, opaque=False):
    """Generic measured paragraphs, including disjoint short/indented lines."""
    result = []
    for page, paragraphs in enumerate(REGION_PARAGRAPHS):
        records = []
        for paragraph, texts in enumerate(paragraphs):
            for index, text in enumerate(texts):
                if discretionary and page == 0 and paragraph == 2 and index == 2:
                    text = "while the account continues across a measured para‐"
                if discretionary and page == 1 and paragraph == 0 and index == 0:
                    text = "graph retains the relationship between its source parts"
                if opaque:
                    text = text.replace("account", "record").replace("observations", "measurements")
                x0 = margin + (36 if paragraph == 1 and (page == 1 or index > 0) else 0)
                if page == 1 and paragraph == 1 and index == 0:
                    x0 = margin + 60
                x1 = margin + (24 if paragraph == 0 and index == 3 else
                               300 if paragraph == 1 and page == 1 else 420)
                top = (72, 300, 672)[paragraph] + index * 18
                size = 10 if paragraph == 1 and page == 1 else 11.4 if paragraph == 1 else 12
                records.append((text, x0, top, x1, size))
            if paragraph == 1 and page == 0:
                # Adjacent fragment metrics overlap by .4em, not parallel rows.
                for index, text in enumerate(REGION_INSET):
                    if opaque:
                        text = text.replace("account", "record")
                    records.append((text, margin + 36, 361.4 + index * 18, margin + 300, 10))
        result.append(records)
    return result


def make_observed_region_flow(*, scale=1, margin=72, font="ObservedFace", problem=None,
                              discretionary=False, opaque=False):
    from web_translator.pdf_layout import (
        build_text_blocks, classify_document_lines, classify_semantic_roles,
        group_words_into_lines, order_page_lines,
    )
    pages = [PdfPage(n, 612 * scale, 792 * scale, 0) for n in (1, 2)]
    raw = []
    for page, records in zip(pages, region_source_lines(
            margin=margin, discretionary=discretionary, opaque=opaque)):
        words = []
        for index, (text, x0, top, x1, size) in enumerate(records):
            line_font = "InsetMono" if size == 10 else font
            if page.number == 2:
                if problem in {"sparse", "staggered", "spanning"} and index >= 4:
                    # Two narrow independent paragraphs; no aligned gutter rows.
                    x0, x1 = ((margin, margin + 180) if index < 8
                              else (margin + 240, margin + 420))
                    if problem == "sparse" and index >= 8:
                        top += 15
                    if problem == "spanning" and index < 8:
                        x0, x1 = margin, margin + 420
                if problem == "ragged" and index in {1, 5, 6, 9}:
                    x1 -= 25
                if problem == "irregular" and index == 5:
                    top += 4
                if problem == "font" and index == 5:
                    line_font = "IncompatibleFace"
                if problem == "anchor-font" and index >= 8:
                    line_font = "OtherAnchor"
                if problem == "anchor-pitch" and index >= 8:
                    top += (index - 8) * 4
                if problem == "half-width" and 4 <= index < 8:
                    x0, x1 = margin + (60 if index == 4 else 0), margin + 210
                if problem == "off-center" and 4 <= index < 8:
                    x0, x1 = margin + 216, margin + 420
                if problem in {"uncertain-majority", "at-majority", "proved-majority"} and 4 <= index < 8:
                    extra = {"uncertain-majority": 1, "at-majority": 2.5, "proved-majority": 5}[problem]
                    x0, x1 = margin + (60 if index == 4 else 36), margin + 246 + extra
                if problem == "parallel" and 4 <= index < 8:
                    top = 132 + (index - 4) * 18
                if problem == "few-calibrated" and index in {1, 2, 9, 10}:
                    x1 -= 35
            if text.startswith("1. "):
                words.append(dict(text="1.", x0=x0 * scale, x1=(x0 + 28) * scale,
                                  top=top * scale, bottom=(top + size) * scale,
                                  size=size * scale, fontname=line_font, chars=[{"text": "1"}, {"text": "."}]))
                text, x0 = text[3:], x0 + 36
            words.append(dict(text=text, x0=x0 * scale, x1=x1 * scale,
                              top=top * scale, bottom=(top + size) * scale,
                              size=size * scale, fontname=line_font,
                              chars=[{"text": char} for char in text if not char.isspace()]))
        if page.number == 2 and problem == "spanning":
            for index, text in enumerate(REGION_INSET):
                words.append(dict(text=text, x0=margin * scale, x1=(margin + 180) * scale,
                                  top=(450 + index * 18) * scale, bottom=(462 + index * 18) * scale,
                                  size=12 * scale, fontname=font,
                                  chars=[{"text": char} for char in text if not char.isspace()]))
        raw.append((group_words_into_lines(words), page.height))
    classified = classify_semantic_roles(classify_document_lines(raw))
    blocks, lines = [], {}
    for page, members in zip(pages, classified):
        lines[page.number] = order_page_lines(members, page.width)
        blocks.extend(build_text_blocks(lines[page.number], page.number))
    return [replace(block, order=index) for index, block in enumerate(blocks)], pages, lines


def make_native_region_pdf(path, *, scale=1, margin=72, font="Helvetica",
                           discretionary=False, opaque=False, problem=None):
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.pdfbase.pdfmetrics import stringWidth
    if discretionary:
        from pathlib import Path
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        font = "RegionFixtureUnicode"
        pdfmetrics.registerFont(TTFont(font, str(
            Path(__file__).parents[1] / "src/web_translator/font_assets/NotoSansKR-Regular.ttf")))
    canvas = Canvas(str(path), pagesize=(612 * scale, 792 * scale), invariant=1)
    for page, records in enumerate(region_source_lines(margin=margin, discretionary=discretionary, opaque=opaque)):
        if page == 1 and problem == "competing":
            records = [(text, margin + 240, top, margin + 420, size) if 4 <= index < 8
                       else (text, x0, top, x1, size)
                       for index, (text, x0, top, x1, size) in enumerate(records)]
            records.extend((text, margin, 450 + index * 18, margin + 180, 12)
                           for index, text in enumerate(REGION_INSET))
        if page == 1 and problem in {"half-width", "uncertain-majority", "proved-majority"}:
            extra = {"half-width": 0, "uncertain-majority": 1, "proved-majority": 5}[problem]
            records = [(text, margin + (60 if index == 4 else 36), top, margin + 246 + extra, size)
                       if 4 <= index < 8 else (text, x0, top, x1, size)
                       for index, (text, x0, top, x1, size) in enumerate(records)]
        for text, x0, top, x1, size in records:
            if text.startswith("1. "):
                marker = canvas.beginText(x0 * scale, (792 - top - size * .793) * scale)
                marker.setFont(font, size * scale)
                marker.setHorizScale(28 / stringWidth("1.", font, size) * 100)
                marker.textLine("1.")
                canvas.drawText(marker)
                text, x0 = text[3:], x0 + 36
            obj = canvas.beginText(x0 * scale, (792 - top - size * .793) * scale)
            line_font = "Courier" if size == 10 else font
            obj.setFont(line_font, size * scale)
            obj.setHorizScale((x1 - x0) / stringWidth(text, line_font, size) * 100)
            obj.textLine(text)
            canvas.drawText(obj)
        canvas.showPage()
    canvas.save()
    return path


CONTEXT_PROSE = (
    "An unrelated inventory records its ordinary sequence",
    "while retaining independently selectable observations",
    "before introducing the following measured descriptions",
    "The first description preserves its complete source order",
    "and retains all relationships for independent inspection",
    "before reaching a separately measured conclusion.",
    "A second description records another distinct observation",
    "and keeps every qualification in its original sequence",
    "until this independent description reaches its conclusion.",
)
CONTEXT_NOTE = ("* An ancillary observation accompanies the inventory",
                "and preserves the complete selectable note.")


def owned_context_records(*, margin=72, font="Helvetica", mixed=True, note=True,
                          label="Variant label", spacing=18, label_first=False, problem=None):
    """Invented grouped prose and real final-owner/raw-kind disagreement."""
    records = [[(*row, "Courier" if row[4] == 10 else font)
                for row in page] for page in region_source_lines(margin=margin)]
    if mixed:
        style = "Times-Italic" if font == "Times-Roman" else "Helvetica-Oblique"
        prose = iter(CONTEXT_PROSE)
        middle = []
        for index in range(11):
            styled = index in {3, 7}
            top = 300 + index * spacing + (2 if index >= 4 else 0) + (2 if index >= 8 else 0)
            middle.append((label if styled else next(prose),
                           margin + (36 if index < 4 or styled else 54), top,
                           margin + (132 if styled else 300), 12, style if styled else font))
        if label_first:
            middle = [(text, x0, top - 3 * spacing, x1, size, own_font)
                      for text, x0, top, x1, size, own_font in middle[3:]]
        if problem == "irregular-run":
            text, x0, top, x1, size, own_font = middle[5]
            middle[5] = (text, x0, top + 4, x1, size, own_font)
        if problem == "broad-label":
            middle[3] = (*middle[3][:3], margin + 300, *middle[3][4:])
        if problem == "mixed-columns":
            middle = [(text, margin + (240 if index >= 7 else 36), top,
                       margin + (420 if index >= 7 else 198), size, own_font)
                      for index, (text, x0, top, x1, size, own_font) in enumerate(middle)]
        if problem == "incompatible-prose":
            middle = [(*row[:5], "Courier" if 4 <= index <= 6 else row[5])
                      for index, row in enumerate(middle)]
        records[1][4:8] = middle
        if problem == "insufficient-anchors":
            records[1][-2:] = [(text, x0, top, x1 - 60, size, own_font)
                              for text, x0, top, x1, size, own_font in records[1][-2:]]
    if note:
        row = records[1][4]
        records[1][4] = (row[0] + " *", *row[1:])
        records[1].extend((text, margin, 746 + index * 12, margin + 250, 8, font)
                          for index, text in enumerate(CONTEXT_NOTE))
    return records


def _context_record_words(row, scale):
    text, x0, top, x1, size, font = row
    parts = [(text, x0, x1, size)]
    if text.endswith(" *"):
        parts = [(text[:-2], x0, x1 - 12, size), ("*", x1 - 8, x1, 8)]
    elif text.startswith("1. "):
        parts = [("1.", x0, x0 + 28, size), (text[3:], x0 + 36, x1, size)]
    return [dict(text=value, x0=left * scale, x1=right * scale, top=top * scale,
                 bottom=(top + own_size) * scale, size=own_size * scale, fontname=font,
                 chars=[dict(text=c, x0=left * scale, x1=right * scale,
                             top=top * scale, bottom=(top + own_size) * scale,
                             size=own_size * scale) for c in value if not c.isspace()])
            for value, left, right, own_size in parts]


def make_observed_owned_context(*, scale=1, margin=72, font="Helvetica", mixed=True,
                               note=True, label="Variant label", spacing=18, label_first=False):
    from web_translator.pdf_layout import (
        build_text_blocks, classify_document_lines, classify_semantic_roles,
        detect_footnotes, group_words_into_lines, order_page_lines,
    )
    pages = [PdfPage(n, 612 * scale, 792 * scale, 0) for n in (1, 2)]
    words = [[word for row in page for word in _context_record_words(row, scale)]
             for page in owned_context_records(margin=margin, font=font, mixed=mixed,
                                                note=note, label=label, spacing=spacing,
                                                label_first=label_first)]
    classified = classify_semantic_roles(classify_document_lines([
        (group_words_into_lines(page_words), page.height)
        for page_words, page in zip(words, pages)]))
    blocks, lines = [], {}
    for page, members, page_words in zip(pages, classified, words):
        lines[page.number] = order_page_lines(members, page.width)
        blocks.extend(detect_footnotes(build_text_blocks(lines[page.number], page.number),
                                      [c for word in page_words for c in word["chars"]],
                                      page_height=page.height))
    return [replace(block, order=index) for index, block in enumerate(blocks)], pages, lines


def make_native_owned_context_pdf(path, *, scale=1, **kwargs):
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.pdfbase.pdfmetrics import stringWidth
    canvas = Canvas(str(path), pagesize=(612 * scale, 792 * scale), invariant=1)
    for page in owned_context_records(**kwargs):
        for row in page:
            for word in _context_record_words(row, 1):
                font, size, text = word["fontname"], word["size"], word["text"]
                obj = canvas.beginText(word["x0"] * scale, (792 - word["top"] - size * .793) * scale)
                obj.setFont(font, size * scale)
                obj.setHorizScale((word["x1"] - word["x0"]) / stringWidth(text, font, size) * 100)
                obj.textLine(text)
                canvas.drawText(obj)
        canvas.showPage()
    canvas.save()
    return path


def make_observed_terminal_paragraphs(*, scale=1.0, margin=72, font="ObservedFace",
                                      texts=TERMINAL_PARAGRAPHS, problem=None):
    """Invented justified prose through grouping/classification/order/ownership."""
    from web_translator.pdf_layout import (
        build_text_blocks, classify_document_lines, classify_semantic_roles,
        group_words_into_lines, order_page_lines,
    )
    pages = [PdfPage(n, 612 * scale, 792 * scale, 0) for n in (1, 2)]
    raw = []
    for page, paragraph in zip(pages, texts):
        words = []
        for index, text in enumerate(paragraph):
            top = (600 if page.number == 1 else 72) + index * 18
            end = margin + (180 if index == len(paragraph) - 1 else 420)
            if page.number == 1:
                if problem == "filled" and index == len(paragraph) - 1:
                    end = margin + 420
                if problem == "ragged" and index < len(paragraph) - 1:
                    end -= index * 23
                if problem == "few-calibrated" and index < len(paragraph) - 2:
                    end -= 48
                if problem in {"irregular", "code-hardwrap", "poetry-hardwrap"} and index >= 2:
                    top += (index - 1) * 4
            words.append(dict(text=text, x0=margin * scale, x1=end * scale,
                              top=top * scale, bottom=(top + 12) * scale,
                              size=12 * scale, fontname=font,
                              chars=[{"text": char} for char in text if not char.isspace()]))
        raw.append((group_words_into_lines(words), page.height))
    classified = classify_semantic_roles(classify_document_lines(raw))
    blocks, lines = [], {}
    for page, members in zip(pages, classified):
        lines[page.number] = order_page_lines(members, page.width)
        blocks.extend(build_text_blocks(lines[page.number], page.number))
    return [replace(block, order=index) for index, block in enumerate(blocks)], pages, lines


def make_native_terminal_pdf(path, *, scale=1.0, margin=72, font="Helvetica", normalized=False):
    """Selectable justified paragraph endings, no simulated translation/approval."""
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.pdfbase.pdfmetrics import stringWidth
    paragraphs = TERMINAL_PARAGRAPHS
    if normalized:
        from pathlib import Path
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        font = "TerminalFixtureUnicode"
        pdfmetrics.registerFont(TTFont(font, str(
            Path(__file__).parents[1] / "src/web_translator/font_assets/NotoSansKR-Regular.ttf")))
        paragraphs = (("Opaque observations retain a careful para‐", "graph with independent measurements",
                       *paragraphs[0][2:]), paragraphs[1])
    canvas = Canvas(str(path), pagesize=(612 * scale, 792 * scale), invariant=1)
    for page, paragraph in enumerate(paragraphs):
        for index, text in enumerate(paragraph):
            top = (600 if page == 0 else 72) + index * 18
            width = 180 if index == len(paragraph) - 1 else 420
            obj = canvas.beginText(margin * scale, (792 - top - 9.516) * scale)
            obj.setFont(font, 12 * scale)
            obj.setHorizScale(width / stringWidth(text, font, 12) * 100)
            obj.textLine(text)
            canvas.drawText(obj)
        canvas.showPage()
    canvas.save()
    return path


def make_render_unit_run(root, *, case="joined"):
    """Selectable invented source plus fixed test-only Korean text and exact binding."""
    import hashlib
    import json
    from reportlab.pdfgen.canvas import Canvas
    from tests.pdf_fixtures import make_pdf_source_record
    from web_translator.models import Translation, write_segments
    from web_translator.pdf_extract import build_pdf_unit_segments

    run = root / "run"
    run.mkdir(parents=True)
    notes = case in {"multiple-note", "note-link-overlap"}
    colliding_labels = {
        "equal-links": ("Alpha", "Alpha"),
        "equal-links-fallback": ("Alpha", "Alpha"),
        "intersecting-links": ("Alpha Beta", "Beta Gamma"),
        "intersecting-links-reversed": ("Beta Gamma", "Alpha Beta"),
    }.get(case)
    texts = (("First member * continues", "second member * ends.") if notes
             else ("7. A paragraph" if case == "list" else "A paragraph", "continues."))
    if case == "links":
        texts = ("First Alpha continues", "second Beta ends.")
    if colliding_labels:
        texts = (f"First {colliding_labels[0]} and Delta continue", f"second {colliding_labels[1]} ends.")
    document = make_unit_document(texts, operation="space")
    blocks = document.blocks
    if case == "equal-links-fallback":
        blocks = [replace(blocks[0], uri="https://example.org/1"), blocks[1]]
    unit = document.translation_units[0]
    if case == "list":
        blocks = [replace(blocks[0], kind="list-item"), blocks[1]]
        unit = replace(unit, kind="list-item")
    units = [unit]
    if colliding_labels:
        document = replace(document, links=sorted([PdfLinkEvidence(
            id=f"pdf:page-{block.page_number:04d}:link-{2 if index == 2 else 1:04d}",
            page_number=block.page_number, source_block_id=block.id,
            source_span=(block.source_text.index(label), block.source_text.index(label) + len(label)),
            bounds=block.bbox, visible_label=label, uri=f"https://example.org/{index + 1}",
            destination=None, reconstructed=True, reason=None)
            for index, (block, label) in enumerate(zip(
                [blocks[0], blocks[1], blocks[0]], [*colliding_labels, "Delta"], strict=True))],
            key=lambda link: (link.page_number, link.id)))
        if case == "equal-links-fallback":
            document = replace(document, links=[link for link in document.links if link.uri != "https://example.org/3"])
    if case == "links":
        document = replace(document, links=[PdfLinkEvidence(
            id=f"pdf:page-{index + 1:04d}:link-0001", page_number=index + 1,
            source_block_id=block.id,
            source_span=(block.source_text.index(label), block.source_text.index(label) + len(label)),
            bounds=block.bbox, visible_label=label,
            uri="https://example.org/second" if index else None,
            destination=None if index else blocks[1].id, reconstructed=True, reason=None)
            for index, (block, label) in enumerate(zip(blocks, ("Alpha", "Beta"), strict=True))])
    if notes:
        body = [replace(block, order=index * 2,
                        destination=f"pdf:page-{index + 1:04d}:block-0002")
                for index, block in enumerate(blocks)]
        blocks = []
        for index, block in enumerate(body):
            note = replace(block, id=block.destination, order=block.order + 1,
                           kind="footnote", bbox=(72.0, 690.0, 540.0, 714.0),
                           source_text="* First note." if index == 0 else "* Second note.",
                           segment_id=None, destination=None)
            blocks.extend((block, note))
            units.append(PdfTranslationUnit(f"pdf:unit-{index + 2:06d}", (note.id,),
                                            "footnote", "body", None, ()))
        document = replace(document, links=[PdfLinkEvidence(
            id=f"pdf:page-{block.page_number:04d}:link-0001", page_number=block.page_number,
            source_block_id=block.id,
            source_span=(block.source_text.index("*"), block.source_text.index("*") + 1),
            bounds=block.bbox, visible_label="*", uri=None,
            destination=block.destination, reconstructed=True, reason=None)
            for block in body])
        if case == "note-link-overlap":
            document.links.insert(1, replace(document.links[0],
                id="pdf:page-0001:link-0002", source_span=(0, 14), visible_label="First member *",
                destination=None, uri="https://example.org/ordinary"))
    source_path = run / "source.pdf"
    canvas = Canvas(str(source_path), pagesize=(612, 792))
    for page in (1, 2):
        for block in blocks:
            if block.page_number == page:
                canvas.drawString(block.bbox[0], 792 - block.bbox[3], block.source_text)
        canvas.showPage()
    canvas.save()
    blocks, units, segments = build_pdf_unit_segments(blocks, units)
    document = replace(document, blocks=blocks, translation_units=units,
                       extracted_schema_version="1.2",
                       source_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest())
    document = PdfDocument.from_dict(document.to_dict())

    def write_json(path, data):
        path.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")

    write_json(run / "document.json", document.to_dict())
    write_json(run / "source.json", replace(make_pdf_source_record(),
               sha256=document.source_sha256, byte_length=source_path.stat().st_size).to_dict())
    write_segments(run / "segments.jsonl", segments)
    bind_render_unit_run(run)
    body_text = "문단 전체를 번역한 검증 문장"
    if case == "links":
        body_text += " Alpha 내부 참조와 Beta 외부 참조입니다."
    if colliding_labels:
        body_text += (" Alpha와 Delta 참조입니다." if case.startswith("equal-links")
                      else " Alpha Beta Gamma와 Delta 참조입니다.")
    if case == "list":
        body_text = segments[0].protected[0].token + ". " + body_text
    if notes:
        markers = [token.token for token in segments[0].protected if token.kind == "footnote-marker"]
        body_text += (" First member " if case == "note-link-overlap" else " 첫째표식 ") + markers[0] + " " + ("분할 확인을 위한 긴 본문입니다. " * 150)
        body_text += " 둘째표식 " + markers[1] + " 마지막 문장입니다."
    translations = {segments[0].id: Translation(segments[0].id, body_text)}
    for index, segment in enumerate(segments[1:]):
        translations[segment.id] = Translation(segment.id,
            segment.protected[0].token + (" 첫째 각주 내용입니다." if index == 0 else " 둘째 각주 내용입니다."))
    return run, document, segments, translations


def bind_render_unit_run(run):
    """Bind generated render fixtures using production exact-byte binding logic."""
    import json
    from web_translator.pdf_unit_bindings import build_pdf_unit_binding
    for directory in ("zones", "assignments"):
        (run / directory).mkdir()
        (run / directory / "zone-001.json").write_text('{"zone_id":"zone-001"}\n', encoding="utf-8")
    binding = build_pdf_unit_binding((run / "document.json").read_bytes(),
        (run / "segments.jsonl").read_bytes(),
        {"zone-001.json": (run / "zones/zone-001.json").read_bytes()},
        {"zone-001.json": (run / "assignments/zone-001.json").read_bytes()})
    (run / "assignments/.pdf-unit-binding.json").write_text(
        json.dumps(binding.to_dict()) + "\n", encoding="utf-8")


def write_native_singleton_fixture(run):
    """Explicit synthetic unit ownership for renderer tests, not producer evidence.

    Call only while constructing a fixture, before approval. Existing physical
    fields and Segment bytes remain unchanged; tests for stale input never call it.
    """
    import json
    from web_translator.pdf_units import TRANSLATABLE_KINDS
    value = json.loads((run / "document.json").read_bytes())
    units = [PdfTranslationUnit(f"pdf:unit-{index:06d}", (block.id,),
             block.kind, block.semantic_role, block.segment_id, ())
             for index, block in enumerate((PdfBlock.from_dict(block) for block in value["blocks"]
                 if block["kind"] in TRANSLATABLE_KINDS and block["source_text"].strip()), 1)]
    value.update(schema_version="1.2", extracted_schema_version="1.2",
                 translation_units=[unit.to_dict() for unit in units], flow_findings=[])
    (run / "document.json").write_text(json.dumps(value) + "\n", encoding="utf-8")
    rebind_native_fixture(run)


def replace_fixture_document(document, **changes):
    """Keep synthetic singleton ownership aligned with deliberate block edits."""
    from web_translator.pdf_units import TRANSLATABLE_KINDS
    updated = replace(document, **changes)
    units = [PdfTranslationUnit(f"pdf:unit-{index:06d}", (block.id,), block.kind,
             block.semantic_role, block.segment_id, ()) for index, block in enumerate(
                 (b for b in updated.blocks if b.kind in TRANSLATABLE_KINDS and b.source_text.strip()), 1)]
    return replace(updated, translation_units=units)


def rebind_native_fixture(run):
    """Bind deliberate synthetic setup changes before any fresh review."""
    import json
    from web_translator.pdf_unit_bindings import build_pdf_unit_binding
    for name in ("zones", "assignments"):
        directory = run / name
        if not directory.exists():
            directory.mkdir()
            (directory / "zone-001.json").write_text('{"zone_id":"zone-001"}\n', encoding="utf-8")
    binding = build_pdf_unit_binding((run / "document.json").read_bytes(),
        (run / "segments.jsonl").read_bytes(),
        {p.name: p.read_bytes() for p in (run / "zones").glob("zone-*.json")},
        {p.name: p.read_bytes() for p in (run / "assignments").glob("zone-*.json")})
    (run / "assignments/.pdf-unit-binding.json").write_text(json.dumps(binding.to_dict()) + "\n", encoding="utf-8")


def protect_fixture_note_markers(run, translations):
    """Make old renderer-only note data use native physical marker provenance."""
    import json
    from web_translator.models import ProtectedToken, read_segments, write_segments
    from web_translator.pdf_units import _unit_protection
    from web_translator.protection import restore_tokens
    document = PdfDocument.from_dict(json.loads((run / "document.json").read_bytes()))
    by_id = {block.id: block for block in document.blocks}
    by_segment = {unit.segment_id: unit for unit in document.translation_units}
    segments = read_segments(run / "segments.jsonl")
    for index, segment in enumerate(segments):
        unit = by_segment[segment.id]
        block = by_id[unit.source_block_ids[0]]
        if block.kind != "footnote" and not (block.destination in by_id and by_id[block.destination].kind == "footnote"):
            continue
        source, occurrences = _unit_protection(unit, by_id)
        raw = restore_tokens(translations[segment.id].text, segment.protected)
        for occurrence in occurrences:
            assert occurrence.value in raw
            raw = raw.replace(occurrence.value, occurrence.placeholder, 1)
        segments[index] = replace(segment, source_text=source, protected=[
            ProtectedToken(item.placeholder, item.kind, item.value) for item in occurrences])
        translations[segment.id] = replace(translations[segment.id], text=raw)
    (run / "segments.jsonl").unlink()
    write_segments(run / "segments.jsonl", segments)
    rebind_native_fixture(run)
