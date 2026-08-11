from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading

import pytest

import web_translator.capture as capture_module
import web_translator.cli as cli_module
from web_translator.capture import CaptureError
from web_translator.cli import main


TRANSLATED_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "translated"
FIXTURE_HTML = b"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <link rel="stylesheet" href="theme.css">
  </head>
  <body>
    <main>
      <img src="logo.svg" alt="Fixture logo">
      <h1>OAuth Token Exchange</h1>
      <p>OAuth clients MUST run <code>git status</code> before retrying.</p>
      <p>OAuth keeps the source token unchanged.</p>
    </main>
  </body>
</html>
"""
FIXTURE_CSS = b"body { font-family: sans-serif; max-width: 60rem; margin: auto; }"
FIXTURE_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
    b'<circle cx="10" cy="10" r="8"/></svg>'
)


@dataclass(frozen=True)
class FixtureServer:
    url: str


@pytest.fixture
def fixture_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[FixtureServer]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/docs/":
                content, content_type = FIXTURE_HTML, "text/html; charset=utf-8"
            elif self.path == "/docs/theme.css":
                content, content_type = FIXTURE_CSS, "text/css"
            elif self.path == "/docs/logo.svg":
                content, content_type = FIXTURE_SVG, "image/svg+xml"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        capture_module,
        "_resolve_public_addresses",
        lambda host, port: ["127.0.0.1"],
    )
    try:
        yield FixtureServer(f"http://fixture.example:{server.server_port}/docs/")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def copy_reviewed_fixture_translations(run_dir: Path) -> None:
    shutil.copy2(TRANSLATED_FIXTURE_DIR / "glossary.json", run_dir / "glossary.json")
    translations = run_dir / "translations"
    translations.mkdir()
    shutil.copy2(
        TRANSLATED_FIXTURE_DIR / "zone-001.jsonl",
        translations / "zone-001.jsonl",
    )
    (run_dir / "review.json").write_text(
        json.dumps(
            {
                "unresolved_required": [],
                "retries": {"zone-001": 0},
                "section_findings": {},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_fixture_pipeline_builds_complete_offline_bundle(
    tmp_path: Path, fixture_server: FixtureServer, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "작업 공간" / "run"
    output_dir = tmp_path / "작업 공간" / "translated-pages" / "fixture"

    assert main(["capture", fixture_server.url, "--run-dir", str(run_dir)]) == 0
    capture = json.loads((run_dir / "capture.json").read_text("utf-8"))
    assert [Path(path).suffix for path in capture["critical_assets"]] == [".css"]
    assert [Path(path).suffix for path in capture["optional_assets"]] == [".svg"]
    assert main(["extract", "--run-dir", str(run_dir)]) == 0
    assert main(
        ["plan-zones", "--run-dir", str(run_dir), "--max-chars", "12000"]
    ) == 0
    copy_reviewed_fixture_translations(run_dir)
    assert main(["validate-translations", "--run-dir", str(run_dir)]) == 0
    assert main(
        ["assemble", "--run-dir", str(run_dir), "--output-dir", str(output_dir)]
    ) == 0
    assert main(
        ["qa", "--run-dir", str(run_dir), "--output-dir", str(output_dir)]
    ) == 0

    output = capsys.readouterr()
    statuses = [json.loads(line) for line in output.out.splitlines()]
    assert [status["command"] for status in statuses] == [
        "capture",
        "extract",
        "plan-zones",
        "validate-translations",
        "assemble",
        "qa",
    ]
    assert all(status["status"] == "ok" for status in statuses)
    assert output.err == ""
    assert (output_dir / "index.html").exists()
    manifest = json.loads((output_dir / "manifest.json").read_text("utf-8"))
    assert manifest["qa_status"] == "passed"
    assert set(manifest["browser_metrics"]) == {
        "desktop-1440x900",
        "narrow-390x844",
    }
    assert {Path(path).name for path in manifest["screenshots"]} == {
        "desktop-1440x900.png",
        "narrow-390x844.png",
    }
    assert all(Path(path).is_file() for path in manifest["screenshots"])
    translated_html = (output_dir / "index.html").read_text("utf-8")
    assert translated_html.count("OAuth(오픈 인증)") == 1
    assert translated_html.count("Token Exchange(토큰 교환)") == 1
    assert "MUST" in translated_html
    assert "<code>git status</code> 명령을 실행한 뒤" in translated_html


def assert_error_status(
    capsys: pytest.CaptureFixture[str], *, command: str, exit_code: int
) -> None:
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "command": command,
        "exit_code": exit_code,
        "status": "error",
    }
    assert output.out.count("\n") == 1
    assert output.err.strip()


def test_invalid_capture_url_returns_argument_exit_code_and_one_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "occupied").write_text("existing", encoding="utf-8")
    assert main(["capture", "file:///private", "--run-dir", str(tmp_path)]) == 2
    assert_error_status(capsys, command="capture", exit_code=2)


def test_subcommand_help_keeps_stdout_reserved_for_one_status_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["capture", "--help"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "command": "capture",
        "exit_code": 0,
        "status": "ok",
    }
    assert output.out.count("\n") == 1
    assert "usage: web-translator capture" in output.err


def test_capture_failure_returns_capture_exit_code_and_one_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_resolution(host: str, port: int) -> list[str]:
        raise CaptureError("fixture DNS failure")

    monkeypatch.setattr(capture_module, "_resolve_public_addresses", fail_resolution)

    assert (
        main(
            [
                "capture",
                "https://fixture.example/",
                "--run-dir",
                str(tmp_path / "run"),
            ]
        )
        == 3
    )
    assert_error_status(capsys, command="capture", exit_code=3)


def test_capture_rejects_nonempty_run_directory_before_network_or_overwrite(
    tmp_path: Path,
    fixture_server: FixtureServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    metadata = run_dir / "capture.json"
    metadata.write_text("do not replace", encoding="utf-8")

    assert main(["capture", fixture_server.url, "--run-dir", str(run_dir)]) == 3
    assert_error_status(capsys, command="capture", exit_code=3)
    assert metadata.read_text("utf-8") == "do not replace"
    assert not (run_dir / "source.html").exists()


def test_missing_extract_contract_returns_contract_exit_code_and_one_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["extract", "--run-dir", str(tmp_path / "missing")]) == 4
    assert_error_status(capsys, command="extract", exit_code=4)


def test_nonpositive_zone_limit_is_an_invalid_argument(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "segments.jsonl").write_text("", encoding="utf-8")

    assert (
        main(
            [
                "plan-zones",
                "--run-dir",
                str(run_dir),
                "--max-chars",
                "0",
            ]
        )
        == 2
    )
    assert_error_status(capsys, command="plan-zones", exit_code=2)


def write_empty_run_contract(run_dir: Path) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "capture.json").write_text(
        json.dumps(
            {
                "requested_url": "https://fixture.example/",
                "final_url": "https://fixture.example/",
                "asset_map": {},
                "critical_assets": [],
                "fingerprints": {"source.html": "0" * 64},
                "missing_optional_assets": [],
                "optional_assets": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "segments.jsonl").write_text("", encoding="utf-8")
    (run_dir / "zones").mkdir()
    (run_dir / "translations").mkdir()
    (run_dir / "glossary.json").write_text("{}\n", encoding="utf-8")


def write_single_segment_run_contract(run_dir: Path) -> None:
    write_empty_run_contract(run_dir)
    segment = {
        "id": "seg-000001",
        "locator": "[data-wt-segment='seg-000001']",
        "semantic_type": "paragraph",
        "heading_path": [],
        "source_text": "OAuth",
        "protected": [],
        "context_ids": [],
        "target": True,
    }
    (run_dir / "segments.jsonl").write_text(
        json.dumps(segment) + "\n", encoding="utf-8"
    )
    zone = {
        "attempt": 0,
        "context_after_ids": [],
        "context_before_ids": [],
        "expected_tokens": {"seg-000001": []},
        "heading_path": [],
        "id": "zone-001",
        "target_ids": ["seg-000001"],
    }
    (run_dir / "zones" / "zone-001.json").write_text(
        json.dumps(zone) + "\n", encoding="utf-8"
    )
    (run_dir / "translations" / "zone-001.jsonl").write_text(
        '{"segment_id":"seg-000001","text":"OAuth"}\n', encoding="utf-8"
    )


def test_zone_filename_must_match_embedded_zone_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    write_empty_run_contract(run_dir)
    mismatched = {
        "attempt": 0,
        "context_after_ids": [],
        "context_before_ids": [],
        "expected_tokens": {},
        "heading_path": [],
        "id": "zone-002",
        "target_ids": [],
    }
    (run_dir / "zones" / "zone-001.json").write_text(
        json.dumps(mismatched) + "\n", encoding="utf-8"
    )

    assert main(["validate-translations", "--run-dir", str(run_dir)]) == 4
    output = capsys.readouterr()
    assert json.loads(output.out)["exit_code"] == 4
    assert "does not match" in output.err


def test_validate_rejects_stale_translation_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    write_empty_run_contract(run_dir)
    (run_dir / "translations" / "stale.jsonl").write_text("", encoding="utf-8")

    assert main(["validate-translations", "--run-dir", str(run_dir)]) == 4
    output = capsys.readouterr()
    assert json.loads(output.out)["exit_code"] == 4
    assert "unexpected translation entries" in output.err


def test_assemble_rejects_unsafe_capture_asset_paths_as_contract_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    write_empty_run_contract(run_dir)
    capture_path = run_dir / "capture.json"
    capture = json.loads(capture_path.read_text("utf-8"))
    capture["asset_map"] = {"https://fixture.example/theme.css": "../escape.css"}
    capture["critical_assets"] = ["../escape.css"]
    capture["fingerprints"] = {
        "../escape.css": "0" * 64,
        "source.html": "0" * 64,
    }
    capture_path.write_text(json.dumps(capture) + "\n", encoding="utf-8")

    assert (
        main(
            [
                "assemble",
                "--run-dir",
                str(run_dir),
                "--output-dir",
                str(tmp_path / "output"),
            ]
        )
        == 4
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["exit_code"] == 4
    assert "safe assets/ path" in output.err


def test_assemble_rejects_inconsistent_capture_fingerprints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    write_empty_run_contract(run_dir)
    capture_path = run_dir / "capture.json"
    capture = json.loads(capture_path.read_text("utf-8"))
    capture["asset_map"] = {
        "https://fixture.example/theme.css": "assets/theme.css"
    }
    capture["critical_assets"] = ["assets/theme.css"]
    capture_path.write_text(json.dumps(capture) + "\n", encoding="utf-8")

    assert (
        main(
            [
                "assemble",
                "--run-dir",
                str(run_dir),
                "--output-dir",
                str(tmp_path / "output"),
            ]
        )
        == 4
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["exit_code"] == 4
    assert "fingerprints must exactly cover" in output.err


def test_assemble_rejects_captured_asset_whose_bytes_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    write_empty_run_contract(run_dir)
    source = run_dir / "source.html"
    source.write_text("<html><body></body></html>", encoding="utf-8")
    assets = run_dir / "assets"
    assets.mkdir()
    stylesheet = assets / "theme.css"
    stylesheet.write_bytes(b"tampered")
    capture_path = run_dir / "capture.json"
    capture = json.loads(capture_path.read_text("utf-8"))
    capture["asset_map"] = {
        "https://fixture.example/theme.css": "assets/theme.css"
    }
    capture["critical_assets"] = ["assets/theme.css"]
    capture["fingerprints"] = {
        "assets/theme.css": hashlib.sha256(b"original").hexdigest(),
        "source.html": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    capture_path.write_text(json.dumps(capture) + "\n", encoding="utf-8")

    assert (
        main(
            [
                "assemble",
                "--run-dir",
                str(run_dir),
                "--output-dir",
                str(tmp_path / "output"),
            ]
        )
        == 4
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["exit_code"] == 4
    assert "fingerprint does not match" in output.err


def test_qa_rejects_review_that_does_not_cover_every_zone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    write_single_segment_run_contract(run_dir)
    (run_dir / "review.json").write_text(
        '{"unresolved_required":[],"retries":{},"section_findings":{}}\n',
        encoding="utf-8",
    )

    assert (
        main(
            [
                "qa",
                "--run-dir",
                str(run_dir),
                "--output-dir",
                str(tmp_path / "output"),
            ]
        )
        == 4
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["exit_code"] == 4
    assert "review retries must exactly cover planned zones" in output.err


@pytest.mark.parametrize(
    "review,diagnostic",
    [
        (
            {
                "unresolved_required": [],
                "retries": {"zone-001": 3},
                "section_findings": {},
            },
            "integers from 0 through 2",
        ),
        (
            {
                "unresolved_required": [],
                "retries": {"zone-001": 0},
                "section_findings": {"zone-999": ["foreign"]},
            },
            "foreign zones",
        ),
    ],
)
def test_qa_rejects_invalid_review_zone_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    review: dict[str, object],
    diagnostic: str,
) -> None:
    run_dir = tmp_path / "run"
    write_single_segment_run_contract(run_dir)
    (run_dir / "review.json").write_text(
        json.dumps(review) + "\n", encoding="utf-8"
    )

    assert (
        main(
            [
                "qa",
                "--run-dir",
                str(run_dir),
                "--output-dir",
                str(tmp_path / "output"),
            ]
        )
        == 4
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["exit_code"] == 4
    assert diagnostic in output.err


def test_extract_rejects_linked_source_without_modifying_its_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside.html"
    original = "<html><body><p>Do not modify</p></body></html>"
    outside.write_text(original, encoding="utf-8")
    try:
        (run_dir / "source.html").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable on this Windows host: {error}")

    assert main(["extract", "--run-dir", str(run_dir)]) == 4
    assert_error_status(capsys, command="extract", exit_code=4)
    assert outside.read_text("utf-8") == original


def test_extract_atomically_replaces_hardlink_without_modifying_other_name(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside.html"
    original = "<html><body><p>OAuth</p></body></html>"
    outside.write_text(original, encoding="utf-8")
    os.link(outside, run_dir / "source.html")
    (run_dir / "capture.json").write_text(
        json.dumps(
            {
                "asset_map": {},
                "critical_assets": [],
                "final_url": "https://fixture.example/",
                "fingerprints": {
                    "source.html": hashlib.sha256(original.encode()).hexdigest()
                },
                "missing_optional_assets": [],
                "optional_assets": [],
                "requested_url": "https://fixture.example/",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["extract", "--run-dir", str(run_dir)]) == 0
    assert outside.read_text("utf-8") == original
    assert 'data-wt-segment="seg-000001"' in (
        run_dir / "source.html"
    ).read_text("utf-8")
    capture = json.loads((run_dir / "capture.json").read_text("utf-8"))
    assert capture["fingerprints"]["source.html"] == hashlib.sha256(
        (run_dir / "source.html").read_bytes()
    ).hexdigest()


def write_extractable_run(run_dir: Path) -> tuple[Path, Path]:
    run_dir.mkdir()
    source = run_dir / "source.html"
    source.write_text("<html><body><p>OAuth</p></body></html>", encoding="utf-8")
    capture_path = run_dir / "capture.json"
    capture_path.write_text(
        json.dumps(
            {
                "asset_map": {},
                "critical_assets": [],
                "final_url": "https://fixture.example/",
                "fingerprints": {
                    "source.html": hashlib.sha256(source.read_bytes()).hexdigest()
                },
                "missing_optional_assets": [],
                "optional_assets": [],
                "requested_url": "https://fixture.example/",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return source, capture_path


def test_extract_rejects_directory_segment_destination_without_partial_update(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    source, capture_path = write_extractable_run(run_dir)
    original_source = source.read_bytes()
    original_capture = capture_path.read_bytes()
    (run_dir / "segments.jsonl").mkdir()

    assert main(["extract", "--run-dir", str(run_dir)]) == 4
    assert_error_status(capsys, command="extract", exit_code=4)
    assert source.read_bytes() == original_source
    assert capture_path.read_bytes() == original_capture
    assert (run_dir / "segments.jsonl").is_dir()


def test_extract_rolls_back_all_files_when_metadata_publication_fails_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    source, capture_path = write_extractable_run(run_dir)
    segments = run_dir / "segments.jsonl"
    segments.write_text("original segments\n", encoding="utf-8")
    originals = (source.read_bytes(), segments.read_bytes(), capture_path.read_bytes())
    real_replace = cli_module.os.replace
    failed = False

    def fail_capture_once(source_path: object, destination_path: object) -> None:
        nonlocal failed
        if Path(destination_path) == capture_path and not failed:
            failed = True
            raise OSError("injected capture metadata publication failure")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(cli_module.os, "replace", fail_capture_once)

    assert main(["extract", "--run-dir", str(run_dir)]) == 4
    assert_error_status(capsys, command="extract", exit_code=4)
    assert (source.read_bytes(), segments.read_bytes(), capture_path.read_bytes()) == originals


def test_assembly_failure_returns_assembly_exit_code_and_one_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    write_empty_run_contract(run_dir)

    assert (
        main(
            [
                "assemble",
                "--run-dir",
                str(run_dir),
                "--output-dir",
                str(tmp_path / "output"),
            ]
        )
        == 5
    )
    assert_error_status(capsys, command="assemble", exit_code=5)


def test_failed_qa_writes_reports_and_returns_qa_exit_code_and_one_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    output_dir = tmp_path / "output"
    write_empty_run_contract(run_dir)
    (run_dir / "review.json").write_text(
        '{"unresolved_required":[],"retries":{},"section_findings":{}}\n',
        encoding="utf-8",
    )

    assert (
        main(
            [
                "qa",
                "--run-dir",
                str(run_dir),
                "--output-dir",
                str(output_dir),
            ]
        )
        == 6
    )
    assert_error_status(capsys, command="qa", exit_code=6)
    assert json.loads((output_dir / "manifest.json").read_text("utf-8"))[
        "qa_status"
    ] == "failed"
    assert (output_dir / "review-report.md").is_file()


def test_qa_report_io_failure_returns_qa_exit_code_and_one_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    write_empty_run_contract(run_dir)
    (run_dir / "review.json").write_text(
        '{"unresolved_required":[],"retries":{},"section_findings":{}}\n',
        encoding="utf-8",
    )
    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory", encoding="utf-8")

    assert (
        main(
            [
                "qa",
                "--run-dir",
                str(run_dir),
                "--output-dir",
                str(occupied / "output"),
            ]
        )
        == 6
    )
    assert_error_status(capsys, command="qa", exit_code=6)
