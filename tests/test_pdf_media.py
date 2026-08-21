"""Tests for deterministic Poppler rendering and source-figure crops."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
import pytest
from reportlab.pdfgen.canvas import Canvas


def _graphic_pdf(path: Path) -> Path:
    canvas = Canvas(str(path), pagesize=(200, 100))
    canvas.setFillColorRGB(0.1, 0.3, 0.8)
    canvas.rect(50, 25, 100, 50, stroke=0, fill=1)
    canvas.save()
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
