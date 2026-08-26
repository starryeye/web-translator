from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pypdf import PdfReader

import web_translator.pdf_qa as pdf_qa_module
import web_translator.pdf_report as pdf_report_module
import web_translator.network as network_module
from web_translator.cli import main
from tests import pdf_fixtures
from tests.pdf_fixtures import make_many_pages_pdf, make_oversized_pdf
from web_translator.pdf_qa import PdfQAFailure, finalize_pdf_output, prepare_pdf_qa
from web_translator.pdf_report import build_pdf_manifest
from tests.test_pdf_qa import PdfQARun, _write_passing_layout_review, assembled_pdf_run


PDF_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "pdf"
_KOREAN_SYLLABLE = re.compile(r"[가-힣]")
_SEMANTIC_ORACLE = {
    "technical-document-v1": (
        "Deterministic Systems Review",
        "결정론적 시스템 검토",
    ),
    "table-report-v1": (
        "First half measurements summarize deterministic acceptance outcomes for the release.",
        "상반기 측정값은 릴리스의 결정론적 승인 결과를 요약합니다.",
    ),
    "two-column-footnotes-v1": (
        "Two-Column Evidence Review",
        "두 열 증거 검토",
    ),
    "figures-captions-v1": ("Figures and Captions", "그림과 캡션"),
}


# Production mutation caught: nondeterministic or incomplete committed acceptance inputs.
def test_committed_pdf_acceptance_fixtures_regenerate_byte_for_byte(
    tmp_path: Path,
) -> None:
    generator = getattr(pdf_fixtures, "generate_acceptance_fixtures", None)
    assert callable(generator), "acceptance fixture generator is missing"

    regenerated = tmp_path / "pdf"
    generator(regenerated)

    expected_directories = {
        "figures-captions-v1",
        "rejections-v1",
        "table-report-v1",
        "technical-document-v1",
        "two-column-footnotes-v1",
    }
    assert {path.name for path in regenerated.iterdir() if path.is_dir()} == (
        expected_directories
    )
    committed_files = sorted(
        path.relative_to(PDF_FIXTURE_ROOT)
        for path in PDF_FIXTURE_ROOT.rglob("*")
        if path.is_file()
    )
    regenerated_files = sorted(
        path.relative_to(regenerated)
        for path in regenerated.rglob("*")
        if path.is_file()
    )
    assert regenerated_files == committed_files
    assert all(
        (regenerated / relative).read_bytes()
        == (PDF_FIXTURE_ROOT / relative).read_bytes()
        for relative in committed_files
    )


@pytest.mark.parametrize(
    ("fixture_name", "source_relative"),
    [
        (name, Path("source.pdf")) for name in pdf_fixtures.PDF_ACCEPTANCE_FIXTURES
    ]
    + [
        (
            "technical-document-v1",
            Path("한국어 경로 with spaces") / "기술 문서 원본.pdf",
        )
    ],
)
def test_committed_pdf_acceptance_fixtures_complete_local_reviewed_pipeline(
    tmp_path: Path,
    fixture_name: str,
    source_relative: Path,
) -> None:
    """Every committed accepted source reaches reviewed public PDF output."""
    fixture_dir = PDF_FIXTURE_ROOT / fixture_name
    expected = json.loads((fixture_dir / "expected.json").read_text(encoding="utf-8"))
    run_dir = tmp_path / "작업 공간" / fixture_name / "run"
    output_dir = tmp_path / "작업 공간" / fixture_name / "final"

    assert main(["pdf-acquire", str(fixture_dir / source_relative), "--run-dir", str(run_dir)]) == 0
    assert main(["pdf-extract", "--run-dir", str(run_dir)]) == 0
    document = json.loads((run_dir / "document.json").read_text(encoding="utf-8"))
    assert document["page_count"] == expected["page_count"]
    assert len(
        {block["table_id"] for block in document["blocks"] if block["table_id"]}
    ) == expected["table_count"]
    assert sum(block["kind"] == "figure" for block in document["blocks"]) == expected["figure_count"]
    assert sum(block["kind"] == "footnote" for block in document["blocks"]) == expected["footnote_count"]

    assert main(["plan-zones", "--run-dir", str(run_dir)]) == 0
    shutil.copy2(fixture_dir / "glossary.json", run_dir / "glossary.json")
    shutil.copy2(fixture_dir / "document-summary.txt", run_dir / "document-summary.txt")
    assert main(["prepare-assignments", "--run-dir", str(run_dir)]) == 0
    shutil.copytree(fixture_dir / "translations", run_dir / "translations")
    shutil.copy2(fixture_dir / "review.json", run_dir / "review.json")
    assert main(["validate-translations", "--run-dir", str(run_dir)]) == 0
    assert main(["pdf-assemble", "--run-dir", str(run_dir), "--output-dir", str(output_dir)]) == 0
    assert main(["pdf-qa", "prepare", "--run-dir", str(run_dir), "--output-dir", str(output_dir)]) == 0

    qa = json.loads((run_dir / "pdf-qa.json").read_text(encoding="utf-8"))
    visual_review = json.loads((fixture_dir / "visual-review.json").read_text(encoding="utf-8"))
    assert visual_review["pages_reviewed"] == list(
        range(1, len(qa["rendered_page_hashes"]) + 1)
    )
    assert visual_review["contact_sheets_reviewed"] == qa["contact_sheet_pages"]
    visual_review["staged_pdf_sha256"] = qa["staged_pdf_sha256"]
    (run_dir / "pdf-layout-review.json").write_text(
        json.dumps(visual_review, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert main(["pdf-qa", "finalize", "--run-dir", str(run_dir), "--output-dir", str(output_dir)]) == 0

    assert sorted(path.name for path in output_dir.iterdir()) == expected["final_artifacts"]
    assert not (run_dir / "staged-output").exists()
    translated_text = "\n".join(
        page.extract_text() or "" for page in PdfReader(output_dir / "translated.pdf").pages
    )
    assert expected["expected_korean_phrase"] in translated_text
    assert "workflow(작업 흐름)" not in translated_text
    records = [
        json.loads(line)
        for line in (fixture_dir / "translations" / "zone-001.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len({record["text"] for record in records}) > 1
    source_by_id = {
        json.loads(line)["id"]: json.loads(line)["source_text"]
        for line in (run_dir / "segments.jsonl").read_text(encoding="utf-8").splitlines()
    }
    translation_by_id = {record["segment_id"]: record["text"] for record in records}
    oracle_source, oracle_korean = _SEMANTIC_ORACLE[fixture_name]
    oracle_id = next(
        segment_id
        for segment_id, source_text in source_by_id.items()
        if source_text == oracle_source
    )
    assert translation_by_id[oracle_id] == oracle_korean
    assert all(
        record["text"] != source_by_id[record["segment_id"]]
        for record in records
        if any(character.isalpha() for character in source_by_id[record["segment_id"]])
    )
    alphabetic = {
        segment_id: source_text
        for segment_id, source_text in source_by_id.items()
        if any(character.isalpha() for character in source_text)
    }
    assert all(_KOREAN_SYLLABLE.search(translation_by_id[segment_id]) for segment_id in alphabetic)
    assert all(source_text not in translation_by_id[segment_id] for segment_id, source_text in alphabetic.items())
    by_source = {
        source_text: translation_by_id[segment_id]
        for segment_id, source_text in alphabetic.items()
    }
    assert len(set(by_source.values())) == len(by_source)
    for line in (run_dir / "segments.jsonl").read_text(encoding="utf-8").splitlines():
        segment = json.loads(line)
        if segment["target"]:
            assert all(token["token"] in translation_by_id[segment["id"]] for token in segment["protected"])
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["inspection"]["page_count"] == expected["page_count"]
    assert manifest["output"]["figure_count"] == expected["figure_count"]
    assert manifest["automated_qa"]["contact_sheet_pages"] == qa["contact_sheet_pages"]


@pytest.mark.parametrize("filename", ["image-only-scan.pdf", "encrypted.pdf", "malformed.pdf"])
def test_committed_pdf_rejections_never_publish_final_output(
    tmp_path: Path, filename: str
) -> None:
    fixture_dir = PDF_FIXTURE_ROOT / pdf_fixtures.PDF_REJECTION_FIXTURE
    run_dir = tmp_path / "run"
    output_dir = tmp_path / "final"

    acquire = main(["pdf-acquire", str(fixture_dir / filename), "--run-dir", str(run_dir)])
    if acquire == 0:
        assert main(["pdf-extract", "--run-dir", str(run_dir)]) == 4
    else:
        assert acquire == 3
    assert not output_dir.exists()


def test_http_pdf_acquire_and_extract_uses_real_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (PDF_FIXTURE_ROOT / "technical-document-v1" / "source.pdf").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(source)))
            self.end_headers()
            self.wfile.write(source)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(network_module, "_resolve_public_addresses", lambda host, port: ["127.0.0.1"])
    run_dir = tmp_path / "http-run"
    url = f"http://fixture.example:{server.server_port}/source.pdf"
    try:
        assert main(["pdf-acquire", url, "--run-dir", str(run_dir)]) == 0
        assert main(["pdf-extract", "--run-dir", str(run_dir)]) == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    record = json.loads((run_dir / "source.json").read_text(encoding="utf-8"))
    assert record["input_kind"] == "public"
    assert record["final_source"] == url
    assert record["sha256"] == __import__("hashlib").sha256(source).hexdigest()


@pytest.mark.parametrize(
    ("builder", "stage", "expected_exit"),
    [(make_oversized_pdf, "pdf-acquire", 3), (make_many_pages_pdf, "pdf-extract", 4)],
)
def test_cli_limit_rejections_never_publish_final_output(
    tmp_path: Path, builder: object, stage: str, expected_exit: int
) -> None:
    source = builder(tmp_path / "input.pdf")  # type: ignore[operator]
    run_dir = tmp_path / "run"
    output_dir = tmp_path / "final"
    acquire = main(["pdf-acquire", str(source), "--run-dir", str(run_dir)])
    result = acquire if stage == "pdf-acquire" else main(["pdf-extract", "--run-dir", str(run_dir)])
    assert result == expected_exit
    assert not output_dir.exists()


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
    def fail_publication(*args: object, **kwargs: object) -> None:
        raise PdfQAFailure("cannot publish final PDF output: rename failed")

    monkeypatch.setattr(
        pdf_qa_module,
        "_publish_final_directory_no_clobber",
        fail_publication,
    )

    with pytest.raises(PdfQAFailure, match="publish final PDF output"):
        finalize_pdf_output(prepared_pdf_run.run_dir, prepared_pdf_run.output_dir)

    assert not prepared_pdf_run.output_dir.exists()
    assert (prepared_pdf_run.run_dir / "staged-output" / "translated.pdf").is_file()
    assert not (prepared_pdf_run.run_dir / "staged-output" / "manifest.json").exists()


@pytest.mark.parametrize(
    "verification",
    ["destination-identity", "parent-visibility"],
)
def test_finalize_rolls_back_successful_move_when_postpublication_check_fails(
    prepared_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
    verification: str,
) -> None:
    staging = prepared_pdf_run.run_dir / "staged-output"
    staged_metadata = staging.stat()
    staged_identity = (staged_metadata.st_dev, staged_metadata.st_ino)

    if verification == "destination-identity":
        real_require_identity = pdf_qa_module._require_anchored_directory_identity

        def fail_published_identity(
            parent: object,
            name: str,
            expected: tuple[int, int],
            context: str,
        ) -> None:
            if context == "published final PDF output":
                raise PdfQAFailure("injected post-publication identity failure")
            real_require_identity(parent, name, expected, context)  # type: ignore[arg-type]

        monkeypatch.setattr(
            pdf_qa_module,
            "_require_anchored_directory_identity",
            fail_published_identity,
        )
    else:
        real_verify_visible = pdf_qa_module.assembly._DirectoryAnchor.verify_visible
        parent_checks = 0

        def fail_published_parent_visibility(
            anchor: pdf_qa_module.assembly._DirectoryAnchor,
        ) -> None:
            nonlocal parent_checks
            if anchor.path == prepared_pdf_run.output_dir.parent:
                parent_checks += 1
                if parent_checks == 2:
                    raise pdf_qa_module.PdfAssemblyError(
                        "injected post-publication parent failure"
                    )
            real_verify_visible(anchor)

        monkeypatch.setattr(
            pdf_qa_module.assembly._DirectoryAnchor,
            "verify_visible",
            fail_published_parent_visibility,
        )

    with pytest.raises(PdfQAFailure, match="injected post-publication"):
        finalize_pdf_output(prepared_pdf_run.run_dir, prepared_pdf_run.output_dir)

    assert not prepared_pdf_run.output_dir.exists()
    restored_metadata = staging.stat()
    assert (restored_metadata.st_dev, restored_metadata.st_ino) == staged_identity
    assert sorted(path.name for path in staging.iterdir()) == [
        "manifest.json",
        "review-report.md",
        "translated.pdf",
    ]


def test_finalize_rollback_preserves_raced_private_name_and_moved_identity(
    prepared_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = prepared_pdf_run.run_dir
    staging = run_dir / "staged-output"
    staged_metadata = staging.stat()
    staged_identity = (staged_metadata.st_dev, staged_metadata.st_ino)
    real_require_identity = pdf_qa_module._require_anchored_directory_identity

    def race_private_name_then_fail(
        parent: object,
        name: str,
        expected: tuple[int, int],
        context: str,
    ) -> None:
        if context == "published final PDF output":
            staging.mkdir()
            (staging / "unrelated.txt").write_text("racer", encoding="utf-8")
            raise PdfQAFailure("injected post-publication identity failure")
        real_require_identity(parent, name, expected, context)  # type: ignore[arg-type]

    monkeypatch.setattr(
        pdf_qa_module,
        "_require_anchored_directory_identity",
        race_private_name_then_fail,
    )

    with pytest.raises(PdfQAFailure, match="injected post-publication"):
        finalize_pdf_output(run_dir, prepared_pdf_run.output_dir)

    assert not prepared_pdf_run.output_dir.exists()
    assert sorted(path.name for path in staging.iterdir()) == ["unrelated.txt"]
    assert (staging / "unrelated.txt").read_text(encoding="utf-8") == "racer"
    moved = [
        path
        for path in run_dir.iterdir()
        if path.is_dir()
        and not path.is_symlink()
        and (path.stat().st_dev, path.stat().st_ino) == staged_identity
    ]
    assert len(moved) == 1
    assert moved[0].name.startswith(".pdf-final-rollback-")
    assert sorted(path.name for path in moved[0].iterdir()) == [
        "manifest.json",
        "review-report.md",
        "translated.pdf",
    ]


def test_finalize_commits_success_if_postpublication_rollback_is_unavailable(
    prepared_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_require_identity = pdf_qa_module._require_anchored_directory_identity

    def fail_published_identity(
        parent: object,
        name: str,
        expected: tuple[int, int],
        context: str,
    ) -> None:
        if context == "published final PDF output":
            raise PdfQAFailure("injected post-publication identity failure")
        real_require_identity(parent, name, expected, context)  # type: ignore[arg-type]

    def fail_rollback(*args: object, **kwargs: object) -> str:
        raise PdfQAFailure("injected rollback failure")

    monkeypatch.setattr(
        pdf_qa_module,
        "_require_anchored_directory_identity",
        fail_published_identity,
    )
    monkeypatch.setattr(pdf_qa_module, "_rollback_final_directory", fail_rollback)

    finalized = finalize_pdf_output(
        prepared_pdf_run.run_dir,
        prepared_pdf_run.output_dir,
    )

    assert finalized == prepared_pdf_run.output_dir
    assert sorted(path.name for path in finalized.iterdir()) == [
        "manifest.json",
        "review-report.md",
        "translated.pdf",
    ]
    assert not (prepared_pdf_run.run_dir / "staged-output").exists()


@pytest.mark.parametrize("racer_kind", ["empty-directory", "symlink"])
def test_finalize_never_clobbers_destination_appearing_after_validation(
    prepared_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
    racer_kind: str,
) -> None:
    output_dir = prepared_pdf_run.output_dir
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    linked_target = output_dir.parent / "linked-target"
    linked_target.mkdir()
    if racer_kind == "symlink":
        probe = output_dir.parent / "symlink-probe"
        try:
            probe.symlink_to(linked_target, target_is_directory=True)
            probe.unlink()
        except OSError as error:
            pytest.skip(f"directory symlinks unavailable: {error}")
    real_validate = pdf_qa_module._validate_locations
    validations = 0

    def validate_then_race(run_anchor: object, requested_output: Path) -> None:
        nonlocal validations
        real_validate(run_anchor, requested_output)  # type: ignore[arg-type]
        validations += 1
        if validations != 2:
            return
        if racer_kind == "empty-directory":
            requested_output.mkdir()
        else:
            requested_output.symlink_to(linked_target, target_is_directory=True)

    monkeypatch.setattr(pdf_qa_module, "_validate_locations", validate_then_race)

    with pytest.raises(PdfQAFailure, match="already exists"):
        finalize_pdf_output(prepared_pdf_run.run_dir, output_dir)

    if racer_kind == "empty-directory":
        assert output_dir.is_dir()
        assert list(output_dir.iterdir()) == []
    else:
        assert output_dir.is_symlink()
        assert output_dir.resolve(strict=True) == linked_target.resolve(strict=True)
    assert (prepared_pdf_run.run_dir / "staged-output" / "translated.pdf").is_file()
    assert not (prepared_pdf_run.run_dir / "staged-output" / "manifest.json").exists()


def test_finalize_rolls_back_when_report_writer_fails(
    prepared_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_report(*args: object, **kwargs: object) -> None:
        raise OSError("report writer failed")

    monkeypatch.setattr(pdf_report_module, "write_pdf_review_report", fail_report)

    with pytest.raises(PdfQAFailure, match="report writer failed"):
        finalize_pdf_output(prepared_pdf_run.run_dir, prepared_pdf_run.output_dir)

    staging = prepared_pdf_run.run_dir / "staged-output"
    assert sorted(path.name for path in staging.iterdir()) == ["translated.pdf"]
    assert not prepared_pdf_run.output_dir.exists()


def test_finalize_rejects_report_evidence_mutated_during_manifest_write(
    prepared_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = prepared_pdf_run.run_dir / "source.json"
    real_write = pdf_report_module.write_pdf_manifest

    def mutate_then_write(*args: object, **kwargs: object) -> object:
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source["warnings"] = ["mutated after validation"]
        source_path.write_text(
            json.dumps(source, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return real_write(*args, **kwargs)

    monkeypatch.setattr(pdf_report_module, "write_pdf_manifest", mutate_then_write)

    with pytest.raises(PdfQAFailure, match="report evidence content changed"):
        finalize_pdf_output(prepared_pdf_run.run_dir, prepared_pdf_run.output_dir)

    assert not prepared_pdf_run.output_dir.exists()
    assert (prepared_pdf_run.run_dir / "staged-output" / "translated.pdf").is_file()


def test_finalize_rejects_linked_report_evidence(
    prepared_pdf_run: PdfQARun,
) -> None:
    segments = prepared_pdf_run.run_dir / "segments.jsonl"
    moved = prepared_pdf_run.run_dir / "held-segments.jsonl"
    segments.rename(moved)
    try:
        segments.symlink_to(moved)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")

    with pytest.raises(PdfQAFailure, match="segments|safe|regular file"):
        finalize_pdf_output(prepared_pdf_run.run_dir, prepared_pdf_run.output_dir)

    assert not prepared_pdf_run.output_dir.exists()


def test_finalize_rejects_source_and_document_sha_mismatch(
    prepared_pdf_run: PdfQARun,
) -> None:
    document_path = prepared_pdf_run.run_dir / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    document["source_sha256"] = "b" * 64
    document_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PdfQAFailure, match="source SHA"):
        finalize_pdf_output(prepared_pdf_run.run_dir, prepared_pdf_run.output_dir)

    assert not prepared_pdf_run.output_dir.exists()


def test_finalize_rejects_qa_translated_block_count_mismatch(
    prepared_pdf_run: PdfQARun,
) -> None:
    qa_path = prepared_pdf_run.run_dir / "pdf-qa.json"
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    qa["metrics"]["translated_block_count"] += 1
    qa_path.write_text(
        json.dumps(qa, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PdfQAFailure, match="translated block count"):
        finalize_pdf_output(prepared_pdf_run.run_dir, prepared_pdf_run.output_dir)

    assert not prepared_pdf_run.output_dir.exists()


def test_finalize_supports_spaces_and_korean_in_final_path(
    prepared_pdf_run: PdfQARun,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "최종 PDF 공간" / "검토된 번역"
    layout_path = prepared_pdf_run.run_dir / "layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["reserved_output_dir"] = str(output_dir)
    layout_path.write_text(
        json.dumps(layout, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    finalized = finalize_pdf_output(prepared_pdf_run.run_dir, output_dir)

    assert finalized == output_dir
    assert sorted(path.name for path in finalized.iterdir()) == [
        "manifest.json",
        "review-report.md",
        "translated.pdf",
    ]


def test_windows_final_publication_uses_relative_no_replace_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePathAnchor:
        def __init__(self, handle: int, path: Path) -> None:
            self.handle = handle
            self.path = path

        def current_path(self) -> Path:
            return self.path

        def close(self) -> None:
            return

    run_dir = tmp_path / "run"
    staged = run_dir / "staged-output"
    output_parent = tmp_path / "최종 결과"
    staged.mkdir(parents=True)
    output_parent.mkdir()
    run_stat = run_dir.stat()
    staged_stat = staged.stat()
    output_stat = output_parent.stat()
    run_anchor = pdf_qa_module.assembly._DirectoryAnchor(
        run_dir,
        "run",
        (run_stat.st_dev, run_stat.st_ino),
        None,
        FakePathAnchor(41, run_dir),
    )
    staged_anchor = pdf_qa_module.assembly._DirectoryAnchor(
        staged,
        "staged",
        (staged_stat.st_dev, staged_stat.st_ino),
        None,
        FakePathAnchor(42, staged),
    )
    output_anchor = pdf_qa_module.assembly._DirectoryAnchor(
        output_parent,
        "output parent",
        (output_stat.st_dev, output_stat.st_ino),
        None,
        FakePathAnchor(43, output_parent),
    )
    closed: list[int] = []

    def open_source(
        root_handle: int,
        name: str,
        **options: int,
    ) -> int:
        assert root_handle == 41
        assert name == "staged-output"
        assert options["desired_access"] & 0x00010000
        assert options["create_disposition"] == 1
        return 99

    def rename_no_replace(handle: int, root_handle: int, name: str) -> None:
        assert (handle, root_handle, name) == (99, 43, "검토 완료")
        staged.rename(output_parent / name)

    monkeypatch.setattr(pdf_qa_module.assembly, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        pdf_qa_module.assembly, "_windows_nt_create_relative", open_source
    )
    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_windows_file_identity",
        lambda handle, require_regular: staged_anchor.identity,
    )
    monkeypatch.setattr(
        pdf_qa_module.assembly, "_windows_rename_open_file", rename_no_replace
    )
    monkeypatch.setattr(
        pdf_qa_module.assembly.pdf_acquire_module,
        "_close_windows_handle",
        closed.append,
    )

    pdf_qa_module._publish_final_directory_no_clobber(
        run_anchor,
        staged_anchor,
        output_anchor,
        "검토 완료",
    )

    assert (output_parent / "검토 완료").is_dir()
    assert closed == [99]


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
