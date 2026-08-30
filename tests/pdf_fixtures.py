"""Small deterministic PDF-contract fixtures shared by PDF tests."""

from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import shutil
import tempfile

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    NameObject,
    NumberObject,
    TextStringObject,
)
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Table, TableStyle
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
from web_translator.pdf_extract import extract_pdf


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
    return make_pdf_at_size(path, 50 * 1024 * 1024 + 1)


def make_pdf_at_size(path: Path, byte_length: int) -> Path:
    """Create a valid PDF with an exact size and an immediately reachable EOF marker."""
    clear_path = path.with_name(f"{path.stem}-clear.pdf")
    make_text_pdf(clear_path)
    padding = 0
    for _ in range(4):
        reader = PdfReader(clear_path)
        writer = PdfWriter()
        writer.append(reader)
        unused_stream = DecodedStreamObject()
        unused_stream.set_data(b" " * padding)
        writer._add_object(unused_stream)
        with path.open("wb") as destination:
            writer.write(destination)
        difference = byte_length - path.stat().st_size
        if difference == 0:
            return path
        padding += difference
        if padding < 0:
            raise ValueError("requested PDF size is smaller than the fixture")
    raise AssertionError("could not create exact-size PDF fixture")


def make_truncated_eof_pdf(path: Path) -> Path:
    make_text_pdf(path)
    data = path.read_bytes().rstrip()
    if not data.endswith(b"%%EOF"):
        raise ValueError("fixture PDF does not end with %%EOF")
    path.write_bytes(data[:-1])
    return path


def make_inconsistent_page_tree_pdf(path: Path) -> Path:
    make_text_pdf(path)
    reader = PdfReader(path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    pages = writer._root_object["/Pages"].get_object()
    pages[NameObject("/Count")] = NumberObject(2)
    with path.open("wb") as destination:
        writer.write(destination)
    return path


def make_dimension_pdf(path: Path, *, width: float, height: float) -> Path:
    canvas = Canvas(str(path), pagesize=(width, height))
    canvas.drawString(0, max(0, height - 12), "x" * 100)
    canvas.save()
    return path


def make_image_text_pdf(
    path: Path,
    *,
    characters: int,
    image_x: float = 0,
    image_width: float = 612,
) -> Path:
    """Create a page with a precise text count and an image rectangle."""
    image_path = path.with_suffix(".png")
    Image.new("RGB", (612, 792), "white").save(image_path)
    canvas = Canvas(str(path), pagesize=(612, 792))
    canvas.drawImage(str(image_path), image_x, 0, width=image_width, height=792)
    canvas.drawString(72, 720, "x" * characters)
    canvas.save()
    return path


def make_string_catalog_type_pdf(path: Path) -> Path:
    make_text_pdf(path)
    reader = PdfReader(path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer._root_object[NameObject("/Type")] = TextStringObject("/Catalog")
    with path.open("wb") as destination:
        writer.write(destination)
    return path


def make_string_pages_type_pdf(path: Path) -> Path:
    make_text_pdf(path)
    reader = PdfReader(path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    pages = writer._root_object["/Pages"].get_object()
    pages[NameObject("/Type")] = TextStringObject("/Pages")
    with path.open("wb") as destination:
        writer.write(destination)
    return path


def make_nonzero_origin_image_pdf(path: Path) -> Path:
    make_image_text_pdf(path, characters=19, image_x=-100, image_width=306)
    reader = PdfReader(path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.pages[0][NameObject("/MediaBox")] = ArrayObject(
        [NumberObject(-100), NumberObject(-200), NumberObject(512), NumberObject(592)]
    )
    with path.open("wb") as destination:
        writer.write(destination)
    return path


PDF_ACCEPTANCE_FIXTURES = (
    "technical-document-v1",
    "table-report-v1",
    "two-column-footnotes-v1",
    "figures-captions-v1",
)

PDF_REJECTION_FIXTURE = "rejections-v1"

_SEMANTIC_DIMENSIONS = (
    "semantic_fidelity",
    "qualification_preservation",
    "naturalness",
    "terminology",
    "boundary_consistency",
    "protected_content",
)

_VISUAL_DIMENSIONS = (
    "heading_hierarchy",
    "text_legibility",
    "table_legibility",
    "figure_caption_pairing",
    "footnote_placement",
    "page_transitions",
    "clipping_overlap",
    "glyph_rendering",
)


def generate_acceptance_fixtures(root: Path) -> None:
    """Generate the complete, reproducible Task 12 PDF acceptance corpus."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    makers = {
        "technical-document-v1": _make_technical_document,
        "table-report-v1": _make_table_report,
        "two-column-footnotes-v1": _make_two_column_footnotes,
        "figures-captions-v1": _make_figures_captions,
    }
    for fixture_name, maker in makers.items():
        fixture_dir = root / fixture_name
        fixture_dir.mkdir()
        maker(fixture_dir / "source.pdf")
        _write_acceptance_sidecars(fixture_dir, fixture_name)
        _write_known_fixture_translations(fixture_dir)

    technical = root / "technical-document-v1" / "source.pdf"
    korean_copy = (
        root
        / "technical-document-v1"
        / "한국어 경로 with spaces"
        / "기술 문서 원본.pdf"
    )
    korean_copy.parent.mkdir()
    shutil.copyfile(technical, korean_copy)

    rejection_dir = root / PDF_REJECTION_FIXTURE
    rejection_dir.mkdir()
    _make_image_only_acceptance_pdf(rejection_dir / "image-only-scan.pdf")
    clear = rejection_dir / ".encrypted-clear.pdf"
    _make_technical_document(clear, pages=1)
    writer = PdfWriter()
    writer.append(PdfReader(clear))
    writer.encrypt("fixture-password")
    with (rejection_dir / "encrypted.pdf").open("wb") as destination:
        writer.write(destination)
    clear.unlink()
    (rejection_dir / "malformed.pdf").write_bytes(b"%PDF-1.7\nnot a valid PDF\n")
    _write_json(
        rejection_dir / "expected.json",
        {
            "encrypted.pdf": {"command": "pdf-extract", "exit_code": 4},
            "image-only-scan.pdf": {"command": "pdf-extract", "exit_code": 4},
            "malformed.pdf": {"command": "pdf-extract", "exit_code": 4},
            "schema_version": "1.0",
        },
    )


def _deterministic_canvas(path: Path, *, pagesize: tuple[float, float] = (612, 792)) -> Canvas:
    return Canvas(
        str(path),
        pagesize=pagesize,
        invariant=1,
        pageCompression=0,
    )


def _draw_lines(
    canvas: Canvas,
    lines: list[str],
    *,
    x: float,
    y: float,
    leading: float = 16.0,
    font: str = "Helvetica",
    size: float = 11.0,
) -> None:
    canvas.setFont(font, size)
    for line in lines:
        canvas.drawString(x, y, line)
        y -= leading


def _make_technical_document(path: Path, *, pages: int = 2) -> None:
    canvas = _deterministic_canvas(path)
    page_content = (
        (
            "Deterministic Systems Review",
            [
                "A deterministic workflow preserves source order, stable identifiers, and review evidence.",
                "The translation system validates every protected token before assembly begins.",
                "Reviewers compare semantic fidelity, terminology, and qualification preservation.",
                "The release artifact contains the translated PDF, manifest, and review report only.",
            ],
        ),
        (
            "Operational Verification",
            [
                "Automated checks confirm selectable Korean text, embedded fonts, and complete page renders.",
                "Contact sheets cover each output page exactly once and bind review to the staged digest.",
                "A failed check keeps private staging intact and never publishes a partial final directory.",
                "This fixture provides stable technical prose for repeatable end-to-end acceptance testing.",
            ],
        ),
    )
    for index in range(pages):
        title, lines = page_content[index % len(page_content)]
        canvas.setFont("Helvetica-Bold", 18)
        canvas.drawString(54, 734, title)
        _draw_lines(canvas, lines, x=54, y=690, leading=34)
        canvas.setFont("Helvetica", 9)
        canvas.drawRightString(558, 36, f"Page {index + 1}")
        canvas.showPage()
    canvas.save()


def _make_table_report(path: Path) -> None:
    canvas = _deterministic_canvas(path)
    for page_number, quarter in enumerate(("First half", "Second half"), start=1):
        canvas.setFont("Helvetica-Bold", 18)
        canvas.drawString(54, 744, "Translation Quality Table Report")
        canvas.setFont("Helvetica", 11)
        canvas.drawString(
            54,
            716,
            f"{quarter} measurements summarize deterministic acceptance outcomes for the release.",
        )
        data = [
            [f"{quarter} metrics", "", ""],
            ["Measure", "Observed", "Required"],
            ["Selectable characters", "240", "At least 100"],
            ["Reviewed pages", str(page_number), str(page_number)],
            ["Required findings", "0", "0"],
            ["Published artifacts", "3", "3"],
        ]
        table = Table(data, colWidths=(210, 120, 150), rowHeights=34)
        table.setStyle(
            TableStyle(
                [
                    ("SPAN", (0, 0), (2, 0)),
                    ("GRID", (0, 1), (-1, -1), 1, colors.black),
                    ("BOX", (0, 0), (-1, -1), 1, colors.black),
                    ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#E8EEF7")),
                    ("FONTNAME", (0, 0), (-1, 1), "Helvetica-Bold"),
                    ("FONTNAME", (0, 2), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        table.wrapOn(canvas, 480, 260)
        table.drawOn(canvas, 54, 456)
        _draw_lines(
            canvas,
            [
                "Merged header cells retain their logical span and all body rows remain readable.",
                "The same report structure continues on the next page without losing table evidence.",
            ],
            x=54,
            y=420,
            leading=24,
        )
        canvas.setFont("Helvetica", 9)
        canvas.drawRightString(558, 36, f"Page {page_number}")
        canvas.showPage()
    canvas.save()


def _make_two_column_footnotes(path: Path) -> None:
    canvas = _deterministic_canvas(path)
    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(54, 744, "Two-Column Evidence Review")
    canvas.setFont("Helvetica", 10)
    for index, x_position in enumerate((54, 330), start=1):
        canvas.drawString(
            x_position,
            700,
            f"Column {index} first logical sentence.",
        )
        canvas.drawString(
            x_position,
            680,
            f"Column {index} second logical sentence.",
        )
    canvas.showPage()

    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(54, 744, "Page-Local Footnote Evidence")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(54, 690, "Source order is validated before bounded zone planning begins.")
    canvas.drawString(54, 648, "Contact-sheet evidence covers every rendered output page exactly once.")
    canvas.drawString(54, 606, "Semantic review checks every required quality dimension.")
    canvas.drawString(54, 578, "Validated Korean text remains selectable in the staged PDF.")
    canvas.drawString(54, 550, "Automated QA records exact output and contact-sheet page counts.")
    canvas.drawString(54, 522, "Final publication exposes exactly three reviewed artifacts.")
    canvas.drawString(72, 270, "The deterministic workflow includes a page-local note")
    canvas.setFont("Helvetica", 7)
    canvas.drawString(335, 275, "1")
    canvas.drawString(72, 40, "1 Footnote evidence remains linked to its marker.")
    canvas.showPage()
    canvas.save()


def _make_figures_captions(path: Path) -> None:
    canvas = _deterministic_canvas(path)
    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(54, 744, "Figures and Captions")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(
        54,
        716,
        "Raster and vector evidence must remain paired with the correct explanatory caption.",
    )

    raster = Image.new("RGB", (240, 110), (42, 114, 168))
    buffer = BytesIO()
    raster.save(buffer, format="PNG", optimize=False, compress_level=9)
    buffer.seek(0)
    canvas.drawImage(ImageReader(buffer), 54, 518, width=220, height=110)
    canvas.setFont("Helvetica", 9)
    canvas.drawString(54, 498, "Figure 1. Raster workflow status panel.")

    canvas.setStrokeColor(colors.HexColor("#1F4E79"))
    canvas.setFillColor(colors.HexColor("#D9EAF7"))
    canvas.rect(54, 314, 220, 110, fill=1, stroke=1)
    canvas.setStrokeColor(colors.HexColor("#C0504D"))
    canvas.setLineWidth(4)
    canvas.line(74, 338, 112, 368)
    canvas.line(112, 368, 154, 352)
    canvas.line(154, 352, 198, 402)
    canvas.line(198, 402, 254, 384)
    canvas.setFillColor(colors.black)
    canvas.setFont("Helvetica", 9)
    canvas.drawString(54, 294, "Figure 2. Vector review coverage trend.")
    _draw_lines(
        canvas,
        [
            "The raster panel verifies image preservation and the vector plot verifies path rendering.",
            "Each caption follows its figure directly so extraction retains an unambiguous pair.",
            "Review checks sharp rendering, readable labels, and the absence of clipping or overlap.",
        ],
        x=54,
        y=250,
        leading=26,
        size=10,
    )
    canvas.setFont("Helvetica", 9)
    canvas.drawRightString(558, 36, "Page 1")
    canvas.showPage()
    canvas.save()


def _make_image_only_acceptance_pdf(path: Path) -> None:
    canvas = _deterministic_canvas(path)
    image = Image.new("RGB", (612, 792), "white")
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=9)
    buffer.seek(0)
    canvas.drawImage(ImageReader(buffer), 0, 0, width=612, height=792)
    canvas.showPage()
    canvas.save()


def _write_acceptance_sidecars(fixture_dir: Path, fixture_name: str) -> None:
    expected = {
        "schema_version": "1.0",
        "fixture": fixture_name,
        "page_count": 2 if fixture_name in {
            "technical-document-v1",
            "table-report-v1",
            "two-column-footnotes-v1",
        } else 1,
        "table_count": 2 if fixture_name == "table-report-v1" else 0,
        "figure_count": 2 if fixture_name == "figures-captions-v1" else 0,
        "footnote_count": 1 if fixture_name == "two-column-footnotes-v1" else 0,
        "expected_korean_phrase": {
            "technical-document-v1": "결정론적 시스템 검토",
            "table-report-v1": "상반기 측정값은 릴리스의 결정론적 승인 결과를 요약합니다.",
            "two-column-footnotes-v1": "두 열 증거 검토",
            "figures-captions-v1": "그림과 캡션",
        }[fixture_name],
        "final_artifacts": ["manifest.json", "review-report.md", "translated.pdf"],
    }
    _write_json(fixture_dir / "expected.json", expected)
    _write_json(fixture_dir / "glossary.json", {"workflow": "작업 흐름"})
    (fixture_dir / "document-summary.txt").write_text(
        f"{fixture_name} 결정론적 PDF 번역 승인 문서",
        encoding="utf-8",
        newline="\n",
    )
    _write_json(
        fixture_dir / "review.json",
        {
            "retries": {"zone-001": 0},
            "section_findings": {
                "zone-001": [
                    {
                        "dimension": dimension,
                        "verdict": "pass",
                        "evidence": f"Fixture review passed {dimension}.",
                    }
                    for dimension in _SEMANTIC_DIMENSIONS
                ]
            },
            "unresolved_required": [],
        },
    )
    _write_json(
        fixture_dir / "visual-review.json",
        {
            "schema_version": "1.0",
            "staged_pdf_sha256": "0" * 64,
            "pages_reviewed": [1],
            "contact_sheets_reviewed": {
                "contact-sheet-001.png": [1]
            },
            "findings": {
                dimension: {
                    "verdict": "pass",
                    "evidence": f"Fixture visual review passed {dimension}.",
                }
                for dimension in _VISUAL_DIMENSIONS
            },
            "unresolved_required": [],
        },
    )


def _write_known_fixture_translations(fixture_dir: Path) -> None:
    """Store reviewed Korean zone results matched to deterministic source IDs."""
    with tempfile.TemporaryDirectory(prefix="pdf-fixture-") as temporary_name:
        temporary = Path(temporary_name)
        extract_pdf(
            fixture_dir / "source.pdf",
            temporary / "document.json",
            temporary / "segments.jsonl",
            temporary / "media",
        )
        records = [
            json.loads(line)
            for line in (temporary / "segments.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    translations = [
        {
            "segment_id": record["id"],
            "text": _FIXTURE_KOREAN_TRANSLATIONS[record["source_text"]]
            + "".join(token["token"] for token in record["protected"]),
            "notes": None,
            "glossary_observations": {},
        }
        for record in records
        if record["target"]
    ]
    if len(translations) != sum(record["target"] for record in records):
        raise AssertionError("fixture translation mapping must cover every target")
    translations_dir = fixture_dir / "translations"
    translations_dir.mkdir()
    (translations_dir / "zone-001.jsonl").write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in translations
        ),
        encoding="utf-8",
        newline="\n",
    )


_FIXTURE_KOREAN_TRANSLATIONS = {
    "Deterministic Systems Review": "결정론적 시스템 검토",
    "Operational Verification": "운영 검증",
    "A deterministic workflow preserves source order, stable identifiers, and review evidence.": "결정론적 작업 흐름은 원본 순서, 안정적인 식별자 및 검토 증거를 보존합니다.",
    "The translation system validates every protected token before assembly begins.": "번역 시스템은 조립을 시작하기 전에 모든 보호 토큰을 검증합니다.",
    "Reviewers compare semantic fidelity, terminology, and qualification preservation.": "검토자는 의미 충실도, 용어 및 한정 표현 보존을 비교합니다.",
    "The release artifact contains the translated PDF, manifest, and review report only.": "릴리스 산출물에는 번역된 PDF, 매니페스트 및 검토 보고서만 포함됩니다.",
    "Automated checks confirm selectable Korean text, embedded fonts, and complete page renders.": "자동 검사는 선택 가능한 한국어 텍스트, 포함된 글꼴 및 완전한 페이지 렌더링을 확인합니다.",
    "Contact sheets cover each output page exactly once and bind review to the staged digest.": "연락처 시트는 각 출력 페이지를 정확히 한 번 포함하고 검토를 스테이징 다이제스트에 연결합니다.",
    "A failed check keeps private staging intact and never publishes a partial final directory.": "실패한 검사는 비공개 스테이징을 그대로 유지하고 부분 최종 디렉터리를 게시하지 않습니다.",
    "This fixture provides stable technical prose for repeatable end-to-end acceptance testing.": "이 픽스처는 반복 가능한 종단 간 승인 테스트를 위한 안정적인 기술 문서를 제공합니다.",
    "Figures and Captions": "그림과 캡션",
    "Raster and vector evidence must remain paired with the correct explanatory caption.": "래스터 및 벡터 증거는 올바른 설명 캡션과 연결된 상태를 유지해야 합니다.",
    "Figure 1. Raster workflow status panel.": "그림 1. 래스터 작업 흐름 상태 패널.",
    "Figure 2. Vector review coverage trend.": "그림 2. 벡터 검토 범위 추세.",
    "The raster panel verifies image preservation and the vector plot verifies path rendering. Each caption follows its figure directly so extraction retains an unambiguous pair. Review checks sharp rendering, readable labels, and the absence of clipping or overlap.": "래스터 패널은 이미지 보존을 검증하고 벡터 그래프는 경로 렌더링을 검증합니다. 각 캡션은 해당 그림 바로 뒤에 배치되어 추출 시 모호하지 않은 쌍을 유지합니다. 검토는 선명한 렌더링, 읽기 쉬운 레이블 및 잘림이나 겹침이 없음을 확인합니다.",
    "Two-Column Evidence Review": "두 열 증거 검토", "Column 1 first logical sentence. Column 1 second logical sentence.": "첫 번째 열의 첫 논리 문장입니다. 첫 번째 열의 두 번째 논리 문장입니다.", "Column 2 first logical sentence. Column 2 second logical sentence.": "두 번째 열의 첫 논리 문장입니다. 두 번째 열의 두 번째 논리 문장입니다.",
    "Page-Local Footnote Evidence": "페이지 내 각주 증거", "Source order is validated before bounded zone planning begins.": "제한된 영역 계획을 시작하기 전에 원본 순서를 검증합니다.", "Contact-sheet evidence covers every rendered output page exactly once.": "연락처 시트는 렌더링된 각 출력 페이지를 정확히 한 번 포함합니다.", "Semantic review checks every required quality dimension.": "의미 검토는 모든 필수 품질 기준을 확인합니다.", "Validated Korean text remains selectable in the staged PDF.": "검증된 한국어 텍스트는 스테이징 PDF에서 선택 가능한 상태로 유지됩니다.", "Automated QA records exact output and contact-sheet page counts.": "자동 QA는 정확한 출력 및 연락처 시트 페이지 수를 기록합니다.", "Final publication exposes exactly three reviewed artifacts.": "최종 게시에는 검토된 세 가지 산출물만 공개됩니다.", "The deterministic workflow includes a page-local note 1": "결정론적 작업 흐름에는 페이지 내 주석 1이 포함됩니다.", "1 Footnote evidence remains linked to its marker.": "1 각주 증거는 해당 표식에 연결된 상태로 유지됩니다.",
    "Semantic review checks every required quality dimension. Validated Korean text remains selectable in the staged PDF. Automated QA records exact output and contact-sheet page counts. Final publication exposes exactly three reviewed artifacts.": "의미 검토는 모든 필수 품질 기준을 확인하며, 검증된 한국어 텍스트는 스테이징 PDF에서 선택 가능하게 유지됩니다. 자동 QA는 정확한 출력 및 연락처 시트 페이지 수를 기록하고 최종 게시에는 검토된 세 가지 산출물만 공개됩니다.",
    "First half measurements summarize deterministic acceptance outcomes for the release.": "상반기 측정값은 릴리스의 결정론적 승인 결과를 요약합니다.", "Second half measurements summarize deterministic acceptance outcomes for the release.": "하반기 측정값은 릴리스의 결정론적 승인 결과를 요약합니다.", "First half metrics": "상반기 지표", "Second half metrics": "하반기 지표", "Measure": "측정 항목", "Observed": "관찰값", "Required": "필수값", "Selectable characters": "선택 가능한 문자", "At least 100": "최소 100개", "Reviewed pages": "검토한 페이지", "Required findings": "필수 지적 사항", "Published artifacts": "게시된 산출물", "Merged header cells retain their logical span and all body rows remain readable. The same report structure continues on the next page without losing table evidence.": "병합된 머리글 셀은 논리적 범위를 유지하고 모든 본문 행은 계속 읽을 수 있습니다. 동일한 보고서 구조는 표 증거를 잃지 않고 다음 페이지로 이어집니다.",
    "0": "0", "1": "1", "2": "2", "3": "3", "240": "240", "Page 1": "1쪽", "Page 2": "2쪽",
}
def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
