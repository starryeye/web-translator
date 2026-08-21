from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.pdf_fixtures import (
    make_pdf_document,
    make_pdf_layout_review,
    make_pdf_block,
    make_pdf_block_style,
    make_pdf_page,
    make_pdf_page_evidence,
    make_pdf_source_record,
    make_pdf_table_cell,
)
from web_translator.paths import create_pdf_run_paths
from web_translator.pdf_models import (
    PdfBlock,
    PdfBlockStyle,
    PdfContractError,
    PdfDocument,
    PdfLayoutReview,
    PdfPage,
    PdfPageEvidence,
    PdfSourceRecord,
    PdfTableCell,
)


@pytest.mark.parametrize(
    ("record_type", "record"),
    [
        (PdfSourceRecord, make_pdf_source_record()),
        (PdfPageEvidence, make_pdf_page_evidence()),
        (PdfBlockStyle, make_pdf_block_style()),
        (PdfTableCell, make_pdf_table_cell()),
        (PdfBlock, make_pdf_block()),
        (PdfPage, make_pdf_page()),
        (PdfDocument, make_pdf_document()),
        (PdfLayoutReview, make_pdf_layout_review()),
    ],
)
def test_pdf_records_round_trip_exact_json_contract(record_type: type[object], record: object) -> None:
    payload = record.to_dict()
    assert record_type.from_dict(payload) == record

    payload["unknown"] = True
    with pytest.raises(PdfContractError, match="fields must be exactly"):
        record_type.from_dict(payload)


@pytest.mark.parametrize(
    ("record_type", "record", "field", "value"),
    [
        (PdfSourceRecord, make_pdf_source_record(), "byte_length", "42"),
        (PdfPageEvidence, make_pdf_page_evidence(), "image_coverage", float("nan")),
        (PdfBlockStyle, make_pdf_block_style(), "font_size", "12"),
        (PdfTableCell, make_pdf_table_cell(), "is_header", "true"),
        (PdfBlock, make_pdf_block(), "kind", "unsupported"),
        (PdfPage, make_pdf_page(), "width", 0.0),
        (PdfDocument, make_pdf_document(), "source_sha256", "not-a-hash"),
        (PdfLayoutReview, make_pdf_layout_review(), "pages_reviewed", [2, 1]),
    ],
)
def test_pdf_records_reject_mistyped_or_invalid_values(
    record_type: type[object], record: object, field: str, value: object
) -> None:
    payload = record.to_dict()
    payload[field] = value

    with pytest.raises(PdfContractError):
        record_type.from_dict(payload)


def test_pdf_document_round_trip_rejects_unknown_fields() -> None:
    document = PdfDocument(
        schema_version="1.0",
        source_sha256="a" * 64,
        page_count=1,
        selectable_characters=42,
        scan_candidate_pages=[],
        pages=[PdfPage(number=1, width=612.0, height=792.0, rotation=0)],
        blocks=[
            PdfBlock(
                id="pdf:page-0001:block-0001",
                page_number=1,
                order=0,
                kind="paragraph",
                bbox=(72.0, 72.0, 540.0, 96.0),
                style=PdfBlockStyle(12.0, False, "left", 0.0, 8.0),
                source_text="Selectable text",
                segment_id="seg-000001",
            )
        ],
    )
    assert PdfDocument.from_dict(document.to_dict()) == document
    payload = document.to_dict()
    payload["unknown"] = True
    with pytest.raises(PdfContractError, match="fields must be exactly"):
        PdfDocument.from_dict(payload)


def test_pdf_document_rejects_blocks_outside_document_order() -> None:
    payload = make_pdf_document().to_dict()
    payload["blocks"].append(make_pdf_block(order=1).to_dict())
    payload["blocks"] = list(reversed(payload["blocks"]))

    with pytest.raises(PdfContractError, match="blocks must be in exact document order"):
        PdfDocument.from_dict(payload)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"table_id": "pdf:page-0002:table-0001", "row": 0, "column": 0},
            "table_id page must match page_number",
        ),
        ({"kind": "table-cell"}, "table-cell blocks must include table_id, row, and column"),
        ({"row": 0}, "table metadata must include table_id, row, and column together"),
    ],
)
def test_pdf_block_rejects_incoherent_table_metadata(
    changes: dict[str, object], message: str
) -> None:
    payload = make_pdf_block().to_dict()
    payload.update(changes)

    with pytest.raises(PdfContractError, match=message):
        PdfBlock.from_dict(payload)


def test_pdf_document_rejects_duplicate_block_ids() -> None:
    payload = make_pdf_document().to_dict()
    duplicate = make_pdf_block(order=1).to_dict()
    duplicate["id"] = payload["blocks"][0]["id"]
    payload["blocks"].append(duplicate)

    with pytest.raises(PdfContractError, match="blocks must have unique IDs"):
        PdfDocument.from_dict(payload)


def test_pdf_run_paths_use_separate_collision_safe_output_root(tmp_path: Path) -> None:
    now = datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC)
    existing = tmp_path / "translated-pdfs" / "report-20260821-010203"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("existing output", encoding="utf-8")

    paths = create_pdf_run_paths(tmp_path, "report.pdf", now)

    assert paths.work_dir.name == "report-20260821-010203-2"
    assert paths.output_dir == tmp_path / "translated-pdfs" / paths.run_id
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "existing output"
    assert paths.work_dir.is_dir()
    assert not paths.output_dir.exists()


@pytest.mark.parametrize(
    ("source_label", "expected_run_id"),
    [
        ("자료 보고서 final.pdf", "final-20260821-010203"),
        (r"C:\자료 폴더\quarterly report.pdf", "quarterly-report-20260821-010203"),
    ],
)
def test_pdf_run_paths_portably_derive_slug_from_source_label(
    tmp_path: Path, source_label: str, expected_run_id: str
) -> None:
    now = datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC)

    paths = create_pdf_run_paths(tmp_path, source_label, now)

    assert paths.run_id == expected_run_id
    assert paths.work_dir.name == expected_run_id
