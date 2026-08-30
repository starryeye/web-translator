from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import struct

from PIL import Image, ImageDraw
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    FloatObject,
    NameObject,
    NullObject,
    TextStringObject,
)
from reportlab.pdfgen.canvas import Canvas

from web_translator.models import ProtectedToken, Segment, Translation, write_segments
from web_translator.pdf_assemble import assemble_pdf
from web_translator.pdf_models import (
    PdfBlock,
    PdfBlockStyle,
    PdfDocument,
    PdfPage,
    PdfSourceRecord,
    PdfTableCell,
)
from web_translator.pdf_qa import (
    PdfQAFailure,
    PdfQAResult,
    finalize_pdf_output,
    prepare_pdf_qa,
    read_pdf_layout_review,
)
from web_translator.pdf_media import build_contact_sheets, render_pdf_pages
from web_translator.pdf_review import build_pdf_semantic_review_input
import web_translator.pdf_qa as pdf_qa_module
from tests.pdf_fixtures import make_text_pdf


REVIEW_DIMENSIONS = (
    "semantic_fidelity",
    "qualification_preservation",
    "naturalness",
    "terminology",
    "boundary_consistency",
    "protected_content",
)

VISUAL_DIMENSIONS = (
    "heading_hierarchy",
    "text_legibility",
    "table_legibility",
    "figure_caption_pairing",
    "footnote_placement",
    "page_transitions",
    "clipping_overlap",
    "glyph_rendering",
)


@dataclass(frozen=True, slots=True)
class PdfQARun:
    run_dir: Path
    output_dir: Path


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_review(run_dir: Path) -> None:
    semantic_input = build_pdf_semantic_review_input(run_dir)
    _write_json(
        run_dir / "review.json",
        {
            "semantic_input_sha256": semantic_input.semantic_input_sha256,
            "retries": {"zone-001": 0},
            "section_findings": {
                "zone-001": [
                    {
                        "dimension": dimension,
                        "verdict": "pass",
                        "evidence": f"Checked {dimension} against the source.",
                    }
                    for dimension in REVIEW_DIMENSIONS
                ]
            },
            "unresolved_required": [],
        },
    )


def _refresh_review_digest(run_dir: Path) -> None:
    review_path = run_dir / "review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["semantic_input_sha256"] = build_pdf_semantic_review_input(
        run_dir
    ).semantic_input_sha256
    _write_json(review_path, review)


def _write_passing_layout_review(run_dir: Path, *, staged_sha256: str | None = None) -> None:
    qa = json.loads((run_dir / "pdf-qa.json").read_text(encoding="utf-8"))
    _write_json(
        run_dir / "pdf-layout-review.json",
        {
            "schema_version": "1.0",
            "staged_pdf_sha256": staged_sha256 or qa["staged_pdf_sha256"],
            "pages_reviewed": list(range(1, len(qa["rendered_page_hashes"]) + 1)),
            "contact_sheets_reviewed": qa["contact_sheet_pages"],
            "findings": {
                dimension: {"verdict": "pass", "evidence": f"Reviewed {dimension}."}
                for dimension in VISUAL_DIMENSIONS
            },
            "unresolved_required": [],
        },
    )


@pytest.fixture
def assembled_pdf_run(tmp_path: Path) -> PdfQARun:
    run_dir = tmp_path / ".web-translator" / "runs" / "result"
    run_dir.mkdir(parents=True)
    (tmp_path / "translated-pdfs").mkdir()
    source_pdf = make_text_pdf(run_dir / "source.pdf")
    source_sha256 = _sha256(source_pdf)
    blocks = [
        PdfBlock(
            id="pdf:page-0001:block-0001",
            page_number=1,
            order=0,
            kind="heading",
            bbox=(72.0, 72.0, 540.0, 100.0),
            style=PdfBlockStyle(18.0, True, "left", 0.0, 8.0),
            source_text="Quality evidence",
            segment_id="seg-000001",
        ),
        PdfBlock(
            id="pdf:page-0001:block-0002",
            page_number=1,
            order=1,
            kind="paragraph",
            bbox=(72.0, 112.0, 540.0, 148.0),
            style=PdfBlockStyle(11.0, False, "left", 0.0, 8.0),
            source_text="Read https://example.com/a%20b",
            segment_id="seg-000002",
            uri="https://example.com/a%20b",
        ),
        PdfBlock(
            id="pdf:page-0001:table-0001:row-0000:cell-0000",
            page_number=1,
            order=2,
            kind="table-cell",
            bbox=(72.0, 170.0, 260.0, 198.0),
            style=PdfBlockStyle(10.0, True, "left", 0.0, 4.0),
            source_text="Item",
            segment_id="seg-000003",
            table_id="pdf:page-0001:table-0001",
            row=0,
            column=0,
        ),
        PdfBlock(
            id="pdf:page-0001:table-0001:row-0000:cell-0001",
            page_number=1,
            order=3,
            kind="table-cell",
            bbox=(260.0, 170.0, 448.0, 198.0),
            style=PdfBlockStyle(10.0, True, "left", 0.0, 4.0),
            source_text="Value",
            segment_id="seg-000004",
            table_id="pdf:page-0001:table-0001",
            row=0,
            column=1,
        ),
        PdfBlock(
            id="pdf:page-0001:block-0003",
            page_number=1,
            order=4,
            kind="figure",
            bbox=(72.0, 230.0, 192.0, 290.0),
            style=PdfBlockStyle(11.0, False, "center", 0.0, 4.0),
            media_path="media/figure-0001.png",
            caption_id="pdf:page-0001:block-0004",
        ),
        PdfBlock(
            id="pdf:page-0001:block-0004",
            page_number=1,
            order=5,
            kind="caption",
            bbox=(72.0, 296.0, 300.0, 320.0),
            style=PdfBlockStyle(9.0, False, "left", 0.0, 4.0),
            source_text="Figure caption",
            segment_id="seg-000005",
            caption_id="pdf:page-0001:block-0003",
        ),
        PdfBlock(
            id="pdf:page-0001:block-0005",
            page_number=1,
            order=6,
            kind="paragraph",
            bbox=(72.0, 336.0, 540.0, 366.0),
            style=PdfBlockStyle(11.0, False, "left", 0.0, 8.0),
            source_text="Jump to heading",
            segment_id="seg-000006",
            destination="pdf:page-0001:block-0001",
        ),
    ]
    document = PdfDocument(
        schema_version="1.0",
        source_sha256=source_sha256,
        page_count=1,
        selectable_characters=120,
        scan_candidate_pages=[],
        pages=[PdfPage(number=1, width=612.0, height=792.0, rotation=0)],
        blocks=blocks,
        table_cells=[
            PdfTableCell(
                id=blocks[2].id,
                table_id="pdf:page-0001:table-0001",
                page_number=1,
                row=0,
                column=0,
                row_span=1,
                column_span=1,
                is_header=True,
                block_id=blocks[2].id,
            ),
            PdfTableCell(
                id=blocks[3].id,
                table_id="pdf:page-0001:table-0001",
                page_number=1,
                row=0,
                column=1,
                row_span=1,
                column_span=1,
                is_header=True,
                block_id=blocks[3].id,
            ),
        ],
    )
    source = PdfSourceRecord(
        schema_version="1.0",
        input_kind="local",
        requested_source="source.pdf",
        final_source="source.pdf",
        content_type="application/pdf",
        byte_length=source_pdf.stat().st_size,
        sha256=source_sha256,
        acquired_at="2026-08-21T01:02:03Z",
        redirects=[],
        warnings=[],
    )
    token = ProtectedToken(
        token="⟦WT:000001⟧",
        kind="url",
        value="https://example.com/a%20b",
    )
    segments = [
        Segment(
            id="seg-000001",
            locator=blocks[0].id,
            semantic_type="heading",
            heading_path=[],
            source_text="Quality evidence",
            protected=[],
            context_ids=[],
            target=True,
        ),
        Segment(
            id="seg-000002",
            locator=blocks[1].id,
            semantic_type="paragraph",
            heading_path=["Quality evidence"],
            source_text="Read ⟦WT:000001⟧",
            protected=[token],
            context_ids=["seg-000001"],
            target=True,
        ),
        Segment(
            id="seg-000003",
            locator=blocks[2].id,
            semantic_type="table-cell",
            heading_path=["Quality evidence"],
            source_text="Item",
            protected=[],
            context_ids=["seg-000002", "seg-000004"],
            target=True,
        ),
        Segment(
            id="seg-000004",
            locator=blocks[3].id,
            semantic_type="table-cell",
            heading_path=["Quality evidence"],
            source_text="Value",
            protected=[],
            context_ids=["seg-000003", "seg-000005"],
            target=True,
        ),
        Segment(
            id="seg-000005",
            locator=blocks[5].id,
            semantic_type="caption",
            heading_path=["Quality evidence"],
            source_text="Figure caption",
            protected=[],
            context_ids=["seg-000004", "seg-000006"],
            target=True,
        ),
        Segment(
            id="seg-000006",
            locator=blocks[6].id,
            semantic_type="paragraph",
            heading_path=["Quality evidence"],
            source_text="Jump to heading",
            protected=[],
            context_ids=["seg-000005"],
            target=True,
        ),
    ]
    translations = {
        "seg-000001": Translation("seg-000001", "PDF 품질 검증"),
        "seg-000002": Translation("seg-000002", "⟦WT:000001⟧ 문서를 읽으세요"),
        "seg-000003": Translation("seg-000003", "항목"),
        "seg-000004": Translation("seg-000004", "값"),
        "seg-000005": Translation("seg-000005", "그림 설명"),
        "seg-000006": Translation("seg-000006", "제목으로 이동"),
    }
    _write_json(run_dir / "document.json", document.to_dict())
    _write_json(run_dir / "source.json", source.to_dict())
    write_segments(run_dir / "segments.jsonl", segments)
    _write_json(run_dir / "glossary.json", {})
    zones = run_dir / "zones"
    zones.mkdir()
    _write_json(
        zones / "zone-001.json",
        {
            "attempt": 0,
            "context_after_ids": [],
            "context_before_ids": [],
            "expected_tokens": {
                "seg-000001": [],
                "seg-000002": ["⟦WT:000001⟧"],
                "seg-000003": [],
                "seg-000004": [],
                "seg-000005": [],
                "seg-000006": [],
            },
            "heading_path": [],
            "id": "zone-001",
            "target_ids": [
                "seg-000001",
                "seg-000002",
                "seg-000003",
                "seg-000004",
                "seg-000005",
                "seg-000006",
            ],
        },
    )
    translation_dir = run_dir / "translations"
    translation_dir.mkdir()
    (translation_dir / "zone-001.jsonl").write_text(
        "".join(
            json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
            for record in translations.values()
        ),
        encoding="utf-8",
    )
    assignments = run_dir / "assignments"
    assignments.mkdir()
    _write_json(
        assignments / "zone-001.json",
        {
            "context_after": [],
            "context_before": [],
            "document_summary": "QA fixture",
            "glossary": {},
            "schema_version": "1.0",
            "targets": [
                {"id": segment.id, "source_text": segment.source_text}
                for segment in segments
            ],
            "zone_id": "zone-001",
        },
    )
    _write_review(run_dir)
    media = run_dir / "media"
    media.mkdir()
    figure = Image.new("RGB", (240, 120), "#336699")
    for x in range(240):
        figure.putpixel((x, x // 2), (220, 120, 20))
    figure.save(media / "figure-0001.png")
    figure.close()
    output_dir = tmp_path / "translated-pdfs" / "result"
    assemble_pdf(run_dir, translations, {}, output_dir)
    assert not output_dir.exists()
    return PdfQARun(run_dir, output_dir)


def test_prepare_pdf_qa_renders_every_page_and_covers_contact_sheets(
    assembled_pdf_run: PdfQARun,
) -> None:
    result = prepare_pdf_qa(
        assembled_pdf_run.run_dir,
        assembled_pdf_run.output_dir,
    )

    reader = PdfReader(assembled_pdf_run.run_dir / "staged-output" / "translated.pdf")
    expected_pages = list(range(1, len(reader.pages) + 1))
    assert result.passed is True
    assert [path.name for path in result.rendered_pages] == [
        f"page-{page:03d}.png" for page in expected_pages
    ]
    assert result.contact_sheet_pages == {"contact-sheet-001.png": expected_pages}
    assert result.staged_pdf_sha256 == _sha256(
        assembled_pdf_run.run_dir / "staged-output" / "translated.pdf"
    )
    assert (assembled_pdf_run.run_dir / "pdf-qa.json").is_file()
    record = json.loads(
        (assembled_pdf_run.run_dir / "pdf-qa.json").read_text(encoding="utf-8")
    )
    assert set(record) == {
        "contact_sheet_hashes",
        "contact_sheet_pages",
        "findings",
        "metrics",
        "passed",
        "rendered_page_hashes",
        "schema_version",
        "staged_pdf_sha256",
    }
    assert record["rendered_page_hashes"] == result.rendered_page_hashes
    assert record["contact_sheet_hashes"] == result.contact_sheet_hashes
    assert [finding["code"] for finding in record["findings"]] == sorted(
        finding["code"] for finding in record["findings"]
    )
    assert not assembled_pdf_run.output_dir.exists()


@pytest.mark.parametrize(
    "relative_path",
    [
        "segments.jsonl",
        "zones/zone-001.json",
        "assignments/zone-001.json",
        "translations/zone-001.jsonl",
        "glossary.json",
    ],
)
def test_prepare_pdf_qa_rejects_inputs_mutated_after_semantic_review(
    assembled_pdf_run: PdfQARun,
    relative_path: str,
) -> None:
    path = assembled_pdf_run.run_dir / relative_path
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(PdfQAFailure, match="digest does not match"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert not (assembled_pdf_run.run_dir / "pdf-qa.json").exists()


def test_prepare_pdf_qa_closes_windows_descendants_for_directory_rename_and_reopens_them(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keeping any rendered child open makes a Windows directory rename fail."""
    opened_before_publish: list[object] = []
    reopened_after_publish: list[object] = []
    real_open = pdf_qa_module.assembly._open_anchored_input_file
    real_publish_directory = pdf_qa_module._publish_new_directory
    real_publish_file = pdf_qa_module.assembly._publish_new_file
    directory_published = False

    def capture_open(
        directory: object,
        name: str,
        context: str,
    ) -> object:
        opened = real_open(directory, name, context)  # type: ignore[arg-type]
        if context in {
            "rendered PDF QA page",
            "PDF QA contact sheet",
            "published rendered PDF QA artifact",
        }:
            target = (
                reopened_after_publish
                if directory_published
                else opened_before_publish
            )
            target.append(opened)
        return opened

    def publish_directory(*args: object, **kwargs: object) -> None:
        nonlocal directory_published
        assert opened_before_publish
        assert all(item.stream.closed for item in opened_before_publish)  # type: ignore[attr-defined]
        real_publish_directory(*args, **kwargs)  # type: ignore[arg-type]
        directory_published = True

    def publish_file(*args: object, **kwargs: object) -> object:
        if args[1] == "pdf-qa.json":
            assert reopened_after_publish
            assert all(
                not item.stream.closed for item in reopened_after_publish  # type: ignore[attr-defined]
            )
        return real_publish_file(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        pdf_qa_module,
        "_WINDOWS_RENAME_REQUIRES_CLOSED_DESCENDANTS",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_open_anchored_input_file",
        capture_open,
    )
    monkeypatch.setattr(pdf_qa_module, "_publish_new_directory", publish_directory)
    monkeypatch.setattr(pdf_qa_module.assembly, "_publish_new_file", publish_file)

    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert all(item.stream.closed for item in opened_before_publish)  # type: ignore[attr-defined]
    assert all(item.stream.closed for item in reopened_after_publish)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "mutation",
    ["extra-child", "replacement-identity", "same-identity-content"],
)
def test_prepare_pdf_qa_rejects_windows_close_rename_reopen_artifact_races(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """The Windows handle-release window must not publish changed QA evidence."""
    real_publish = pdf_qa_module._publish_new_directory

    def publish_then_mutate(
        source_parent: object,
        source_name: str,
        source: object,
        destination_parent: object,
        destination_name: str,
    ) -> None:
        real_publish(
            source_parent,  # type: ignore[arg-type]
            source_name,
            source,  # type: ignore[arg-type]
            destination_parent,  # type: ignore[arg-type]
            destination_name,
        )
        published = destination_parent.current_path() / destination_name  # type: ignore[attr-defined]
        page = published / "page-001.png"
        if mutation == "extra-child":
            (published / "unexpected.txt").write_bytes(b"foreign extra child")
        elif mutation == "replacement-identity":
            page.unlink()
            page.write_bytes(b"foreign replacement identity")
        else:
            with page.open("r+b") as stream:
                stream.seek(0)
                stream.write(b"foreign same-inode mutation")
                stream.truncate()

    monkeypatch.setattr(
        pdf_qa_module,
        "_WINDOWS_RENAME_REQUIRES_CLOSED_DESCENDANTS",
        True,
        raising=False,
    )
    monkeypatch.setattr(pdf_qa_module, "_publish_new_directory", publish_then_mutate)

    with pytest.raises(PdfQAFailure, match="rendered PDF QA artifacts"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert not (assembled_pdf_run.run_dir / "qa-pages").exists()
    assert not (assembled_pdf_run.run_dir / "pdf-qa.json").exists()
    assert not assembled_pdf_run.output_dir.exists()


@pytest.mark.parametrize(
    "mutation",
    ["extra-child", "replacement-identity", "same-identity-content"],
)
def test_prepare_pdf_qa_rechecks_public_snapshot_after_json_commit(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """The JSON commit must not bless QA pages changed after postverification."""
    real_publish = pdf_qa_module.assembly._publish_new_file

    def publish_json_then_mutate(
        source_directory: object,
        source_name: str,
        destination_directory: object,
        destination_name: str,
    ) -> object:
        published = real_publish(
            source_directory,  # type: ignore[arg-type]
            source_name,
            destination_directory,  # type: ignore[arg-type]
            destination_name,
        )
        if destination_name != "pdf-qa.json":
            return published
        pages = assembled_pdf_run.run_dir / "qa-pages"
        page = pages / "page-001.png"
        if mutation == "extra-child":
            (pages / "unexpected.txt").write_bytes(b"post-JSON extra child")
        elif mutation == "replacement-identity":
            page.unlink()
            page.write_bytes(b"post-JSON replacement identity")
        else:
            with page.open("r+b") as stream:
                stream.seek(0)
                stream.write(b"post-JSON same-inode rewrite")
                stream.truncate()
        return published

    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_publish_new_file",
        publish_json_then_mutate,
    )

    with pytest.raises(PdfQAFailure, match="rendered PDF QA artifacts"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert not (assembled_pdf_run.run_dir / "pdf-qa.json").exists()
    assert not (assembled_pdf_run.run_dir / "qa-pages").exists()
    assert not assembled_pdf_run.output_dir.exists()


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX directory descriptors")
def test_prepare_pdf_qa_cleanup_preserves_replacement_raced_before_unlink(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = b"unrelated replacement PDF QA record"
    keep = assembled_pdf_run.run_dir / "keep.txt"
    keep.write_bytes(b"unrelated run content")
    real_verify = pdf_qa_module._verify_qa_artifact_snapshot
    real_unlink = os.unlink
    real_rename = os.rename
    race_installed = False

    def fail_after_json_commit(*args: object, **kwargs: object) -> None:
        if (assembled_pdf_run.run_dir / "pdf-qa.json").exists():
            raise PdfQAFailure("injected post-JSON integrity failure")
        real_verify(*args, **kwargs)  # type: ignore[arg-type]

    def race_cleanup_unlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal race_installed
        name = os.fsdecode(path)
        if not race_installed and (
            name == "pdf-qa.json" or name.startswith(".pdf-qa-cleanup-")
        ):
            if name == "pdf-qa.json":
                real_rename(
                    "pdf-qa.json",
                    "captured-owned-pdf-qa.json",
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                )
            (assembled_pdf_run.run_dir / "pdf-qa.json").write_bytes(replacement)
            race_installed = True
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(
        pdf_qa_module,
        "_verify_qa_artifact_snapshot",
        fail_after_json_commit,
    )
    monkeypatch.setattr(pdf_qa_module.os, "unlink", race_cleanup_unlink)

    with pytest.raises(PdfQAFailure, match="post-JSON integrity failure"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert race_installed
    assert (assembled_pdf_run.run_dir / "pdf-qa.json").read_bytes() == replacement
    assert keep.read_bytes() == b"unrelated run content"
    assert not assembled_pdf_run.output_dir.exists()


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX directory descriptors")
def test_remove_owned_qa_record_restores_a_mismatching_visible_file(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    replacement = run_dir / "pdf-qa.json"
    replacement.write_bytes(b"unrelated visible record")
    anchor = pdf_qa_module.assembly._open_directory_anchor(run_dir, "run")
    try:
        pdf_qa_module._remove_owned_qa_record(
            anchor,
            "pdf-qa.json",
            pdf_qa_module.assembly._PublishedFile((0, 0)),
        )
    finally:
        anchor.close()

    assert replacement.read_bytes() == b"unrelated visible record"
    assert list(run_dir.glob(".pdf-qa-cleanup-*")) == []


def test_windows_report_cleanup_fails_closed_when_native_disposition_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staged-output"
    staging.mkdir()
    report = staging / "manifest.json"
    report.write_bytes(b"owned manifest")
    metadata = staging.stat()

    class FakePathAnchor:
        handle = 41

        def current_path(self) -> Path:
            return staging

        def close(self) -> None:
            return None

    anchor = pdf_qa_module.assembly._DirectoryAnchor(
        staging,
        "staged PDF output",
        (metadata.st_dev, metadata.st_ino),
        None,
        FakePathAnchor(),
    )
    owned_identity = report.stat()
    closed: list[int] = []
    monkeypatch.setattr(pdf_qa_module.assembly, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_windows_open_relative_file",
        lambda _root, _name: 99,
    )
    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_windows_file_identity",
        lambda _handle, require_regular: (
            owned_identity.st_dev,
            owned_identity.st_ino,
        ),
    )

    def disposition_fails(_handle: int) -> None:
        raise pdf_qa_module.PdfAssemblyError("native disposition rejected")

    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_windows_delete_open_file",
        disposition_fails,
    )
    monkeypatch.setattr(
        pdf_qa_module.assembly.pdf_acquire_module,
        "_close_windows_handle",
        closed.append,
    )

    with pytest.raises(PdfQAFailure, match="native disposition rejected"):
        pdf_qa_module._remove_owned_qa_record(
            anchor,
            "manifest.json",
            pdf_qa_module.assembly._PublishedFile(
                (owned_identity.st_dev, owned_identity.st_ino)
            ),
        )

    assert report.read_bytes() == b"owned manifest"
    assert closed == [99]


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX directory descriptors")
def test_qa_snapshot_enumerates_the_held_directory_after_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = tmp_path / "held"
    replacement = tmp_path / "replacement"
    held.mkdir()
    replacement.mkdir()
    payload = b"rendered artifact"
    for directory in (held, replacement):
        (directory / "page-001.png").write_bytes(payload)
    (held / "unexpected.txt").write_bytes(b"held extra child")
    anchor = pdf_qa_module.assembly._open_directory_anchor(held, "held QA pages")
    artifact = pdf_qa_module.assembly._open_anchored_input_file(
        anchor,
        "page-001.png",
        "rendered PDF QA artifact",
    )
    real_current_path = pdf_qa_module.assembly._DirectoryAnchor.current_path

    def replaced_path(
        current: pdf_qa_module.assembly._DirectoryAnchor,
    ) -> Path:
        if current is anchor:
            return replacement
        return real_current_path(current)

    monkeypatch.setattr(
        pdf_qa_module.assembly._DirectoryAnchor,
        "current_path",
        replaced_path,
    )
    try:
        with pytest.raises(PdfQAFailure, match="exactly match"):
            pdf_qa_module._verify_qa_artifact_snapshot(
                anchor,
                {"page-001.png": artifact},
                {"page-001.png": artifact.identity},
                {"page-001.png": hashlib.sha256(payload).hexdigest()},
            )
    finally:
        pdf_qa_module.assembly._close_opened_file(artifact)
        anchor.close()


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows directory rename semantics")
def test_windows_publishes_nonempty_qa_directory_after_releasing_descendant_handles(
    tmp_path: Path,
) -> None:
    """Windows forbids renaming a directory while one of its children is open."""
    staging_path = tmp_path / "staging 한국어"
    destination_path = tmp_path / "destination 한국어"
    staging_path.mkdir()
    destination_path.mkdir()
    staging_anchor = pdf_qa_module.assembly._open_directory_anchor(
        staging_path, "staging"
    )
    destination_anchor = pdf_qa_module.assembly._open_directory_anchor(
        destination_path, "destination"
    )
    pages_anchor = pdf_qa_module.assembly._create_child_directory(
        staging_anchor,
        "qa-pages",
        "rendered PDF QA pages",
    )
    opened: dict[str, object] = {}
    expected_hashes: dict[str, str] = {}
    published_anchor = None
    try:
        for name, payload in {
            "page-001.png": b"held rendered page",
            "contact-sheet-001.png": b"held contact sheet",
        }.items():
            item = pdf_qa_module.assembly._create_anchored_binary_file(
                pages_anchor, name
            )
            item.stream.write(payload)
            pdf_qa_module.assembly._finalize_opened_file(item, name)
            opened[name] = item
            expected_hashes[name] = hashlib.sha256(payload).hexdigest()

        published_anchor = pdf_qa_module._publish_qa_artifact_directory(
            staging_anchor,
            "qa-pages",
            pages_anchor,
            destination_anchor,
            "qa-pages",
            opened,  # type: ignore[arg-type]
            expected_hashes,
            release_descendant_handles=True,
        )

        assert not (staging_path / "qa-pages").exists()
        published = destination_path / "qa-pages"
        assert (published / "page-001.png").read_bytes() == b"held rendered page"
        assert (published / "contact-sheet-001.png").read_bytes() == b"held contact sheet"
        assert all(not item.stream.closed for item in opened.values())  # type: ignore[attr-defined]
    finally:
        for item in opened.values():
            pdf_qa_module.assembly._close_opened_file(item)  # type: ignore[arg-type]
        if published_anchor is not None:
            published_anchor.close()
        pages_anchor.close()
        destination_anchor.close()
        staging_anchor.close()


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows directory rename semantics")
def test_windows_nonempty_qa_directory_publication_never_clobbers_destination(
    tmp_path: Path,
) -> None:
    staging_path = tmp_path / "staging"
    destination_path = tmp_path / "destination"
    staging_path.mkdir()
    destination_path.mkdir()
    foreign_destination = destination_path / "qa-pages"
    foreign_destination.mkdir()
    (foreign_destination / "foreign.txt").write_bytes(b"foreign destination")
    staging_anchor = pdf_qa_module.assembly._open_directory_anchor(
        staging_path, "staging"
    )
    destination_anchor = pdf_qa_module.assembly._open_directory_anchor(
        destination_path, "destination"
    )
    pages_anchor = pdf_qa_module.assembly._create_child_directory(
        staging_anchor,
        "qa-pages",
        "rendered PDF QA pages",
    )
    item = pdf_qa_module.assembly._create_anchored_binary_file(
        pages_anchor, "page-001.png"
    )
    payload = b"owned rendered page"
    item.stream.write(payload)
    pdf_qa_module.assembly._finalize_opened_file(item, "page-001.png")
    opened = {"page-001.png": item}
    try:
        with pytest.raises(PdfQAFailure, match="destination already exists"):
            pdf_qa_module._publish_qa_artifact_directory(
                staging_anchor,
                "qa-pages",
                pages_anchor,
                destination_anchor,
                "qa-pages",
                opened,
                {"page-001.png": hashlib.sha256(payload).hexdigest()},
                release_descendant_handles=True,
            )

        assert (staging_path / "qa-pages" / "page-001.png").read_bytes() == payload
        assert (foreign_destination / "foreign.txt").read_bytes() == b"foreign destination"
    finally:
        for opened_item in opened.values():
            pdf_qa_module.assembly._close_opened_file(opened_item)
        pages_anchor.close()
        destination_anchor.close()
        staging_anchor.close()


# Production mutation caught: accepting malformed or incomplete visual-review evidence.
@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda review: review.__setitem__("unexpected", True), "fields must be exactly"),
        (lambda review: review.__setitem__("pages_reviewed", []), "page coverage"),
        (lambda review: review["pages_reviewed"].append(1), "page coverage"),
        (
            lambda review: review.__setitem__("contact_sheets_reviewed", {}),
            "contact-sheet coverage",
        ),
        (
            lambda review: review["findings"].pop("glyph_rendering"),
            "visual dimensions",
        ),
        (
            lambda review: review["findings"].__setitem__(
                "extra", {"verdict": "pass", "evidence": "Unexpected dimension."}
            ),
            "visual dimensions",
        ),
        (
            lambda review: review["findings"]["glyph_rendering"].__setitem__(
                "evidence", ""
            ),
            "nonempty",
        ),
        (
            lambda review: review["findings"]["glyph_rendering"].__setitem__(
                "verdict", "warning"
            ),
            "not supported",
        ),
        (
            lambda review: review.__setitem__("unresolved_required", ["glyph_rendering"]),
            "required-fix",
        ),
    ],
)
def test_read_pdf_layout_review_rejects_noncanonical_evidence(
    assembled_pdf_run: PdfQARun,
    mutate: object,
    message: str,
) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    _write_passing_layout_review(assembled_pdf_run.run_dir)
    review_path = assembled_pdf_run.run_dir / "pdf-layout-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    mutate(review)  # type: ignore[operator]
    _write_json(review_path, review)

    with pytest.raises(PdfQAFailure, match=message):
        read_pdf_layout_review(review_path, assembled_pdf_run.run_dir / "pdf-qa.json")


def test_read_pdf_layout_review_rejects_duplicate_dimension_keys(
    assembled_pdf_run: PdfQARun,
) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    _write_passing_layout_review(assembled_pdf_run.run_dir)
    review_path = assembled_pdf_run.run_dir / "pdf-layout-review.json"
    text = review_path.read_text(encoding="utf-8")
    duplicate = (
        '"glyph_rendering":{"evidence":"Duplicate.","verdict":"pass"},'
        '"glyph_rendering":{"evidence":"Reviewed glyph_rendering.","verdict":"pass"}'
    )
    text = text.replace(
        '"glyph_rendering": {"evidence": "Reviewed glyph_rendering.", "verdict": "pass"}',
        duplicate,
    )
    review_path.write_text(text, encoding="utf-8")

    with pytest.raises(PdfQAFailure, match="duplicate JSON field"):
        read_pdf_layout_review(review_path, assembled_pdf_run.run_dir / "pdf-qa.json")


def test_read_pdf_layout_review_accepts_matching_required_fix_evidence(
    assembled_pdf_run: PdfQARun,
) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    _write_passing_layout_review(assembled_pdf_run.run_dir)
    review_path = assembled_pdf_run.run_dir / "pdf-layout-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["findings"]["glyph_rendering"]["verdict"] = "required-fix"
    review["unresolved_required"] = ["glyph_rendering"]
    _write_json(review_path, review)

    parsed = read_pdf_layout_review(
        review_path, assembled_pdf_run.run_dir / "pdf-qa.json"
    )

    assert parsed.unresolved_required == ["glyph_rendering"]


def test_finalize_rejects_current_pdf_that_disagrees_with_qa_hash(
    assembled_pdf_run: PdfQARun,
) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    _write_passing_layout_review(assembled_pdf_run.run_dir)
    staged_pdf = assembled_pdf_run.run_dir / "staged-output" / "translated.pdf"
    staged_pdf.write_bytes(staged_pdf.read_bytes() + b"\n")

    with pytest.raises(PdfQAFailure, match="staged PDF hash"):
        finalize_pdf_output(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert not assembled_pdf_run.output_dir.exists()


def test_finalize_rejects_unresolved_visual_finding(
    assembled_pdf_run: PdfQARun,
) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    _write_passing_layout_review(assembled_pdf_run.run_dir)
    review_path = assembled_pdf_run.run_dir / "pdf-layout-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["findings"]["glyph_rendering"]["verdict"] = "required-fix"
    review["unresolved_required"] = ["glyph_rendering"]
    _write_json(review_path, review)

    with pytest.raises(PdfQAFailure, match="unresolved required"):
        finalize_pdf_output(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert not assembled_pdf_run.output_dir.exists()


def test_finalize_rejects_failed_automated_qa_record(
    assembled_pdf_run: PdfQARun,
) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    _write_passing_layout_review(assembled_pdf_run.run_dir)
    qa_path = assembled_pdf_run.run_dir / "pdf-qa.json"
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    qa["passed"] = False
    _write_json(qa_path, qa)

    with pytest.raises(PdfQAFailure, match="automated PDF QA did not pass"):
        finalize_pdf_output(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert not assembled_pdf_run.output_dir.exists()


# Production mutation caught: publishing a PDF after its visual review has become stale.
def test_finalize_rejects_stale_visual_review(assembled_pdf_run: PdfQARun) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    _write_passing_layout_review(assembled_pdf_run.run_dir, staged_sha256="0" * 64)

    with pytest.raises(PdfQAFailure, match="staged PDF hash"):
        finalize_pdf_output(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert not assembled_pdf_run.output_dir.exists()


# Production mutation caught: replacing an already existing or linked final output path.
def test_finalize_rejects_existing_output_and_keeps_staging(assembled_pdf_run: PdfQARun) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    _write_passing_layout_review(assembled_pdf_run.run_dir)
    assembled_pdf_run.output_dir.mkdir(parents=True)

    with pytest.raises(PdfQAFailure, match="already exists"):
        finalize_pdf_output(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert (assembled_pdf_run.run_dir / "staged-output" / "translated.pdf").is_file()


def test_finalize_rejects_linked_output_and_keeps_staging(
    assembled_pdf_run: PdfQARun, tmp_path: Path
) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    _write_passing_layout_review(assembled_pdf_run.run_dir)
    linked_target = tmp_path / "linked-target"
    linked_target.mkdir()
    assembled_pdf_run.output_dir.parent.mkdir(parents=True, exist_ok=True)
    assembled_pdf_run.output_dir.symlink_to(linked_target, target_is_directory=True)

    with pytest.raises(PdfQAFailure, match="linked|already exists"):
        finalize_pdf_output(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert (assembled_pdf_run.run_dir / "staged-output" / "translated.pdf").is_file()


def _rewrite_layout_hash(run: PdfQARun) -> None:
    pdf = run.run_dir / "staged-output" / "translated.pdf"
    layout_path = run.run_dir / "layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["staged_pdf_sha256"] = _sha256(pdf)
    _write_json(layout_path, layout)


def _rewrite_pdf(run: PdfQARun, mutate: object) -> None:
    pdf = run.run_dir / "staged-output" / "translated.pdf"
    reader = PdfReader(pdf)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    mutate(writer)  # type: ignore[operator]
    with pdf.open("wb") as stream:
        writer.write(stream)
    _rewrite_layout_hash(run)


def _replace_embedded_font_program(
    writer: PdfWriter,
    face: str,
    payload: bytes,
) -> None:
    for page in writer.pages:
        fonts = page["/Resources"].get_object()["/Font"].get_object()
        for reference in fonts.values():
            font = reference.get_object()
            if f"NotoSansCJKKR-{face}" not in str(font.get("/BaseFont", "")):
                continue
            descendants = font.get("/DescendantFonts", [])
            candidate = descendants[0].get_object() if descendants else font
            descriptor = candidate["/FontDescriptor"].get_object()
            stream = DecodedStreamObject()
            stream.set_data(payload)
            descriptor[NameObject("/FontFile2")] = writer._add_object(stream)
            return
    raise AssertionError(f"fixture {face} font was not found")


def _corrupt_embedded_font_glyph_table(writer: PdfWriter, face: str) -> None:
    for page in writer.pages:
        fonts = page["/Resources"].get_object()["/Font"].get_object()
        for reference in fonts.values():
            font = reference.get_object()
            if f"NotoSansCJKKR-{face}" not in str(font.get("/BaseFont", "")):
                continue
            candidate = font.get("/DescendantFonts", [font])[0].get_object()
            descriptor = candidate["/FontDescriptor"].get_object()
            payload = bytearray(descriptor["/FontFile2"].get_object().get_data())
            table_count = struct.unpack_from(">H", payload, 4)[0]
            for index in range(table_count):
                tag, _checksum, offset, length = struct.unpack_from(
                    ">4sIII", payload, 12 + index * 16
                )
                if tag == b"glyf":
                    payload[offset + length // 2] ^= 1
                    _replace_embedded_font_program(writer, face, bytes(payload))
                    return
            raise AssertionError(f"fixture {face} glyph table was not found")
    raise AssertionError(f"fixture {face} font was not found")


def _assert_no_public_qa_evidence(run: PdfQARun) -> None:
    assert not (run.run_dir / "qa-pages").exists()
    assert not (run.run_dir / "pdf-qa.json").exists()
    assert not run.output_dir.exists()


def test_build_contact_sheets_limits_each_sheet_to_twelve_numbered_pages(
    tmp_path: Path,
) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    rendered: list[Path] = []
    for number in range(1, 26):
        path = pages / f"page-{number:03d}.png"
        Image.new("RGB", (80, 120), (number, number, number)).save(path)
        rendered.append(path)

    mapping = build_contact_sheets(rendered, tmp_path / "contacts")

    assert mapping == {
        "contact-sheet-001.png": list(range(1, 13)),
        "contact-sheet-002.png": list(range(13, 25)),
        "contact-sheet-003.png": [25],
    }
    for name in mapping:
        with Image.open(tmp_path / "contacts" / name) as sheet:
            assert sheet.width > 0
            assert sheet.height > 0
            assert sheet.getbbox() is not None


def test_prepare_pdf_qa_rejects_missing_bold_font(
    assembled_pdf_run: PdfQARun,
) -> None:
    pdf = assembled_pdf_run.run_dir / "staged-output" / "translated.pdf"
    reader = PdfReader(pdf)
    writer = PdfWriter()
    writer.append(reader)
    for page in writer.pages:
        fonts = page["/Resources"].get_object()["/Font"].get_object()
        for key in list(fonts):
            base_font = str(fonts[key].get_object().get("/BaseFont", ""))
            if "Bold" in base_font:
                del fonts[key]
    with pdf.open("wb") as stream:
        writer.write(stream)
    layout_path = assembled_pdf_run.run_dir / "layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["staged_pdf_sha256"] = _sha256(pdf)
    _write_json(layout_path, layout)

    with pytest.raises(PdfQAFailure, match="Bold"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert not (assembled_pdf_run.run_dir / "qa-pages").exists()
    assert not (assembled_pdf_run.run_dir / "pdf-qa.json").exists()
    assert not assembled_pdf_run.output_dir.exists()


def test_prepare_pdf_qa_rejects_linked_prior_evidence_without_touching_target(
    assembled_pdf_run: PdfQARun,
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    keep = target / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    (assembled_pdf_run.run_dir / "qa-pages").symlink_to(target, target_is_directory=True)

    with pytest.raises(PdfQAFailure, match="qa-pages"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert keep.read_text(encoding="utf-8") == "keep"
    assert (assembled_pdf_run.run_dir / "qa-pages").is_symlink()
    assert not assembled_pdf_run.output_dir.exists()


def test_prepare_pdf_qa_rejects_missing_translation_id(
    assembled_pdf_run: PdfQARun,
) -> None:
    translations = assembled_pdf_run.run_dir / "translations" / "zone-001.jsonl"
    lines = translations.read_text(encoding="utf-8").splitlines()
    translations.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    _refresh_review_digest(assembled_pdf_run.run_dir)

    with pytest.raises(PdfQAFailure, match="translations must exactly cover"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_changed_protected_token(
    assembled_pdf_run: PdfQARun,
) -> None:
    translations = assembled_pdf_run.run_dir / "translations" / "zone-001.jsonl"
    translations.write_text(
        translations.read_text(encoding="utf-8").replace(
            "⟦WT:000001⟧", "⟦WT:999999⟧"
        ),
        encoding="utf-8",
    )
    _refresh_review_digest(assembled_pdf_run.run_dir)

    with pytest.raises(PdfQAFailure, match="protected-token|restore"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_table_grid_mismatch(
    assembled_pdf_run: PdfQARun,
) -> None:
    path = assembled_pdf_run.run_dir / "document.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["table_cells"] = document["table_cells"][:-1]
    _write_json(path, document)

    with pytest.raises(PdfQAFailure, match="table cells"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_missing_or_tampered_figure_media(
    assembled_pdf_run: PdfQARun,
) -> None:
    media = assembled_pdf_run.run_dir / "media" / "figure-0001.png"
    Image.new("RGB", (240, 120), "magenta").save(media)

    with pytest.raises(PdfQAFailure, match="figure media"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_unresolved_semantic_review(
    assembled_pdf_run: PdfQARun,
) -> None:
    path = assembled_pdf_run.run_dir / "review.json"
    review = json.loads(path.read_text(encoding="utf-8"))
    review["section_findings"]["zone-001"][0]["verdict"] = "required-fix"
    review["unresolved_required"] = ["zone-001:semantic_fidelity"]
    _write_json(path, review)

    with pytest.raises(PdfQAFailure, match="unresolved required"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_encrypted_staged_pdf(
    assembled_pdf_run: PdfQARun,
) -> None:
    pdf = assembled_pdf_run.run_dir / "staged-output" / "translated.pdf"
    reader = PdfReader(pdf)
    writer = PdfWriter()
    writer.append(reader)
    writer.encrypt("secret")
    with pdf.open("wb") as stream:
        writer.write(stream)
    _rewrite_layout_hash(assembled_pdf_run)

    with pytest.raises(PdfQAFailure, match="encrypted"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_missing_unicode_map(
    assembled_pdf_run: PdfQARun,
) -> None:
    def remove_unicode_map(writer: PdfWriter) -> None:
        for page in writer.pages:
            fonts = page["/Resources"].get_object()["/Font"].get_object()
            for reference in fonts.values():
                font = reference.get_object()
                if "NotoSansCJKKR-Regular" in str(font.get("/BaseFont", "")):
                    del font[NameObject("/ToUnicode")]
                    return
        raise AssertionError("fixture Regular font was not found")

    _rewrite_pdf(assembled_pdf_run, remove_unicode_map)

    with pytest.raises(PdfQAFailure, match="/ToUnicode"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_unselectable_translated_block(
    assembled_pdf_run: PdfQARun,
) -> None:
    path = assembled_pdf_run.run_dir / "translations" / "zone-001.jsonl"
    path.write_text(
        path.read_text(encoding="utf-8").replace("그림 설명", "스테이지에 없는 번역"),
        encoding="utf-8",
    )
    _refresh_review_digest(assembled_pdf_run.run_dir)

    with pytest.raises(PdfQAFailure, match="not selectable"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_invalid_external_link_annotation(
    assembled_pdf_run: PdfQARun,
) -> None:
    def corrupt_uri(writer: PdfWriter) -> None:
        for page in writer.pages:
            for reference in page.get("/Annots", []):
                annotation = reference.get_object()
                action = annotation.get("/A")
                if action is not None and action.get_object().get("/S") == "/URI":
                    action.get_object()[NameObject("/URI")] = TextStringObject(
                        "javascript:alert(1)"
                    )
                    return
        raise AssertionError("fixture external link was not found")

    _rewrite_pdf(assembled_pdf_run, corrupt_uri)

    with pytest.raises(PdfQAFailure, match="unsafe external URI"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_invalid_page_content_stream(
    assembled_pdf_run: PdfQARun,
) -> None:
    def empty_stream(writer: PdfWriter) -> None:
        stream = DecodedStreamObject()
        stream.set_data(b"")
        writer.pages[0][NameObject("/Contents")] = writer._add_object(stream)

    _rewrite_pdf(assembled_pdf_run, empty_stream)

    with pytest.raises(PdfQAFailure, match="content stream"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


@pytest.mark.parametrize("mutation", ["overflow", "overlap", "small-font"])
def test_prepare_pdf_qa_rejects_invalid_layout_evidence(
    assembled_pdf_run: PdfQARun,
    mutation: str,
) -> None:
    path = assembled_pdf_run.run_dir / "layout.json"
    layout = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "overflow":
        layout["flowables"][0]["bounds"][0] = -1.0
    elif mutation == "overlap":
        layout["flowables"][1]["bounds"] = layout["flowables"][0]["bounds"]
        layout["flowables"][1]["frame"] = layout["flowables"][0]["frame"]
    else:
        layout["flowables"][0]["font_size"] = 8.0
    _write_json(path, layout)

    with pytest.raises(PdfQAFailure, match="layout"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_unintended_blank_page(
    assembled_pdf_run: PdfQARun,
) -> None:
    def append_blank(writer: PdfWriter) -> None:
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream.set_data(b"q Q\n")
        page[NameObject("/Contents")] = writer._add_object(stream)

    _rewrite_pdf(assembled_pdf_run, append_blank)

    with pytest.raises(PdfQAFailure, match="blank output page"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_render_failure_without_publishing(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from web_translator.pdf_media import PdfMediaError

    def fail_render(*args: object, **kwargs: object) -> list[Path]:
        raise PdfMediaError("Poppler render failed")

    monkeypatch.setattr(pdf_qa_module, "render_pdf_pages", fail_render)

    with pytest.raises(PdfQAFailure, match="Poppler render failed"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert not (assembled_pdf_run.run_dir / "qa-pages").exists()
    assert not (assembled_pdf_run.run_dir / "pdf-qa.json").exists()


@pytest.mark.parametrize("failure", ["media-error", "interrupt"])
def test_prepare_pdf_qa_removes_empty_owned_render_destination_after_immediate_failure(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from web_translator.pdf_media import PdfMediaError

    def fail_immediately(*args: object, **kwargs: object) -> list[Path]:
        del args, kwargs
        if failure == "media-error":
            raise PdfMediaError("injected immediate render failure")
        raise KeyboardInterrupt("injected immediate render interruption")

    monkeypatch.setattr(pdf_qa_module, "render_pdf_pages", fail_immediately)

    expected = PdfQAFailure if failure == "media-error" else KeyboardInterrupt
    with pytest.raises(expected, match="immediate render"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)
    assert list(assembled_pdf_run.run_dir.glob(".pdf-qa-preparing-*")) == []
    assert list(assembled_pdf_run.run_dir.glob(".pdf-qa-raced-*")) == []
    assert list(assembled_pdf_run.run_dir.rglob("render-input.pdf")) == []


@pytest.mark.parametrize(
    "replacement_contents",
    [None, b"nonempty cleanup replacement"],
    ids=["empty-replacement", "nonempty-replacement"],
)
def test_prepare_pdf_qa_preserves_posix_cleanup_replacement_races(
    assembled_pdf_run: PdfQARun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_contents: bytes | None,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX dirfd-relative cleanup regression")

    from web_translator.pdf_media import PdfMediaError

    held_original = tmp_path / "racer-held-original"
    held_identity: tuple[int, int] | None = None
    replacement_identity: tuple[int, int] | None = None
    real_rename = os.rename
    real_rmdir = os.rmdir

    def install_replacement() -> None:
        nonlocal held_identity, replacement_identity
        preparations = list(
            assembled_pdf_run.run_dir.glob(".pdf-qa-preparing-*")
        )
        assert len(preparations) == 1
        pages = preparations[0] / "qa-pages"
        held_metadata = pages.stat()
        held_identity = held_metadata.st_dev, held_metadata.st_ino
        real_rename(pages, held_original)
        pages.mkdir()
        if replacement_contents is not None:
            (pages / "replacement.txt").write_bytes(replacement_contents)
        metadata = pages.stat()
        replacement_identity = metadata.st_dev, metadata.st_ino

    def racing_rename(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if source == "qa-pages" and replacement_identity is None:
            install_replacement()
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def racing_rmdir(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path == "qa-pages" and replacement_identity is None:
            install_replacement()
        real_rmdir(path, dir_fd=dir_fd)

    def fail_immediately(*args: object, **kwargs: object) -> list[Path]:
        del args, kwargs
        raise PdfMediaError("injected render failure before POSIX cleanup race")

    monkeypatch.setattr(pdf_qa_module.os, "rename", racing_rename)
    monkeypatch.setattr(pdf_qa_module.os, "rmdir", racing_rmdir)
    monkeypatch.setattr(pdf_qa_module, "render_pdf_pages", fail_immediately)

    with pytest.raises(PdfQAFailure, match="before POSIX cleanup race"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert replacement_identity is not None
    assert held_original.is_dir()
    held_metadata = held_original.stat()
    assert (held_metadata.st_dev, held_metadata.st_ino) == held_identity
    _assert_no_public_qa_evidence(assembled_pdf_run)
    assert list(assembled_pdf_run.run_dir.glob(".pdf-qa-preparing-*")) == []
    assert list(assembled_pdf_run.run_dir.rglob("render-input.pdf")) == []
    quarantines = list(assembled_pdf_run.run_dir.glob(".pdf-qa-raced-*"))
    assert len(quarantines) == 1
    quarantine_metadata = quarantines[0].stat()
    assert (quarantine_metadata.st_dev, quarantine_metadata.st_ino) == (
        replacement_identity
    )
    if replacement_contents is not None:
        assert (quarantines[0] / "replacement.txt").read_bytes() == (
            replacement_contents
        )


@pytest.mark.parametrize("partial_page", [False, True])
@pytest.mark.parametrize("failure", ["media-error", "interrupt"])
def test_prepare_pdf_qa_cleans_owned_destination_after_render_failure(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
    partial_page: bool,
    failure: str,
) -> None:
    from web_translator.pdf_media import PdfMediaError

    racer = assembled_pdf_run.run_dir / "unrelated-render-racer"
    racer.mkdir()
    marker = racer / "keep.txt"
    marker.write_text("preserve me", encoding="utf-8")

    def fail_after_destination(
        source_pdf: Path,
        destination: Path,
        **kwargs: object,
    ) -> list[Path]:
        destination.mkdir(parents=True, exist_ok=True)
        if partial_page:
            image = Image.new("RGB", (32, 32), "white")
            try:
                image.save(destination / "page-1.png")
            finally:
                image.close()
        if failure == "media-error":
            raise PdfMediaError("injected render failure after destination")
        raise KeyboardInterrupt("injected render interruption after destination")

    monkeypatch.setattr(pdf_qa_module, "render_pdf_pages", fail_after_destination)

    expected = PdfQAFailure if failure == "media-error" else KeyboardInterrupt
    with pytest.raises(expected, match="after destination"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)
    assert list(assembled_pdf_run.run_dir.glob(".pdf-qa-preparing-*")) == []
    assert list(assembled_pdf_run.run_dir.rglob("render-input.pdf")) == []
    assert marker.read_text(encoding="utf-8") == "preserve me"


def test_prepare_pdf_qa_preserves_renderer_racer_without_preparation_leak(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt_with_racer(
        source_pdf: Path,
        destination: Path,
        **kwargs: object,
    ) -> list[Path]:
        destination.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (32, 32), "white")
        try:
            image.save(destination / "page-1.png")
        finally:
            image.close()
        (destination / "unrelated.txt").write_text("preserve me", encoding="utf-8")
        raise KeyboardInterrupt("injected render interruption with racer")

    monkeypatch.setattr(pdf_qa_module, "render_pdf_pages", interrupt_with_racer)

    with pytest.raises(KeyboardInterrupt, match="with racer"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)
    assert list(assembled_pdf_run.run_dir.glob(".pdf-qa-preparing-*")) == []
    assert list(assembled_pdf_run.run_dir.rglob("render-input.pdf")) == []
    markers = list(assembled_pdf_run.run_dir.rglob("unrelated.txt"))
    assert len(markers) == 1
    assert markers[0].read_text(encoding="utf-8") == "preserve me"


@pytest.mark.parametrize(
    "racer_name",
    ["page-1.png", "page-999.png", "contact-sheet-001.png"],
)
@pytest.mark.parametrize("failure", ["media-error", "interrupt"])
def test_prepare_pdf_qa_quarantines_failed_render_filename_racers(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
    racer_name: str,
    failure: str,
) -> None:
    from web_translator.pdf_media import PdfMediaError

    racer_bytes = f"racer:{racer_name}:{failure}".encode()

    def fail_after_filename_race(
        source_pdf: Path,
        destination: Path,
        **kwargs: object,
    ) -> list[Path]:
        del source_pdf, kwargs
        partial = destination / "page-1.png"
        image = Image.new("RGB", (32, 32), "white")
        try:
            image.save(partial)
        finally:
            image.close()
        if racer_name == partial.name:
            partial.unlink()
        (destination / racer_name).write_bytes(racer_bytes)
        if failure == "media-error":
            raise PdfMediaError("injected render failure with filename racer")
        raise KeyboardInterrupt("injected render interruption with filename racer")

    monkeypatch.setattr(
        pdf_qa_module,
        "render_pdf_pages",
        fail_after_filename_race,
    )

    expected = PdfQAFailure if failure == "media-error" else KeyboardInterrupt
    with pytest.raises(expected, match="filename racer"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)
    assert list(assembled_pdf_run.run_dir.glob(".pdf-qa-preparing-*")) == []
    assert list(assembled_pdf_run.run_dir.rglob("render-input.pdf")) == []
    quarantines = list(assembled_pdf_run.run_dir.glob(".pdf-qa-raced-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / racer_name).read_bytes() == racer_bytes


def test_remove_qa_pages_quarantines_exact_directory_when_posix_rmdir_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_path = tmp_path / "run"
    staging_path = run_path / ".pdf-qa-preparing-test"
    pages_path = staging_path / "qa-pages"
    pages_path.mkdir(parents=True)
    racer_bytes = b"raced-between-empty-check-and-rmdir"
    real_rmdir = os.rmdir
    run_anchor = pdf_qa_module.assembly._open_directory_anchor(run_path, "run")
    staging_anchor = pdf_qa_module.assembly._open_existing_child_directory(
        run_anchor, staging_path.name, "staging"
    )
    pages_anchor = pdf_qa_module.assembly._open_existing_child_directory(
        staging_anchor, "qa-pages", "pages"
    )

    def racing_rmdir(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        if (
            isinstance(path, str)
            and path.startswith(".pdf-qa-raced-")
            and dir_fd == run_anchor.descriptor
        ):
            (run_path / path / "page-999.png").write_bytes(racer_bytes)
        real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(pdf_qa_module.os, "rmdir", racing_rmdir)
    try:
        pdf_qa_module._remove_qa_pages(
            staging_anchor,
            "qa-pages",
            pages_anchor,
            quarantine_parent=run_anchor,
        )
    finally:
        pages_anchor.close()
        staging_anchor.close()
        run_anchor.close()

    assert not pages_path.exists()
    quarantines = list(run_path.glob(".pdf-qa-raced-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "page-999.png").read_bytes() == racer_bytes


def test_remove_qa_pages_quarantines_exact_directory_on_unexpected_posix_delete_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_path = tmp_path / "run"
    staging_path = run_path / ".pdf-qa-preparing-test"
    pages_path = staging_path / "qa-pages"
    pages_path.mkdir(parents=True)
    run_anchor = pdf_qa_module.assembly._open_directory_anchor(run_path, "run")
    staging_anchor = pdf_qa_module.assembly._open_existing_child_directory(
        run_anchor, staging_path.name, "staging"
    )
    pages_anchor = pdf_qa_module.assembly._open_existing_child_directory(
        staging_anchor, "qa-pages", "pages"
    )
    run_descriptor = run_anchor.descriptor
    attempts: list[tuple[object, int | None]] = []

    def denied_rmdir(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        attempts.append((path, dir_fd))
        raise OSError(errno.EACCES, "injected delete denial")

    monkeypatch.setattr(pdf_qa_module.os, "rmdir", denied_rmdir)
    try:
        pdf_qa_module._remove_qa_pages(
            staging_anchor,
            "qa-pages",
            pages_anchor,
            quarantine_parent=run_anchor,
        )
    finally:
        pages_anchor.close()
        staging_anchor.close()
        run_anchor.close()

    assert len(attempts) == 1
    assert isinstance(attempts[0][0], str)
    assert attempts[0][0].startswith(".pdf-qa-raced-")
    assert attempts[0][1] == run_descriptor
    assert not pages_path.exists()
    quarantines = list(run_path.glob(".pdf-qa-raced-*"))
    assert len(quarantines) == 1
    assert list(quarantines[0].iterdir()) == []


def test_remove_qa_pages_uses_windows_held_handle_to_remove_empty_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class WindowsOSProxy:
        name = "nt"

        def __getattr__(self, name: str) -> object:
            return getattr(os, name)

    class WindowsPathAnchor:
        def __init__(self, path: Path, handle: int) -> None:
            self.path = path
            self.handle = handle
            self.close_count = 0

        def current_path(self) -> Path:
            return self.path

        def close(self) -> None:
            self.close_count += 1

    run_path = tmp_path / "run"
    staging_path = run_path / ".pdf-qa-preparing-test"
    pages_path = staging_path / "qa-pages"
    pages_path.mkdir(parents=True)
    run_path_anchor = WindowsPathAnchor(run_path, 101)
    staging_path_anchor = WindowsPathAnchor(staging_path, 202)
    pages_path_anchor = WindowsPathAnchor(pages_path, 303)
    anchor_type = pdf_qa_module.assembly._DirectoryAnchor

    def identity(path: Path) -> tuple[int, int]:
        metadata = path.lstat()
        return metadata.st_dev, metadata.st_ino

    run_anchor = anchor_type(run_path, "run", identity(run_path), None, run_path_anchor)
    staging_anchor = anchor_type(
        staging_path, "staging", identity(staging_path), None, staging_path_anchor
    )
    pages_anchor = anchor_type(
        pages_path, "pages", identity(pages_path), None, pages_path_anchor
    )
    deleted: list[int] = []
    moved: list[tuple[int, int, str]] = []

    def windows_delete(handle: int) -> None:
        deleted.append(handle)
        pages_path.rmdir()

    monkeypatch.setattr(
        pdf_qa_module.assembly, "_windows_delete_open_file", windows_delete
    )
    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_windows_rename_open_file",
        lambda source, destination, name: moved.append((source, destination, name)),
    )
    monkeypatch.setattr(pdf_qa_module, "os", WindowsOSProxy())

    try:
        pdf_qa_module._remove_qa_pages(
            staging_anchor,
            "qa-pages",
            pages_anchor,
            quarantine_parent=run_anchor,
        )
    finally:
        pages_anchor.close()
        staging_anchor.close()
        run_anchor.close()

    assert deleted == [303]
    assert moved == []
    assert not pages_path.exists()
    assert pages_path_anchor.close_count == 1
    assert staging_path_anchor.close_count == 1
    assert run_path_anchor.close_count == 1


def test_remove_qa_pages_uses_windows_held_handle_move_and_closes_anchors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class WindowsOSProxy:
        name = "nt"

        def __getattr__(self, name: str) -> object:
            return getattr(os, name)

    class WindowsPathAnchor:
        def __init__(self, path: Path, handle: int) -> None:
            self.path = path
            self.handle = handle
            self.close_count = 0

        def current_path(self) -> Path:
            return self.path

        def close(self) -> None:
            self.close_count += 1

    run_path = tmp_path / "run"
    staging_path = run_path / ".pdf-qa-preparing-test"
    pages_path = staging_path / "qa-pages"
    pages_path.mkdir(parents=True)
    racer_bytes = b"windows-page-racer"
    (pages_path / "page-999.png").write_bytes(racer_bytes)

    run_path_anchor = WindowsPathAnchor(run_path, 101)
    staging_path_anchor = WindowsPathAnchor(staging_path, 202)
    pages_path_anchor = WindowsPathAnchor(pages_path, 303)
    anchor_type = pdf_qa_module.assembly._DirectoryAnchor

    def identity(path: Path) -> tuple[int, int]:
        metadata = path.lstat()
        return metadata.st_dev, metadata.st_ino

    run_anchor = anchor_type(run_path, "run", identity(run_path), None, run_path_anchor)
    staging_anchor = anchor_type(
        staging_path,
        "staging",
        identity(staging_path),
        None,
        staging_path_anchor,
    )
    pages_anchor = anchor_type(
        pages_path,
        "pages",
        identity(pages_path),
        None,
        pages_path_anchor,
    )

    deleted: list[int] = []
    moved: list[tuple[int, int, str]] = []

    def windows_rename(
        source_handle: int,
        destination_handle: int,
        destination_name: str,
    ) -> None:
        moved.append((source_handle, destination_handle, destination_name))
        pages_path.rename(run_path / destination_name)

    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_windows_delete_open_file",
        deleted.append,
    )
    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_windows_rename_open_file",
        windows_rename,
    )
    monkeypatch.setattr(pdf_qa_module, "os", WindowsOSProxy())

    try:
        pdf_qa_module._remove_qa_pages(
            staging_anchor,
            "qa-pages",
            pages_anchor,
            quarantine_parent=run_anchor,
        )
    finally:
        pages_anchor.close()
        staging_anchor.close()
        run_anchor.close()

    assert deleted == [303]
    assert len(moved) == 1
    assert moved[0][:2] == (303, 101)
    assert moved[0][2].startswith(".pdf-qa-raced-")
    quarantine = run_path / moved[0][2]
    assert (quarantine / "page-999.png").read_bytes() == racer_bytes
    assert pages_path_anchor.close_count == 1
    assert staging_path_anchor.close_count == 1
    assert run_path_anchor.close_count == 1


def test_prepare_pdf_qa_regenerates_safe_prior_evidence_and_preserves_unrelated_files(
    assembled_pdf_run: PdfQARun,
) -> None:
    first = prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    keep = assembled_pdf_run.run_dir / "keep.txt"
    keep.write_text("unrelated", encoding="utf-8")

    second = prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert second.staged_pdf_sha256 == first.staged_pdf_sha256
    assert second.rendered_page_hashes == first.rendered_page_hashes
    assert second.contact_sheet_pages == first.contact_sheet_pages
    assert keep.read_text(encoding="utf-8") == "unrelated"
    assert not assembled_pdf_run.output_dir.exists()


def test_prepare_pdf_qa_preserves_unrelated_racer_across_base_exception(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    keep = assembled_pdf_run.run_dir / "keep.txt"
    keep.write_text("unrelated", encoding="utf-8")
    real_publish = pdf_qa_module.assembly._publish_new_file

    def interrupt_json_publication(*args: object, **kwargs: object) -> object:
        destination = assembled_pdf_run.run_dir / "pdf-qa.json"
        destination.write_text("racer", encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_publish_new_file",
        interrupt_json_publication,
    )
    with pytest.raises(KeyboardInterrupt):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    monkeypatch.setattr(pdf_qa_module.assembly, "_publish_new_file", real_publish)

    assert (assembled_pdf_run.run_dir / "pdf-qa.json").read_text(encoding="utf-8") == "racer"
    assert keep.read_text(encoding="utf-8") == "unrelated"
    assert not assembled_pdf_run.output_dir.exists()


def test_pdf_qa_result_contract_rejects_unknown_fields_and_inexact_coverage(
    assembled_pdf_run: PdfQARun,
) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    path = assembled_pdf_run.run_dir / "pdf-qa.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    parsed = PdfQAResult.from_dict(value, assembled_pdf_run.run_dir / "qa-pages")
    assert parsed.passed is True

    value["unknown"] = True
    with pytest.raises(PdfQAFailure, match="fields must be exactly"):
        PdfQAResult.from_dict(value, assembled_pdf_run.run_dir / "qa-pages")
    del value["unknown"]
    value["contact_sheet_pages"]["contact-sheet-001.png"].append(1)
    with pytest.raises(PdfQAFailure, match="coverage"):
        PdfQAResult.from_dict(value, assembled_pdf_run.run_dir / "qa-pages")


def test_prepare_pdf_qa_rejects_broken_figure_caption_relationship(
    assembled_pdf_run: PdfQARun,
) -> None:
    path = assembled_pdf_run.run_dir / "document.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    caption = next(block for block in document["blocks"] if block["kind"] == "caption")
    caption["caption_id"] = "pdf:page-0001:block-0001"
    _write_json(path, document)

    with pytest.raises(PdfQAFailure, match="figure-caption"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_rendered_glyph_replacement_box(
    assembled_pdf_run: PdfQARun,
) -> None:
    translation_path = assembled_pdf_run.run_dir / "translations" / "zone-001.jsonl"
    records = [
        Translation.from_dict(json.loads(line))
        for line in translation_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    translations = {
        record.segment_id: (
            Translation(record.segment_id, "□")
            if record.segment_id == "seg-000005"
            else record
        )
        for record in records
    }
    translation_path.write_text(
        "".join(
            json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
            for record in translations.values()
        ),
        encoding="utf-8",
    )
    for path in (
        assembled_pdf_run.run_dir / "layout.json",
        assembled_pdf_run.run_dir / "staged-output" / "translated.pdf",
    ):
        path.unlink()
    (assembled_pdf_run.run_dir / "staged-output").rmdir()
    assemble_pdf(
        assembled_pdf_run.run_dir,
        translations,
        {},
        assembled_pdf_run.output_dir,
    )
    _refresh_review_digest(assembled_pdf_run.run_dir)

    with pytest.raises(PdfQAFailure, match="glyph replacement boxes"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_link_inside_prior_qa_pages(
    assembled_pdf_run: PdfQARun,
    tmp_path: Path,
) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    outside = tmp_path / "outside.png"
    Image.new("RGB", (10, 10), "red").save(outside)
    (assembled_pdf_run.run_dir / "qa-pages" / "page-999.png").symlink_to(outside)

    with pytest.raises(PdfQAFailure, match="regular file|unsafe|symbolic links"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert outside.is_file()
    assert (assembled_pdf_run.run_dir / "qa-pages" / "page-999.png").is_symlink()


def test_render_pdf_pages_orders_double_digit_poppler_names_numerically(
    tmp_path: Path,
) -> None:
    source = tmp_path / "thirteen.pdf"
    canvas = Canvas(str(source), pagesize=(100, 100))
    for number in range(1, 14):
        shade = number / 20
        canvas.setFillColorRGB(shade, shade, shade)
        canvas.rect(0, 0, 100, 100, stroke=0, fill=1)
        canvas.showPage()
    canvas.save()

    pages = render_pdf_pages(source, tmp_path / "rendered", dpi=72, name_width=3)

    assert [path.name for path in pages] == [
        f"page-{number:03d}.png" for number in range(1, 14)
    ]
    shades = []
    for path in pages:
        with Image.open(path) as image:
            shades.append(image.convert("RGB").getpixel((50, 50))[0])
    assert shades == sorted(shades)


def test_render_pdf_pages_uses_empty_existing_owned_destination(
    tmp_path: Path,
) -> None:
    source = make_text_pdf(tmp_path / "source.pdf")
    destination = tmp_path / "rendered"
    destination.mkdir()
    metadata = destination.stat()

    pages = render_pdf_pages(
        source,
        destination,
        dpi=72,
        name_width=3,
        existing_destination_identity=(metadata.st_dev, metadata.st_ino),
    )

    assert [path.name for path in pages] == ["page-001.png"]


def test_render_pdf_pages_never_follows_existing_destination_symlink(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_media import PdfMediaError

    source = make_text_pdf(tmp_path / "source.pdf")
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "rendered"
    destination.symlink_to(outside, target_is_directory=True)
    metadata = outside.stat()

    with pytest.raises(PdfMediaError, match="safe existing PDF render destination"):
        render_pdf_pages(
            source,
            destination,
            existing_destination_identity=(metadata.st_dev, metadata.st_ino),
        )

    assert list(outside.iterdir()) == []


def test_render_pdf_pages_rejects_non_page_output_in_owned_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import web_translator.pdf_media as media_module
    from web_translator.pdf_media import PdfMediaError

    source = make_text_pdf(tmp_path / "source.pdf")
    destination = tmp_path / "rendered"
    destination.mkdir()
    metadata = destination.stat()

    def write_mixed_output(command: list[str], action: str) -> None:
        prefix = Path(command[-1])
        image = Image.new("RGB", (20, 20), "white")
        try:
            image.save(prefix.parent / "page-1.png")
        finally:
            image.close()
        (prefix.parent / "unexpected.txt").write_text("racer", encoding="utf-8")

    monkeypatch.setattr(media_module, "_run_poppler", write_mixed_output)

    with pytest.raises(PdfMediaError, match="only page PNG files"):
        render_pdf_pages(
            source,
            destination,
            existing_destination_identity=(metadata.st_dev, metadata.st_ino),
        )

    assert (destination / "unexpected.txt").read_text(encoding="utf-8") == "racer"


def test_render_pdf_pages_rechecks_output_after_name_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import web_translator.pdf_media as media_module
    from web_translator.pdf_media import PdfMediaError

    source = make_text_pdf(tmp_path / "source.pdf")
    destination = tmp_path / "rendered"
    destination.mkdir()
    metadata = destination.stat()
    real_replace = media_module.os.replace

    def replace_then_race(source_path: object, destination_path: object) -> None:
        real_replace(source_path, destination_path)
        (destination / "late-racer.txt").write_text("preserve me", encoding="utf-8")

    monkeypatch.setattr(media_module.os, "replace", replace_then_race)

    with pytest.raises(PdfMediaError, match="only page PNG files"):
        render_pdf_pages(
            source,
            destination,
            name_width=3,
            existing_destination_identity=(metadata.st_dev, metadata.st_ino),
        )

    assert (destination / "late-racer.txt").read_text(encoding="utf-8") == "preserve me"


def test_prepare_pdf_qa_rejects_layout_block_text_mapping_swap(
    assembled_pdf_run: PdfQARun,
) -> None:
    path = assembled_pdf_run.run_dir / "layout.json"
    layout = json.loads(path.read_text(encoding="utf-8"))
    heading = next(item for item in layout["flowables"] if item["kind"] == "heading")
    caption = next(item for item in layout["flowables"] if item["kind"] == "caption")
    heading["block_id"], caption["block_id"] = caption["block_id"], heading["block_id"]
    _write_json(path, layout)

    with pytest.raises(
        PdfQAFailure,
        match=(
            "not selectable.*seg-000001|not selectable.*seg-000005|"
            "expected output destination"
        ),
    ):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_missing_required_internal_link(
    assembled_pdf_run: PdfQARun,
) -> None:
    def remove_internal_link(writer: PdfWriter) -> None:
        for page in writer.pages:
            annotations = page.get("/Annots")
            if annotations is None:
                continue
            retained = [
                item
                for item in annotations
                if item.get_object().get("/A") is not None
            ]
            page[NameObject("/Annots")] = type(annotations)(retained)

    _rewrite_pdf(assembled_pdf_run, remove_internal_link)

    with pytest.raises(PdfQAFailure, match="internal link"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_internal_link_to_wrong_valid_output_target(
    assembled_pdf_run: PdfQARun,
) -> None:
    def retarget_internal_link(writer: PdfWriter) -> None:
        for page in writer.pages:
            for reference in page.get("/Annots", []):
                annotation = reference.get_object()
                if annotation.get("/Dest") is None:
                    continue
                annotation[NameObject("/Dest")] = ArrayObject(
                    [
                        writer.pages[0].indirect_reference,
                        NameObject("/XYZ"),
                        FloatObject(260.0),
                        FloatObject(600.0),
                        NullObject(),
                    ]
                )
                return
        raise AssertionError("fixture internal link was not found")

    _rewrite_pdf(assembled_pdf_run, retarget_internal_link)

    with pytest.raises(PdfQAFailure, match="expected output destination"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_review_retry_zone_disagreement(
    assembled_pdf_run: PdfQARun,
) -> None:
    path = assembled_pdf_run.run_dir / "review.json"
    review = json.loads(path.read_text(encoding="utf-8"))
    review["retries"] = {"zone-999": 0}
    _write_json(path, review)

    with pytest.raises(PdfQAFailure, match="zones|retries"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_rejects_frame_outside_actual_pdf_page(
    assembled_pdf_run: PdfQARun,
) -> None:
    path = assembled_pdf_run.run_dir / "layout.json"
    layout = json.loads(path.read_text(encoding="utf-8"))
    layout["flowables"][0]["frame"] = [54.0, 54.0, 700.0, 684.0]
    _write_json(path, layout)

    with pytest.raises(PdfQAFailure, match="page bounds"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


def test_prepare_pdf_qa_cleans_post_publication_base_exception_by_identity(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_publish = pdf_qa_module.assembly._publish_new_file

    def publish_then_interrupt(*args: object, **kwargs: object) -> object:
        real_publish(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_publish_new_file",
        publish_then_interrupt,
    )
    with pytest.raises(KeyboardInterrupt):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert not (assembled_pdf_run.run_dir / "qa-pages").exists()
    assert not (assembled_pdf_run.run_dir / "pdf-qa.json").exists()
    assert not assembled_pdf_run.output_dir.exists()


def test_prepare_pdf_qa_rejects_raced_render_symlink_without_following_it(
    assembled_pdf_run: PdfQARun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside.png"
    Image.new("RGB", (40, 40), "magenta").save(outside)
    real_build = pdf_qa_module.build_contact_sheets

    def build_then_race(*args: object, **kwargs: object) -> dict[str, list[int]]:
        mapping = real_build(*args, **kwargs)  # type: ignore[arg-type]
        page = assembled_pdf_run.run_dir / next(
            path.name
            for path in assembled_pdf_run.run_dir.iterdir()
            if path.name.startswith(".pdf-qa-preparing-")
        ) / "qa-pages" / "page-001.png"
        page.unlink()
        page.symlink_to(outside)
        return mapping

    monkeypatch.setattr(pdf_qa_module, "build_contact_sheets", build_then_race)

    with pytest.raises(PdfQAFailure, match="regular file|symbolic|identity"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert outside.is_file()
    assert not (assembled_pdf_run.run_dir / "qa-pages").exists()
    assert not (assembled_pdf_run.run_dir / "pdf-qa.json").exists()


def test_prepare_pdf_qa_removes_owned_staging_after_prepublication_failure(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_publication(*args: object, **kwargs: object) -> None:
        raise PdfQAFailure("injected directory publication failure")

    monkeypatch.setattr(pdf_qa_module, "_publish_new_directory", fail_publication)

    with pytest.raises(PdfQAFailure, match="injected directory publication failure"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    assert list(assembled_pdf_run.run_dir.glob(".pdf-qa-preparing-*")) == []
    assert not (assembled_pdf_run.run_dir / "qa-pages").exists()
    assert not (assembled_pdf_run.run_dir / "pdf-qa.json").exists()


def test_pdf_qa_result_requires_sequential_contact_sheet_names(
    assembled_pdf_run: PdfQARun,
) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    path = assembled_pdf_run.run_dir / "pdf-qa.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    digest = value["contact_sheet_hashes"].pop("contact-sheet-001.png")
    pages = value["contact_sheet_pages"].pop("contact-sheet-001.png")
    value["contact_sheet_hashes"]["contact-sheet-002.png"] = digest
    value["contact_sheet_pages"]["contact-sheet-002.png"] = pages

    with pytest.raises(PdfQAFailure, match="contact-sheet.*sequential"):
        PdfQAResult.from_dict(value, assembled_pdf_run.run_dir / "qa-pages")


def test_prepare_pdf_qa_requires_external_link_for_each_source_block(
    assembled_pdf_run: PdfQARun,
) -> None:
    path = assembled_pdf_run.run_dir / "document.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    internal = next(
        block for block in document["blocks"] if block["destination"] is not None
    )
    internal["destination"] = None
    internal["uri"] = "https://example.com/a%20b"
    _write_json(path, document)

    with pytest.raises(PdfQAFailure, match="external URI annotation.*block"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)


@pytest.mark.parametrize("face", ["Regular", "Bold"])
@pytest.mark.parametrize("payload", [b"", b"not-a-true-type-font"])
def test_prepare_pdf_qa_rejects_empty_or_corrupt_embedded_font_program(
    assembled_pdf_run: PdfQARun,
    face: str,
    payload: bytes,
) -> None:
    _rewrite_pdf(
        assembled_pdf_run,
        lambda writer: _replace_embedded_font_program(writer, face, payload),
    )

    with pytest.raises(PdfQAFailure, match=f"{face}.*font|font.*{face}"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)


@pytest.mark.parametrize("face", ["Regular", "Bold"])
def test_prepare_pdf_qa_rejects_checksum_corrupt_embedded_font_program(
    assembled_pdf_run: PdfQARun,
    face: str,
) -> None:
    _rewrite_pdf(
        assembled_pdf_run,
        lambda writer: _corrupt_embedded_font_glyph_table(writer, face),
    )

    with pytest.raises(PdfQAFailure, match=f"{face}.*font|font.*{face}"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)


def test_prepare_pdf_qa_rejects_known_tofu_boxes_in_korean_block_renders(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = json.loads(
        (assembled_pdf_run.run_dir / "layout.json").read_text(encoding="utf-8")
    )

    def render_tofu(
        source_pdf: Path,
        destination: Path,
        *,
        dpi: int,
        name_width: int,
        existing_destination_identity: tuple[int, int],
    ) -> list[Path]:
        assert dpi == 144
        assert name_width == 3
        page_count = len(PdfReader(source_pdf).pages)
        width = round(layout["page_size"]["width"] * 2)
        height = round(layout["page_size"]["height"] * 2)
        metadata = destination.stat()
        assert existing_destination_identity == (metadata.st_dev, metadata.st_ino)
        paths: list[Path] = []
        for page_number in range(1, page_count + 1):
            image = Image.new("RGB", (width, height), "white")
            draw = ImageDraw.Draw(image)
            for item in layout["flowables"]:
                if item["page_number"] != page_number or item["kind"] == "figure":
                    continue
                x, y, box_width, box_height = item["bounds"]
                left = round(x * 2) + 2
                top = round(height - (y + box_height) * 2) + 2
                glyph_height = max(6, min(16, round(box_height * 2) - 4))
                glyph_width = max(5, min(11, round(box_width * 2 / 6)))
                for index in range(4):
                    glyph_left = left + index * (glyph_width + 3)
                    draw.rectangle(
                        (
                            glyph_left,
                            top,
                            glyph_left + glyph_width,
                            top + glyph_height,
                        ),
                        outline="black",
                        width=1,
                    )
            path = destination / f"page-{page_number:03d}.png"
            image.save(path)
            image.close()
            paths.append(path)
        return paths

    monkeypatch.setattr(pdf_qa_module, "render_pdf_pages", render_tofu)

    with pytest.raises(PdfQAFailure, match="replacement boxes|tofu"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)


def test_prepare_pdf_qa_rejects_pure_white_page_even_with_flowable_evidence(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def render_white(
        source_pdf: Path,
        destination: Path,
        *,
        dpi: int,
        name_width: int,
        existing_destination_identity: tuple[int, int],
    ) -> list[Path]:
        metadata = destination.stat()
        assert existing_destination_identity == (metadata.st_dev, metadata.st_ino)
        paths: list[Path] = []
        for page_number, _page in enumerate(PdfReader(source_pdf).pages, start=1):
            path = destination / f"page-{page_number:03d}.png"
            Image.new("RGB", (1224, 1584), "white").save(path)
            paths.append(path)
        return paths

    monkeypatch.setattr(pdf_qa_module, "render_pdf_pages", render_white)

    with pytest.raises(PdfQAFailure, match="blank output page"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)


def test_prepare_pdf_qa_rejects_staged_pdf_swap_before_poppler_render(
    assembled_pdf_run: PdfQARun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = make_text_pdf(tmp_path / "replacement.pdf")
    staged = assembled_pdf_run.run_dir / "staged-output" / "translated.pdf"
    real_render = pdf_qa_module.render_pdf_pages

    def swap_then_render(*args: object, **kwargs: object) -> list[Path]:
        replacement.replace(staged)
        return real_render(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pdf_qa_module, "render_pdf_pages", swap_then_render)

    with pytest.raises(PdfQAFailure, match="staged translated PDF.*identity|changed identity"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)


@pytest.mark.parametrize("mutation", ["overwrite", "truncate"])
def test_prepare_pdf_qa_rejects_same_inode_staged_pdf_mutation_after_render(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    staged = assembled_pdf_run.run_dir / "staged-output" / "translated.pdf"
    real_render = pdf_qa_module.render_pdf_pages

    def render_then_mutate(*args: object, **kwargs: object) -> list[Path]:
        paths = real_render(*args, **kwargs)  # type: ignore[arg-type]
        identity = (staged.stat().st_dev, staged.stat().st_ino)
        with staged.open("r+b") as stream:
            if mutation == "overwrite":
                first = stream.read(1)
                assert first
                stream.seek(0)
                stream.write(bytes([first[0] ^ 0xFF]))
            else:
                stream.truncate(staged.stat().st_size // 2)
        assert (staged.stat().st_dev, staged.stat().st_ino) == identity
        return paths

    monkeypatch.setattr(pdf_qa_module, "render_pdf_pages", render_then_mutate)

    with pytest.raises(PdfQAFailure, match="staged translated PDF.*content"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)
    assert list(assembled_pdf_run.run_dir.glob(".pdf-qa-preparing-*")) == []
    assert list(assembled_pdf_run.run_dir.rglob("render-input.pdf")) == []


def test_prepare_pdf_qa_checks_staged_pdf_content_immediately_after_render(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = assembled_pdf_run.run_dir / "staged-output" / "translated.pdf"
    original = staged.read_bytes()
    identity = (staged.stat().st_dev, staged.stat().st_ino)
    real_render = pdf_qa_module.render_pdf_pages
    real_validate = pdf_qa_module._validate_rendered_pages

    def render_then_mutate(*args: object, **kwargs: object) -> list[Path]:
        paths = real_render(*args, **kwargs)  # type: ignore[arg-type]
        with staged.open("r+b") as stream:
            stream.write(bytes([original[0] ^ 0xFF]))
        assert (staged.stat().st_dev, staged.stat().st_ino) == identity
        return paths

    def restore_then_validate(*args: object, **kwargs: object) -> None:
        with staged.open("r+b") as stream:
            stream.write(original)
            stream.truncate()
        real_validate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pdf_qa_module, "render_pdf_pages", render_then_mutate)
    monkeypatch.setattr(pdf_qa_module, "_validate_rendered_pages", restore_then_validate)

    with pytest.raises(PdfQAFailure, match="staged translated PDF.*content"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)
    assert list(assembled_pdf_run.run_dir.glob(".pdf-qa-preparing-*")) == []
    assert list(assembled_pdf_run.run_dir.rglob("render-input.pdf")) == []


@pytest.mark.parametrize("fault", ["premature-eof", "oserror", "interrupt"])
def test_prepare_pdf_qa_fails_closed_when_staged_pdf_reread_fails(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    class RereadFaultStream:
        def __init__(self, wrapped: object) -> None:
            self.wrapped = wrapped
            self.read_count = 0

        def read(self, *args: object, **kwargs: object) -> bytes:
            self.read_count += 1
            if self.read_count == 2:
                if fault == "premature-eof":
                    return b""
                if fault == "oserror":
                    raise OSError("injected staged translated PDF reread failure")
                raise KeyboardInterrupt(
                    "injected staged translated PDF reread interruption"
                )
            return self.wrapped.read(*args, **kwargs)  # type: ignore[union-attr,no-any-return]

        def __getattr__(self, name: str) -> object:
            return getattr(self.wrapped, name)

    real_open = pdf_qa_module.assembly._open_anchored_input_file

    def open_with_reread_fault(*args: object, **kwargs: object) -> object:
        opened = real_open(*args, **kwargs)  # type: ignore[arg-type]
        if args[1] == "translated.pdf":
            opened.stream = RereadFaultStream(opened.stream)  # type: ignore[assignment]
        return opened

    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_open_anchored_input_file",
        open_with_reread_fault,
    )

    expected = KeyboardInterrupt if fault == "interrupt" else PdfQAFailure
    with pytest.raises(expected, match="staged translated PDF"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)
    assert list(assembled_pdf_run.run_dir.glob(".pdf-qa-preparing-*")) == []
    assert list(assembled_pdf_run.run_dir.rglob("render-input.pdf")) == []


@pytest.mark.parametrize("interrupt_after", ["record", "page"])
def test_prepare_pdf_qa_keeps_new_public_pair_after_prior_cleanup_interruption(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_after: str,
) -> None:
    first = prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    real_remove = pdf_qa_module.assembly._remove_owned_file
    prior_record_removed = False

    def remove_then_interrupt(
        directory: object,
        name: str,
        published: object,
    ) -> None:
        nonlocal prior_record_removed
        real_remove(directory, name, published)  # type: ignore[arg-type]
        if name == "prior-pdf-qa.json":
            prior_record_removed = True
            if interrupt_after == "record":
                raise KeyboardInterrupt
        elif interrupt_after == "page" and prior_record_removed and name.startswith("page-"):
            raise KeyboardInterrupt

    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_remove_owned_file",
        remove_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    record_path = assembled_pdf_run.run_dir / "pdf-qa.json"
    pages_path = assembled_pdf_run.run_dir / "qa-pages"
    assert record_path.is_file()
    assert pages_path.is_dir()
    record = PdfQAResult.from_dict(
        json.loads(record_path.read_text(encoding="utf-8")),
        pages_path,
    )
    assert record.staged_pdf_sha256 == first.staged_pdf_sha256
    assert all((pages_path / name).is_file() for name in record.rendered_page_hashes)
    assert all((pages_path / name).is_file() for name in record.contact_sheet_hashes)
    assert not assembled_pdf_run.output_dir.exists()


def test_publish_new_directory_rejects_and_preserves_source_name_swap(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_publish = pdf_qa_module._publish_new_directory
    racer_marker = "unrelated-racer"

    def swap_source_then_publish(
        source_parent: object,
        source_name: str,
        source: object,
        destination_parent: object,
        destination_name: str,
    ) -> None:
        parent_path = source_parent.current_path()  # type: ignore[attr-defined]
        (parent_path / source_name).rename(parent_path / "held-original-qa-pages")
        racer = parent_path / source_name
        racer.mkdir()
        (racer / "keep.txt").write_text(racer_marker, encoding="utf-8")
        real_publish(
            source_parent,  # type: ignore[arg-type]
            source_name,
            source,  # type: ignore[arg-type]
            destination_parent,  # type: ignore[arg-type]
            destination_name,
        )

    monkeypatch.setattr(pdf_qa_module, "_publish_new_directory", swap_source_then_publish)

    with pytest.raises(PdfQAFailure, match="identity"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)
    markers = list(assembled_pdf_run.run_dir.rglob("keep.txt"))
    assert len(markers) == 1
    assert markers[0].read_text(encoding="utf-8") == racer_marker


def test_publish_new_directory_quarantines_swap_during_posix_rename(
    assembled_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if pdf_qa_module.os.name == "nt":
        pytest.skip("POSIX dirfd rename race")
    real_publish = pdf_qa_module._publish_new_directory
    real_rename = pdf_qa_module.os.rename
    racer_marker = "post-check-racer"

    def publish_with_rename_race(
        source_parent: object,
        source_name: str,
        source: object,
        destination_parent: object,
        destination_name: str,
    ) -> None:
        raced = False

        def race_rename(
            source_path: object,
            destination_path: object,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal raced
            if (
                not raced
                and source_path == source_name
                and destination_path == destination_name
            ):
                raced = True
                parent_path = source_parent.current_path()  # type: ignore[attr-defined]
                real_rename(
                    parent_path / source_name,
                    parent_path / "held-original-after-precheck",
                )
                racer = parent_path / source_name
                racer.mkdir()
                (racer / "keep.txt").write_text(racer_marker, encoding="utf-8")
            real_rename(source_path, destination_path, *args, **kwargs)

        monkeypatch.setattr(pdf_qa_module.os, "rename", race_rename)
        real_publish(
            source_parent,  # type: ignore[arg-type]
            source_name,
            source,  # type: ignore[arg-type]
            destination_parent,  # type: ignore[arg-type]
            destination_name,
        )

    monkeypatch.setattr(pdf_qa_module, "_publish_new_directory", publish_with_rename_race)

    with pytest.raises(PdfQAFailure, match="identity"):
        prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)

    _assert_no_public_qa_evidence(assembled_pdf_run)
    markers = list(assembled_pdf_run.run_dir.rglob("keep.txt"))
    assert len(markers) == 1
    assert markers[0].read_text(encoding="utf-8") == racer_marker
