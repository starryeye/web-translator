from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
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
import web_translator.pdf_media as pdf_media_module
from web_translator.cli import main
from tests import pdf_fixtures
from tests.pdf_fixtures import make_many_pages_pdf, make_oversized_pdf
from tests.test_pdf_extract import _column_pdf, _ruled_table_pdf
from web_translator.pdf_qa import PdfQAFailure, finalize_pdf_output, prepare_pdf_qa
from web_translator.pdf_media import PdfMediaError
from web_translator.pdf_report import build_pdf_manifest
from tests.test_pdf_qa import PdfQARun, _write_passing_layout_review, assembled_pdf_run


PDF_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "pdf"
_KOREAN_SYLLABLE = re.compile(r"[가-힣]")
_PROTECTED_TOKEN = re.compile(r"⟦WT:\d{6}⟧")
_PRESERVED_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_PRESERVED_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"[A-Za-z][A-Za-z0-9-]*(?:[_./:][A-Za-z0-9-]+)+"
    r"|[A-Za-z][A-Za-z_-]*\d[A-Za-z0-9_-]*"
    r")(?![A-Za-z0-9_])"
)
_PRESERVED_ACRONYM = re.compile(
    r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]{1,}(?![A-Za-z0-9_])"
)
_LATIN_ALPHABET = re.compile(r"[A-Za-z]")
_SEMANTIC_ORACLE = {
    "technical-document-v1": {
        "Deterministic Systems Review": "결정론적 시스템 검토",
        "A deterministic workflow preserves source order, stable identifiers, and review evidence.": (
            "결정론적 작업 흐름은 원본 순서, 안정적인 식별자 및 검토 증거를 "
            "보존합니다."
        ),
        "The translation system validates every protected token before assembly begins.": (
            "번역 시스템은 조립을 시작하기 전에 모든 보호 토큰을 검증합니다."
        ),
        "Reviewers compare semantic fidelity, terminology, and qualification preservation.": (
            "검토자는 의미 충실도, 용어 및 한정 표현 보존을 비교합니다."
        ),
        "The release artifact contains the translated PDF, manifest, and review report only.": (
            "릴리스 산출물에는 번역된 PDF, 매니페스트 및 검토 보고서만 포함됩니다."
        ),
        "Page 1": "1쪽",
        "Operational Verification": "운영 검증",
        "Automated checks confirm selectable Korean text, embedded fonts, and complete page renders.": (
            "자동 검사는 선택 가능한 한국어 텍스트, 포함된 글꼴 및 완전한 페이지 렌더링을 "
            "확인합니다."
        ),
        "Contact sheets cover each output page exactly once and bind review to the staged digest.": (
            "연락처 시트는 각 출력 페이지를 정확히 한 번 포함하고 검토를 스테이징 다이제스트에 "
            "연결합니다."
        ),
        "A failed check keeps private staging intact and never publishes a partial final directory.": (
            "실패한 검사는 비공개 스테이징을 그대로 유지하고 부분 최종 디렉터리를 게시하지 "
            "않습니다."
        ),
        "This fixture provides stable technical prose for repeatable end-to-end acceptance testing.": (
            "이 픽스처는 반복 가능한 종단 간 승인 테스트를 위한 안정적인 기술 문서를 제공합니다."
        ),
        "Page 2": "2쪽",
    },
    "table-report-v1": {
        "First half measurements summarize deterministic acceptance outcomes for the release.": (
            "상반기 측정값은 릴리스의 결정론적 승인 결과를 요약합니다."
        ),
        "First half metrics": "상반기 지표",
        "Measure": "측정 항목",
        "Observed": "관찰값",
        "Required": "필수값",
        "Selectable characters": "선택 가능한 문자",
        "At least 100": "최소 100개",
        "Reviewed pages": "검토한 페이지",
        "Required findings": "필수 지적 사항",
        "Published artifacts": "게시된 산출물",
        "Merged header cells retain their logical span and all body rows remain readable. The same report structure continues on the next page without losing table evidence.": (
            "병합된 머리글 셀은 논리적 범위를 유지하고 모든 본문 행은 계속 읽을 수 있습니다. "
            "동일한 보고서 구조는 표 증거를 잃지 않고 다음 페이지로 이어집니다."
        ),
        "Page 1": "1쪽",
        "Second half measurements summarize deterministic acceptance outcomes for the release.": (
            "하반기 측정값은 릴리스의 결정론적 승인 결과를 요약합니다."
        ),
        "Second half metrics": "하반기 지표",
        "Page 2": "2쪽",
    },
    "two-column-footnotes-v1": {
        "Two-Column Evidence Review": "두 열 증거 검토",
        "Column 1 first logical sentence. Column 1 second logical sentence.": (
            "첫 번째 열의 첫 논리 문장입니다. 첫 번째 열의 두 번째 논리 문장입니다."
        ),
        "Column 2 first logical sentence. Column 2 second logical sentence.": (
            "두 번째 열의 첫 논리 문장입니다. 두 번째 열의 두 번째 논리 문장입니다."
        ),
        "Page-Local Footnote Evidence": "페이지 내 각주 증거",
        "Source order is validated before bounded zone planning begins.": (
            "제한된 영역 계획을 시작하기 전에 원본 순서를 검증합니다."
        ),
        "Contact-sheet evidence covers every rendered output page exactly once.": (
            "연락처 시트는 렌더링된 각 출력 페이지를 정확히 한 번 포함합니다."
        ),
        "Semantic review checks every required quality dimension. Validated Korean text remains selectable in the staged PDF. Automated QA records exact output and contact-sheet page counts. Final publication exposes exactly three reviewed artifacts.": (
            "의미 검토는 모든 필수 품질 기준을 확인하며, 검증된 한국어 텍스트는 스테이징 PDF에서 선택 "
            "가능하게 유지됩니다. 자동 QA는 정확한 출력 및 연락처 시트 페이지 수를 기록하고 최종 "
            "게시에는 검토된 세 가지 산출물만 공개됩니다."
        ),
        "The deterministic workflow includes a page-local note 1": (
            "결정론적 작업 흐름에는 페이지 내 주석 1이 포함됩니다."
        ),
        "1 Footnote evidence remains linked to its marker.": (
            "1 각주 증거는 해당 표식에 연결된 상태로 유지됩니다."
        ),
    },
    "figures-captions-v1": {
        "Figures and Captions": "그림과 캡션",
        "Raster and vector evidence must remain paired with the correct explanatory caption.": (
            "래스터 및 벡터 증거는 올바른 설명 캡션과 연결된 상태를 유지해야 합니다."
        ),
        "Figure 1. Raster workflow status panel.": (
            "그림 1. 래스터 작업 흐름 상태 패널."
        ),
        "Figure 2. Vector review coverage trend.": "그림 2. 벡터 검토 범위 추세.",
        "The raster panel verifies image preservation and the vector plot verifies path rendering. Each caption follows its figure directly so extraction retains an unambiguous pair. Review checks sharp rendering, readable labels, and the absence of clipping or overlap.": (
            "래스터 패널은 이미지 보존을 검증하고 벡터 그래프는 경로 렌더링을 검증합니다. 각 캡션은 "
            "해당 그림 바로 뒤에 배치되어 추출 시 모호하지 않은 쌍을 유지합니다. 검토는 선명한 렌더링, "
            "읽기 쉬운 레이블 및 잘림이나 겹침이 없음을 확인합니다."
        ),
        "Page 1": "1쪽",
    },
}


def _without_protected_tokens(text: str, protected_tokens: list[str]) -> str:
    for token in protected_tokens:
        text = text.replace(token, "")
    return " ".join(text.split())


def _without_allowed_preserved_latin(text: str, source_text: str) -> str:
    for pattern in (_PRESERVED_URL, _PRESERVED_IDENTIFIER, _PRESERVED_ACRONYM):
        allowed = {match.group(0) for match in pattern.finditer(source_text)}
        text = pattern.sub(
            lambda match: " " if match.group(0) in allowed else match.group(0),
            text,
        )
    return text


def _assert_committed_fixture_semantics(
    fixture_name: str,
    segments: list[dict[str, object]],
    records: list[dict[str, object]],
    expected_pairs: dict[str, str] | None = None,
) -> None:
    target_by_id = {
        str(segment["id"]): segment
        for segment in segments
        if segment["target"]
    }
    translation_by_id = {
        str(record["segment_id"]): str(record["text"])
        for record in records
    }
    assert set(translation_by_id) == set(target_by_id)

    oracle = expected_pairs if expected_pairs is not None else _SEMANTIC_ORACLE[fixture_name]
    actual_pairs: dict[str, str] = {}
    normalized_translations: dict[str, str] = {}
    for segment_id, segment in target_by_id.items():
        source = str(segment["source_text"])
        translation = translation_by_id[segment_id]
        protected = segment["protected"]
        assert isinstance(protected, list)
        protected_tokens = [str(token["token"]) for token in protected]
        expected_token_counts = Counter(protected_tokens)
        assert Counter(_PROTECTED_TOKEN.findall(source)) == expected_token_counts
        assert Counter(_PROTECTED_TOKEN.findall(translation)) == expected_token_counts

        if not any(character.isalpha() for character in source):
            continue
        previous = actual_pairs.setdefault(source, translation)
        assert previous == translation, f"repeated source changed translation: {source!r}"

        source_body = _without_protected_tokens(source, protected_tokens)
        translation_body = _without_protected_tokens(translation, protected_tokens)
        assert _KOREAN_SYLLABLE.search(translation_body), (
            f"alphabetic source lacks Korean translation: {source!r}"
        )
        assert source_body.casefold() not in translation_body.casefold(), (
            f"normalized source body remains in translation: {source!r}"
        )
        prose_candidate = _without_allowed_preserved_latin(
            translation_body,
            source_body,
        )
        assert _LATIN_ALPHABET.search(prose_candidate) is None, (
            f"Latin alphabet remains in translation: {translation!r}"
        )
        normalized_translations[source] = translation_body

    assert actual_pairs == oracle, "literal semantic oracle mismatch"
    assert len(set(normalized_translations.values())) == len(normalized_translations), (
        "distinct source meanings collapsed to a generic translated body"
    )


# Production mutations caught: a Korean label wrapped around protected-token-split
# English source prose, or different generic Korean labels substituted for distinct
# source meanings.
@pytest.mark.parametrize(
    ("second_source", "second_translation", "second_expected", "protected"),
    [
        (
            "The source body uses ⟦WT:000001⟧ and protected content.",
            "한국어 레이블: The source body uses and protected content. "
            "⟦WT:000001⟧",
            "원문 본문은 보호 토큰을 사용합니다. ⟦WT:000001⟧",
            [
                {
                    "token": "⟦WT:000001⟧",
                    "kind": "url",
                    "value": "https://fixture.example/report",
                }
            ],
        ),
        (
            "The translation system validates every protected token before assembly begins.",
            "두 번째 일반 번역",
            "번역 시스템은 조립을 시작하기 전에 모든 보호 토큰을 검증합니다.",
            [],
        ),
    ],
)
def test_semantic_oracle_rejects_passthrough_and_generic_translation_mutations(
    second_source: str,
    second_translation: str,
    second_expected: str,
    protected: list[dict[str, str]],
) -> None:
    anchor_source = "Deterministic Systems Review"
    anchor_translation = "결정론적 시스템 검토"
    segments: list[dict[str, object]] = [
        {
            "id": "seg-000001",
            "source_text": anchor_source,
            "protected": [],
            "target": True,
        },
        {
            "id": "seg-000002",
            "source_text": second_source,
            "protected": protected,
            "target": True,
        },
    ]
    records: list[dict[str, object]] = [
        {"segment_id": "seg-000001", "text": anchor_translation},
        {"segment_id": "seg-000002", "text": second_translation},
    ]

    with pytest.raises(AssertionError):
        _assert_committed_fixture_semantics(
            "mutation",
            segments,
            records,
            {
                anchor_source: anchor_translation,
                second_source: second_expected,
            },
        )


def test_semantic_oracle_allows_exact_protected_url_acronym_and_identifier() -> None:
    token = "⟦WT:000001⟧"
    url = "https://docs.example/pdf"
    source = f"Use PDF from {token} with {url} for build_id-7."
    translation = f"한국어 설명 PDF {url} build_id-7 {token}"
    segments: list[dict[str, object]] = [
        {
            "id": "seg-000001",
            "source_text": source,
            "protected": [
                {
                    "token": token,
                    "kind": "url",
                    "value": "https://fixture.example/report",
                }
            ],
            "target": True,
        }
    ]
    records: list[dict[str, object]] = [
        {"segment_id": "seg-000001", "text": translation}
    ]

    _assert_committed_fixture_semantics(
        "mutation",
        segments,
        records,
        {source: translation},
    )


def test_semantic_oracle_rejects_unprotected_uppercase_english_prose() -> None:
    source = "Ordinary source sentence."
    translation = "한국어 레이블 THIS IS PASSTHROUGH"
    segments: list[dict[str, object]] = [
        {
            "id": "seg-000001",
            "source_text": source,
            "protected": [],
            "target": True,
        }
    ]
    records: list[dict[str, object]] = [
        {"segment_id": "seg-000001", "text": translation}
    ]

    with pytest.raises(AssertionError, match="Latin alphabet remains"):
        _assert_committed_fixture_semantics(
            "mutation",
            segments,
            records,
            {source: translation},
        )


@pytest.mark.parametrize(
    "translation",
    [
        "한국어 설명 SECRET",
        "한국어 https://evil.example/x",
    ],
    ids=["single-unapproved-acronym", "source-absent-url"],
)
def test_semantic_oracle_rejects_any_source_absent_latin_span(
    translation: str,
) -> None:
    source = "Ordinary source sentence."
    segments: list[dict[str, object]] = [
        {
            "id": "seg-000001",
            "source_text": source,
            "protected": [],
            "target": True,
        }
    ]
    records: list[dict[str, object]] = [
        {"segment_id": "seg-000001", "text": translation}
    ]

    with pytest.raises(AssertionError, match="Latin alphabet remains"):
        _assert_committed_fixture_semantics(
            "mutation",
            segments,
            records,
            {source: translation},
        )


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
    segments = [
        json.loads(line)
        for line in (run_dir / "segments.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    _assert_committed_fixture_semantics(fixture_name, segments, records)
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


@pytest.mark.parametrize(
    "builder", [lambda path: _column_pdf(path, columns=3), lambda path: _ruled_table_pdf(path, crossing_border=True)]
)
def test_cli_ambiguous_extraction_rejections_never_publish(
    tmp_path: Path, builder: object
) -> None:
    source = builder(tmp_path / "input.pdf")  # type: ignore[operator]
    run_dir = tmp_path / "run"
    assert main(["pdf-acquire", str(source), "--run-dir", str(run_dir)]) == 0
    assert main(["pdf-extract", "--run-dir", str(run_dir)]) == 4
    assert not (tmp_path / "final").exists()


def test_cli_pdf_qa_finalize_rejects_stale_review_and_collision(
    assembled_pdf_run: PdfQARun, tmp_path: Path
) -> None:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    _write_passing_layout_review(assembled_pdf_run.run_dir, staged_sha256="0" * 64)
    assert main(["pdf-qa", "finalize", "--run-dir", str(assembled_pdf_run.run_dir), "--output-dir", str(assembled_pdf_run.output_dir)]) == 6
    assert not assembled_pdf_run.output_dir.exists()
    _write_passing_layout_review(assembled_pdf_run.run_dir)
    assembled_pdf_run.output_dir.mkdir(parents=True)
    assert main(["pdf-qa", "finalize", "--run-dir", str(assembled_pdf_run.run_dir), "--output-dir", str(assembled_pdf_run.output_dir)]) == 6
    assert list(assembled_pdf_run.output_dir.iterdir()) == []


def test_cli_pdf_extract_rejects_missing_poppler_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf_media_module, "find_poppler", lambda: (_ for _ in ()).throw(PdfMediaError("missing pdfinfo and pdftoppm")))
    run_dir = tmp_path / "run"
    source = PDF_FIXTURE_ROOT / "figures-captions-v1" / "source.pdf"
    assert main(["pdf-acquire", str(source), "--run-dir", str(run_dir)]) == 0
    assert main(["pdf-extract", "--run-dir", str(run_dir)]) == 4
    assert not (tmp_path / "final").exists()


@pytest.fixture
def prepared_pdf_run(assembled_pdf_run: PdfQARun) -> PdfQARun:
    prepare_pdf_qa(assembled_pdf_run.run_dir, assembled_pdf_run.output_dir)
    _write_passing_layout_review(assembled_pdf_run.run_dir)
    return assembled_pdf_run


@pytest.mark.skipif(
    os.name != "nt",
    reason="requires real Windows durable flush and directory publication",
)
def test_windows_finalize_durably_publishes_exact_reviewed_output(
    prepared_pdf_run: PdfQARun,
) -> None:
    """A real Windows run must flush and publish the three-file transaction."""
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
    assert all(path.stat().st_size > 0 for path in finalized.iterdir())
    assert not (prepared_pdf_run.run_dir / "staged-output").exists()


def test_fsync_final_files_uses_anchored_write_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = tmp_path / "staged-output"
    staged.mkdir()
    payloads = {
        "manifest.json": b"{}\n",
        "review-report.md": b"# reviewed\n",
        "translated.pdf": b"%PDF-1.7\nfinal\n",
    }
    for name, payload in payloads.items():
        (staged / name).write_bytes(payload)
    anchor = pdf_qa_module.assembly._open_directory_anchor(staged, "staged output")
    opened = {
        name: pdf_qa_module.assembly._open_anchored_input_file(
            anchor, name, "final PDF artifact"
        )
        for name in payloads
    }
    expected_hashes = {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in payloads.items()
    }
    real_open = os.open
    anchored_opens: list[tuple[str, int, int | None]] = []

    def capture_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if isinstance(path, str) and path in payloads:
            anchored_opens.append((path, flags, dir_fd))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pdf_qa_module.os, "open", capture_open)
    try:
        pdf_qa_module._fsync_final_files(anchor, opened, expected_hashes)
    finally:
        for item in opened.values():
            pdf_qa_module.assembly._close_opened_file(item)
        anchor.close()

    assert sorted(name for name, _flags, _dir_fd in anchored_opens) == sorted(payloads)
    assert all(flags & os.O_RDWR for _name, flags, _dir_fd in anchored_opens)
    assert all(dir_fd is not None for _name, _flags, dir_fd in anchored_opens)
    assert {name: (staged / name).read_bytes() for name in payloads} == payloads


def test_fsync_final_files_uses_windows_anchored_flush_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = tmp_path / "staged-output"
    staged.mkdir()
    payloads = {
        "manifest.json": b"{}\n",
        "review-report.md": b"# reviewed\n",
        "translated.pdf": b"%PDF-1.7\nfinal\n",
    }
    for name, payload in payloads.items():
        (staged / name).write_bytes(payload)
    anchor = pdf_qa_module.assembly._open_directory_anchor(staged, "staged output")
    opened = {
        name: pdf_qa_module.assembly._open_anchored_input_file(
            anchor, name, "final PDF artifact"
        )
        for name in payloads
    }
    expected_hashes = {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in payloads.items()
    }
    next_handle = 100
    handle_names: dict[int, str] = {}
    desired_access: dict[str, int] = {}
    flushed: list[int] = []
    closed: list[int] = []

    def open_relative(
        root_handle: int,
        name: str,
        **options: int,
    ) -> int:
        nonlocal next_handle
        assert root_handle == 55
        handle = next_handle
        next_handle += 1
        handle_names[handle] = name
        desired_access[name] = options["desired_access"]
        return handle

    monkeypatch.setattr(pdf_qa_module.assembly, "_IS_WINDOWS", True)
    monkeypatch.setattr(pdf_qa_module.assembly, "_windows_anchor_handle", lambda _anchor: 55)
    monkeypatch.setattr(pdf_qa_module.assembly, "_windows_nt_create_relative", open_relative)
    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_windows_file_identity",
        lambda handle, require_regular: opened[handle_names[handle]].identity,
    )
    monkeypatch.setattr(
        pdf_qa_module,
        "_windows_flush_file_buffers",
        flushed.append,
        raising=False,
    )
    monkeypatch.setattr(
        pdf_qa_module.assembly.pdf_acquire_module,
        "_close_windows_handle",
        closed.append,
    )
    try:
        pdf_qa_module._fsync_final_files(anchor, opened, expected_hashes)
    finally:
        for item in opened.values():
            pdf_qa_module.assembly._close_opened_file(item)
        anchor.close()

    assert set(desired_access) == set(payloads)
    assert all(access & 0x40000000 for access in desired_access.values())
    assert flushed == closed
    assert len(flushed) == 3


def test_finalize_closes_windows_descendants_before_directory_publication(
    prepared_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []
    real_open = pdf_qa_module.assembly._open_anchored_input_file
    real_rename = pdf_qa_module._rename_anchored_directory_no_replace

    def capture_final_file(
        directory: object,
        name: str,
        context: str,
    ) -> object:
        item = real_open(directory, name, context)  # type: ignore[arg-type]
        if context in {"staged translated PDF", "final PDF artifact"}:
            captured.append(item)
        return item

    def require_closed_descendants(*args: object, **kwargs: object) -> None:
        if args[1] == "staged-output":
            assert captured
            assert all(item.stream.closed for item in captured)  # type: ignore[attr-defined]
        real_rename(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        pdf_qa_module,
        "_WINDOWS_RENAME_REQUIRES_CLOSED_DESCENDANTS",
        True,
    )
    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_open_anchored_input_file",
        capture_final_file,
    )
    monkeypatch.setattr(
        pdf_qa_module,
        "_rename_anchored_directory_no_replace",
        require_closed_descendants,
    )

    finalized = finalize_pdf_output(
        prepared_pdf_run.run_dir,
        prepared_pdf_run.output_dir,
    )

    assert finalized == prepared_pdf_run.output_dir
    assert all(item.stream.closed for item in captured)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "mutation",
    ["extra-child", "replacement-identity", "same-identity-content"],
)
def test_finalize_rechecks_exact_transaction_after_directory_publication(
    prepared_pdf_run: PdfQARun,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    real_rename = pdf_qa_module._rename_anchored_directory_no_replace

    def publish_then_mutate(*args: object, **kwargs: object) -> None:
        real_rename(*args, **kwargs)  # type: ignore[arg-type]
        if args[1] != "staged-output":
            return
        destination = prepared_pdf_run.output_dir
        translated = destination / "translated.pdf"
        if mutation == "extra-child":
            (destination / "unexpected.txt").write_bytes(b"foreign extra child")
        elif mutation == "replacement-identity":
            translated.unlink()
            translated.write_bytes(b"foreign replacement")
        else:
            with translated.open("r+b") as stream:
                stream.seek(0)
                stream.write(b"foreign same-inode rewrite")
                stream.truncate()

    monkeypatch.setattr(
        pdf_qa_module,
        "_WINDOWS_RENAME_REQUIRES_CLOSED_DESCENDANTS",
        True,
    )
    monkeypatch.setattr(
        pdf_qa_module,
        "_rename_anchored_directory_no_replace",
        publish_then_mutate,
    )

    with pytest.raises(PdfQAFailure, match="final PDF artifacts"):
        finalize_pdf_output(
            prepared_pdf_run.run_dir,
            prepared_pdf_run.output_dir,
        )

    assert not prepared_pdf_run.output_dir.exists()
    assert (prepared_pdf_run.run_dir / "staged-output").is_dir()


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
    monkeypatch.setattr(
        pdf_qa_module,
        "_verify_final_artifact_snapshot",
        lambda *_args, **_kwargs: None,
    )

    def open_published(
        _parent: object,
        name: str,
        _label: str,
    ) -> object:
        path = output_parent / name
        return pdf_qa_module.assembly._DirectoryAnchor(
            path,
            "published",
            staged_anchor.identity,
            None,
            FakePathAnchor(44, path),
        )

    monkeypatch.setattr(
        pdf_qa_module.assembly,
        "_open_existing_child_directory",
        open_published,
    )
    expected_identities = {
        name: (1, index)
        for index, name in enumerate(pdf_qa_module._FINAL_OUTPUT_NAMES, start=1)
    }
    expected_hashes = {
        name: f"{index:064x}"
        for index, name in enumerate(pdf_qa_module._FINAL_OUTPUT_NAMES, start=1)
    }

    pdf_qa_module._publish_final_directory_no_clobber(
        run_anchor,
        staged_anchor,
        output_anchor,
        "검토 완료",
        expected_identities,
        expected_hashes,
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
