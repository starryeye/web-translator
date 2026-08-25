from __future__ import annotations

import json

import pytest

import web_translator.pdf_qa as pdf_qa_module
from web_translator.pdf_qa import PdfQAFailure, finalize_pdf_output, prepare_pdf_qa
from web_translator.pdf_report import build_pdf_manifest
from tests.test_pdf_qa import PdfQARun, _write_passing_layout_review, assembled_pdf_run


@pytest.fixture
def prepared_pdf_run(assembled_pdf_run: PdfQARun) -> PdfQARun:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    _write_passing_layout_review(assembled_pdf_run.run_dir)
    return assembled_pdf_run


# Production mutation caught: leaving a partial public directory when final rename fails.
def test_finalize_rolls_back_publication_when_rename_fails(
    prepared_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_replace(source: object, destination: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(pdf_qa_module.os, "replace", fail_replace)

    with pytest.raises(PdfQAFailure, match="publish final PDF output"):
        finalize_pdf_output(prepared_pdf_run.run_dir, prepared_pdf_run.output_dir)

    assert not prepared_pdf_run.output_dir.exists()
    assert (prepared_pdf_run.run_dir / "staged-output" / "translated.pdf").is_file()
    assert not (prepared_pdf_run.run_dir / "staged-output" / "manifest.json").exists()


# Production mutation caught: nondeterministic PDF manifest serialization from unchanged evidence.
def test_pdf_manifest_is_deterministic(prepared_pdf_run: PdfQARun) -> None:
    first = build_pdf_manifest(prepared_pdf_run.run_dir)
    second = build_pdf_manifest(prepared_pdf_run.run_dir)

    assert json.dumps(first, ensure_ascii=False, indent=2, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, indent=2, sort_keys=True
    )


# Production mutation caught: exposing any artifact set other than the reviewed three-file final output.
def test_finalize_atomically_publishes_exact_reviewed_output(prepared_pdf_run: PdfQARun) -> None:
    finalized = finalize_pdf_output(prepared_pdf_run.run_dir, prepared_pdf_run.output_dir)

    assert finalized == prepared_pdf_run.output_dir
    assert sorted(path.name for path in finalized.iterdir()) == [
        "manifest.json",
        "review-report.md",
        "translated.pdf",
    ]
    manifest = json.loads((finalized / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == {
        "automated_qa",
        "block_counts",
        "inspection",
        "languages",
        "output",
        "qa_status",
        "schema_version",
        "source",
        "terminology",
        "tool_version",
        "translation",
        "visual_review",
        "warnings",
    }
    assert manifest["qa_status"] == "passed"
    assert manifest["output"]["sha256"] == manifest["automated_qa"]["staged_pdf_sha256"]
    assert manifest["translation"]["retries"] == {"zone-001": 0}
    assert manifest["visual_review"]["unresolved_required"] == []
    report = (finalized / "review-report.md").read_text(encoding="utf-8")
    assert "Status: **PASS**" in report
    assert "Checked semantic_fidelity against the source." in report
    assert "Reviewed glyph_rendering." in report
