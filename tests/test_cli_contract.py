from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path

import pytest

import web_translator.cli as cli_module
import web_translator.pdf_assemble as pdf_assemble_module
from web_translator.cli import (
    CLIContractError,
    _build_manifest_provenance,
    _detect_source_language,
    _read_capture,
    _read_review,
    main,
)
from web_translator.models import MasterReview, QAResult, Segment, Translation, write_segments
from web_translator.pdf_models import (
    PdfBlock,
    PdfBlockStyle,
    PdfDocument,
    PdfPage,
    PdfSourceRecord,
)
from web_translator.pdf_qa import PdfQAFailure
from web_translator.pdf_review import build_pdf_semantic_review_input
from web_translator.paths import create_pdf_run_paths
from web_translator.zones import Zone
from tests.pdf_fixtures import make_image_only_pdf, make_text_pdf


def test_native_pdf_assignment_publishes_binding_with_unchanged_package_shape(tmp_path):
    from tests.test_pdf_unit_bindings import BINDING, make_unit_run, manual_binding
    run = make_unit_run(tmp_path)
    assert main(["prepare-assignments", "--run-dir", str(run)]) == 0
    assert json.loads((run / "assignments" / BINDING).read_bytes()) == manual_binding(run)
    payload = json.loads((run / "assignments" / "zone-001.json").read_bytes())
    assert set(payload) == {"context_after", "context_before", "document_summary", "glossary", "schema_version", "targets", "zone_id"}


@pytest.mark.parametrize("command", ["plan-zones", "prepare-assignments"])
def test_required_flow_finding_prevents_assignment(tmp_path, command):
    from tests.test_pdf_unit_bindings import make_unit_run, write_json
    run = make_unit_run(tmp_path)
    document = json.loads((run / "document.json").read_bytes())
    document["flow_findings"] = [{"code": "ambiguous-page-continuation", "left_block_id": document["blocks"][0]["id"], "right_block_id": document["blocks"][1]["id"], "severity": "required", "message": "Uncertain continuation"}]
    write_json(run / "document.json", document)
    if command == "plan-zones":
        (run / "zones" / "zone-001.json").unlink()
        (run / "zones").rmdir()
    assert main([command, "--run-dir", str(run)]) == cli_module.EXIT_CONTRACT_FAILURE
    assert not (run / "assignments").exists()
    if command == "plan-zones":
        assert not (run / "zones").exists()


def test_native_pdf_planning_rejects_unit_budget_before_writes(tmp_path, capsys):
    from tests.test_pdf_unit_bindings import make_unit_run
    run = make_unit_run(tmp_path)
    (run / "zones" / "zone-001.json").unlink()
    (run / "zones").rmdir()
    assert main(["plan-zones", "--run-dir", str(run), "--max-chars", "3"]) == cli_module.EXIT_CONTRACT_FAILURE
    assert "pdf:unit-000001" in capsys.readouterr().err
    assert not (run / "zones").exists()


def test_native_pdf_assignment_failed_rename_publishes_nothing(tmp_path, monkeypatch):
    import web_translator.pdf_qa as pdf_qa
    from tests.test_pdf_unit_bindings import make_unit_run
    run = make_unit_run(tmp_path)
    original = os.replace
    def fail_assignment(source, destination, *args, **kwargs):
        if Path(destination) == run / "assignments":
            raise OSError("injected assignment rename failure")
        return original(source, destination, *args, **kwargs)
    monkeypatch.setattr(os, "replace", fail_assignment)
    def fail_anchored_assignment(*args, **kwargs):
        raise OSError("injected assignment rename failure")
    monkeypatch.setattr(pdf_qa, "_rename_anchored_directory_no_replace", fail_anchored_assignment)
    assert main(["prepare-assignments", "--run-dir", str(run)]) == cli_module.EXIT_CONTRACT_FAILURE
    assert not (run / "assignments").exists()
    assert not list(run.glob(".assignments-*"))


def test_native_assignment_rejects_package_changed_after_binding_write(tmp_path, monkeypatch):
    from tests.test_pdf_unit_bindings import BINDING, make_unit_run
    run = make_unit_run(tmp_path)
    original = cli_module._write_json_atomic
    def mutate_package(path, value):
        original(path, value)
        if path.name == BINDING:
            package = path.parent / "zone-001.json"
            package.write_bytes(package.read_bytes() + b" ")
    monkeypatch.setattr(cli_module, "_write_json_atomic", mutate_package)
    assert main(["prepare-assignments", "--run-dir", str(run)]) == cli_module.EXIT_CONTRACT_FAILURE
    assert not (run / "assignments").exists()
    assert not list(run.glob(".assignments-*"))


def _inject_assignment_rename(monkeypatch, run, mutation):
    """Exercise both the existing and anchored no-clobber publication boundaries."""
    import web_translator.pdf_qa as pdf_qa
    original_replace = os.replace
    original_rename = pdf_qa._rename_anchored_directory_no_replace
    def replace(source, destination, *args, **kwargs):
        result = original_replace(source, destination, *args, **kwargs)
        if Path(destination) == run / "assignments":
            mutation()
        return result
    def rename(source_parent, source_name, identity, destination_parent, destination_name, **kwargs):
        result = original_rename(source_parent, source_name, identity, destination_parent, destination_name, **kwargs)
        if destination_name == "assignments":
            mutation()
        return result
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(pdf_qa, "_rename_anchored_directory_no_replace", rename)


def test_native_assignment_source_mutation_at_rename_rolls_back_and_allows_retry(tmp_path, monkeypatch):
    from tests.test_pdf_unit_bindings import make_unit_run, binding_api
    run = make_unit_run(tmp_path)
    document = run / "document.json"
    payload = document.read_bytes()
    with monkeypatch.context() as patch:
        _inject_assignment_rename(patch, run, lambda: document.write_bytes(payload + b" "))
        assert main(["prepare-assignments", "--run-dir", str(run)]) == cli_module.EXIT_CONTRACT_FAILURE
    assert not (run / "assignments").exists()
    assert not list(run.glob(".assignments-*"))
    document.write_bytes(payload)
    assert main(["prepare-assignments", "--run-dir", str(run)]) == 0
    with binding_api().hold_pdf_unit_binding(run):
        pass


def test_native_assignment_output_mutation_at_rename_rolls_back(tmp_path, monkeypatch):
    from tests.test_pdf_unit_bindings import make_unit_run
    run = make_unit_run(tmp_path)
    def mutate():
        package = run / "assignments" / "zone-001.json"
        package.write_bytes(package.read_bytes() + b" ")
    _inject_assignment_rename(monkeypatch, run, mutate)
    assert main(["prepare-assignments", "--run-dir", str(run)]) == cli_module.EXIT_CONTRACT_FAILURE
    assert not (run / "assignments").exists()


def test_native_assignment_rename_error_after_move_rolls_back(tmp_path, monkeypatch):
    from tests.test_pdf_unit_bindings import make_unit_run
    run = make_unit_run(tmp_path)
    def raise_after_move():
        raise OSError("injected error after completed directory move")
    _inject_assignment_rename(monkeypatch, run, raise_after_move)
    assert main(["prepare-assignments", "--run-dir", str(run)]) == cli_module.EXIT_CONTRACT_FAILURE
    assert not (run / "assignments").exists()


def test_native_assignment_rollback_preserves_foreign_replaced_file(tmp_path, monkeypatch):
    from tests.test_pdf_unit_bindings import make_unit_run
    run = make_unit_run(tmp_path)
    def replace_file():
        path = run / "assignments" / "zone-001.json"
        path.rename(run / "old-assignment.json")
        path.write_text("foreign content")
    _inject_assignment_rename(monkeypatch, run, replace_file)
    assert main(["prepare-assignments", "--run-dir", str(run)]) == cli_module.EXIT_CONTRACT_FAILURE
    assert (run / "assignments" / "zone-001.json").read_text() == "foreign content"


def test_native_assignment_rollback_preserves_foreign_replacement(tmp_path, monkeypatch):
    from tests.test_pdf_unit_bindings import make_unit_run
    run = make_unit_run(tmp_path)
    def replace_output():
        (run / "assignments").rename(run / "moved-assignments")
        (run / "assignments").mkdir()
        (run / "assignments" / "foreign.txt").write_text("keep")
    _inject_assignment_rename(monkeypatch, run, replace_output)
    assert main(["prepare-assignments", "--run-dir", str(run)]) == cli_module.EXIT_CONTRACT_FAILURE
    assert (run / "assignments" / "foreign.txt").read_text() == "keep"


def test_native_assignment_does_not_replace_racing_empty_destination(tmp_path, monkeypatch):
    from tests.test_pdf_unit_bindings import BINDING, make_unit_run
    run = make_unit_run(tmp_path)
    original = cli_module._write_json_atomic
    foreign_identity = []
    def race_destination(path, value):
        original(path, value)
        if path.name == BINDING:
            (run / "assignments").mkdir()
            foreign_identity.append((run / "assignments").stat().st_ino)
    monkeypatch.setattr(cli_module, "_write_json_atomic", race_destination)
    assert main(["prepare-assignments", "--run-dir", str(run)]) == cli_module.EXIT_CONTRACT_FAILURE
    assert (run / "assignments").stat().st_ino == foreign_identity[0]
    assert list((run / "assignments").iterdir()) == []


def test_native_pdf_translation_validation_rejects_stale_binding(tmp_path):
    from tests.test_pdf_unit_bindings import make_unit_run
    run = make_unit_run(tmp_path)
    assert main(["prepare-assignments", "--run-dir", str(run)]) == 0
    (run / "translations").mkdir()
    (run / "translations" / "zone-001.jsonl").write_text(json.dumps(Translation("seg-000001", "번역 문장").to_dict()) + "\n")
    path = run / "document.json"
    path.write_bytes(path.read_bytes() + b" ")
    assert main(["validate-translations", "--run-dir", str(run)]) == cli_module.EXIT_CONTRACT_FAILURE


@pytest.mark.parametrize("origin", ["1.0", "1.1"])
def test_adapted_pdf_cannot_prepare_new_assignments(tmp_path, origin):
    from tests.test_pdf_unit_bindings import make_unit_run, write_json
    run = make_unit_run(tmp_path)
    path = run / "document.json"
    value = json.loads(path.read_bytes())
    value["extracted_schema_version"] = origin
    write_json(path, value)
    assert main(["prepare-assignments", "--run-dir", str(run)]) == cli_module.EXIT_CONTRACT_FAILURE
    assert not (run / "assignments").exists()


def test_native_assignment_rejects_source_mutation_before_publish(tmp_path, monkeypatch):
    from tests.test_pdf_unit_bindings import make_unit_run
    run = make_unit_run(tmp_path)
    original = cli_module._assignment_records
    def mutate(*args):
        path = run / "document.json"
        path.write_bytes(path.read_bytes() + b" ")
        return original(*args)
    monkeypatch.setattr(cli_module, "_assignment_records", mutate)
    assert main(["prepare-assignments", "--run-dir", str(run)]) == cli_module.EXIT_CONTRACT_FAILURE
    assert not (run / "assignments").exists()


def test_native_assignment_consumes_held_segments_during_swap_restore(tmp_path, monkeypatch):
    from tests.test_pdf_unit_bindings import make_unit_run
    run = make_unit_run(tmp_path)
    original = cli_module._read_segments
    def swap_restore(run_dir, unit_inputs=None):
        path = run_dir / "segments.jsonl"
        held = run_dir / "held-segments.jsonl"
        value = json.loads(path.read_text())
        value["source_text"] = "Unbound substitute"
        path.rename(held)
        try:
            path.write_text(json.dumps(value) + "\n")
            return original(run_dir, unit_inputs)
        finally:
            path.unlink()
            held.rename(path)
    monkeypatch.setattr(cli_module, "_read_segments", swap_restore)
    assert main(["prepare-assignments", "--run-dir", str(run)]) == 0
    payload = json.loads((run / "assignments" / "zone-001.json").read_bytes())
    assert payload["targets"][0]["source_text"] == "A paragraph continues."


def test_web_assignment_payload_is_unchanged_and_has_no_pdf_binding(tmp_path):
    from tests.test_pipeline import _web_cli_paths, write_single_segment_run_contract
    run, _ = _web_cli_paths(tmp_path)
    write_single_segment_run_contract(run)
    (run / "document-summary.txt").write_text("Summary")
    assert main(["prepare-assignments", "--run-dir", str(run)]) == 0
    assert sorted(path.name for path in (run / "assignments").iterdir()) == ["zone-001.json"]
    assert json.loads((run / "assignments" / "zone-001.json").read_bytes()) == {
        "context_after": [], "context_before": [], "document_summary": "Summary",
        "glossary": {}, "schema_version": "1.0", "zone_id": "zone-001",
        "targets": [{"heading_path": [], "id": "seg-000001", "protected": [],
                     "semantic_type": "paragraph", "source_text": "OAuth"}],
    }


REVIEW_DIMENSIONS = (
    "semantic_fidelity",
    "qualification_preservation",
    "naturalness",
    "terminology",
    "boundary_consistency",
    "protected_content",
)


def _pdf_cli_paths(tmp_path: Path) -> tuple[Path, Path]:
    paths = create_pdf_run_paths(
        tmp_path,
        "source.pdf",
        datetime(2026, 8, 30, tzinfo=UTC),
    )
    return paths.work_dir, paths.output_dir


def test_pdf_consumer_rejects_output_outside_exact_allocator_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = create_pdf_run_paths(
        tmp_path,
        "source.pdf",
        datetime(2026, 8, 30, tzinfo=UTC),
    )
    monkeypatch.setattr(cli_module, "prepare_pdf_qa", lambda *_args: None)

    exit_code = main(
        [
            "pdf-qa",
            "prepare",
            "--run-dir",
            str(paths.work_dir),
            "--output-dir",
            str(tmp_path / "translated-pdfs" / "different-run"),
        ]
    )

    assert exit_code == cli_module.EXIT_CONTRACT_FAILURE


def test_pdf_consumer_retains_allocator_roots_for_entire_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("native Windows retained-root injection is covered separately")
    paths = create_pdf_run_paths(
        tmp_path,
        "source.pdf",
        datetime(2026, 8, 30, tzinfo=UTC),
    )
    moved = tmp_path.with_name(f"{tmp_path.name}-held")

    def replace_workspace(*_args: object) -> None:
        tmp_path.rename(moved)
        tmp_path.mkdir()

    monkeypatch.setattr(cli_module, "prepare_pdf_qa", replace_workspace)

    exit_code = main(
        [
            "pdf-qa",
            "prepare",
            "--run-dir",
            str(paths.work_dir),
            "--output-dir",
            str(paths.output_dir),
        ]
    )

    assert exit_code == cli_module.EXIT_CONTRACT_FAILURE


def _write_pdf_assembly_cli_run(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    block = PdfBlock(
        id="pdf:page-0001:block-0001",
        page_number=1,
        order=0,
        kind="paragraph",
        bbox=(72.0, 72.0, 540.0, 96.0),
        style=PdfBlockStyle(11.0, False, "left", 0.0, 8.0),
        source_text="Selectable text",
        segment_id="seg-000001",
    )
    document = PdfDocument(
        schema_version="1.0",
        source_sha256="a" * 64,
        page_count=1,
        selectable_characters=15,
        scan_candidate_pages=[],
        pages=[PdfPage(number=1, width=612.0, height=792.0, rotation=0)],
        blocks=[block],
        table_cells=[],
    )
    source = PdfSourceRecord(
        schema_version="1.0",
        input_kind="local",
        requested_source="source.pdf",
        final_source="source.pdf",
        content_type="application/pdf",
        byte_length=123,
        sha256="a" * 64,
        acquired_at="2026-08-21T01:02:03Z",
        redirects=[],
        warnings=[],
    )
    segment = Segment(
        id="seg-000001",
        locator=block.id,
        semantic_type="paragraph",
        heading_path=[],
        source_text=block.source_text,
        protected=[],
        context_ids=[],
        target=True,
    )
    _write_json(run_dir / "document.json", document.to_dict())
    _write_json(run_dir / "source.json", source.to_dict())
    write_segments(run_dir / "segments.jsonl", [segment])
    _write_json(run_dir / "glossary.json", {})
    zones = run_dir / "zones"
    zones.mkdir()
    _write_json(
        zones / "zone-001.json",
        {
            "attempt": 0,
            "context_after_ids": [],
            "context_before_ids": [],
            "expected_tokens": {"seg-000001": []},
            "heading_path": [],
            "id": "zone-001",
            "target_ids": ["seg-000001"],
        },
    )
    translations = run_dir / "translations"
    translations.mkdir()
    (translations / "zone-001.jsonl").write_text(
        json.dumps(
            Translation("seg-000001", "한국어 본문").to_dict(),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    assignments = run_dir / "assignments"
    assignments.mkdir()
    _write_json(assignments / "zone-001.json", {"zone_id": "zone-001"})
    review = _review_payload()
    review["semantic_input_sha256"] = build_pdf_semantic_review_input(
        run_dir
    ).semantic_input_sha256
    _write_json(run_dir / "review.json", review)


def test_pdf_assemble_cli_requires_review_and_stages_without_publishing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, final_output = _pdf_cli_paths(tmp_path)
    _write_pdf_assembly_cli_run(run_dir)

    exit_code = main(
        ["pdf-assemble", "--run-dir", str(run_dir), "--output-dir", str(final_output)]
    )

    assert exit_code == 0
    assert (run_dir / "staged-output" / "translated.pdf").is_file()
    assert (run_dir / "layout.json").is_file()
    assert not final_output.exists()
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-assemble",
        "exit_code": 0,
        "status": "ok",
    }


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX rename proves a held-review/path swap without invalid Windows injection",
)
def test_pdf_assemble_cli_rejects_segments_swap_restore_before_publication(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, final_output = _pdf_cli_paths(tmp_path)
    _write_pdf_assembly_cli_run(run_dir)
    segments_path = run_dir / "segments.jsonl"
    held_path = run_dir / "held-reviewed-segments.jsonl"
    original = segments_path.read_bytes()
    raced_record = json.loads(original.decode("utf-8"))
    raced_record["source_text"] = "Raced source body consumed by assembly"
    raced = (json.dumps(raced_record, ensure_ascii=False) + "\n").encode("utf-8")
    real_assemble = cli_module.assemble_pdf
    real_normalize = pdf_assemble_module._normalize_pdf_translations
    consumed_source_texts: list[str] = []

    def capture_normalization(
        document: object,
        segments: object,
        translations: object,
        glossary: object,
    ) -> object:
        consumed_source_texts.extend(
            segment.source_text for segment in segments  # type: ignore[union-attr]
        )
        return real_normalize(
            document, segments, translations, glossary  # type: ignore[arg-type]
        )

    def swap_restore_around_assembly(
        run: Path,
        translations: object,
        glossary: object,
        output: Path,
        *,
        semantic_snapshot: object | None = None,
    ) -> Path:
        segments_path.rename(held_path)
        segments_path.write_bytes(raced)
        try:
            if semantic_snapshot is None:
                return real_assemble(
                    run,
                    translations,  # type: ignore[arg-type]
                    glossary,  # type: ignore[arg-type]
                    output,
                )
            return real_assemble(
                run,
                translations,  # type: ignore[arg-type]
                glossary,  # type: ignore[arg-type]
                output,
                semantic_snapshot=semantic_snapshot,
            )
        finally:
            segments_path.unlink(missing_ok=True)
            held_path.rename(segments_path)

    monkeypatch.setattr(
        pdf_assemble_module,
        "_normalize_pdf_translations",
        capture_normalization,
    )
    monkeypatch.setattr(cli_module, "assemble_pdf", swap_restore_around_assembly)

    exit_code = main(
        ["pdf-assemble", "--run-dir", str(run_dir), "--output-dir", str(final_output)]
    )

    assert exit_code == cli_module.EXIT_ASSEMBLY_FAILURE, (
        f"assembly consumed swapped segments: {consumed_source_texts}"
    )
    assert segments_path.read_bytes() == original
    assert not (run_dir / "staged-output").exists()
    assert not (run_dir / "layout.json").exists()
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-assemble",
        "exit_code": cli_module.EXIT_ASSEMBLY_FAILURE,
        "status": "error",
    }


def test_pdf_assemble_cli_rejects_missing_semantic_review_before_staging(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, final_output = _pdf_cli_paths(tmp_path)
    _write_pdf_assembly_cli_run(run_dir)
    (run_dir / "review.json").unlink()

    exit_code = main(
        ["pdf-assemble", "--run-dir", str(run_dir), "--output-dir", str(final_output)]
    )

    assert exit_code == cli_module.EXIT_CONTRACT_FAILURE
    assert not (run_dir / "staged-output").exists()
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-assemble",
        "exit_code": cli_module.EXIT_CONTRACT_FAILURE,
        "status": "error",
    }


def test_pdf_assemble_cli_rejects_unresolved_semantic_review_as_assembly_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, final_output = _pdf_cli_paths(tmp_path)
    _write_pdf_assembly_cli_run(run_dir)
    review = json.loads((run_dir / "review.json").read_text(encoding="utf-8"))
    review["section_findings"]["zone-001"][0]["verdict"] = "required-fix"
    review["unresolved_required"] = ["zone-001:semantic_fidelity"]
    _write_json(run_dir / "review.json", review)

    exit_code = main(
        ["pdf-assemble", "--run-dir", str(run_dir), "--output-dir", str(final_output)]
    )

    assert exit_code == cli_module.EXIT_ASSEMBLY_FAILURE
    assert not (run_dir / "staged-output").exists()
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-assemble",
        "exit_code": cli_module.EXIT_ASSEMBLY_FAILURE,
        "status": "error",
    }


def test_pdf_assemble_cli_maps_pdf_assembly_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, final_output = _pdf_cli_paths(tmp_path)
    _write_pdf_assembly_cli_run(run_dir)

    def fail_assembly(*args: object, **kwargs: object) -> Path:
        from web_translator.pdf_assemble import PdfAssemblyError

        raise PdfAssemblyError("font embedding failed")

    monkeypatch.setattr(cli_module, "assemble_pdf", fail_assembly)

    exit_code = main(
        ["pdf-assemble", "--run-dir", str(run_dir), "--output-dir", str(final_output)]
    )

    assert exit_code == cli_module.EXIT_ASSEMBLY_FAILURE
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-assemble",
        "exit_code": cli_module.EXIT_ASSEMBLY_FAILURE,
        "status": "error",
    }


def test_pdf_qa_cli_requires_both_directories_and_offers_prepare_and_finalize(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["pdf-qa", "prepare"]) == cli_module.EXIT_INVALID_ARGUMENTS
    assert main(["pdf-qa", "finalize"]) == cli_module.EXIT_INVALID_ARGUMENTS
    capsys.readouterr()
    assert main(["pdf-qa", "--help"]) == 0

    output = capsys.readouterr()
    assert "prepare" in output.err
    assert "finalize" in output.err


def test_pdf_qa_prepare_cli_maps_pdf_qa_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, output_dir = _pdf_cli_paths(tmp_path)

    def fail_prepare(*args: object, **kwargs: object) -> object:
        from web_translator.pdf_qa import PdfQAFailure

        raise PdfQAFailure("render failed")

    monkeypatch.setattr(cli_module, "prepare_pdf_qa", fail_prepare)
    exit_code = main(
        [
            "pdf-qa",
            "prepare",
            "--run-dir",
            str(run_dir),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == cli_module.EXIT_QA_FAILURE
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-qa",
        "exit_code": cli_module.EXIT_QA_FAILURE,
        "status": "error",
    }


def test_pdf_qa_finalize_cli_publishes_before_emitting_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, output_dir = _pdf_cli_paths(tmp_path)

    def finalize(run: Path, output: Path) -> Path:
        assert run == run_dir
        output.mkdir()
        return output

    monkeypatch.setattr(cli_module, "finalize_pdf_output", finalize)

    exit_code = main(
        [
            "pdf-qa",
            "finalize",
            "--run-dir",
            str(run_dir),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 0
    assert output_dir.is_dir()
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-qa",
        "exit_code": 0,
        "status": "ok",
    }


def test_pdf_qa_finalize_cli_maps_pdf_qa_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, output_dir = _pdf_cli_paths(tmp_path)

    def fail_finalize(*args: object, **kwargs: object) -> object:
        raise PdfQAFailure("visual review failed")

    monkeypatch.setattr(cli_module, "finalize_pdf_output", fail_finalize)
    exit_code = main(
        [
            "pdf-qa",
            "finalize",
            "--run-dir",
            str(run_dir),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == cli_module.EXIT_QA_FAILURE
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-qa",
        "exit_code": cli_module.EXIT_QA_FAILURE,
        "status": "error",
    }


def test_pdf_extract_cli_publishes_document_segments_and_media_atomically(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = make_text_pdf(tmp_path / "input.pdf")
    run_dir, _output_dir = _pdf_cli_paths(tmp_path)
    assert main(["pdf-acquire", str(source), "--run-dir", str(run_dir)]) == 0
    capsys.readouterr()

    exit_code = main(["pdf-extract", "--run-dir", str(run_dir)])

    assert exit_code == 0
    assert json.loads((run_dir / "document.json").read_text(encoding="utf-8"))[
        "page_count"
    ] == 1
    assert (run_dir / "segments.jsonl").is_file()
    assert (run_dir / "media").is_dir()
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-extract",
        "exit_code": 0,
        "status": "ok",
    }


def test_pdf_extract_cli_maps_extraction_rejection_to_contract_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = make_image_only_pdf(tmp_path / "image-only.pdf")
    run_dir, _output_dir = _pdf_cli_paths(tmp_path)
    assert main(["pdf-acquire", str(source), "--run-dir", str(run_dir)]) == 0
    capsys.readouterr()

    exit_code = main(["pdf-extract", "--run-dir", str(run_dir)])

    assert exit_code == cli_module.EXIT_CONTRACT_FAILURE
    assert not (run_dir / "document.json").exists()
    assert not (run_dir / "segments.jsonl").exists()
    assert not (run_dir / "media").exists()
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-extract",
        "exit_code": cli_module.EXIT_CONTRACT_FAILURE,
        "status": "error",
    }


def test_pdf_extract_cli_keeps_existing_outputs_when_staging_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_text_pdf(tmp_path / "input.pdf")
    run_dir, _output_dir = _pdf_cli_paths(tmp_path)
    assert main(["pdf-acquire", str(source), "--run-dir", str(run_dir)]) == 0
    capsys.readouterr()
    (run_dir / "document.json").write_text("old document", encoding="utf-8")
    (run_dir / "segments.jsonl").write_text("old segments", encoding="utf-8")
    (run_dir / "media").mkdir()
    (run_dir / "media" / "keep.txt").write_text("old media", encoding="utf-8")

    def fail_extraction(*args: object, **kwargs: object) -> object:
        from web_translator.pdf_extract import PdfExtractionError

        raise PdfExtractionError("ambiguous column evidence")

    monkeypatch.setattr(cli_module, "extract_pdf", fail_extraction)

    exit_code = main(["pdf-extract", "--run-dir", str(run_dir)])

    assert exit_code == cli_module.EXIT_CONTRACT_FAILURE
    assert (run_dir / "document.json").read_text(encoding="utf-8") == "old document"
    assert (run_dir / "segments.jsonl").read_text(encoding="utf-8") == "old segments"
    assert (run_dir / "media" / "keep.txt").read_text(encoding="utf-8") == "old media"
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-extract",
        "exit_code": cli_module.EXIT_CONTRACT_FAILURE,
        "status": "error",
    }


def test_pdf_extract_cli_rolls_back_all_outputs_when_publication_fails_midway(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_text_pdf(tmp_path / "input.pdf")
    run_dir, _output_dir = _pdf_cli_paths(tmp_path)
    assert main(["pdf-acquire", str(source), "--run-dir", str(run_dir)]) == 0
    capsys.readouterr()
    (run_dir / "document.json").write_text("old document", encoding="utf-8")
    (run_dir / "segments.jsonl").write_text("old segments", encoding="utf-8")
    (run_dir / "media").mkdir()
    (run_dir / "media" / "keep.txt").write_text("old media", encoding="utf-8")
    original_replace = cli_module.os.replace

    def fail_staged_segments(source_path: object, destination: object) -> None:
        source_candidate = Path(source_path)  # type: ignore[arg-type]
        destination_candidate = Path(destination)  # type: ignore[arg-type]
        if (
            source_candidate.name == "segments.jsonl"
            and source_candidate.parent.name.startswith(".pdf-extracting-")
            and destination_candidate == run_dir / "segments.jsonl"
        ):
            raise OSError("segments publication failed")
        original_replace(source_path, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_module.os, "replace", fail_staged_segments)

    exit_code = main(["pdf-extract", "--run-dir", str(run_dir)])

    assert exit_code == cli_module.EXIT_CONTRACT_FAILURE
    assert (run_dir / "document.json").read_text(encoding="utf-8") == "old document"
    assert (run_dir / "segments.jsonl").read_text(encoding="utf-8") == "old segments"
    assert (run_dir / "media" / "keep.txt").read_text(encoding="utf-8") == "old media"
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-extract",
        "exit_code": cli_module.EXIT_CONTRACT_FAILURE,
        "status": "error",
    }


def test_pdf_acquire_cli_requires_an_empty_run_directory_and_writes_source_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    run_dir, _output_dir = _pdf_cli_paths(tmp_path)

    exit_code = main(["pdf-acquire", str(source), "--run-dir", str(run_dir)])

    assert exit_code == 0
    assert (run_dir / "source.pdf").read_bytes() == source.read_bytes()
    source_json = json.loads((run_dir / "source.json").read_text(encoding="utf-8"))
    assert source_json["input_kind"] == "local"
    assert source_json["requested_source"] == "source.pdf"
    assert source_json["warnings"] == []
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-acquire", "exit_code": 0, "status": "ok"
    }


def test_pdf_acquire_cli_rejects_nonempty_directory_without_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    run_dir, _output_dir = _pdf_cli_paths(tmp_path)
    existing = run_dir / "keep.txt"
    existing.write_text("keep", encoding="utf-8")

    exit_code = main(["pdf-acquire", str(source), "--run-dir", str(run_dir)])

    assert exit_code == cli_module.EXIT_CAPTURE_FAILURE
    assert existing.read_text(encoding="utf-8") == "keep"
    assert not (run_dir / "source.pdf").exists()
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-acquire",
        "exit_code": cli_module.EXIT_CAPTURE_FAILURE,
        "status": "error",
    }


def test_pdf_acquire_cli_rolls_back_source_when_metadata_publication_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    run_dir, _output_dir = _pdf_cli_paths(tmp_path)

    def fail_metadata(path: Path, value: object) -> None:
        raise OSError("metadata disk failure")

    monkeypatch.setattr(cli_module, "_write_json_atomic", fail_metadata)

    exit_code = main(["pdf-acquire", str(source), "--run-dir", str(run_dir)])

    assert exit_code == cli_module.EXIT_CAPTURE_FAILURE
    assert not (run_dir / "source.pdf").exists()
    assert not (run_dir / "source.json").exists()
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-acquire",
        "exit_code": cli_module.EXIT_CAPTURE_FAILURE,
        "status": "error",
    }


def test_pdf_acquire_cli_rolls_back_source_when_metadata_destination_races(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    run_dir, _output_dir = _pdf_cli_paths(tmp_path)
    import web_translator.pdf_acquire as acquire_module

    original_link = acquire_module.os.link

    def race_metadata_destination(
        source: Path, destination: str, **kwargs: object
    ) -> None:
        if destination == "source.json":
            (run_dir / destination).write_text("racer", encoding="utf-8")
        original_link(source, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(acquire_module.os, "link", race_metadata_destination)

    exit_code = main(["pdf-acquire", str(source), "--run-dir", str(run_dir)])

    assert exit_code == cli_module.EXIT_CAPTURE_FAILURE
    assert not (run_dir / "source.pdf").exists()
    assert (run_dir / "source.json").read_text(encoding="utf-8") == "racer"
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-acquire",
        "exit_code": cli_module.EXIT_CAPTURE_FAILURE,
        "status": "error",
    }


def test_pdf_acquire_cli_maps_windows_fallback_link_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    run_dir, _output_dir = _pdf_cli_paths(tmp_path)
    import web_translator.pdf_acquire as acquire_module

    monkeypatch.setattr(
        acquire_module, "_supports_descriptor_relative_operations", lambda: False
    )
    original_link = acquire_module.os.link

    def fail_final_source_link(
        source_path: str | Path, destination: str | Path, **kwargs: object
    ) -> None:
        if Path(destination) == run_dir / "source.pdf":
            raise NotImplementedError()
        original_link(source_path, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        acquire_module.os,
        "link",
        fail_final_source_link,
    )

    exit_code = main(["pdf-acquire", str(source), "--run-dir", str(run_dir)])

    assert exit_code == cli_module.EXIT_CAPTURE_FAILURE
    assert json.loads(capsys.readouterr().out) == {
        "command": "pdf-acquire",
        "exit_code": cli_module.EXIT_CAPTURE_FAILURE,
        "status": "error",
    }


def _zone(zone_id: str = "zone-001") -> Zone:
    return Zone(
        id=zone_id,
        heading_path=["Concepts"],
        target_ids=[f"{zone_id}-segment"],
        context_before_ids=[],
        context_after_ids=[],
        expected_tokens={f"{zone_id}-segment": ()},
    )


def _review_finding(
    dimension: str,
    *,
    verdict: str = "pass",
    evidence: str | None = None,
) -> dict[str, str]:
    return {
        "dimension": dimension,
        "verdict": verdict,
        "evidence": evidence or f"Reviewer checked {dimension} against the source.",
    }


def _review_payload(*, zone_ids: tuple[str, ...] = ("zone-001",)) -> dict[str, object]:
    return {
        "unresolved_required": [],
        "retries": {zone_id: 0 for zone_id in zone_ids},
        "section_findings": {
            zone_id: [_review_finding(dimension) for dimension in REVIEW_DIMENSIONS]
            for zone_id in zone_ids
        },
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def test_review_accepts_exact_typed_six_dimension_evidence(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    _write_json(path, _review_payload())

    review = _read_review(path, [_zone()])

    assert review.retries == {"zone-001": 0}
    assert review.unresolved_required == []
    assert len(review.semantic_findings["zone-001"]) == 6
    assert {
        finding.dimension for finding in review.semantic_findings["zone-001"]
    } == set(REVIEW_DIMENSIONS)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["section_findings"].pop("zone-002"),
            "section findings must exactly cover planned zones",
        ),
        (
            lambda payload: payload["section_findings"]["zone-001"].pop(),
            "must contain each canonical dimension exactly once",
        ),
        (
            lambda payload: payload["section_findings"]["zone-001"].append(
                dict(payload["section_findings"]["zone-001"][0])
            ),
            "must contain each canonical dimension exactly once",
        ),
        (
            lambda payload: payload["section_findings"]["zone-001"][0].update(
                dimension="invented_dimension"
            ),
            "unknown review dimension",
        ),
        (
            lambda payload: payload["section_findings"]["zone-001"][0].update(
                evidence="  "
            ),
            "evidence must be a non-empty string",
        ),
        (
            lambda payload: payload["section_findings"]["zone-001"][0].update(
                verdict="looks-good"
            ),
            "verdict must be 'pass' or 'required-fix'",
        ),
        (
            lambda payload: payload["section_findings"]["zone-001"][0].update(
                extra="invented"
            ),
            "finding fields must be exactly",
        ),
    ],
)
def test_review_rejects_incomplete_duplicate_or_invented_evidence(
    tmp_path: Path,
    mutate: object,
    message: str,
) -> None:
    path = tmp_path / "review.json"
    payload = _review_payload(zone_ids=("zone-001", "zone-002"))
    mutate(payload)  # type: ignore[operator]
    _write_json(path, payload)

    with pytest.raises(CLIContractError, match=message):
        _read_review(path, [_zone("zone-001"), _zone("zone-002")])


@pytest.mark.parametrize(
    ("unresolved", "verdict", "message"),
    [
        ([], "required-fix", "must exactly match required-fix findings"),
        (["zone-001:semantic_fidelity"], "pass", "must exactly match required-fix findings"),
        (
            ["zone-001:semantic_fidelity", "zone-001:semantic_fidelity"],
            "required-fix",
            "sorted and unique",
        ),
    ],
)
def test_review_rejects_unresolved_list_that_disagrees_with_required_fixes(
    tmp_path: Path,
    unresolved: list[str],
    verdict: str,
    message: str,
) -> None:
    path = tmp_path / "review.json"
    payload = _review_payload()
    payload["unresolved_required"] = unresolved
    payload["section_findings"]["zone-001"][0]["verdict"] = verdict
    _write_json(path, payload)

    with pytest.raises(CLIContractError, match=message):
        _read_review(path, [_zone()])


def test_review_rejects_unknown_top_level_fields(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    payload = _review_payload()
    payload["invented"] = True
    _write_json(path, payload)

    with pytest.raises(CLIContractError, match="review fields must be exactly"):
        _read_review(path, [_zone()])


def _capture_contract(
    run_dir: Path,
    *,
    asset_url: str,
    asset_bytes: bytes = b"body{}",
) -> Path:
    run_dir.mkdir()
    source = run_dir / "source.html"
    source.write_bytes(b"<html lang='en'></html>")
    assets = run_dir / "assets"
    assets.mkdir()
    asset = assets / "inline.css"
    asset.write_bytes(asset_bytes)
    relative_asset = "assets/inline.css"
    payload = {
        "asset_map": {asset_url: relative_asset},
        "captured_at": "2026-08-12T01:02:03Z",
        "critical_assets": [relative_asset],
        "final_url": "https://example.com/docs/",
        "fingerprints": {
            "source.html": hashlib.sha256(source.read_bytes()).hexdigest(),
            relative_asset: hashlib.sha256(asset_bytes).hexdigest(),
        },
        "missing_optional_assets": [],
        "optional_assets": [],
        "requested_url": "https://example.com/docs/",
    }
    _write_json(run_dir / "capture.json", payload)
    return run_dir


@pytest.mark.parametrize(
    ("asset_url", "asset_bytes"),
    [
        ("data:text/css,body%7Bcolor%3Ared%7D", b"body{color:red}"),
        (
            "data:text/css;charset=utf-8;base64,"
            + base64.b64encode(b"body{color:red}").decode("ascii"),
            b"body{color:red}",
        ),
        ("data:text/css;base64,YQ%3D%3D", b"a"),
    ],
)
def test_capture_accepts_strict_size_bounded_data_css_provenance(
    tmp_path: Path, asset_url: str, asset_bytes: bytes
) -> None:
    run_dir = _capture_contract(tmp_path / "run", asset_url=asset_url, asset_bytes=asset_bytes)

    capture = _read_capture(run_dir)

    assert capture["captured_at"] == "2026-08-12T01:02:03Z"
    assert capture["asset_map"] == {asset_url: "assets/inline.css"}


@pytest.mark.parametrize(
    "asset_url",
    [
        "data:text/html,%3Cscript%3Ealert(1)%3C/script%3E",
        "data:text/css;charset=iso-8859-1,body%7B%7D",
        "data:text/css;name=theme.css,body%7B%7D",
        "data:text/css;base64,not-valid-base64!",
        "data:text/css,body%ZZ",
        "data:text/css,",
        "data:text/css,%FF",
        "blob:https://example.com/identifier",
        "#theme",
    ],
)
def test_capture_rejects_non_css_or_ambiguous_inline_asset_provenance(
    tmp_path: Path, asset_url: str
) -> None:
    run_dir = _capture_contract(tmp_path / "run", asset_url=asset_url)

    with pytest.raises(CLIContractError, match="capture asset URL"):
        _read_capture(run_dir)


def test_capture_rejects_data_css_over_capture_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "MAX_DATA_CSS_BYTES", 4, raising=False)
    asset_url = "data:text/css;base64," + base64.b64encode(b"12345").decode("ascii")
    run_dir = _capture_contract(tmp_path / "run", asset_url=asset_url, asset_bytes=b"12345")

    with pytest.raises(CLIContractError, match="capture asset URL.*size limit"):
        _read_capture(run_dir)


def test_capture_accepts_original_data_css_provenance_for_rewritten_local_asset(
    tmp_path: Path,
) -> None:
    run_dir = _capture_contract(
        tmp_path / "run",
        asset_url="data:text/css,body%7Bcolor%3Ared%7D",
        asset_bytes=b"body{color:blue}",
    )

    capture = _read_capture(run_dir)

    assert capture["critical_assets"] == ["assets/inline.css"]
    assert capture["fingerprints"]["assets/inline.css"] == hashlib.sha256(
        b"body{color:blue}"
    ).hexdigest()


def test_capture_rejects_data_css_when_encoded_provenance_exceeds_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "MAX_DATA_CSS_BYTES", 32, raising=False)
    payload = b"a" * 24
    asset_url = "data:text/css;base64," + base64.b64encode(payload).decode("ascii")
    run_dir = _capture_contract(tmp_path / "run", asset_url=asset_url, asset_bytes=payload)

    with pytest.raises(CLIContractError, match="capture asset URL.*size limit"):
        _read_capture(run_dir)


def _manifest_inputs() -> dict[str, object]:
    segment = Segment(
        id="seg-000001",
        locator="[data-wt-segment='seg-000001']",
        semantic_type="paragraph",
        heading_path=[],
        source_text=(
            "This English documentation explains a stable technical interface and its "
            "security requirements for application developers."
        ),
        protected=[],
        context_ids=[],
        target=True,
    )
    zone = _zone()
    zone = Zone(
        id=zone.id,
        heading_path=zone.heading_path,
        target_ids=[segment.id],
        context_before_ids=[],
        context_after_ids=[],
        expected_tokens={segment.id: ()},
    )
    capture: dict[str, object] = {
        "asset_map": {"https://example.com/theme.css": "assets/theme.css"},
        "captured_at": "2026-08-12T01:02:03Z",
        "critical_assets": ["assets/theme.css"],
        "final_url": "https://example.com/docs/",
        "fingerprints": {"assets/theme.css": "a" * 64, "source.html": "b" * 64},
        "missing_optional_assets": [],
        "optional_assets": [],
        "requested_url": "https://example.com/docs/",
    }
    return {
        "result": QAResult(
            passed=True,
            required_findings=[],
            warnings=[],
            screenshots=[],
            source_url="https://example.com/docs/",
        ),
        "capture": capture,
        "segments": [segment],
        "zones": [zone],
        "translated_segment_ids": {segment.id},
        "review": MasterReview(
            unresolved_required=[],
            retries={zone.id: 0},
            section_findings={zone.id: []},
        ),
    }


def test_manifest_provenance_is_typed_exact_and_cross_validated() -> None:
    provenance = _build_manifest_provenance(**_manifest_inputs())  # type: ignore[arg-type]

    payload = provenance.to_dict()
    assert set(payload) == {
        "assets",
        "capture",
        "coverage",
        "languages",
        "retries",
        "schema_version",
        "terminology_policy",
        "tool",
    }
    assert payload["languages"] == {"source": "en", "target": "ko"}
    assert payload["coverage"] == {
        "segments": 1,
        "target_segments": 1,
        "translated_segments": 1,
        "zones": 1,
    }
    assert payload["assets"]["captured"] == [
        {
            "classification": "critical",
            "local_path": "assets/theme.css",
            "sha256": "a" * 64,
            "source": "https://example.com/theme.css",
        }
    ]


def test_source_language_detection_is_local_seeded_and_deterministic() -> None:
    segments = _manifest_inputs()["segments"]
    assert _detect_source_language(segments) == "en"  # type: ignore[arg-type]
    assert _detect_source_language(segments) == "en"  # type: ignore[arg-type]
    assert _detect_source_language([]) == "und"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda values: values["capture"].update(captured_at="2026-08-12T01:02:03+09:00"),
            "timestamp must be an ISO-8601 UTC value ending in Z",
        ),
        (
            lambda values: values["capture"].update(final_url="https://example.com/other"),
            "final URL must match QA source URL",
        ),
        (
            lambda values: values.update(translated_segment_ids=set()),
            "translation coverage must exactly cover target segments",
        ),
        (
            lambda values: values.update(zones=[]),
            "zone coverage must exactly cover target segments once",
        ),
        (
            lambda values: values["review"].retries.clear(),
            "retries must exactly cover zones",
        ),
        (
            lambda values: values["capture"]["fingerprints"].update(
                {"assets/theme.css": "invented"}
            ),
            "asset fingerprint is missing or invalid",
        ),
    ],
)
def test_manifest_provenance_rejects_artifact_disagreement(
    mutate: object, message: str
) -> None:
    values = _manifest_inputs()
    mutate(values)  # type: ignore[operator]

    with pytest.raises(CLIContractError, match=message):
        _build_manifest_provenance(**values)  # type: ignore[arg-type]
