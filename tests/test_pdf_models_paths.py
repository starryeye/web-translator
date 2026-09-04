from datetime import UTC, datetime
import os
from pathlib import Path
from types import SimpleNamespace

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
    PdfLinkEvidence,
    PdfPage,
    PdfPageEvidence,
    PdfSourceRecord,
    PdfTableCell,
)


def test_pdf_block_requires_a_supported_semantic_role() -> None:
    payload = make_pdf_block().to_dict()
    payload["semantic_role"] = "magazine-sidebar"
    with pytest.raises(PdfContractError, match="semantic_role is not supported"):
        PdfBlock.from_dict(payload)


def test_pdf_document_upgrades_schema_1_blocks_to_body_role() -> None:
    payload = make_pdf_document().to_dict()
    payload["schema_version"] = "1.0"
    for block in payload["blocks"]:
        block.pop("semantic_role", None)
    loaded = PdfDocument.from_dict(payload)
    assert loaded.schema_version == "1.1"
    assert [block.semantic_role for block in loaded.blocks] == ["body"]


def _link_evidence() -> PdfLinkEvidence:
    return PdfLinkEvidence(
        id="pdf:page-0001:link-0001",
        page_number=1,
        source_block_id="pdf:page-0001:block-0001",
        source_span=(0, 10),
        bounds=(72.0, 72.0, 140.0, 96.0),
        visible_label="Selectable",
        uri="https://example.com/one",
        destination=None,
        reconstructed=True,
        reason=None,
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
        (PdfLinkEvidence, _link_evidence()),
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


def test_pdf_source_record_requires_sorted_unique_warning_evidence() -> None:
    payload = make_pdf_source_record().to_dict()
    payload["warnings"] = ["generic-content-type: application/octet-stream"]

    record = PdfSourceRecord.from_dict(payload)

    assert record.warnings == ["generic-content-type: application/octet-stream"]

    unsorted = dict(payload)
    unsorted["warnings"] = ["z-warning", "a-warning"]
    with pytest.raises(PdfContractError, match="warnings must be sorted and unique"):
        PdfSourceRecord.from_dict(unsorted)

    duplicate = dict(payload)
    duplicate["warnings"] = ["generic-content-type: application/octet-stream"] * 2
    with pytest.raises(PdfContractError, match="warnings must be sorted and unique"):
        PdfSourceRecord.from_dict(duplicate)

    missing = dict(payload)
    del missing["warnings"]
    with pytest.raises(PdfContractError, match="fields must be exactly"):
        PdfSourceRecord.from_dict(missing)


def test_pdf_document_round_trip_rejects_unknown_fields() -> None:
    document = PdfDocument(
        schema_version="1.1",
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
        links=[_link_evidence()],
        extraction_warnings=[],
    )
    assert PdfDocument.from_dict(document.to_dict()) == document
    payload = document.to_dict()
    payload["unknown"] = True
    with pytest.raises(PdfContractError, match="fields must be exactly"):
        PdfDocument.from_dict(payload)


def test_pdf_document_strictly_validates_link_spans_and_warning_evidence() -> None:
    document = make_pdf_document()
    payload = document.to_dict()
    payload["links"] = [_link_evidence().to_dict()]
    payload["extraction_warnings"] = [
        "page 1 link 2: label='Docs' destination='missing-target' reason=unresolved"
    ]

    parsed = PdfDocument.from_dict(payload)

    assert parsed.links == [_link_evidence()]
    assert parsed.extraction_warnings == payload["extraction_warnings"]

    payload["links"][0]["source_span"] = [0, 11]
    with pytest.raises(PdfContractError, match="visible label|source span"):
        PdfDocument.from_dict(payload)


def test_unreconstructed_link_requires_complete_label_destination_and_reason() -> None:
    payload = _link_evidence().to_dict()
    payload.update(
        {
            "source_block_id": None,
            "source_span": None,
            "uri": None,
            "destination": "unresolved-named-destination",
            "reconstructed": False,
            "reason": "visible source intersects multiple blocks",
        }
    )

    assert PdfLinkEvidence.from_dict(payload).visible_label == "Selectable"

    payload["visible_label"] = ""
    with pytest.raises(PdfContractError, match="visible_label"):
        PdfLinkEvidence.from_dict(payload)


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


def test_pdf_run_paths_return_lexically_absolute_paths_without_resolving_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    paths = create_pdf_run_paths(
        Path("workspace"),
        "report.pdf",
        datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC),
    )

    assert paths.work_dir.is_absolute()
    assert paths.output_dir.is_absolute()
    assert paths.work_dir == (
        tmp_path
        / "workspace"
        / ".web-translator"
        / "runs"
        / "report-20260821-010203"
    )


@pytest.mark.parametrize("linked_component", ["workspace", "runs", "output", "dangling-output"])
def test_pdf_run_paths_reject_linked_workspace_run_and_output_roots(
    tmp_path: Path,
    linked_component: str,
) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "linked-target"
    target.mkdir()
    try:
        if linked_component == "workspace":
            workspace.symlink_to(target, target_is_directory=True)
        else:
            workspace.mkdir()
            if linked_component == "runs":
                control = workspace / ".web-translator"
                control.mkdir()
                (control / "runs").symlink_to(target, target_is_directory=True)
            elif linked_component == "output":
                (workspace / "translated-pdfs").symlink_to(
                    target, target_is_directory=True
                )
            else:
                (workspace / "translated-pdfs").symlink_to(
                    tmp_path / "missing-target", target_is_directory=True
                )
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    with pytest.raises(ValueError, match="link|reparse|safe directory"):
        create_pdf_run_paths(
            workspace,
            "report.pdf",
            datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC),
        )


def test_pdf_run_paths_reject_windows_reparse_output_root_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    output_root = workspace / "translated-pdfs"
    output_root.mkdir(parents=True)
    real_stat = os.stat

    def reparse_stat(path: str | os.PathLike[str], *args: object, **kwargs: object) -> object:
        result = real_stat(path, *args, **kwargs)
        if Path(path) == output_root:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_file_attributes=0x400,
            )
        return result

    monkeypatch.setattr(os, "stat", reparse_stat)

    with pytest.raises(ValueError, match="reparse"):
        create_pdf_run_paths(
            workspace,
            "report.pdf",
            datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC),
        )


def test_pdf_run_paths_reject_workspace_replacement_during_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    held_workspace = tmp_path / "held-workspace"
    run_id = "report-20260821-010203"
    real_mkdir = os.mkdir
    replaced = False

    def replace_workspace_then_mkdir(
        path: str | os.PathLike[str],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal replaced
        if not replaced and Path(path).name == run_id:
            replaced = True
            workspace.rename(held_workspace)
            real_mkdir(workspace)
            real_mkdir(workspace / ".web-translator")
            real_mkdir(workspace / ".web-translator" / "runs")
            real_mkdir(workspace / "translated-pdfs")
        if dir_fd is None:
            real_mkdir(path, mode)
        else:
            real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", replace_workspace_then_mkdir)

    with pytest.raises(ValueError, match="changed identity|moved"):
        create_pdf_run_paths(
            workspace,
            "report.pdf",
            datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC),
        )

    assert replaced is True
    assert not (workspace / ".web-translator" / "runs" / run_id).exists()


def test_pdf_run_contract_requires_matching_exact_allocator_children(
    tmp_path: Path,
) -> None:
    from web_translator.paths import hold_allocated_run_paths

    workspace = tmp_path / "workspace"
    paths = create_pdf_run_paths(
        workspace,
        "source.pdf",
        datetime(2026, 8, 30, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="same run ID"):
        with hold_allocated_run_paths(
            workspace,
            paths.work_dir,
            paths.output_dir.with_name("different-run-id"),
            output_root="translated-pdfs",
        ):
            pass


def test_pdf_run_contract_retains_held_workspace_identity(tmp_path: Path) -> None:
    from web_translator.paths import hold_allocated_run_paths

    if os.name == "nt":
        pytest.skip("native Windows identity injection is covered separately")
    workspace = tmp_path / "workspace"
    paths = create_pdf_run_paths(
        workspace,
        "source.pdf",
        datetime(2026, 8, 30, tzinfo=UTC),
    )
    moved = tmp_path / "held-workspace"

    with hold_allocated_run_paths(
        workspace,
        paths.work_dir,
        paths.output_dir,
        output_root="translated-pdfs",
    ) as contract:
        workspace.rename(moved)
        workspace.mkdir()
        with pytest.raises(ValueError, match="workspace.*changed identity"):
            contract.verify()
