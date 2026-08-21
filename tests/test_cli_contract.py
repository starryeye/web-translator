from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

import web_translator.cli as cli_module
from web_translator.cli import (
    CLIContractError,
    _build_manifest_provenance,
    _detect_source_language,
    _read_capture,
    _read_review,
    main,
)
from web_translator.models import MasterReview, QAResult, Segment
from web_translator.zones import Zone


REVIEW_DIMENSIONS = (
    "semantic_fidelity",
    "qualification_preservation",
    "naturalness",
    "terminology",
    "boundary_consistency",
    "protected_content",
)


def test_pdf_acquire_cli_requires_an_empty_run_directory_and_writes_source_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    run_dir = tmp_path / "run"

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
    run_dir = tmp_path / "run"
    run_dir.mkdir()
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
    run_dir = tmp_path / "run"

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
    run_dir = tmp_path / "run"
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
    import web_translator.pdf_acquire as acquire_module

    monkeypatch.setattr(
        acquire_module, "_supports_descriptor_relative_operations", lambda: False
    )
    original_link = acquire_module.os.link

    def fail_final_source_link(
        source_path: str | Path, destination: str | Path, **kwargs: object
    ) -> None:
        if Path(destination) == tmp_path / "run" / "source.pdf":
            raise NotImplementedError()
        original_link(source_path, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        acquire_module.os,
        "link",
        fail_final_source_link,
    )

    exit_code = main(["pdf-acquire", str(source), "--run-dir", str(tmp_path / "run")])

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
