"""Tests for deterministic Poppler rendering and source-figure crops."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, NameObject, NumberObject
import pytest
from reportlab.pdfgen.canvas import Canvas


def _graphic_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(200, 100))
    canvas.setFillColorRGB(0.1, 0.3, 0.8)
    canvas.rect(50, 25, 100, 50, stroke=0, fill=1)
    canvas.save()
    return path


def _nonzero_origin_raster_pdf(path: Path) -> Path:
    image_path = path.with_suffix(".png")
    Image.new("RGB", (100, 50), (12, 34, 56)).save(image_path)
    canvas = Canvas(str(path), pagesize=(200, 100))
    canvas.drawImage(str(image_path), -50, -25, width=100, height=50)
    canvas.save()

    reader = PdfReader(path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    bounds = ArrayObject(
        [NumberObject(-50), NumberObject(-25), NumberObject(150), NumberObject(75)]
    )
    writer.pages[0][NameObject("/MediaBox")] = bounds
    writer.pages[0][NameObject("/CropBox")] = ArrayObject(bounds)
    with path.open("wb") as destination:
        writer.write(destination)
    return path


def _raster_with_single_attached_vector_pdf(path: Path) -> Path:
    image_path = path.with_suffix(".png")
    Image.new("RGB", (50, 40), (90, 80, 70)).save(image_path)
    canvas = Canvas(str(path), pagesize=(200, 100))
    canvas.drawImage(str(image_path), 50, 30, width=50, height=40)
    canvas.line(100, 50, 130, 50)
    canvas.save()
    return path


def _nonzero_origin_vector_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(200, 100))
    canvas.line(-40, 0, 0, 30)
    canvas.line(0, 30, 40, 0)
    canvas.save()
    reader = PdfReader(path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    bounds = ArrayObject(
        [NumberObject(-50), NumberObject(-25), NumberObject(150), NumberObject(75)]
    )
    writer.pages[0][NameObject("/MediaBox")] = bounds
    writer.pages[0][NameObject("/CropBox")] = ArrayObject(bounds)
    with path.open("wb") as destination:
        writer.write(destination)
    return path


def test_find_poppler_returns_absolute_executable_paths() -> None:
    from web_translator.pdf_media import find_poppler

    tools = find_poppler()

    assert tools.pdfinfo.is_absolute()
    assert tools.pdfinfo.name == "pdfinfo"
    assert tools.pdftoppm.is_absolute()
    assert tools.pdftoppm.name == "pdftoppm"


def test_find_poppler_reports_both_required_commands_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import web_translator.pdf_media as media_module
    from web_translator.pdf_media import PdfMediaError

    monkeypatch.setattr(media_module.shutil, "which", lambda name: None)

    with pytest.raises(PdfMediaError, match=r"pdfinfo.*pdftoppm.*install Poppler"):
        media_module.find_poppler()


def test_find_poppler_wraps_stale_path_evidence_in_media_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import web_translator.pdf_media as media_module
    from web_translator.pdf_media import PdfMediaError

    monkeypatch.setattr(
        media_module.shutil,
        "which",
        lambda name: str(tmp_path / name),
    )

    with pytest.raises(PdfMediaError, match="cannot resolve Poppler commands"):
        media_module.find_poppler()


def test_render_pdf_pages_uses_fixed_dpi_and_returns_png_pages(tmp_path: Path) -> None:
    from web_translator.pdf_media import render_pdf_pages

    pages = render_pdf_pages(
        _graphic_pdf(tmp_path / "graphic.pdf"),
        tmp_path / "rendered",
        dpi=144,
    )

    assert [path.name for path in pages] == ["page-1.png"]
    with Image.open(pages[0]) as image:
        assert image.format == "PNG"
        assert image.size == (400, 200)


@pytest.mark.parametrize(
    ("pixels", "encoded_bytes", "message"),
    [
        ([36_000_001], [1], "pixels per page"),
        ([36_000_000] * 10 + [1], [1] * 11, "total rendered pixels"),
        ([1], [64 * 1024 * 1024 + 1], "encoded bytes per page"),
        ([1] * 17, [64 * 1024 * 1024] * 16 + [1], "encoded bytes total"),
    ],
)
def test_render_budget_rejects_one_unit_above_each_exact_limit(
    pixels: list[int],
    encoded_bytes: list[int],
    message: str,
) -> None:
    import web_translator.pdf_media as media_module

    with pytest.raises(media_module.PdfMediaError, match=message):
        media_module._validate_render_budget_counts(pixels, encoded_bytes)


def test_render_budget_accepts_exact_limits_and_one_unit_below() -> None:
    import web_translator.pdf_media as media_module

    media_module._validate_render_budget_counts(
        [36_000_000] * 10,
        [1] * 10,
    )
    media_module._validate_render_budget_counts(
        [1] * 16,
        [64 * 1024 * 1024] * 16,
    )
    media_module._validate_render_budget_counts(
        [35_999_999] * 10,
        [1] * 10,
    )
    media_module._validate_render_budget_counts(
        [1] * 16,
        [64 * 1024 * 1024 - 1] * 16,
    )


def test_render_rejects_oversized_geometry_before_locating_poppler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import web_translator.pdf_media as media_module

    source = tmp_path / "oversized.pdf"
    canvas = Canvas(str(source), pagesize=(14_401, 792))
    canvas.drawString(72, 720, "oversized render geometry")
    canvas.save()

    def poppler_must_not_run() -> object:
        raise AssertionError("geometry budget must reject before Poppler lookup")

    monkeypatch.setattr(media_module, "find_poppler", poppler_must_not_run)

    with pytest.raises(media_module.PdfMediaError, match="pixels per page"):
        media_module.render_pdf_pages(source, tmp_path / "rendered", dpi=144)


def test_render_geometry_normalizes_strict_pdf_parser_failures(
    tmp_path: Path,
) -> None:
    import web_translator.pdf_media as media_module

    source = tmp_path / "malformed.pdf"
    source.write_bytes(b"not a PDF")

    with pytest.raises(media_module.PdfMediaError, match="inspect PDF render geometry"):
        media_module.render_pdf_pages(source, tmp_path / "rendered", dpi=144)


def test_render_rejects_oversized_user_unit_before_locating_poppler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import FloatObject, NameObject

    import web_translator.pdf_media as media_module

    source = tmp_path / "oversized-user-unit.pdf"
    canvas = Canvas(str(source), pagesize=(612, 792))
    canvas.drawString(72, 720, "ordinary media box with an extreme user unit")
    canvas.save()
    reader = PdfReader(source)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.pages[0][NameObject("/UserUnit")] = FloatObject(100.0)
    with source.open("wb") as stream:
        writer.write(stream)

    def poppler_must_not_run() -> object:
        raise AssertionError("geometry budget must reject before Poppler lookup")

    monkeypatch.setattr(media_module, "find_poppler", poppler_must_not_run)

    with pytest.raises(media_module.PdfMediaError, match="pixels per page"):
        media_module.render_pdf_pages(source, tmp_path / "rendered", dpi=144)


def test_render_preflight_budgets_default_poppler_media_box_not_crop_box(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, FloatObject, NameObject

    import web_translator.pdf_media as media_module

    source = tmp_path / "media-box-over-budget.pdf"
    canvas = Canvas(str(source), pagesize=(100, 100))
    canvas.drawString(10, 50, "small crop inside huge media surface")
    canvas.save()
    reader = PdfReader(source)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    page = writer.pages[0]
    page[NameObject("/MediaBox")] = ArrayObject(
        [FloatObject(0), FloatObject(0), FloatObject(100_000), FloatObject(100_000)]
    )
    page[NameObject("/CropBox")] = ArrayObject(
        [FloatObject(0), FloatObject(0), FloatObject(100), FloatObject(100)]
    )
    with source.open("wb") as stream:
        writer.write(stream)

    def poppler_must_not_run() -> object:
        raise AssertionError("MediaBox budget must reject before Poppler lookup")

    monkeypatch.setattr(media_module, "find_poppler", poppler_must_not_run)

    with pytest.raises(media_module.PdfMediaError, match="pixels per page"):
        media_module.render_pdf_pages(source, tmp_path / "rendered", dpi=144)


def test_crop_figure_regions_keeps_rendered_pixels_and_exact_dimensions(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_media import FigureRegion, crop_figure_regions

    crops = crop_figure_regions(
        _graphic_pdf(tmp_path / "source.pdf"),
        [
            FigureRegion(
                page_number=1,
                bbox=(50.0, 25.0, 150.0, 75.0),
                page_width=200.0,
                page_height=100.0,
            )
        ],
        tmp_path / "media",
        dpi=144,
    )

    assert [path.name for path in crops] == ["figure-0001.png"]
    with Image.open(crops[0]) as image:
        assert image.size == (200, 100)
        assert image.getpixel((100, 50)) == (25, 76, 204)


def test_detected_raster_crop_normalizes_nonzero_page_box_origin(
    tmp_path: Path,
) -> None:
    import pdfplumber

    from web_translator.pdf_media import crop_figure_regions, detect_figure_regions

    source = _nonzero_origin_raster_pdf(tmp_path / "nonzero-origin.pdf")
    with pdfplumber.open(source) as document:
        regions = detect_figure_regions(document.pages[0], page_number=1)

    assert [region.bbox for region in regions] == [(-50.0, 75.0, 50.0, 125.0)]
    crops = crop_figure_regions(source, regions, tmp_path / "nonzero-media")
    with Image.open(crops[0]) as image:
        assert image.size == (200, 100)
        assert image.getpixel((100, 50)) == (12, 34, 56)


def test_raster_crop_includes_one_connected_vector_object(tmp_path: Path) -> None:
    import pdfplumber

    from web_translator.pdf_media import crop_figure_regions, detect_figure_regions

    source = _raster_with_single_attached_vector_pdf(tmp_path / "attached-vector.pdf")
    with pdfplumber.open(source) as document:
        regions = detect_figure_regions(document.pages[0], page_number=1)

    assert [region.bbox for region in regions] == [(50.0, 30.0, 130.0, 70.0)]
    crops = crop_figure_regions(source, regions, tmp_path / "attached-vector-media")
    with Image.open(crops[0]) as image:
        assert image.size == (160, 80)


def test_detected_vector_crop_normalizes_nonzero_page_box_origin(
    tmp_path: Path,
) -> None:
    import pdfplumber

    from web_translator.pdf_media import crop_figure_regions, detect_figure_regions

    source = _nonzero_origin_vector_pdf(tmp_path / "nonzero-vector.pdf")
    with pdfplumber.open(source) as document:
        regions = detect_figure_regions(document.pages[0], page_number=1)

    assert [region.bbox for region in regions] == [(-40.0, 70.0, 40.0, 100.0)]
    crops = crop_figure_regions(source, regions, tmp_path / "nonzero-vector-media")
    with Image.open(crops[0]) as image:
        assert image.size == (160, 60)


def test_crop_figure_regions_rejects_a_region_outside_the_rendered_page(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_media import FigureRegion, PdfMediaError, crop_figure_regions

    with pytest.raises(PdfMediaError, match="cannot crop figure region"):
        crop_figure_regions(
            _graphic_pdf(tmp_path / "source.pdf"),
            [
                FigureRegion(
                    page_number=1,
                    bbox=(250.0, 20.0, 300.0, 60.0),
                    page_width=200.0,
                    page_height=100.0,
                )
            ],
            tmp_path / "media",
        )


def test_crop_figure_regions_cleans_first_root_when_second_allocation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tempfile

    import web_translator.pdf_media as media_module
    from web_translator.pdf_media import FigureRegion, PdfMediaError

    real_mkdtemp = tempfile.mkdtemp
    calls = 0

    def fail_second_mkdtemp(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("crop root allocation failed")
        return real_mkdtemp(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(media_module.tempfile, "mkdtemp", fail_second_mkdtemp)
    destination = tmp_path / "new-parent" / "media"

    with pytest.raises(PdfMediaError, match="crop root allocation failed"):
        media_module.crop_figure_regions(
            _graphic_pdf(tmp_path / "source.pdf"),
            [FigureRegion(1, (50, 25, 150, 75), 200, 100)],
            destination,
        )

    assert not list(destination.parent.glob(".pdf-render-*"))
    assert not destination.parent.exists()


def test_crop_figure_regions_cleans_first_root_when_second_allocation_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tempfile

    import web_translator.pdf_media as media_module
    from web_translator.pdf_media import FigureRegion

    real_mkdtemp = tempfile.mkdtemp
    calls = 0

    def interrupt_second_mkdtemp(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return real_mkdtemp(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(media_module.tempfile, "mkdtemp", interrupt_second_mkdtemp)
    destination = tmp_path / "new-parent" / "media"

    with pytest.raises(KeyboardInterrupt):
        media_module.crop_figure_regions(
            _graphic_pdf(tmp_path / "source.pdf"),
            [FigureRegion(1, (50, 25, 150, 75), 200, 100)],
            destination,
        )

    assert not list(destination.parent.glob(".pdf-render-*"))
    assert not destination.parent.exists()
