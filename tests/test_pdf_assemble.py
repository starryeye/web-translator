from __future__ import annotations

from dataclasses import replace
import hashlib
from importlib.resources import as_file, files
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

from fontTools.ttLib import TTFont
import pdfplumber
from PIL import Image
from pypdf import PdfReader
import pytest

import scripts.vendor_pdf_fonts as font_vendor
import web_translator.pdf_assemble as pdf_assemble_module
from web_translator.models import (
    ProtectedToken,
    Segment,
    Translation,
    read_segments,
    write_segments,
)
from web_translator.pdf_assemble import PdfAssemblyError, assemble_pdf
from web_translator.pdf_flowables import read_pdf_layout
from web_translator.pdf_models import (
    PdfBlock,
    PdfBlockStyle,
    PdfDocument,
    PdfPage,
    PdfSourceRecord,
    PdfTableCell,
)


ROOT = Path(__file__).parents[1]
FONT_SOURCE_URL = (
    "https://raw.githubusercontent.com/notofonts/noto-cjk/"
    "f8d157532fbfaeda587e826d4cd5b21a49186f7c/"
    "Sans/Variable/TTF/NotoSansCJKkr-VF.ttf"
)
FONT_SOURCE_SHA256 = "7715af52f5fe77153ce5678546258993982d2da61abea8d25fb89eb5aaec5ca6"
PLANNED_LICENSE_URL = (
    "https://raw.githubusercontent.com/notofonts/noto-cjk/"
    "f8d157532fbfaeda587e826d4cd5b21a49186f7c/LICENSE"
)
FONT_LICENSE_URL = (
    "https://raw.githubusercontent.com/notofonts/noto-cjk/"
    "f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/LICENSE"
)
FONT_LICENSE_SHA256 = "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"
UNICODE_RANGES = [
    {"name": "ASCII", "start": "U+0000", "end": "U+007F"},
    {"name": "Latin-1", "start": "U+0080", "end": "U+00FF"},
    {"name": "Hangul Jamo", "start": "U+1100", "end": "U+11FF"},
    {"name": "General Punctuation", "start": "U+2000", "end": "U+206F"},
    {"name": "Currency Symbols", "start": "U+20A0", "end": "U+20CF"},
    {"name": "Arrows", "start": "U+2190", "end": "U+21FF"},
    {"name": "CJK Symbols and Punctuation", "start": "U+3000", "end": "U+303F"},
    {"name": "Hangul Compatibility Jamo", "start": "U+3130", "end": "U+318F"},
    {"name": "Hangul Syllables", "start": "U+AC00", "end": "U+D7A3"},
]


def _assembly_run(
    root: Path,
    *,
    width: float = 612.0,
    height: float = 792.0,
) -> tuple[Path, dict[str, Translation], dict[str, str]]:
    run_dir = root / "작업 경로 with spaces" / "run"
    run_dir.mkdir(parents=True)
    style = PdfBlockStyle(
        font_size=11.0,
        bold=False,
        alignment="left",
        indentation=0.0,
        space_after=8.0,
    )
    blocks = [
        PdfBlock(
            id="pdf:page-0001:block-0001",
            page_number=1,
            order=0,
            kind="heading",
            bbox=(72.0, 72.0, 540.0, 96.0),
            style=PdfBlockStyle(18.0, True, "left", 0.0, 12.0),
            source_text="Overview",
            segment_id="seg-000001",
        ),
        PdfBlock(
            id="pdf:page-0001:block-0002",
            page_number=1,
            order=1,
            kind="paragraph",
            bbox=(72.0, 108.0, 540.0, 144.0),
            style=style,
            source_text="Korean body with OAuth and ⟦WT:000001⟧.",
            segment_id="seg-000002",
        ),
        PdfBlock(
            id="pdf:page-0001:block-0003",
            page_number=1,
            order=2,
            kind="list-item",
            bbox=(90.0, 156.0, 540.0, 180.0),
            style=PdfBlockStyle(11.0, False, "left", 18.0, 5.0),
            source_text="First ⟦WT:000002⟧",
            segment_id="seg-000003",
        ),
    ]
    document = PdfDocument(
        schema_version="1.0",
        source_sha256="a" * 64,
        page_count=1,
        selectable_characters=64,
        scan_candidate_pages=[],
        pages=[PdfPage(number=1, width=width, height=height, rotation=0)],
        blocks=blocks,
        table_cells=[],
    )
    (run_dir / "document.json").write_text(
        json.dumps(document.to_dict(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    source = PdfSourceRecord(
        schema_version="1.0",
        input_kind="local",
        requested_source="기술 보고서.pdf",
        final_source="기술 보고서.pdf",
        content_type="application/pdf",
        byte_length=123,
        sha256="a" * 64,
        acquired_at="2026-08-21T01:02:03Z",
        redirects=[],
        warnings=[],
    )
    (run_dir / "source.json").write_text(
        json.dumps(source.to_dict(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    segments = [
        Segment(
            id="seg-000001",
            locator=blocks[0].id,
            semantic_type="heading",
            heading_path=["Overview"],
            source_text=blocks[0].source_text,
            protected=[],
            context_ids=["seg-000002"],
            target=True,
        ),
        Segment(
            id="seg-000002",
            locator=blocks[1].id,
            semantic_type="paragraph",
            heading_path=["Overview"],
            source_text=blocks[1].source_text,
            protected=[
                ProtectedToken("⟦WT:000001⟧", "identifier", "client<id>")
            ],
            context_ids=["seg-000001", "seg-000003"],
            target=True,
        ),
        Segment(
            id="seg-000003",
            locator=blocks[2].id,
            semantic_type="list-item",
            heading_path=["Overview"],
            source_text=blocks[2].source_text,
            protected=[
                ProtectedToken("⟦WT:000002⟧", "identifier", "<둘째>")
            ],
            context_ids=["seg-000002"],
            target=True,
        ),
    ]
    write_segments(run_dir / "segments.jsonl", segments)
    translations = {
        "seg-000001": Translation("seg-000001", "<안내 & 개요>"),
        "seg-000002": Translation(
            "seg-000002", "한국어 본문 & OAuth ⟦WT:000001⟧."
        ),
        "seg-000003": Translation("seg-000003", "첫째 ⟦WT:000002⟧"),
    }
    return run_dir, translations, {"OAuth": "권한 위임"}


def _rich_assembly_run(
    root: Path,
    *,
    table_columns: int = 10,
    table_rows: int = 36,
) -> tuple[Path, dict[str, Translation], dict[str, str], dict[str, str]]:
    run_dir = root / f"rich-{table_columns}-columns" / "run"
    media_dir = run_dir / "media"
    media_dir.mkdir(parents=True)
    Image.new("RGB", (240, 120), (42, 120, 196)).save(
        media_dir / "figure-0001.png"
    )
    style = PdfBlockStyle(11.0, False, "left", 0.0, 6.0)
    blocks: list[PdfBlock] = []
    table_cells: list[PdfTableCell] = []
    segments: list[Segment] = []
    translations: dict[str, Translation] = {}
    page_block_numbers = {1: 0, 2: 0}

    def add_block(
        *,
        page: int,
        kind: str,
        source: str,
        translated: str | None,
        bbox: tuple[float, float, float, float],
        block_style: PdfBlockStyle = style,
        **relationships: object,
    ) -> PdfBlock:
        page_block_numbers[page] += 1
        identifier = (
            f"pdf:page-{page:04d}:block-{page_block_numbers[page]:04d}"
        )
        segment_id = f"seg-{len(segments) + 1:06d}" if translated is not None else None
        block = PdfBlock(
            id=identifier,
            page_number=page,
            order=len(blocks),
            kind=kind,  # type: ignore[arg-type]
            bbox=bbox,
            style=block_style,
            source_text=source,
            segment_id=segment_id,
            **relationships,  # type: ignore[arg-type]
        )
        blocks.append(block)
        if segment_id is not None:
            segments.append(
                Segment(
                    id=segment_id,
                    locator=identifier,
                    semantic_type=kind,
                    heading_path=[],
                    source_text=source,
                    protected=[],
                    context_ids=[],
                    target=True,
                )
            )
            translations[segment_id] = Translation(segment_id, translated)
        return block

    add_block(
        page=1,
        kind="header",
        source="반복 머리글",
        translated=None,
        bbox=(54.0, 18.0, 558.0, 34.0),
    )
    add_block(
        page=1,
        kind="heading",
        source="Rich content",
        translated="풍부한 콘텐츠",
        bbox=(54.0, 52.0, 558.0, 78.0),
        block_style=PdfBlockStyle(18.0, True, "left", 0.0, 10.0),
    )
    table_id = "pdf:page-0001:table-0001"
    table_start_order = len(blocks)
    for row in range(table_rows):
        for column in range(table_columns):
            if row == 0 and column == 1:
                continue
            column_span = 2 if row == 0 and column == 0 else 1
            identifier = (
                f"pdf:page-0001:table-0001:row-{row:04d}:cell-{column:04d}"
            )
            empty = row == 2 and column == 1
            source = "" if empty else (
                "Merged heading" if row == 0 and column == 0 else f"R{row} C{column}"
            )
            translated = None if empty else (
                "병합 머리글" if row == 0 and column == 0 else f"행{row} 열{column}"
            )
            segment_id = f"seg-{len(segments) + 1:06d}" if translated is not None else None
            block = PdfBlock(
                id=identifier,
                page_number=1,
                order=len(blocks),
                kind="table-cell",
                bbox=(
                    36.0 + column * 54.0,
                    100.0 + row * 18.0,
                    36.0 + (column + column_span) * 54.0,
                    118.0 + row * 18.0,
                ),
                style=PdfBlockStyle(9.0, False, "left", 0.0, 0.0),
                source_text=source,
                segment_id=segment_id,
                table_id=table_id,
                row=row,
                column=column,
                row_span=1,
                column_span=column_span,
            )
            blocks.append(block)
            table_cells.append(
                PdfTableCell(
                    id=identifier,
                    table_id=table_id,
                    page_number=1,
                    row=row,
                    column=column,
                    row_span=1,
                    column_span=column_span,
                    is_header=row == 0,
                    block_id=identifier,
                )
            )
            if segment_id is not None:
                segments.append(
                    Segment(
                        id=segment_id,
                        locator=identifier,
                        semantic_type="table-cell",
                        heading_path=["Rich content"],
                        source_text=source,
                        protected=[],
                        context_ids=[],
                        target=True,
                    )
                )
                translations[segment_id] = Translation(segment_id, translated)

    figure = add_block(
        page=1,
        kind="figure",
        source="",
        translated=None,
        bbox=(72.0, 390.0, 192.0, 450.0),
        media_path="media/figure-0001.png",
    )
    caption = add_block(
        page=1,
        kind="caption",
        source="Figure 1. source rendered pixels",
        translated="그림 1. 원본 렌더링 픽셀",
        bbox=(72.0, 454.0, 240.0, 470.0),
        caption_id=figure.id,
    )
    blocks[blocks.index(figure)] = replace(figure, caption_id=caption.id)
    external = add_block(
        page=1,
        kind="paragraph",
        source="External link",
        translated="외부 링크",
        bbox=(72.0, 480.0, 220.0, 500.0),
        uri="https://example.com/search?q=a&lang=ko",
    )
    owner = add_block(
        page=1,
        kind="paragraph",
        source="Page local marker 1",
        translated="페이지 지역 표지 1",
        bbox=(72.0, 510.0, 260.0, 530.0),
    )
    page_note = add_block(
        page=1,
        kind="footnote",
        source="1 page local note",
        translated="1 페이지 지역 각주",
        bbox=(72.0, 748.0, 260.0, 766.0),
        block_style=PdfBlockStyle(9.0, False, "left", 0.0, 0.0),
    )
    section_owner = add_block(
        page=1,
        kind="paragraph",
        source="Section marker 2",
        translated="절 표지 2",
        bbox=(72.0, 540.0, 260.0, 560.0),
    )
    target = add_block(
        page=2,
        kind="heading",
        source="Internal destination",
        translated="내부 목적지",
        bbox=(72.0, 100.0, 300.0, 126.0),
        block_style=PdfBlockStyle(16.0, True, "left", 0.0, 10.0),
    )
    section_note = add_block(
        page=2,
        kind="footnote",
        source="2 section note",
        translated="2 절 끝 각주",
        bbox=(72.0, 650.0, 260.0, 668.0),
        block_style=PdfBlockStyle(9.0, False, "left", 0.0, 0.0),
    )
    blocks[blocks.index(owner)] = replace(owner, destination=page_note.id)
    blocks[blocks.index(section_owner)] = replace(
        section_owner,
        destination=section_note.id,
    )
    internal = add_block(
        page=2,
        kind="paragraph",
        source="Internal link",
        translated="내부 링크",
        bbox=(72.0, 140.0, 220.0, 160.0),
        destination=blocks[table_start_order + 1].id,
    )
    for page in (1, 2):
        add_block(
            page=page,
            kind="footer",
            source="반복 바닥글",
            translated=None,
            bbox=(54.0, 770.0, 558.0, 784.0),
        )
        add_block(
            page=page,
            kind="page-number",
            source=str(page),
            translated=None,
            bbox=(540.0, 770.0, 558.0, 784.0),
        )
    document = PdfDocument(
        schema_version="1.0",
        source_sha256="a" * 64,
        page_count=2,
        selectable_characters=5000,
        scan_candidate_pages=[],
        pages=[
            PdfPage(number=1, width=612.0, height=792.0, rotation=0),
            PdfPage(number=2, width=612.0, height=792.0, rotation=0),
        ],
        blocks=blocks,
        table_cells=table_cells,
    )
    (run_dir / "document.json").write_text(
        json.dumps(document.to_dict(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    source = PdfSourceRecord(
        schema_version="1.0",
        input_kind="local",
        requested_source="rich.pdf",
        final_source="rich.pdf",
        content_type="application/pdf",
        byte_length=123,
        sha256="a" * 64,
        acquired_at="2026-08-21T01:02:03Z",
        redirects=[],
        warnings=[],
    )
    (run_dir / "source.json").write_text(
        json.dumps(source.to_dict(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    write_segments(run_dir / "segments.jsonl", segments)
    return run_dir, translations, {}, {
        "caption": caption.id,
        "external": external.id,
        "figure": figure.id,
        "internal": internal.id,
        "owner": owner.id,
        "page_note": page_note.id,
        "section_note": section_note.id,
        "section_owner": section_owner.id,
        "table_header": blocks[table_start_order].id,
        "table_link_target": blocks[table_start_order + 1].id,
        "target": target.id,
    }


def _embedded_font_programs(path: Path) -> dict[str, bytes]:
    programs: dict[str, bytes] = {}
    for page in PdfReader(path).pages:
        fonts = page["/Resources"].get_object()["/Font"].get_object()
        for reference in fonts.values():
            font = reference.get_object()
            descendants = font.get("/DescendantFonts")
            if descendants:
                font = descendants[0].get_object()
            descriptor_ref = font.get("/FontDescriptor")
            if descriptor_ref is None:
                continue
            descriptor = descriptor_ref.get_object()
            program_ref = descriptor.get("/FontFile2")
            if program_ref is None:
                continue
            programs[str(descriptor["/FontName"])] = program_ref.get_object().get_data()
    return programs


def _embedded_font_contracts(path: Path) -> dict[str, tuple[bool, bool]]:
    contracts: dict[str, tuple[bool, bool]] = {}
    for page in PdfReader(path).pages:
        fonts = page["/Resources"].get_object()["/Font"].get_object()
        for reference in fonts.values():
            root = reference.get_object()
            descendants = root.get("/DescendantFonts")
            font = descendants[0].get_object() if descendants else root
            descriptor_ref = font.get("/FontDescriptor")
            if descriptor_ref is None:
                continue
            descriptor = descriptor_ref.get_object()
            name = str(descriptor["/FontName"])
            contracts[name] = (
                descriptor.get("/FontFile2") is not None,
                root.get("/ToUnicode") is not None,
            )
    return contracts


def _heading_font_sizes(path: Path, labels: list[str]) -> list[float]:
    found: dict[str, float] = {}

    def visit(
        text: str,
        _current_matrix: object,
        _text_matrix: object,
        _font: object,
        font_size: float,
    ) -> None:
        for label in labels:
            if label in text:
                found[label] = float(font_size)

    for page in PdfReader(path).pages:
        page.extract_text(visitor_text=visit)
    return [found[label] for label in labels]


def test_font_resources_are_pinned_static_subsets_with_exact_provenance() -> None:
    asset_root = files("web_translator").joinpath("font_assets")
    provenance = json.loads(asset_root.joinpath("PROVENANCE.json").read_text("utf-8"))

    assert set(provenance) == {
        "license",
        "outputs",
        "schema_version",
        "source_sha256",
        "source_url",
        "unicode_ranges",
    }
    assert provenance["schema_version"] == "1.0"
    assert provenance["source_url"] == FONT_SOURCE_URL
    assert provenance["source_sha256"] == FONT_SOURCE_SHA256
    assert provenance["license"] == {
        "planned_url": PLANNED_LICENSE_URL,
        "planned_url_status": 404,
        "sha256": FONT_LICENSE_SHA256,
        "url": FONT_LICENSE_URL,
    }
    assert provenance["unicode_ranges"] == UNICODE_RANGES
    assert set(provenance["outputs"]) == {
        "NotoSansKR-Bold.ttf",
        "NotoSansKR-Regular.ttf",
    }

    for name, weight in (
        ("NotoSansKR-Regular.ttf", 400),
        ("NotoSansKR-Bold.ttf", 700),
    ):
        resource = asset_root.joinpath(name)
        data = resource.read_bytes()
        assert provenance["outputs"][name] == {
            "axes": {"wght": weight},
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        with as_file(resource) as font_path:
            font = TTFont(font_path)
            assert "fvar" not in font
            assert font["OS/2"].usWeightClass == weight
            cmap = font.getBestCmap()
            assert cmap is not None
            assert {
                0x20,
                0xE9,
                0x1100,
                0x2014,
                0x20A9,
                0x2192,
                0x3002,
                0x3131,
                0xAC00,
                0xD7A3,
            } <= set(cmap)
            assert 0x4E00 not in cmap

    license_text = asset_root.joinpath("OFL.txt").read_text("utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in license_text
    assert hashlib.sha256(license_text.encode("utf-8")).hexdigest() == FONT_LICENSE_SHA256


def test_vendoring_refuses_source_hash_mismatch_without_outputs(tmp_path: Path) -> None:
    source = tmp_path / "wrong.ttf"
    source.write_bytes(b"not the pinned font")
    license_path = tmp_path / "OFL.txt"
    license_path.write_text("license", encoding="utf-8")
    destination = tmp_path / "font-assets"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/vendor_pdf_fonts.py"),
            "--source-file",
            str(source),
            "--license-file",
            str(license_path),
            "--output-dir",
            str(destination),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert "source SHA-256 mismatch" in result.stderr
    assert not destination.exists()


def test_vendoring_refuses_tampered_license_hash_without_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.ttf"
    source.write_bytes(b"controlled source bytes")
    monkeypatch.setattr(
        font_vendor,
        "FONT_SOURCE_SHA256",
        hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    license_path = tmp_path / "OFL.txt"
    license_path.write_bytes(
        files("web_translator").joinpath("font_assets/OFL.txt").read_bytes()
        + b"\ntampered\n"
    )
    destination = tmp_path / "font-assets"

    with pytest.raises(font_vendor.FontVendoringError, match="license SHA-256 mismatch"):
        font_vendor.vendor_fonts(
            destination,
            source_file=source,
            license_file=license_path,
        )

    assert not destination.exists()


def test_assemble_pdf_rejects_tampered_bundled_license_without_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    package_root = tmp_path / "installed" / "web_translator"
    with as_file(files("web_translator").joinpath("font_assets")) as original:
        shutil.copytree(original, package_root / "font_assets")
    (package_root / "font_assets" / "OFL.txt").write_text(
        "tampered license", encoding="utf-8"
    )
    monkeypatch.setattr(pdf_assemble_module, "files", lambda _name: package_root)

    with pytest.raises(PdfAssemblyError, match="bundled font license hash mismatch"):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()


def test_assemble_pdf_embeds_regular_and_bold_with_tounicode_without_headings(
    tmp_path: Path,
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    document = PdfDocument.from_dict(
        json.loads((run_dir / "document.json").read_text(encoding="utf-8"))
    )
    remaining_blocks = [
        replace(block, order=index)
        for index, block in enumerate(document.blocks[1:])
    ]
    (run_dir / "document.json").write_text(
        json.dumps(
            replace(document, blocks=remaining_blocks).to_dict(),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    remaining_segments = [
        replace(
            segment,
            heading_path=[],
            context_ids=[
                context
                for context in segment.context_ids
                if context != "seg-000001"
            ],
        )
        for segment in read_segments(run_dir / "segments.jsonl")[1:]
    ]
    write_segments(run_dir / "segments.jsonl", remaining_segments)
    translations.pop("seg-000001")

    staged = assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    contracts = _embedded_font_contracts(staged)
    assert len(contracts) == 2
    assert any("Regular" in name for name in contracts)
    assert any("Bold" in name for name in contracts)
    assert all(font_file_2 and to_unicode for font_file_2, to_unicode in contracts.values())


def test_assemble_pdf_normalizes_heading_levels_from_document_evidence(
    tmp_path: Path,
) -> None:
    run_dir, _translations, glossary = _assembly_run(tmp_path)
    labels = ["Top One", "Child One", "Child Two", "Top Two"]
    source_sizes = [18.0, 14.0, 14.0, 18.0]
    heading_paths = [[], ["Top One"], ["Top One", "Child One"], ["Top One", "Child Two"]]
    blocks = [
        PdfBlock(
            id=f"pdf:page-0001:block-{index:04d}",
            page_number=1,
            order=index - 1,
            kind="heading",
            bbox=(72.0, 48.0 + index * 48.0, 540.0, 72.0 + index * 48.0),
            style=PdfBlockStyle(size, True, "left", 72.0, 12.0),
            source_text=label,
            segment_id=f"seg-{index:06d}",
        )
        for index, (label, size) in enumerate(zip(labels, source_sizes, strict=True), start=1)
    ]
    document = PdfDocument.from_dict(
        json.loads((run_dir / "document.json").read_text(encoding="utf-8"))
    )
    (run_dir / "document.json").write_text(
        json.dumps(replace(document, blocks=blocks).to_dict()) + "\n",
        encoding="utf-8",
    )
    segments = [
        Segment(
            id=f"seg-{index:06d}",
            locator=block.id,
            semantic_type="heading",
            heading_path=path,
            source_text=block.source_text,
            protected=[],
            context_ids=[],
            target=True,
        )
        for index, (block, path) in enumerate(zip(blocks, heading_paths, strict=True), start=1)
    ]
    write_segments(run_dir / "segments.jsonl", segments)
    translations = {
        segment.id: Translation(segment.id, labels[index])
        for index, segment in enumerate(segments)
    }

    staged = assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert _heading_font_sizes(staged, labels) == [18.0, 16.0, 16.0, 18.0]


def test_assemble_pdf_preserves_list_marker_family_and_relative_nesting(
    tmp_path: Path,
) -> None:
    run_dir, _translations, glossary = _assembly_run(tmp_path)
    entries = [
        ("- RootBullet", 72.0),
        ("1. RootOrdered", 72.0),
        ("1.2) NestedOrdered", 90.0),
        ("- NestedBullet", 90.0),
        ("A. AlphaDot", 72.0),
        ("A) AlphaParen", 72.0),
        ("iv) RomanOrdered", 90.0),
        ("‣ TriangleBullet", 72.0),
        ("◦ WhiteBullet", 90.0),
        ("⁃ HyphenBullet", 90.0),
        ("∙ DotBullet", 72.0),
    ]
    blocks = [
        PdfBlock(
            id=f"pdf:page-0001:block-{index:04d}",
            page_number=1,
            order=index - 1,
            kind="list-item",
            bbox=(indentation, 48.0 + index * 36.0, 540.0, 72.0 + index * 36.0),
            style=PdfBlockStyle(11.0, False, "left", indentation, 5.0),
            source_text=text,
            segment_id=f"seg-{index:06d}",
        )
        for index, (text, indentation) in enumerate(entries, start=1)
    ]
    document = PdfDocument.from_dict(
        json.loads((run_dir / "document.json").read_text(encoding="utf-8"))
    )
    (run_dir / "document.json").write_text(
        json.dumps(replace(document, blocks=blocks).to_dict()) + "\n",
        encoding="utf-8",
    )
    segments = [
        Segment(
            id=f"seg-{index:06d}",
            locator=block.id,
            semantic_type="list-item",
            heading_path=[],
            source_text=block.source_text,
            protected=[],
            context_ids=[],
            target=True,
        )
        for index, block in enumerate(blocks, start=1)
    ]
    write_segments(run_dir / "segments.jsonl", segments)
    translations = {
        segment.id: Translation(segment.id, segment.source_text)
        for segment in segments
    }

    staged = assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    extracted = "\n".join(page.extract_text() or "" for page in PdfReader(staged).pages)
    expected_markers = {
        "TriangleBullet": "•",
        "WhiteBullet": "•",
        "HyphenBullet": "•",
        "DotBullet": "•",
    }
    for marker_and_body, _indentation in entries:
        source_marker, body = marker_and_body.split(" ", 1)
        rendered_marker = expected_markers.get(body, source_marker)
        assert extracted.count(f"{rendered_marker} {body}") == 1
        assert extracted.count(body) == 1
    with pdfplumber.open(staged) as document_reader:
        words = document_reader.pages[0].extract_words()
    x_by_body = {
        str(word["text"]): float(word["x0"])
        for word in words
        if str(word["text"])
        in {
            "RootBullet",
            "RootOrdered",
            "NestedOrdered",
            "NestedBullet",
            "AlphaDot",
            "AlphaParen",
            "RomanOrdered",
            "TriangleBullet",
            "WhiteBullet",
            "HyphenBullet",
            "DotBullet",
        }
    }
    assert x_by_body["RootBullet"] == pytest.approx(x_by_body["RootOrdered"], abs=0.5)
    assert x_by_body["NestedBullet"] == pytest.approx(x_by_body["NestedOrdered"], abs=0.5)
    assert x_by_body["NestedBullet"] - x_by_body["RootBullet"] == pytest.approx(18.0, abs=1.0)


def test_assemble_pdf_creates_children_only_in_held_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    moved_run = run_dir.with_name("moved-run")
    original_open = pdf_assemble_module._open_directory_anchor
    swapped = False

    def open_then_swap(
        path: Path,
        label: str,
        **kwargs: object,
    ) -> object:
        nonlocal swapped
        anchor = original_open(path, label, **kwargs)
        if label == "run" and not swapped:
            swapped = True
            run_dir.rename(moved_run)
            run_dir.mkdir()
        return anchor

    monkeypatch.setattr(
        pdf_assemble_module,
        "_open_directory_anchor",
        open_then_swap,
    )

    with pytest.raises(PdfAssemblyError, match="run directory changed identity"):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert list(run_dir.iterdir()) == []
    assert not (moved_run / "staged-output").exists()
    assert not (moved_run / "layout.json").exists()
    assert not any(
        child.name.startswith(".pdf-assembling-") for child in moved_run.iterdir()
    )


def test_assemble_pdf_opens_run_anchor_before_reading_any_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    original_run = run_dir.with_name("original-run")
    original_open = pdf_assemble_module._open_directory_anchor
    swapped = False

    def swap_before_open(
        path: Path,
        label: str,
        **kwargs: object,
    ) -> object:
        nonlocal swapped
        if label == "run" and not swapped:
            swapped = True
            run_dir.rename(original_run)
            run_dir.mkdir()
        return original_open(path, label, **kwargs)

    monkeypatch.setattr(
        pdf_assemble_module,
        "_open_directory_anchor",
        swap_before_open,
    )

    with pytest.raises(PdfAssemblyError, match="PDF document"):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    for directory in (original_run, run_dir):
        assert not (directory / "staged-output").exists()
        assert not (directory / "layout.json").exists()
        assert not any(
            child.name.startswith(".pdf-assembling-")
            for child in directory.iterdir()
        )


def test_assemble_pdf_rejects_symlink_swapped_before_anchored_input_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not pdf_assemble_module._DIRFD_PUBLICATION_SUPPORTED:
        pytest.skip("POSIX dirfd input-open regression")
    run_dir, translations, glossary = _assembly_run(tmp_path)
    document = run_dir / "document.json"
    original_document = run_dir / "original-document.json"
    outside_document = tmp_path / "outside-document.json"
    outside_document.write_bytes(document.read_bytes())
    original_open = pdf_assemble_module.os.open
    swapped = False

    def swap_before_input_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "document.json" and dir_fd is not None and not swapped:
            swapped = True
            document.rename(original_document)
            document.symlink_to(outside_document)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pdf_assemble_module.os, "open", swap_before_input_open)

    with pytest.raises(PdfAssemblyError, match="PDF document"):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert document.is_symlink()
    assert outside_document.read_bytes() == original_document.read_bytes()
    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()


def test_assemble_pdf_rejects_input_name_swapped_after_anchored_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not pdf_assemble_module._DIRFD_PUBLICATION_SUPPORTED:
        pytest.skip("POSIX dirfd input-open regression")
    run_dir, translations, glossary = _assembly_run(tmp_path)
    document = run_dir / "document.json"
    opened_document = run_dir / "opened-document.json"
    original_open = pdf_assemble_module.os.open
    swapped = False

    def swap_after_input_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "document.json" and dir_fd is not None and not swapped:
            swapped = True
            payload = document.read_bytes()
            document.rename(opened_document)
            document.write_bytes(payload)
        return descriptor

    monkeypatch.setattr(pdf_assemble_module.os, "open", swap_after_input_open)

    with pytest.raises(PdfAssemblyError, match="changed identity"):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert document.read_bytes() == opened_document.read_bytes()
    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()


def test_assemble_pdf_rechecks_all_open_input_names_as_one_evidence_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not pdf_assemble_module._DIRFD_PUBLICATION_SUPPORTED:
        pytest.skip("POSIX dirfd input-open regression")
    run_dir, translations, glossary = _assembly_run(tmp_path)
    document = run_dir / "document.json"
    opened_document = run_dir / "opened-document.json"
    original_open = pdf_assemble_module.os.open
    swapped = False

    def swap_document_while_opening_source(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "source.json" and dir_fd is not None and not swapped:
            swapped = True
            payload = document.read_bytes()
            document.rename(opened_document)
            document.write_bytes(payload)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        pdf_assemble_module.os,
        "open",
        swap_document_while_opening_source,
    )

    with pytest.raises(PdfAssemblyError, match="document.json"):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()


def test_posix_anchored_input_closes_descriptor_if_stream_conversion_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not pdf_assemble_module._DIRFD_PUBLICATION_SUPPORTED:
        pytest.skip("POSIX dirfd input-open regression")
    run_dir, _translations, _glossary = _assembly_run(tmp_path)
    anchor = pdf_assemble_module._open_directory_anchor(run_dir, "run")
    original_fdopen = pdf_assemble_module.os.fdopen
    captured_descriptor: int | None = None

    def interrupt_fdopen(
        descriptor: int, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        nonlocal captured_descriptor
        if mode == "rb":
            captured_descriptor = descriptor
            raise KeyboardInterrupt()
        return original_fdopen(descriptor, mode, *args, **kwargs)

    monkeypatch.setattr(pdf_assemble_module.os, "fdopen", interrupt_fdopen)
    try:
        with pytest.raises(KeyboardInterrupt):
            pdf_assemble_module._open_anchored_input_file(
                anchor,
                "document.json",
                "PDF document",
            )
        assert captured_descriptor is not None
        with pytest.raises(OSError):
            os.fstat(captured_descriptor)
    finally:
        anchor.close()


def test_assemble_pdf_closes_input_and_run_handles_on_read_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not pdf_assemble_module._DIRFD_PUBLICATION_SUPPORTED:
        pytest.skip("POSIX dirfd input-open regression")
    run_dir, translations, glossary = _assembly_run(tmp_path)
    original_anchor_open = pdf_assemble_module._open_directory_anchor
    original_fdopen = pdf_assemble_module.os.fdopen
    input_descriptor: int | None = None
    run_descriptor: int | None = None

    class InterruptingInput:
        def __init__(self, stream: object) -> None:
            self.stream = stream

        @property
        def closed(self) -> bool:
            return bool(self.stream.closed)  # type: ignore[attr-defined]

        def read(self, *_args: object, **_kwargs: object) -> bytes:
            raise KeyboardInterrupt()

        def seek(self, *args: object, **kwargs: object) -> int:
            return int(self.stream.seek(*args, **kwargs))  # type: ignore[attr-defined]

        def close(self) -> None:
            self.stream.close()  # type: ignore[attr-defined]

        def fileno(self) -> int:
            return int(self.stream.fileno())  # type: ignore[attr-defined]

    def capture_anchor(path: Path, label: str, **kwargs: object) -> object:
        nonlocal run_descriptor
        anchor = original_anchor_open(path, label, **kwargs)
        if label == "run":
            run_descriptor = anchor.descriptor
        return anchor

    def interrupting_fdopen(
        descriptor: int, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        nonlocal input_descriptor
        stream = original_fdopen(descriptor, mode, *args, **kwargs)
        if mode == "rb" and input_descriptor is None:
            input_descriptor = descriptor
            return InterruptingInput(stream)
        return stream

    monkeypatch.setattr(pdf_assemble_module, "_open_directory_anchor", capture_anchor)
    monkeypatch.setattr(pdf_assemble_module.os, "fdopen", interrupting_fdopen)

    with pytest.raises(KeyboardInterrupt):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert input_descriptor is not None
    assert run_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(input_descriptor)
    with pytest.raises(OSError):
        os.fstat(run_descriptor)
    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()


def test_assemble_pdf_writes_reportlab_through_open_anchored_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    original_document = pdf_assemble_module.SimpleDocTemplate
    swapped = False

    def swap_staging_before_reportlab(destination: object, **kwargs: object) -> object:
        nonlocal swapped
        if not swapped:
            swapped = True
            temporary = next(
                child
                for child in run_dir.iterdir()
                if child.name.startswith(".pdf-assembling-")
            )
            staging = temporary / "staged-output"
            staging.rename(temporary / "moved-staged-output")
            staging.symlink_to(outside, target_is_directory=True)
        return original_document(destination, **kwargs)

    monkeypatch.setattr(
        pdf_assemble_module,
        "SimpleDocTemplate",
        swap_staging_before_reportlab,
    )

    with pytest.raises(PdfAssemblyError):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert list(outside.iterdir()) == []
    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()


def test_assemble_pdf_cleans_child_if_mkdir_interrupts_after_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    original_mkdir = pdf_assemble_module.os.mkdir

    def create_then_interrupt(
        path: str | Path,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        original_mkdir(path, mode, dir_fd=dir_fd)
        if dir_fd is not None and str(path).startswith(".pdf-assembling-"):
            raise KeyboardInterrupt()

    monkeypatch.setattr(pdf_assemble_module.os, "mkdir", create_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert not any(
        child.name.startswith(".pdf-assembling-") for child in run_dir.iterdir()
    )
    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()


def test_assemble_pdf_cleans_file_if_open_interrupts_after_exclusive_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    original_open = pdf_assemble_module.os.open

    def create_then_interrupt(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is not None and path == "translated.pdf":
            os.close(descriptor)
            raise KeyboardInterrupt()
        return descriptor

    monkeypatch.setattr(pdf_assemble_module.os, "open", create_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert not any(
        child.name.startswith(".pdf-assembling-") for child in run_dir.iterdir()
    )
    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()


def test_assemble_pdf_anchors_publication_against_staging_directory_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = run_dir / "moved-owned-staging"
    original_link = pdf_assemble_module.os.link
    raced = False

    def swap_before_link(
        source: str | Path, destination: str | Path, **kwargs: object
    ) -> None:
        nonlocal raced
        if not raced and Path(destination).name == "translated.pdf":
            raced = True
            (run_dir / "staged-output").rename(moved)
            (run_dir / "staged-output").symlink_to(outside, target_is_directory=True)
        original_link(source, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pdf_assemble_module.os, "link", swap_before_link)

    with pytest.raises(PdfAssemblyError, match="staging directory changed identity"):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert list(outside.iterdir()) == []
    if moved.exists():
        assert list(moved.iterdir()) == []
    assert (run_dir / "staged-output").is_symlink()
    assert not (run_dir / "layout.json").exists()


def test_assemble_pdf_cleans_anchored_link_if_syscall_interrupts_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    original_link = pdf_assemble_module.os.link

    def publish_then_interrupt(
        source: str | Path, destination: str | Path, **kwargs: object
    ) -> None:
        original_link(source, destination, **kwargs)  # type: ignore[arg-type]
        if Path(destination).name == "translated.pdf":
            raise KeyboardInterrupt()

    monkeypatch.setattr(pdf_assemble_module.os, "link", publish_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()


def test_assemble_pdf_stages_selectable_korean_without_publishing(
    tmp_path: Path,
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    final_output = tmp_path / "translated-pdfs" / "result"

    staged = assemble_pdf(run_dir, translations, glossary, final_output)

    assert staged == run_dir / "staged-output" / "translated.pdf"
    assert staged.is_file()
    assert sorted(path.name for path in staged.parent.iterdir()) == ["translated.pdf"]
    assert not final_output.exists()
    text = "\n".join(page.extract_text() or "" for page in PdfReader(staged).pages)
    expected = [
        "<안내 & 개요>",
        "한국어 본문 & OAuth(권한 위임) client<id>.",
        "첫째 <둘째>",
        "Source: 기술 보고서.pdf",
        "Generated: ",
    ]
    positions = [text.index(value) for value in expected]
    assert positions == sorted(positions)

    programs = _embedded_font_programs(staged)
    assert len(programs) == 2
    assert any("Regular" in name for name in programs)
    assert any("Bold" in name for name in programs)

    layout = read_pdf_layout(run_dir / "layout.json")
    assert layout.schema_version == "1.0"
    assert layout.reserved_output_dir == str(final_output)
    assert layout.staged_pdf_sha256 == hashlib.sha256(staged.read_bytes()).hexdigest()
    assert layout.page_size.name == "LETTER"
    assert layout.minimum_font_size == 9.0
    assert [(item.block_id, item.kind, item.source_order) for item in layout.flowables] == [
        ("pdf:page-0001:block-0001", "heading", 0),
        ("pdf:page-0001:block-0002", "paragraph", 1),
        ("pdf:page-0001:block-0003", "list-item", 2),
    ]
    assert all(item.font_size >= 9.0 for item in layout.flowables)
    assert all(item.page_number >= 1 for item in layout.flowables)


def test_assemble_pdf_chooses_a4_for_a4_like_source_pages(tmp_path: Path) -> None:
    run_dir, translations, glossary = _assembly_run(
        tmp_path, width=595.275590551, height=841.88976378
    )

    staged = assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    page = PdfReader(staged).pages[0]
    assert float(page.mediabox.width) == pytest.approx(595.2756, abs=0.01)
    assert float(page.mediabox.height) == pytest.approx(841.8898, abs=0.01)
    assert read_pdf_layout(run_dir / "layout.json").page_size.name == "A4"


def test_pdf_layout_reader_rejects_unknown_fields(tmp_path: Path) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    assemble_pdf(run_dir, translations, glossary, tmp_path / "final")
    path = run_dir / "layout.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["invented"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PdfAssemblyError, match="layout fields must be exactly"):
        read_pdf_layout(path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload.update(minimum_font_size=8.99),
            "minimum_font_size must be at least 9",
        ),
        (
            lambda payload: payload["flowables"][0].update(font_size=8.99),
            "flowable font size is below minimum_font_size",
        ),
        (
            lambda payload: payload["flowables"].append(
                dict(payload["flowables"][0])
            ),
            "flowable block and split-part pairs must be unique",
        ),
        (
            lambda payload: payload.update(reserved_output_dir=""),
            "reserved_output_dir must be nonempty",
        ),
    ],
)
def test_pdf_layout_reader_rejects_cross_field_contract_violations(
    tmp_path: Path, mutate: object, message: str
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    assemble_pdf(run_dir, translations, glossary, tmp_path / "final")
    path = run_dir / "layout.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)  # type: ignore[operator]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PdfAssemblyError, match=message):
        read_pdf_layout(path)


def test_pdf_layout_reader_rejects_overlapping_peer_flowables(
    tmp_path: Path,
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    assemble_pdf(run_dir, translations, glossary, tmp_path / "final")
    path = run_dir / "layout.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["flowables"][1].update(
        bounds=list(payload["flowables"][0]["bounds"]),
        frame=list(payload["flowables"][0]["frame"]),
        page_number=payload["flowables"][0]["page_number"],
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PdfAssemblyError, match="overlapping peer flowables"):
        read_pdf_layout(path)


def test_assemble_pdf_cleans_owned_partial_staging_on_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    keep = run_dir / "keep.txt"
    keep.write_text("unrelated", encoding="utf-8")

    def interrupt_layout(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        pdf_assemble_module,
        "_write_layout_stream",
        interrupt_layout,
        raising=False,
    )

    with pytest.raises(KeyboardInterrupt):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert keep.read_text(encoding="utf-8") == "unrelated"
    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()
    assert not any(path.name.startswith(".pdf-assembling-") for path in run_dir.iterdir())


def test_assemble_pdf_cleans_run_visible_staging_on_publication_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    keep = run_dir / "keep.txt"
    keep.write_text("unrelated", encoding="utf-8")
    original_publish = pdf_assemble_module._publish_new_file

    def interrupt_publication(
        source_directory: object,
        source_name: str,
        destination_directory: object,
        destination_name: str,
    ) -> object:
        if destination_name == "translated.pdf":
            return original_publish(
                source_directory,
                source_name,
                destination_directory,
                destination_name,
            )
        assert (run_dir / "staged-output" / "translated.pdf").is_file()
        raise KeyboardInterrupt()

    monkeypatch.setattr(pdf_assemble_module, "_publish_new_file", interrupt_publication)

    with pytest.raises(KeyboardInterrupt):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert keep.read_text(encoding="utf-8") == "unrelated"
    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()
    assert not any(path.name.startswith(".pdf-assembling-") for path in run_dir.iterdir())


def test_assemble_pdf_base_exception_does_not_delete_replaced_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    original_publish = pdf_assemble_module._publish_new_file

    def replace_then_interrupt(
        source_directory: object,
        source_name: str,
        destination_directory: object,
        destination_name: str,
    ) -> object:
        destination = run_dir / "staged-output" / destination_name
        if destination_name == "translated.pdf":
            identity = original_publish(
                source_directory,
                source_name,
                destination_directory,
                destination_name,
            )
            destination.unlink()
            destination.write_bytes(b"unrelated racer")
            return identity
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        pdf_assemble_module, "_publish_new_file", replace_then_interrupt
    )

    with pytest.raises(KeyboardInterrupt):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    racer = run_dir / "staged-output" / "translated.pdf"
    assert racer.read_bytes() == b"unrelated racer"
    assert not (run_dir / "layout.json").exists()


def test_assemble_pdf_does_not_replace_raced_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    original_link = pdf_assemble_module.os.link

    def race_staged_destination(
        source: str | Path, destination: str | Path, **kwargs: object
    ) -> None:
        destination_path = Path(destination)
        if destination_path.name == "translated.pdf":
            destination_directory = kwargs.get("dst_dir_fd")
            if isinstance(destination_directory, int):
                descriptor = os.open(
                    destination_path.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=destination_directory,
                )
                try:
                    os.write(descriptor, b"unrelated racer")
                finally:
                    os.close(descriptor)
            else:
                destination_path.write_bytes(b"unrelated racer")
        original_link(source, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pdf_assemble_module.os, "link", race_staged_destination)

    with pytest.raises(PdfAssemblyError, match="already exists"):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    racer = run_dir / "staged-output" / "translated.pdf"
    assert racer.read_bytes() == b"unrelated racer"
    assert not (run_dir / "layout.json").exists()


def test_assemble_pdf_fails_closed_without_anchored_run_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    monkeypatch.setattr(
        pdf_assemble_module,
        "_DIRFD_PUBLICATION_SUPPORTED",
        False,
    )

    with pytest.raises(PdfAssemblyError, match="anchored destination check unavailable"):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()
    assert not any(
        child.name.startswith(".pdf-assembling-") for child in run_dir.iterdir()
    )


@pytest.mark.parametrize(
    ("pointer_size", "root_handle", "root_offset", "length_offset", "name_offset"),
    [
        (8, 0x0102030405060708, 8, 16, 20),
        (4, 0x01020304, 4, 8, 12),
    ],
)
def test_windows_file_rename_info_uses_relative_no_replace_contract(
    pointer_size: int,
    root_handle: int,
    root_offset: int,
    length_offset: int,
    name_offset: int,
) -> None:
    name = "translated.pdf"
    encoded = name.encode("utf-16-le")

    payload = pdf_assemble_module._windows_file_rename_information(
        root_handle,
        name,
        pointer_size=pointer_size,
    )

    assert struct.unpack_from("<I", payload, 0) == (0,)
    pointer_format = "<Q" if pointer_size == 8 else "<I"
    assert struct.unpack_from(pointer_format, payload, root_offset) == (root_handle,)
    assert struct.unpack_from("<I", payload, length_offset) == (len(encoded),)
    assert payload[name_offset:] == encoded


def test_windows_rename_open_file_uses_native_relative_information_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching back to the Win32 class-3 wrapper breaks held-root renames."""
    import ctypes

    calls: list[tuple[int, int, bytes]] = []

    class FakeCall:
        def __init__(self, operation: object) -> None:
            self.operation = operation
            self.argtypes: object | None = None
            self.restype: object | None = None

        def __call__(self, *args: object) -> object:
            return self.operation(*args)  # type: ignore[operator]

    def native_rename(
        source_handle: object,
        _status_block: object,
        payload: object,
        payload_size: object,
        information_class: object,
    ) -> int:
        size = int(payload_size)
        calls.append(
            (
                int(source_handle),
                int(information_class),
                ctypes.string_at(payload, size),
            )
        )
        return 0

    class FakeNtdll:
        NtSetInformationFile = FakeCall(native_rename)

    def load_library(name: str, **_kwargs: object) -> FakeNtdll:
        assert name == "ntdll"
        return FakeNtdll()

    monkeypatch.setattr(ctypes, "WinDLL", load_library, raising=False)

    pdf_assemble_module._windows_rename_open_file(501, 702, "translated.pdf")

    assert len(calls) == 1
    source_handle, information_class, payload = calls[0]
    assert source_handle == 501
    assert information_class == 10
    assert struct.unpack_from("<Q", payload, 8) == (702,)
    assert payload[20:] == "translated.pdf".encode("utf-16-le")


@pytest.mark.skipif(os.name != "nt", reason="requires the real Windows NT filesystem ABI")
def test_windows_native_relative_rename_moves_to_held_destination(
    tmp_path: Path,
) -> None:
    """The real Windows syscall must accept a held destination root handle."""
    source_directory = tmp_path / "source 경로"
    destination_directory = tmp_path / "destination 경로"
    source_directory.mkdir()
    destination_directory.mkdir()
    (source_directory / "staged.pdf").write_bytes(b"owned translated PDF")
    source_anchor = pdf_assemble_module._open_directory_anchor(source_directory, "source")
    destination_anchor = pdf_assemble_module._open_directory_anchor(
        destination_directory,
        "destination",
    )
    source_handle: int | None = None
    try:
        source_handle = pdf_assemble_module._windows_open_relative_file(
            pdf_assemble_module._windows_anchor_handle(source_anchor),
            "staged.pdf",
        )
        pdf_assemble_module._windows_rename_open_file(
            source_handle,
            pdf_assemble_module._windows_anchor_handle(destination_anchor),
            "번역 결과.pdf",
        )
    finally:
        if source_handle is not None:
            pdf_assemble_module.pdf_acquire_module._close_windows_handle(source_handle)
        source_anchor.close()
        destination_anchor.close()

    assert not (source_directory / "staged.pdf").exists()
    assert (destination_directory / "번역 결과.pdf").read_bytes() == b"owned translated PDF"


@pytest.mark.skipif(os.name != "nt", reason="requires the real Windows NT filesystem ABI")
def test_windows_native_relative_rename_never_clobbers_existing_destination(
    tmp_path: Path,
) -> None:
    """A native relative rename must preserve a raced destination byte-for-byte."""
    source_directory = tmp_path / "source"
    destination_directory = tmp_path / "destination"
    source_directory.mkdir()
    destination_directory.mkdir()
    source = source_directory / "staged.pdf"
    destination = destination_directory / "translated.pdf"
    source.write_bytes(b"owned translated PDF")
    destination.write_bytes(b"foreign racer")
    source_anchor = pdf_assemble_module._open_directory_anchor(source_directory, "source")
    destination_anchor = pdf_assemble_module._open_directory_anchor(
        destination_directory,
        "destination",
    )
    source_handle: int | None = None
    try:
        source_handle = pdf_assemble_module._windows_open_relative_file(
            pdf_assemble_module._windows_anchor_handle(source_anchor),
            source.name,
        )
        with pytest.raises(FileExistsError):
            pdf_assemble_module._windows_rename_open_file(
                source_handle,
                pdf_assemble_module._windows_anchor_handle(destination_anchor),
                destination.name,
            )
    finally:
        if source_handle is not None:
            pdf_assemble_module.pdf_acquire_module._close_windows_handle(source_handle)
        source_anchor.close()
        destination_anchor.close()

    assert source.read_bytes() == b"owned translated PDF"
    assert destination.read_bytes() == b"foreign racer"


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (
            "_windows_create_relative_directory",
            {
                "create_disposition": 2,
                "create_options": 0x00000001 | 0x00000020 | 0x00200000,
                "file_attributes": 0x00000010,
            },
        ),
        (
            "_windows_open_relative_directory",
            {
                "create_disposition": 1,
                "create_options": 0x00000001 | 0x00000020 | 0x00200000,
                "file_attributes": 0,
            },
        ),
        (
            "_windows_create_relative_file",
            {
                "create_disposition": 2,
                "create_options": 0x00000020 | 0x00000040 | 0x00200000,
                "file_attributes": 0x00000080,
            },
        ),
        (
            "_windows_open_relative_file",
            {
                "create_disposition": 1,
                "create_options": 0x00000020 | 0x00000040 | 0x00200000,
                "file_attributes": 0,
            },
        ),
        (
            "_windows_open_relative_read_file",
            {
                "create_disposition": 1,
                "create_options": 0x00000020 | 0x00000040 | 0x00200000,
                "file_attributes": 0,
            },
        ),
        (
            "_windows_open_relative_entry",
            {
                "create_disposition": 1,
                "create_options": 0x00000020 | 0x00200000,
                "file_attributes": 0,
            },
        ),
    ],
)
def test_windows_relative_open_contract_is_rooted_and_no_follow(
    operation: str,
    expected: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, str, dict[str, int]]] = []

    def capture(root_handle: int, name: str, **kwargs: int) -> int:
        calls.append((root_handle, name, kwargs))
        return 707

    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_nt_create_relative",
        capture,
    )

    result = getattr(pdf_assemble_module, operation)(41, "translated.pdf")

    assert result == 707
    assert len(calls) == 1
    root_handle, name, contract = calls[0]
    assert root_handle == 41
    assert name == "translated.pdf"
    assert contract["create_disposition"] == expected["create_disposition"]
    assert contract["create_options"] == expected["create_options"]
    assert contract["file_attributes"] == expected["file_attributes"]
    assert contract["desired_access"] & 0x00100000


def test_windows_nt_create_passes_relative_root_name_and_create_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    captured: dict[str, object] = {}

    class FakeCall:
        def __init__(self, operation: object) -> None:
            self.operation = operation
            self.argtypes: object | None = None
            self.restype: object | None = None

        def __call__(self, *args: object) -> object:
            return self.operation(*args)  # type: ignore[operator]

    def capture_create(
        output_handle: object,
        desired_access: object,
        object_attributes: object,
        _status_block: object,
        _allocation_size: object,
        file_attributes: object,
        share_access: object,
        create_disposition: object,
        create_options: object,
        _ea_buffer: object,
        _ea_length: object,
    ) -> int:
        attributes = object_attributes._obj  # type: ignore[attr-defined]
        unicode_name = attributes.object_name.contents
        captured.update(
            root_directory=int(attributes.root_directory),
            name=unicode_name.buffer,
            attributes=int(attributes.attributes),
            desired_access=int(desired_access),
            file_attributes=int(file_attributes),
            share_access=int(share_access),
            create_disposition=int(create_disposition),
            create_options=int(create_options),
        )
        output_handle._obj.value = 505  # type: ignore[attr-defined]
        return 0

    class FakeNtdll:
        NtCreateFile = FakeCall(capture_create)

    monkeypatch.setattr(pdf_assemble_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: FakeNtdll(),
        raising=False,
    )

    handle = pdf_assemble_module._windows_nt_create_relative(
        0x01020304,
        "translated.pdf",
        desired_access=0x001F01FF,
        create_disposition=2,
        create_options=0x00200060,
        file_attributes=0x80,
    )

    assert handle == 505
    assert captured == {
        "root_directory": 0x01020304,
        "name": "translated.pdf",
        "attributes": 0x40,
        "desired_access": 0x001F01FF,
        "file_attributes": 0x80,
        "share_access": 0x7,
        "create_disposition": 2,
        "create_options": 0x00200060,
    }


def test_windows_nt_relative_open_preserves_missing_entry_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    class FakeCall:
        def __init__(self, operation: object) -> None:
            self.operation = operation
            self.argtypes: object | None = None
            self.restype: object | None = None

        def __call__(self, *args: object) -> object:
            return self.operation(*args)  # type: ignore[operator]

    class FakeNtdll:
        NtCreateFile = FakeCall(lambda *_args: -1)
        RtlNtStatusToDosError = FakeCall(lambda _status: 2)

    monkeypatch.setattr(pdf_assemble_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: FakeNtdll(),
        raising=False,
    )

    with pytest.raises(FileNotFoundError):
        pdf_assemble_module._windows_nt_create_relative(
            41,
            "layout.json",
            desired_access=0x00100080,
            create_disposition=1,
            create_options=0x00200020,
            file_attributes=0,
        )


def test_windows_anchored_destination_absence_uses_root_handle_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "run"
    directory.mkdir()
    result = directory.lstat()
    calls: list[tuple[int, str]] = []
    closed: list[int] = []

    class FakeAnchor:
        handle = 41

        def current_path(self) -> Path:
            return directory

        def close(self) -> None:
            return None

    anchor = pdf_assemble_module._DirectoryAnchor(
        directory,
        "run",
        (result.st_dev, result.st_ino),
        None,
        FakeAnchor(),
    )
    monkeypatch.setattr(pdf_assemble_module, "_IS_WINDOWS", True)

    def existing(root_handle: int, name: str) -> int:
        calls.append((root_handle, name))
        return 707

    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_open_relative_entry",
        existing,
        raising=False,
    )
    monkeypatch.setattr(
        pdf_assemble_module.pdf_acquire_module,
        "_close_windows_handle",
        closed.append,
    )

    with pytest.raises(PdfAssemblyError, match="already exists"):
        pdf_assemble_module._require_anchored_name_absent(anchor, "layout.json")

    assert calls == [(41, "layout.json")]
    assert closed == [707]

    def missing(_root_handle: int, name: str) -> int:
        raise FileNotFoundError(name)

    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_open_relative_entry",
        missing,
        raising=False,
    )
    pdf_assemble_module._require_anchored_name_absent(anchor, "staged-output")


def test_windows_input_identity_verification_uses_read_only_relative_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "run"
    directory.mkdir()
    result = directory.lstat()
    calls: list[tuple[int, str]] = []
    closed: list[int] = []

    class FakeAnchor:
        handle = 41

        def current_path(self) -> Path:
            return directory

        def close(self) -> None:
            return None

    anchor = pdf_assemble_module._DirectoryAnchor(
        directory,
        "run",
        (result.st_dev, result.st_ino),
        None,
        FakeAnchor(),
    )
    monkeypatch.setattr(pdf_assemble_module, "_IS_WINDOWS", True)

    def read_only_open(root_handle: int, name: str) -> int:
        calls.append((root_handle, name))
        return 808

    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_open_relative_read_file",
        read_only_open,
    )
    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_open_relative_file",
        lambda *_args: pytest.fail("input verification requested DELETE access"),
    )
    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_file_identity",
        lambda _handle, *, require_regular: (7, 11),
    )
    monkeypatch.setattr(
        pdf_assemble_module.pdf_acquire_module,
        "_close_windows_handle",
        closed.append,
    )

    pdf_assemble_module._verify_anchored_input_identity(
        anchor,
        "document.json",
        (7, 11),
    )

    assert calls == [(41, "document.json")]
    assert closed == [808]


def test_windows_relative_open_closes_handle_if_native_call_interrupts_after_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    closed: list[int] = []

    class FakeCall:
        def __init__(self, operation: object) -> None:
            self.operation = operation
            self.argtypes: object | None = None
            self.restype: object | None = None

        def __call__(self, *args: object) -> object:
            return self.operation(*args)  # type: ignore[operator]

    def create_then_interrupt(output_handle: object, *_args: object) -> int:
        pointer = ctypes.cast(output_handle, ctypes.POINTER(ctypes.c_void_p))
        pointer[0] = ctypes.c_void_p(909)
        raise KeyboardInterrupt()

    class FakeNtdll:
        NtCreateFile = FakeCall(create_then_interrupt)

    monkeypatch.setattr(pdf_assemble_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: FakeNtdll(),
        raising=False,
    )
    monkeypatch.setattr(
        pdf_assemble_module.pdf_acquire_module,
        "_close_windows_handle",
        closed.append,
    )

    with pytest.raises(KeyboardInterrupt):
        pdf_assemble_module._windows_nt_create_relative(
            41,
            "translated.pdf",
            desired_access=0x00100080,
            create_disposition=1,
            create_options=0x00200060,
            file_attributes=0,
        )

    assert closed == [909]


def test_windows_anchored_input_closes_native_handle_if_stream_conversion_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "document.json").write_text("{}", encoding="utf-8")
    result = directory.lstat()
    closed: list[int] = []

    class FakeAnchor:
        handle = 41

        def current_path(self) -> Path:
            return directory

        def close(self) -> None:
            return None

    class FakeMsvcrt:
        @staticmethod
        def open_osfhandle(_handle: int, _flags: int) -> int:
            raise KeyboardInterrupt()

    anchor = pdf_assemble_module._DirectoryAnchor(
        directory,
        "run",
        (result.st_dev, result.st_ino),
        None,
        FakeAnchor(),
    )
    monkeypatch.setattr(pdf_assemble_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_open_relative_read_file",
        lambda _root, _name: 919,
        raising=False,
    )
    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_file_identity",
        lambda _handle, *, require_regular: (7, 11),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt())
    monkeypatch.setattr(
        pdf_assemble_module.pdf_acquire_module,
        "_close_windows_handle",
        closed.append,
    )

    with pytest.raises(KeyboardInterrupt):
        pdf_assemble_module._open_anchored_input_file(
            anchor,
            "document.json",
            "PDF document",
        )

    assert closed == [919]


def test_windows_child_creation_closes_handle_if_anchor_construction_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "run"
    directory.mkdir()
    result = directory.lstat()
    closed: list[int] = []
    deleted: list[int] = []

    class FakeAnchor:
        handle = 41

        def current_path(self) -> Path:
            return directory

        def close(self) -> None:
            return None

    parent = pdf_assemble_module._DirectoryAnchor(
        directory,
        "run",
        (result.st_dev, result.st_ino),
        None,
        FakeAnchor(),
    )
    monkeypatch.setattr(pdf_assemble_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_create_relative_directory",
        lambda _root, _name: 919,
    )

    def interrupt_anchor(_handle: int) -> object:
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        pdf_assemble_module.pdf_acquire_module,
        "_WindowsDirectoryPathAnchor",
        interrupt_anchor,
    )
    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_delete_open_file",
        deleted.append,
    )
    monkeypatch.setattr(
        pdf_assemble_module.pdf_acquire_module,
        "_close_windows_handle",
        closed.append,
    )

    with pytest.raises(KeyboardInterrupt):
        pdf_assemble_module._create_child_directory(parent, "child", "child")

    assert deleted == [919]
    assert closed == [919]


def test_windows_existing_media_anchor_closes_handle_if_construction_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "run"
    directory.mkdir()
    result = directory.lstat()
    closed: list[int] = []

    class FakeAnchor:
        handle = 41

        def current_path(self) -> Path:
            return directory

        def close(self) -> None:
            return None

    parent = pdf_assemble_module._DirectoryAnchor(
        directory,
        "run",
        (result.st_dev, result.st_ino),
        None,
        FakeAnchor(),
    )
    monkeypatch.setattr(pdf_assemble_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_open_relative_directory",
        lambda _root, _name: 929,
    )
    monkeypatch.setattr(
        pdf_assemble_module.pdf_acquire_module,
        "_WindowsDirectoryPathAnchor",
        lambda _handle: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        pdf_assemble_module.pdf_acquire_module,
        "_close_windows_handle",
        closed.append,
    )

    with pytest.raises(KeyboardInterrupt):
        pdf_assemble_module._open_existing_child_directory(
            parent,
            "media",
            "PDF media",
        )

    assert closed == [929]


def test_windows_publication_closes_source_handle_on_rename_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []

    class FakeAnchor:
        def __init__(self, handle: int) -> None:
            self.handle = handle

        def current_path(self) -> Path:
            return tmp_path

        def close(self) -> None:
            return None

    source_anchor = pdf_assemble_module._DirectoryAnchor(
        tmp_path / "source", "source", (1, 1), None, FakeAnchor(41)
    )
    destination_anchor = pdf_assemble_module._DirectoryAnchor(
        tmp_path / "destination", "destination", (1, 2), None, FakeAnchor(42)
    )
    monkeypatch.setattr(pdf_assemble_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_open_relative_file",
        lambda _root, _name: 808,
    )

    def interrupt_rename(_source: int, _root: int, _name: str) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_rename_open_file",
        interrupt_rename,
    )
    monkeypatch.setattr(
        pdf_assemble_module.pdf_acquire_module,
        "_close_windows_handle",
        closed.append,
    )

    with pytest.raises(KeyboardInterrupt):
        pdf_assemble_module._windows_move_anchored_file(
            source_anchor,
            "translated.pdf",
            destination_anchor,
            "translated.pdf",
        )

    assert closed == [808]


def test_windows_publication_opens_source_relative_to_held_directory_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ctypes

    source = tmp_path / "source"
    source.mkdir()
    moved_source = tmp_path / "moved-source"
    destination = tmp_path / "destination"
    destination.mkdir()
    (source / "translated.pdf").write_bytes(b"owned translated PDF")
    paths_by_handle: dict[int, Path] = {}
    swapped = False

    class FakeAnchor:
        def __init__(self, handle: int, current: Path) -> None:
            self.handle = handle
            self._current = current

        def current_path(self) -> Path:
            return self._current

        def close(self) -> None:
            return None

    class FakeCall:
        def __init__(self, operation: object) -> None:
            self.operation = operation
            self.argtypes: object | None = None
            self.restype: object | None = None

        def __call__(self, *args: object) -> object:
            return self.operation(*args)  # type: ignore[operator]

    def swap_source_directory() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        source.rename(moved_source)
        source.mkdir()
        (source / "translated.pdf").write_bytes(b"foreign translated PDF")

    def path_based_open(*_args: object) -> int:
        swap_source_directory()
        paths_by_handle[101] = source / "translated.pdf"
        return 101

    def path_based_rename(
        handle: object,
        _information_class: object,
        _payload: object,
        _payload_size: object,
    ) -> int:
        os.rename(paths_by_handle[int(handle)], destination / "translated.pdf")
        return 1

    class FakeKernel32:
        CreateFileW = FakeCall(path_based_open)
        SetFileInformationByHandle = FakeCall(path_based_rename)

    def relative_open(root_handle: int, name: str) -> int:
        assert root_handle == 41
        assert name == "translated.pdf"
        swap_source_directory()
        paths_by_handle[202] = moved_source / name
        return 202

    def relative_rename(handle: int, root_handle: int, name: str) -> None:
        assert root_handle == 42
        os.rename(paths_by_handle[handle], destination / name)

    source_anchor = pdf_assemble_module._DirectoryAnchor(
        source,
        "source",
        (1, 1),
        None,
        FakeAnchor(41, source),
    )
    destination_anchor = pdf_assemble_module._DirectoryAnchor(
        destination,
        "destination",
        (1, 2),
        None,
        FakeAnchor(42, destination),
    )
    monkeypatch.setattr(pdf_assemble_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: FakeKernel32(),
        raising=False,
    )
    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_open_relative_file",
        relative_open,
        raising=False,
    )
    monkeypatch.setattr(
        pdf_assemble_module,
        "_windows_rename_open_file",
        relative_rename,
        raising=False,
    )

    published_handle = pdf_assemble_module._windows_move_anchored_file(
        source_anchor,
        "translated.pdf",
        destination_anchor,
        "translated.pdf",
    )

    assert published_handle == 202
    assert (destination / "translated.pdf").read_bytes() == b"owned translated PDF"
    assert (source / "translated.pdf").read_bytes() == b"foreign translated PDF"


@pytest.mark.parametrize("existing", ["staged-output", "layout.json", "final"])
def test_assemble_pdf_rejects_preexisting_destinations_without_overwrite(
    tmp_path: Path, existing: str
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    final_output = tmp_path / "final"
    path = final_output if existing == "final" else run_dir / existing
    if existing == "layout.json":
        path.write_text("keep", encoding="utf-8")
    else:
        path.mkdir(parents=True)
        (path / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(PdfAssemblyError, match="already exists"):
        assemble_pdf(run_dir, translations, glossary, final_output)

    if path.is_dir():
        assert (path / "keep.txt").read_text(encoding="utf-8") == "keep"
    else:
        assert path.read_text(encoding="utf-8") == "keep"


def test_assemble_pdf_reflows_rich_content_with_complete_layout_evidence(
    tmp_path: Path,
) -> None:
    run_dir, translations, glossary, identifiers = _rich_assembly_run(tmp_path)

    staged = assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    reader = PdfReader(staged)
    assert len(reader.pages) >= 4
    dimensions = [
        (float(page.mediabox.width), float(page.mediabox.height))
        for page in reader.pages
    ]
    assert any(width < height for width, height in dimensions)
    assert any(width > height for width, height in dimensions)
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    landscape_pages = sum(width > height for width, height in dimensions)
    assert extracted.count("병합 머리글") == landscape_pages
    assert extracted.count("반복 머리글") == len(reader.pages)
    assert extracted.count("반복 바닥글") == len(reader.pages)
    assert "그림 1. 원본 렌더링 픽셀" in extracted
    assert "1 페이지 지역 각주" in extracted
    assert "2 절 끝 각주" in extracted

    external_uris: list[str] = []
    internal_destinations: list[object] = []
    for page in reader.pages:
        for annotation_ref in page.get("/Annots", []):
            annotation = annotation_ref.get_object()
            action = annotation.get("/A")
            if action is not None and str(action.get_object().get("/S")) == "/URI":
                external_uris.append(str(action.get_object()["/URI"]))
            if annotation.get("/Dest") is not None:
                internal_destinations.append(annotation["/Dest"])
    assert external_uris == ["https://example.com/search?q=a&lang=ko"]
    assert len(internal_destinations) >= 3

    layout = read_pdf_layout(run_dir / "layout.json")
    by_block: dict[str, list[object]] = {}
    for item in layout.flowables:
        by_block.setdefault(item.block_id, []).append(item)
        assert all(math.isfinite(value) for value in (*item.bounds, *item.frame))
        x, y, width, height = item.bounds
        frame_x, frame_y, frame_width, frame_height = item.frame
        assert frame_x <= x <= x + width <= frame_x + frame_width
        assert frame_y <= y <= y + height <= frame_y + frame_height
    for parts in by_block.values():
        assert [item.split_part for item in parts] == list(range(len(parts)))
    rich_document = PdfDocument.from_dict(
        json.loads((run_dir / "document.json").read_text(encoding="utf-8"))
    )
    assert {
        block.id for block in rich_document.blocks if block.kind == "table-cell"
    } <= set(by_block)
    header_parts = by_block[identifiers["table_header"]]
    assert len(header_parts) == landscape_pages
    assert [item.split_part for item in header_parts] == list(range(len(header_parts)))
    assert all(item.bounds[2] == pytest.approx(98.0, abs=0.5) for item in header_parts)
    table_blocks = [
        block for block in rich_document.blocks if block.kind == "table-cell"
    ]
    for block in table_blocks:
        assert len(by_block[block.id]) == (
            landscape_pages if block.row == 0 else 1
        )
        assert all(item.frame == header_parts[0].frame for item in by_block[block.id])
    table_target = by_block[identifiers["table_link_target"]][0]
    table_target_top = table_target.bounds[1] + table_target.bounds[3]
    assert any(
        float(destination[3]) == pytest.approx(table_target_top, abs=0.5)
        for destination in internal_destinations
        if isinstance(destination, list) and len(destination) >= 4
    )

    figure_layout = by_block[identifiers["figure"]][0]
    caption_layout = by_block[identifiers["caption"]][0]
    assert figure_layout.page_number == caption_layout.page_number
    assert figure_layout.bounds[2] / figure_layout.bounds[3] == pytest.approx(2.0)
    assert figure_layout.bounds[2] <= 120.0
    assert figure_layout.bounds[3] <= 60.0
    assert figure_layout.bounds[1] - sum(caption_layout.bounds[1::2]) <= 12.0

    owner_layout = by_block[identifiers["owner"]][0]
    page_note_layout = by_block[identifiers["page_note"]][0]
    assert owner_layout.page_number == page_note_layout.page_number
    assert page_note_layout.frame[1] < owner_layout.frame[1]
    section_owner_layout = by_block[identifiers["section_owner"]][0]
    section_note_layout = by_block[identifiers["section_note"]][0]
    target_layout = by_block[identifiers["target"]][0]
    assert section_note_layout.frame == section_owner_layout.frame
    assert extracted.index("2 절 끝 각주") < extracted.index("내부 목적지")
    assert section_note_layout.page_number <= target_layout.page_number


def test_pdf_layout_reader_rejects_contained_table_cell_peer_overlap(
    tmp_path: Path,
) -> None:
    run_dir, translations, glossary, _identifiers = _rich_assembly_run(
        tmp_path,
        table_columns=4,
        table_rows=4,
    )
    assemble_pdf(run_dir, translations, glossary, tmp_path / "final")
    path = run_dir / "layout.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    cells = [item for item in payload["flowables"] if item["kind"] == "table-cell"]
    outer, inner = cells[-2:]
    outer_x, outer_y, outer_width, outer_height = outer["bounds"]
    inner.update(
        bounds=[
            outer_x + 1.0,
            outer_y + 1.0,
            outer_width / 2.0,
            outer_height / 2.0,
        ],
        frame=list(outer["frame"]),
        page_number=outer["page_number"],
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PdfAssemblyError, match="overlapping peer flowables"):
        read_pdf_layout(path)


def test_assemble_pdf_keeps_readable_native_table_on_portrait_pages(
    tmp_path: Path,
) -> None:
    run_dir, translations, glossary, _identifiers = _rich_assembly_run(
        tmp_path,
        table_columns=4,
        table_rows=4,
    )

    staged = assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert all(
        float(page.mediabox.width) < float(page.mediabox.height)
        for page in PdfReader(staged).pages
    )


def test_page_local_footnote_is_emitted_once_when_its_owner_splits(
    tmp_path: Path,
) -> None:
    run_dir, translations, glossary, identifiers = _rich_assembly_run(
        tmp_path,
        table_columns=4,
        table_rows=4,
    )
    document = PdfDocument.from_dict(
        json.loads((run_dir / "document.json").read_text(encoding="utf-8"))
    )
    owner = next(block for block in document.blocks if block.id == identifiers["owner"])
    assert owner.segment_id is not None
    translations[owner.segment_id] = Translation(
        owner.segment_id,
        "페이지 지역 표지 1 " * 250,
    )

    staged = assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    reader = PdfReader(staged)
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert extracted.count("1 페이지 지역 각주") == 1
    layout = read_pdf_layout(run_dir / "layout.json")
    owner_parts = [
        item for item in layout.flowables if item.block_id == identifiers["owner"]
    ]
    page_note = next(
        item for item in layout.flowables if item.block_id == identifiers["page_note"]
    )
    assert len(owner_parts) > 1
    assert page_note.page_number == owner_parts[0].page_number


def test_page_local_footnote_owned_by_repeated_table_cell_is_emitted_once(
    tmp_path: Path,
) -> None:
    run_dir, translations, glossary, identifiers = _rich_assembly_run(tmp_path)
    path = run_dir / "document.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for block in payload["blocks"]:
        if block["id"] == identifiers["owner"]:
            block["destination"] = None
        elif block["id"] == identifiers["table_link_target"]:
            block["destination"] = identifiers["page_note"]
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    staged = assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    extracted = "\n".join(page.extract_text() or "" for page in PdfReader(staged).pages)
    assert extracted.count("1 페이지 지역 각주") == 1
    layout = read_pdf_layout(run_dir / "layout.json")
    owner_parts = [
        item
        for item in layout.flowables
        if item.block_id == identifiers["table_link_target"]
    ]
    note = next(
        item for item in layout.flowables if item.block_id == identifiers["page_note"]
    )
    assert len(owner_parts) > 1
    assert note.page_number == owner_parts[0].page_number


def test_caption_above_figure_is_emitted_once_in_source_order(
    tmp_path: Path,
) -> None:
    run_dir, translations, glossary, identifiers = _rich_assembly_run(
        tmp_path,
        table_columns=4,
        table_rows=4,
    )
    path = run_dir / "document.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    blocks = payload["blocks"]
    figure_index = next(
        index for index, block in enumerate(blocks) if block["id"] == identifiers["figure"]
    )
    caption_index = next(
        index for index, block in enumerate(blocks) if block["id"] == identifiers["caption"]
    )
    blocks[figure_index], blocks[caption_index] = blocks[caption_index], blocks[figure_index]
    for order, block in enumerate(blocks):
        block["order"] = order
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    staged = assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    extracted = "\n".join(page.extract_text() or "" for page in PdfReader(staged).pages)
    assert extracted.count("그림 1. 원본 렌더링 픽셀") == 1
    layout = read_pdf_layout(run_dir / "layout.json")
    captions = [
        item for item in layout.flowables if item.block_id == identifiers["caption"]
    ]
    figure = next(
        item for item in layout.flowables if item.block_id == identifiers["figure"]
    )
    assert len(captions) == 1
    caption = captions[0]
    assert caption.page_number == figure.page_number
    figure_top = figure.bounds[1] + figure.bounds[3]
    assert 0.0 <= caption.bounds[1] - figure_top <= 12.0


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/a b",
        "https://example.com/a<bad>",
        "https://example.com/a%2",
        "https:///missing-authority",
        "https://example.com:bad/path",
        "https://example.com:-1/path",
        "https://example.com:/path",
        "https://example.com:0/path",
        "https://example.com:65536/path",
        "https://example.com:99999/path",
        "https://a@b@c/path",
        "https://@example.com/path",
        "https://:pass@example.com/path",
        "https://example..com/path",
        "https://[2001:db8::1/path",
        "https://[not-ip]/path",
        "https://2001:db8::1/path",
        "mailto:",
    ],
)
def test_rich_assembly_rejects_raw_or_structurally_invalid_uri(
    tmp_path: Path,
    uri: str,
) -> None:
    run_dir, translations, glossary, identifiers = _rich_assembly_run(
        tmp_path,
        table_columns=4,
        table_rows=4,
    )
    path = run_dir / "document.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    external = next(
        block for block in payload["blocks"] if block["id"] == identifiers["external"]
    )
    external["uri"] = uri
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(PdfAssemblyError, match="unsafe external URI"):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")


@pytest.mark.parametrize(
    "expected",
    [
        "https://example.com/a%20b?q=x%2Fy&lang=ko",
        "https://example.com:1/path",
        "https://example.com:65535/path",
        "https://user:pass@example.com:443/path",
        "https://user:@example.com/path",
        "https://[2001:db8::1]:443/a%20b?q=x%2Fy",
    ],
)
def test_rich_assembly_preserves_valid_external_uri_exactly(
    tmp_path: Path,
    expected: str,
) -> None:
    run_dir, translations, glossary, identifiers = _rich_assembly_run(
        tmp_path,
        table_columns=4,
        table_rows=4,
    )
    path = run_dir / "document.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    external = next(
        block for block in payload["blocks"] if block["id"] == identifiers["external"]
    )
    external["uri"] = expected
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    staged = assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    uris = [
        str(annotation.get_object()["/A"].get_object()["/URI"])
        for page in PdfReader(staged).pages
        for annotation in page.get("/Annots", [])
        if annotation.get_object().get("/A") is not None
        and str(annotation.get_object()["/A"].get_object().get("/S")) == "/URI"
    ]
    assert uris == [expected]


def test_rich_assembly_closes_anchored_media_on_build_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, translations, glossary, _identifiers = _rich_assembly_run(
        tmp_path,
        table_columns=4,
        table_rows=4,
    )
    original_media_anchor = pdf_assemble_module._open_existing_child_directory
    original_input = pdf_assemble_module._open_anchored_input_file
    media_directory_descriptor: int | None = None
    media_file_descriptor: int | None = None

    def capture_media_anchor(*args: object, **kwargs: object) -> object:
        nonlocal media_directory_descriptor
        anchor = original_media_anchor(*args, **kwargs)
        media_directory_descriptor = anchor.descriptor
        return anchor

    def capture_media_file(*args: object, **kwargs: object) -> object:
        nonlocal media_file_descriptor
        opened = original_input(*args, **kwargs)
        if "figure media" in str(args[2]):
            media_file_descriptor = opened.stream.fileno()
        return opened

    monkeypatch.setattr(
        pdf_assemble_module,
        "_open_existing_child_directory",
        capture_media_anchor,
    )
    monkeypatch.setattr(
        pdf_assemble_module,
        "_open_anchored_input_file",
        capture_media_file,
    )
    monkeypatch.setattr(
        pdf_assemble_module,
        "_build_rich_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert media_directory_descriptor is not None
    assert media_file_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(media_directory_descriptor)
    with pytest.raises(OSError):
        os.fstat(media_file_descriptor)
    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()
    assert not any(
        path.name.startswith(".pdf-assembling-") for path in run_dir.iterdir()
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "figure media"),
        ("dimensions", "dimensions do not match source bounds"),
        ("bounds", "figure bounds fall outside source page"),
    ],
)
def test_rich_assembly_fails_closed_on_invalid_media_evidence(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    run_dir, translations, glossary, _identifiers = _rich_assembly_run(
        tmp_path,
        table_columns=4,
        table_rows=4,
    )
    media = run_dir / "media" / "figure-0001.png"
    if mutation == "missing":
        media.unlink()
    elif mutation == "dimensions":
        Image.new("RGB", (120, 120), (42, 120, 196)).save(media)
    else:
        payload = json.loads((run_dir / "document.json").read_text(encoding="utf-8"))
        figure = next(block for block in payload["blocks"] if block["kind"] == "figure")
        figure["bbox"] = [72.0, 390.0, 700.0, 450.0]
        (run_dir / "document.json").write_text(
            json.dumps(payload, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    with pytest.raises(PdfAssemblyError, match=message):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()


def test_assemble_pdf_fails_when_table_is_unreadable_at_nine_points(
    tmp_path: Path,
) -> None:
    run_dir, translations, glossary, _identifiers = _rich_assembly_run(
        tmp_path,
        table_columns=14,
        table_rows=4,
    )

    with pytest.raises(PdfAssemblyError, match="table .* unreadable at 9-point"):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()
