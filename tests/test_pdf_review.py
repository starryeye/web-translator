from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from web_translator.cli import (
    CLIContractError,
    EXIT_CONTRACT_FAILURE,
    _read_pdf_review,
    main,
)
from web_translator.pdf_review import (
    PdfSemanticReviewError,
    build_pdf_semantic_review_input,
    validate_pdf_semantic_review,
)
from web_translator.zones import Zone
from tests.test_pdf_unit_bindings import bound_unit_run


def test_native_review_includes_document_and_binding(bound_unit_run):
    snapshot = build_pdf_semantic_review_input(bound_unit_run)
    names = {item.path for item in snapshot.files}
    assert "document.json" in names
    assert "assignments/.pdf-unit-binding.json" in names


def test_native_review_rejects_stale_binding(bound_unit_run):
    path = bound_unit_run / "document.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(PdfSemanticReviewError, match="binding"):
        build_pdf_semantic_review_input(bound_unit_run)


def test_unit_semantic_input_rejects_duplicate_file_evidence(bound_unit_run):
    from web_translator.pdf_review import PdfSemanticReviewInput
    value = build_pdf_semantic_review_input(bound_unit_run).to_dict()
    value["files"].insert(0, value["files"][0])
    with pytest.raises(PdfSemanticReviewError, match="unique"):
        PdfSemanticReviewInput.from_dict(value)


def test_native_review_rejects_foreign_assignment_file(bound_unit_run):
    (bound_unit_run / "assignments" / ".unknown.json").write_text("{}")
    with pytest.raises(PdfSemanticReviewError):
        build_pdf_semantic_review_input(bound_unit_run)


def test_rebinding_changed_unit_evidence_does_not_reuse_old_review(bound_unit_run):
    from tests.test_pdf_unit_bindings import BINDING, manual_binding, write_json
    run = bound_unit_run
    old = build_pdf_semantic_review_input(run)
    path = run / "document.json"
    value = json.loads(path.read_bytes())
    value["translation_units"][0]["joins"][0]["evidence"]["left"]["text_indent"] = 2.0
    write_json(path, value)
    write_json(run / "assignments" / BINDING, manual_binding(run))
    review = {"semantic_input_sha256": old.semantic_input_sha256, "retries": {}, "section_findings": {}, "unresolved_required": []}
    with pytest.raises(PdfSemanticReviewError, match="digest does not match"):
        validate_pdf_semantic_review(run, review)


def test_native_semantic_consumption_retains_document_identity(bound_unit_run):
    from web_translator.pdf_review import hold_pdf_semantic_inputs
    path = bound_unit_run / "document.json"
    with pytest.raises(PdfSemanticReviewError, match="changed identity"):
        with hold_pdf_semantic_inputs(bound_unit_run) as snapshot:
            payload = path.read_bytes()
            path.rename(path.with_name("old-document.json"))
            path.write_bytes(payload)
            snapshot.verify()


_DIMENSIONS = (
    "semantic_fidelity",
    "qualification_preservation",
    "naturalness",
    "terminology",
    "boundary_consistency",
    "protected_content",
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _reviewed_run(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    run_dir = tmp_path / ".web-translator" / "runs" / "run"
    (tmp_path / "translated-pdfs").mkdir()
    _write(run_dir / "segments.jsonl", '{"id":"seg-000001"}\n')
    _write(run_dir / "glossary.json", '{"OAuth":"권한 위임"}\n')
    _write(run_dir / "zones" / "zone-001.json", '{"id":"zone-001"}\n')
    _write(
        run_dir / "assignments" / "zone-001.json",
        '{"zone_id":"zone-001"}\n',
    )
    _write(
        run_dir / "translations" / "zone-001.jsonl",
        '{"segment_id":"seg-000001","text":"번역"}\n',
    )
    semantic_input = build_pdf_semantic_review_input(run_dir)
    review: dict[str, object] = {
        "semantic_input_sha256": semantic_input.semantic_input_sha256,
        "retries": {"zone-001": 0},
        "section_findings": {"zone-001": []},
        "unresolved_required": [],
    }
    return run_dir, review


def test_pdf_semantic_review_input_is_typed_canonical_and_deterministic(
    tmp_path: Path,
) -> None:
    run_dir, review = _reviewed_run(tmp_path)

    first = build_pdf_semantic_review_input(run_dir)
    second = build_pdf_semantic_review_input(run_dir)

    assert first == second
    assert first.semantic_input_sha256 == review["semantic_input_sha256"]
    assert first.to_dict() == {
        "schema_version": "1.0",
        "semantic_input_sha256": first.semantic_input_sha256,
        "terminology_policy": {
            "policy_id": "korean-first-technical-terms",
            "policy_version": "2.0",
        },
        "files": [record.to_dict() for record in first.files],
    }
    assert [record.path for record in first.files] == [
        "assignments/zone-001.json",
        "glossary.json",
        "segments.jsonl",
        "translations/zone-001.jsonl",
        "zones/zone-001.json",
    ]
    validate_pdf_semantic_review(run_dir, review)


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
def test_pdf_semantic_review_rejects_every_reviewed_input_mutation(
    tmp_path: Path,
    relative_path: str,
) -> None:
    run_dir, review = _reviewed_run(tmp_path)
    path = run_dir / relative_path
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(PdfSemanticReviewError, match="digest does not match"):
        validate_pdf_semantic_review(run_dir, review)


def test_pdf_semantic_review_rejects_foreign_or_missing_zone_evidence(
    tmp_path: Path,
) -> None:
    run_dir, _review = _reviewed_run(tmp_path)
    _write(run_dir / "assignments" / "zone-002.json", json.dumps({"zone_id": "zone-002"}))

    with pytest.raises(PdfSemanticReviewError, match="exactly cover the same zones"):
        build_pdf_semantic_review_input(run_dir)


def test_pdf_review_input_command_writes_strict_digest_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _review = _reviewed_run(tmp_path)

    assert main(["pdf-review-input", "--run-dir", str(run_dir)]) == 0

    artifact = json.loads(
        (run_dir / "semantic-review-input.json").read_text(encoding="utf-8")
    )
    assert artifact == build_pdf_semantic_review_input(run_dir).to_dict()
    assert capsys.readouterr().out == (
        '{"command": "pdf-review-input", "exit_code": 0, "status": "ok"}\n'
    )
    assert main(["pdf-review-input", "--run-dir", str(run_dir)]) == (
        EXIT_CONTRACT_FAILURE
    )


def test_pdf_assembly_review_reader_binds_digest_without_changing_web_reader(
    tmp_path: Path,
) -> None:
    run_dir, review = _reviewed_run(tmp_path)
    review["section_findings"] = {
        "zone-001": [
            {
                "dimension": dimension,
                "verdict": "pass",
                "evidence": f"Reviewed {dimension}.",
            }
            for dimension in _DIMENSIONS
        ]
    }
    review_path = run_dir / "review.json"
    _write(review_path, json.dumps(review, ensure_ascii=False) + "\n")
    zone = Zone(
        id="zone-001",
        heading_path=[],
        target_ids=["seg-000001"],
        context_before_ids=[],
        context_after_ids=[],
        attempt=0,
        expected_tokens={"seg-000001": []},
    )

    parsed = _read_pdf_review(review_path, [zone])
    assert parsed.retries == {"zone-001": 0}

    translation = run_dir / "translations" / "zone-001.jsonl"
    translation.write_bytes(translation.read_bytes() + b" ")
    with pytest.raises(CLIContractError, match="digest does not match"):
        _read_pdf_review(review_path, [zone])


def test_held_semantic_snapshot_reads_and_verifies_the_same_exact_inputs(
    tmp_path: Path,
) -> None:
    from web_translator.pdf_review import hold_pdf_semantic_inputs

    run_dir, review = _reviewed_run(tmp_path)
    translation = run_dir / "translations" / "zone-001.jsonl"
    held_translation = run_dir / "held-translation.jsonl"

    with hold_pdf_semantic_inputs(run_dir) as snapshot:
        assert snapshot.review_input.semantic_input_sha256 == review[
            "semantic_input_sha256"
        ]
        assert snapshot.payloads["translations/zone-001.jsonl"] == (
            '{"segment_id":"seg-000001","text":"번역"}\n'.encode()
        )
        if os.name == "nt":
            pytest.skip("native Windows replacement is covered by pipeline injection")
        translation.rename(held_translation)
        translation.write_bytes(
            b'{"segment_id":"seg-000001","text":"raced"}\n'
        )
        with pytest.raises(PdfSemanticReviewError, match="changed identity"):
            snapshot.verify()
