from __future__ import annotations

from dataclasses import replace
import hashlib
from importlib.resources import as_file, files
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

from fontTools.ttLib import TTFont
import pdfplumber
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


def test_assemble_pdf_fails_closed_without_anchored_child_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    monkeypatch.setattr(
        pdf_assemble_module,
        "_DIRFD_PUBLICATION_SUPPORTED",
        False,
    )

    with pytest.raises(PdfAssemblyError, match="anchored child creation unavailable"):
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


def test_assemble_pdf_fails_closed_on_task_8_rich_blocks(tmp_path: Path) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    payload = json.loads((run_dir / "document.json").read_text(encoding="utf-8"))
    payload["blocks"][2]["kind"] = "caption"
    (run_dir / "document.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PdfAssemblyError, match="unsupported PDF block kind.*caption"):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()
