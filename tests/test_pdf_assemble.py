from __future__ import annotations

import hashlib
from importlib.resources import as_file, files
import json
from pathlib import Path
import subprocess
import sys

from fontTools.ttLib import TTFont
from pypdf import PdfReader
import pytest

import web_translator.pdf_assemble as pdf_assemble_module
from web_translator.models import ProtectedToken, Segment, Translation, write_segments
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

    monkeypatch.setattr(pdf_assemble_module, "write_pdf_layout", interrupt_layout)

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

    def interrupt_publication(source: Path, destination: Path) -> object:
        if destination.name == "translated.pdf":
            return original_publish(source, destination)
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

    def replace_then_interrupt(source: Path, destination: Path) -> object:
        if destination.name == "translated.pdf":
            identity = original_publish(source, destination)
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
            destination_path.write_bytes(b"unrelated racer")
        original_link(source, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pdf_assemble_module.os, "link", race_staged_destination)

    with pytest.raises(PdfAssemblyError, match="already exists"):
        assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    racer = run_dir / "staged-output" / "translated.pdf"
    assert racer.read_bytes() == b"unrelated racer"
    assert not (run_dir / "layout.json").exists()


def test_assemble_pdf_uses_no_hard_link_fallback_for_windows_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, translations, glossary = _assembly_run(tmp_path)
    import web_translator.pdf_flowables as flowables_module

    rename_destinations: list[Path] = []
    original_rename = pdf_assemble_module.os.rename

    def unsupported_link(*args: object, **kwargs: object) -> None:
        raise NotImplementedError()

    def tracked_rename(source: str | Path, destination: str | Path) -> None:
        rename_destinations.append(Path(destination))
        original_rename(source, destination)

    monkeypatch.setattr(pdf_assemble_module.os, "link", unsupported_link)
    monkeypatch.setattr(pdf_assemble_module.os, "rename", tracked_rename)
    monkeypatch.setattr(pdf_assemble_module, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(flowables_module.os, "link", unsupported_link)
    monkeypatch.setattr(flowables_module, "_IS_WINDOWS", True, raising=False)

    staged = assemble_pdf(run_dir, translations, glossary, tmp_path / "final")

    assert staged.is_file()
    assert (run_dir / "layout.json").is_file()
    assert len(rename_destinations) == 3
    assert rename_destinations[-2:] == [staged, run_dir / "layout.json"]


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
