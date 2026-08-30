from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
from urllib.parse import urljoin

import pytest
import httpx
from bs4 import BeautifulSoup

from web_translator import __version__
import web_translator.capture as capture_module
import web_translator.cli as cli_module
import web_translator.network as network_module
from web_translator.capture import CaptureError, capture_page
from web_translator.cli import main
from web_translator.models import MasterReview, QAResult


TRANSLATED_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "translated"
REPRESENTATIVE_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "representative"
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
REVIEW_DIMENSIONS = (
    "semantic_fidelity",
    "qualification_preservation",
    "naturalness",
    "terminology",
    "boundary_consistency",
    "protected_content",
)


def _web_cli_paths(tmp_path: Path, run_id: str = "run") -> tuple[Path, Path]:
    run_dir = tmp_path / ".web-translator" / "runs" / run_id
    output_dir = tmp_path / "translated-pages" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    return run_dir, output_dir


def reviewed_zone_findings() -> list[dict[str, str]]:
    return [
        {
            "dimension": dimension,
            "verdict": "pass",
            "evidence": f"Fixture reviewer compared {dimension} with the source and approved it.",
        }
        for dimension in REVIEW_DIMENSIONS
    ]


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
        network_module,
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
    reviewed = [
        json.loads(line)
        for line in (TRANSLATED_FIXTURE_DIR / "zone-001.jsonl")
        .read_text("utf-8")
        .splitlines()
        if line.strip()
    ]
    segments = [
        json.loads(line)
        for line in (run_dir / "segments.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    translated: list[dict[str, object]] = []
    for segment in segments:
        if not segment["target"]:
            continue
        if segment["semantic_type"] == "located:attributes":
            token = segment["protected"][0]["token"]
            translated.append(
                {
                    "segment_id": segment["id"],
                    "text": f"{token}픽스처 로고",
                    "notes": None,
                    "glossary_observations": {},
                }
            )
            continue
        if segment["semantic_type"] == "heading":
            template = reviewed[0]
        elif "before retrying" in segment["source_text"]:
            template = reviewed[1]
        else:
            template = reviewed[2]
        translated.append({**template, "segment_id": segment["id"]})
    translations = run_dir / "translations"
    translations.mkdir()
    (translations / "zone-001.jsonl").write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in translated
        ),
        encoding="utf-8",
    )
    (run_dir / "review.json").write_text(
        json.dumps(
            {
                "unresolved_required": [],
                "retries": {"zone-001": 0},
                "section_findings": {"zone-001": reviewed_zone_findings()},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_fixture_pipeline_builds_complete_offline_bundle(
    tmp_path: Path, fixture_server: FixtureServer, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "작업 공간"
    run_dir, output_dir = _web_cli_paths(workspace, "fixture")

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
    screenshot_dir = run_dir / "qa-screenshots"
    screenshot_dir.mkdir()
    screenshot_sentinels: list[Path] = []
    for filename in ("desktop-1440x900.png", "narrow-390x844.png"):
        sentinel = tmp_path / f"outside-{filename}"
        sentinel.write_bytes(f"sentinel:{filename}".encode())
        os.link(sentinel, screenshot_dir / filename)
        screenshot_sentinels.append(sentinel)
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
    assert set(manifest) == {
        "assets",
        "browser_metrics",
        "capture",
        "capture_metadata",
        "coverage",
        "languages",
        "qa_status",
        "required_findings",
        "retries",
        "schema_version",
        "screenshots",
        "source_url",
        "terminology_policy",
        "tool",
        "warnings",
    }
    assert manifest["schema_version"] == "1.0"
    assert manifest["tool"] == {"name": "web-translator", "version": __version__}
    assert manifest["capture"] == {
        "captured_at": capture["captured_at"],
        "final_url": capture["final_url"],
        "requested_url": capture["requested_url"],
    }
    assert manifest["capture"]["captured_at"].endswith("Z")
    assert manifest["languages"] == {"source": "en", "target": "ko"}
    assert manifest["terminology_policy"] == {
        "id": "english-technical-first-use-ko-gloss",
        "version": "1.0",
    }
    persisted_segments = [
        json.loads(line)
        for line in (run_dir / "segments.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    zone_files = sorted((run_dir / "zones").glob("zone-*.json"))
    assert manifest["coverage"] == {
        "segments": len(persisted_segments),
        "target_segments": sum(segment["target"] for segment in persisted_segments),
        "translated_segments": sum(segment["target"] for segment in persisted_segments),
        "zones": len(zone_files),
    }
    assert manifest["retries"] == {"zone-001": 0}
    expected_assets = [
        {
            "classification": (
                "critical" if local_path in capture["critical_assets"] else "optional"
            ),
            "local_path": local_path,
            "sha256": capture["fingerprints"][local_path],
            "source": source,
        }
        for source, local_path in sorted(capture["asset_map"].items())
    ]
    assert manifest["assets"] == {
        "captured": expected_assets,
        "missing_optional": capture["missing_optional_assets"],
    }
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
    for sentinel in screenshot_sentinels:
        filename = sentinel.name.removeprefix("outside-")
        screenshot = screenshot_dir / filename
        assert sentinel.read_bytes() == f"sentinel:{filename}".encode()
        assert not os.path.samefile(sentinel, screenshot)
        assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    translated_html = (output_dir / "index.html").read_text("utf-8")
    assert translated_html.count("OAuth(오픈 인증)") == 1
    assert translated_html.count("Token Exchange(토큰 교환)") == 1
    assert "MUST" in translated_html
    assert "<code>git status</code> 명령을 실행한 뒤" in translated_html


def materialize_representative_snapshot(snapshot_dir: Path, run_dir: Path) -> dict[str, object]:
    metadata = json.loads((snapshot_dir / "snapshot.json").read_text("utf-8"))
    run_dir.mkdir(parents=True, exist_ok=True)
    source = run_dir / "source.html"
    shutil.copy2(snapshot_dir / "index.html", source)
    assets = run_dir / "assets"
    assets.mkdir()
    css = assets / "theme.css"
    shutil.copy2(snapshot_dir / "theme.css", css)
    asset_source = urljoin(str(metadata["final_url"]), "theme.css")
    capture = {
        "asset_map": {asset_source: "assets/theme.css"},
        "captured_at": metadata["captured_at"],
        "critical_assets": ["assets/theme.css"],
        "final_url": metadata["final_url"],
        "fingerprints": {
            "assets/theme.css": hashlib.sha256(css.read_bytes()).hexdigest(),
            "source.html": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "missing_optional_assets": [],
        "optional_assets": [],
        "requested_url": metadata["final_url"],
    }
    (run_dir / "capture.json").write_text(
        json.dumps(capture, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return metadata


def write_snapshot_translations_and_review(snapshot_dir: Path, run_dir: Path) -> None:
    rules = json.loads((snapshot_dir / "translations.json").read_text("utf-8"))
    segments = {
        record["id"]: record
        for record in (
            json.loads(line)
            for line in (run_dir / "segments.jsonl").read_text("utf-8").splitlines()
            if line.strip()
        )
    }
    zones = [
        json.loads(path.read_text("utf-8"))
        for path in sorted((run_dir / "zones").glob("zone-*.json"))
    ]
    translations = run_dir / "translations"
    translations.mkdir()
    for zone in zones:
        records: list[dict[str, object]] = []
        for segment_id in zone["target_ids"]:
            segment = segments[segment_id]
            matches = [rule for rule in rules if rule["contains"] in segment["source_text"]]
            assert matches, f"snapshot lacks a translation rule for {segment['source_text']!r}"
            rule = max(matches, key=lambda item: len(item["contains"]))
            text = rule["translation"]
            for index, protected in enumerate(segment["protected"]):
                text = text.replace(f"{{protected_{index}}}", protected["token"])
            assert "{protected_" not in text
            records.append(
                {
                    "segment_id": segment_id,
                    "text": text,
                    "notes": None,
                    "glossary_observations": {},
                }
            )
        (translations / f"{zone['id']}.jsonl").write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
    shutil.copy2(snapshot_dir / "glossary.json", run_dir / "glossary.json")
    review = {
        "unresolved_required": [],
        "retries": {zone["id"]: 0 for zone in zones},
        "section_findings": {
            zone["id"]: [
                {
                    "dimension": dimension,
                    "verdict": "pass",
                    "evidence": (
                        f"Snapshot reviewer compared {dimension} for every target in "
                        f"{zone['id']} with its source and neighboring section context."
                    ),
                }
                for dimension in REVIEW_DIMENSIONS
            ]
            for zone in zones
        },
    }
    (run_dir / "review.json").write_text(
        json.dumps(review, ensure_ascii=False) + "\n", encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("snapshot_name", "expected_phrases", "glosses"),
    [
        (
            "spring-ai-concepts-v1",
            ["이식 가능한 인터페이스", "공급자별 선택 사항", "지시와 문맥"],
            {"Spring AI": "스프링 AI", "Model API": "모델 API"},
        ),
        (
            "rfc8693-v1",
            ["보안 토큰을 요청", "권한 부여 서버", "보안 요구사항을 정의"],
            {"Token Exchange": "토큰 교환", "Security Token Service": "보안 토큰 서비스"},
        ),
    ],
)
def test_versioned_representative_snapshot_runs_complete_reviewed_pipeline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    snapshot_name: str,
    expected_phrases: list[str],
    glosses: dict[str, str],
) -> None:
    snapshot_dir = REPRESENTATIVE_FIXTURE_DIR / snapshot_name
    workspace = tmp_path / "대표 문서 작업" / snapshot_name
    run_dir, output_dir = _web_cli_paths(workspace, snapshot_name)
    metadata = materialize_representative_snapshot(snapshot_dir, run_dir)

    assert main(["extract", "--run-dir", str(run_dir)]) == 0
    assert main(["plan-zones", "--run-dir", str(run_dir), "--max-chars", "240"]) == 0
    write_snapshot_translations_and_review(snapshot_dir, run_dir)
    assert main(["validate-translations", "--run-dir", str(run_dir)]) == 0
    assert main(["assemble", "--run-dir", str(run_dir), "--output-dir", str(output_dir)]) == 0
    assert main(["qa", "--run-dir", str(run_dir), "--output-dir", str(output_dir)]) == 0

    translated_html = (output_dir / "index.html").read_text("utf-8")
    assert re.search(r"[가-힣]", translated_html)
    assert all(phrase in translated_html for phrase in expected_phrases)
    for term, gloss in glosses.items():
        assert translated_html.count(term) >= 1
        assert translated_html.count(f"{term}({gloss})") == 1
    assert "theme.css" in translated_html
    translated_soup = BeautifulSoup(translated_html, "html.parser")
    for element in translated_soup.find_all(True):
        for attribute in ("href", "src"):
            value = element.get(attribute)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                assert element.name == "a"
                assert element.find_parent(attrs={"data-wt-attribution": "source"})

    manifest = json.loads((output_dir / "manifest.json").read_text("utf-8"))
    assert manifest["qa_status"] == "passed"
    assert manifest["capture"]["final_url"] == metadata["final_url"]
    assert manifest["languages"] == {"source": "en", "target": "ko"}
    assert manifest["coverage"]["zones"] >= 2
    assert manifest["coverage"]["target_segments"] == manifest["coverage"][
        "translated_segments"
    ]
    assert set(manifest["browser_metrics"]) == {
        "desktop-1440x900",
        "narrow-390x844",
    }
    assert {Path(path).name for path in manifest["screenshots"]} == {
        "desktop-1440x900.png",
        "narrow-390x844.png",
    }
    output = capsys.readouterr()
    assert output.err == ""
    assert all(json.loads(line)["status"] == "ok" for line in output.out.splitlines())


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
    run_dir, _output_dir = _web_cli_paths(tmp_path)
    assert main(["capture", "file:///private", "--run-dir", str(run_dir)]) == 2
    assert_error_status(capsys, command="capture", exit_code=2)


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_unexpected_handler_errors_escape_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    def fail_unexpectedly(run_dir: Path) -> None:
        raise error_type("simulated programming bug")

    monkeypatch.setattr(cli_module, "_validate_run_root", fail_unexpectedly)
    run_dir, _output_dir = _web_cli_paths(tmp_path)

    with pytest.raises(error_type, match="simulated programming bug"):
        main(["extract", "--run-dir", str(run_dir)])


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

    monkeypatch.setattr(network_module, "_resolve_public_addresses", fail_resolution)
    run_dir, _output_dir = _web_cli_paths(tmp_path)

    assert (
        main(
            [
                "capture",
                "https://fixture.example/",
                "--run-dir",
                str(run_dir),
            ]
        )
        == 3
    )
    assert_error_status(capsys, command="capture", exit_code=3)


def test_capture_persists_semantic_asset_classes_with_alias_merging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = (
        '<html><head><link rel="stylesheet" href="alias.css">'
        '<style>.hero{background:url("font.css")}</style></head>'
        '<body><img src="critical-target"></body></html>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        responses: dict[str, httpx.Response] = {
            "https://fixture.example/": httpx.Response(
                200, text=html, headers={"content-type": "text/html"}
            ),
            "https://fixture.example/alias.css": httpx.Response(
                302, headers={"location": "/critical-target"}
            ),
            "https://fixture.example/critical-target": httpx.Response(
                200, content=b"body{}", headers={"content-type": "text/css"}
            ),
            "https://fixture.example/font.css": httpx.Response(
                200,
                content=b"optional non-css bytes",
                headers={"content-type": "application/octet-stream"},
            ),
        }
        return responses[str(request.url)]

    monkeypatch.setattr(
        network_module,
        "_resolve_public_addresses",
        lambda host, port: ["93.184.216.34"],
    )
    result = capture_page(
        "https://fixture.example/",
        tmp_path,
        transport=httpx.MockTransport(handler),
    )
    critical = result.asset_map["https://fixture.example/critical-target"]
    optional = result.asset_map["https://fixture.example/font.css"]

    assert result.asset_map["https://fixture.example/alias.css"] == critical
    assert result.critical_assets == [critical]
    assert result.optional_assets == [optional]
    assert Path(optional).suffix == ".css"


@pytest.mark.parametrize("stylesheet_reference", ["/target", "/alias.css"])
def test_capture_rejects_optional_asset_cache_promotion_to_stylesheet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stylesheet_reference: str,
) -> None:
    html = (
        '<html><head><style>@import url("'
        + stylesheet_reference
        + '");</style></head><body><img src="/target"></body></html>'
    )
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://fixture.example/":
            return httpx.Response(
                200, text=html, headers={"content-type": "text/html"}
            )
        if str(request.url) == "https://fixture.example/alias.css":
            return httpx.Response(302, headers={"location": "/target"})
        return httpx.Response(
            200, content=svg, headers={"content-type": "image/svg+xml"}
        )

    monkeypatch.setattr(
        network_module,
        "_resolve_public_addresses",
        lambda host, port: ["93.184.216.34"],
    )

    with pytest.raises(CaptureError, match="optional asset as a critical stylesheet"):
        capture_page(
            "https://fixture.example/",
            tmp_path,
            transport=httpx.MockTransport(handler),
        )


def test_capture_rejects_nonempty_run_directory_before_network_or_overwrite(
    tmp_path: Path,
    fixture_server: FixtureServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
    run_dir.mkdir(exist_ok=True)
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
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
    run_dir.mkdir(exist_ok=True)
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
    run_dir.mkdir(parents=True, exist_ok=True)
    source = run_dir / "source.html"
    source.write_text(
        "<html><head></head><body></body></html>", encoding="utf-8"
    )
    (run_dir / "capture.json").write_text(
        json.dumps(
            {
                "requested_url": "https://fixture.example/",
                "final_url": "https://fixture.example/",
                "captured_at": "2026-08-12T00:00:00Z",
                "asset_map": {},
                "critical_assets": [],
                "fingerprints": {
                    "source.html": hashlib.sha256(source.read_bytes()).hexdigest()
                },
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
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
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
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
    write_empty_run_contract(run_dir)
    (run_dir / "translations" / "stale.jsonl").write_text("", encoding="utf-8")

    assert main(["validate-translations", "--run-dir", str(run_dir)]) == 4
    output = capsys.readouterr()
    assert json.loads(output.out)["exit_code"] == 4
    assert "unexpected translation entries" in output.err


def test_validate_one_completed_zone_before_other_results_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
    write_empty_run_contract(run_dir)
    segments = [
        {
            "id": f"seg-{index:06d}",
            "locator": f"[data-wt-segment='seg-{index:06d}']",
            "semantic_type": "paragraph",
            "heading_path": [],
            "source_text": f"Source {index}",
            "protected": [],
            "context_ids": [],
            "target": True,
        }
        for index in (1, 2)
    ]
    (run_dir / "segments.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in segments), encoding="utf-8"
    )
    for index in (1, 2):
        zone_id = f"zone-{index:03d}"
        segment_id = f"seg-{index:06d}"
        (run_dir / "zones" / f"{zone_id}.json").write_text(
            json.dumps(
                {
                    "attempt": 0,
                    "context_after_ids": [],
                    "context_before_ids": [],
                    "expected_tokens": {segment_id: []},
                    "heading_path": [],
                    "id": zone_id,
                    "target_ids": [segment_id],
                }
            )
            + "\n",
            encoding="utf-8",
        )
    (run_dir / "translations" / "zone-001.jsonl").write_text(
        '{"segment_id":"seg-000001","text":"번역 1","notes":null,"glossary_observations":{}}\n',
        encoding="utf-8",
    )

    assert (
        main(
            [
                "validate-translations",
                "--run-dir",
                str(run_dir),
                "--zone-id",
                "zone-001",
            ]
        )
        == 0
    )
    assert main(["validate-translations", "--run-dir", str(run_dir)]) == 4
    statuses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [status["status"] for status in statuses] == ["ok", "error"]


def test_prepare_assignments_builds_bounded_immutable_zone_packages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
    write_single_segment_run_contract(run_dir)
    (run_dir / "document-summary.txt").write_text(
        "OAuth 문서의 목적과 흐름", encoding="utf-8"
    )
    (run_dir / "glossary.json").write_text(
        '{"OAuth":"권한 위임"}\n', encoding="utf-8"
    )

    assert main(["prepare-assignments", "--run-dir", str(run_dir)]) == 0

    package = json.loads(
        (run_dir / "assignments" / "zone-001.json").read_text("utf-8")
    )
    assert package == {
        "context_after": [],
        "context_before": [],
        "document_summary": "OAuth 문서의 목적과 흐름",
        "glossary": {"OAuth": "권한 위임"},
        "schema_version": "1.0",
        "targets": [
            {
                "heading_path": [],
                "id": "seg-000001",
                "protected": [],
                "semantic_type": "paragraph",
                "source_text": "OAuth",
            }
        ],
        "zone_id": "zone-001",
    }
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


@pytest.mark.parametrize("summary", ["", "X" * 4_001])
def test_prepare_assignments_rejects_missing_or_oversized_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    summary: str,
) -> None:
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
    write_single_segment_run_contract(run_dir)
    (run_dir / "document-summary.txt").write_text(summary, encoding="utf-8")

    assert main(["prepare-assignments", "--run-dir", str(run_dir)]) == 4
    assert "document summary" in capsys.readouterr().err


def test_validate_one_zone_rejects_unknown_zone_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
    write_single_segment_run_contract(run_dir)

    assert (
        main(
            [
                "validate-translations",
                "--run-dir",
                str(run_dir),
                "--zone-id",
                "zone-999",
            ]
        )
        == 4
    )
    assert "unknown zone ID" in capsys.readouterr().err


def test_assemble_rejects_unsafe_capture_asset_paths_as_contract_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
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
                str(allocated_output_dir),
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
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
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
                str(allocated_output_dir),
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
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
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
                str(allocated_output_dir),
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
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
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
                str(allocated_output_dir),
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
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
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
                str(allocated_output_dir),
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
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
    run_dir.mkdir(exist_ok=True)
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
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
    run_dir.mkdir(exist_ok=True)
    outside = tmp_path / "outside.html"
    original = "<html><body><p>OAuth</p></body></html>"
    outside.write_text(original, encoding="utf-8")
    os.link(outside, run_dir / "source.html")
    (run_dir / "capture.json").write_text(
        json.dumps(
                {
                    "asset_map": {},
                    "captured_at": "2026-08-12T00:00:00Z",
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
    run_dir.mkdir(exist_ok=True)
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
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
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
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
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
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
    output_dir = allocated_output_dir
    write_empty_run_contract(run_dir)
    output_dir.mkdir()

    assert (
        main(
            [
                "assemble",
                "--run-dir",
                str(run_dir),
                "--output-dir",
                str(output_dir),
            ]
        )
        == 5
    )
    assert_error_status(capsys, command="assemble", exit_code=5)


@pytest.mark.parametrize("command", ["assemble", "qa"])
def test_missing_captured_source_is_a_contract_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    run_dir, output_dir = _web_cli_paths(tmp_path, command)
    write_empty_run_contract(run_dir)
    (run_dir / "source.html").unlink()
    (run_dir / "review.json").write_text(
        '{"unresolved_required":[],"retries":{},"section_findings":{}}\n',
        encoding="utf-8",
    )

    assert (
        main(
            [
                command,
                "--run-dir",
                str(run_dir),
                "--output-dir",
                str(output_dir),
            ]
        )
        == 4
    )
    assert_error_status(capsys, command=command, exit_code=4)


def test_failed_qa_writes_reports_and_returns_qa_exit_code_and_one_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
    output_dir = allocated_output_dir
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
    run_dir, allocated_output_dir = _web_cli_paths(tmp_path)
    write_empty_run_contract(run_dir)
    (run_dir / "review.json").write_text(
        '{"unresolved_required":[],"retries":{},"section_findings":{}}\n',
        encoding="utf-8",
    )
    allocated_output_dir.mkdir()
    (allocated_output_dir / "review-report.md").mkdir()

    assert (
        main(
            [
                "qa",
                "--run-dir",
                str(run_dir),
                "--output-dir",
                str(allocated_output_dir),
            ]
        )
        == 6
    )
    assert_error_status(capsys, command="qa", exit_code=6)


def passing_qa_result(source_url: str) -> QAResult:
    return QAResult(
        passed=True,
        required_findings=[],
        warnings=[],
        screenshots=[],
        source_url=source_url,
    )


def empty_master_review() -> MasterReview:
    return MasterReview(
        unresolved_required=[], retries={}, section_findings={}
    )


def test_qa_evidence_report_failure_never_publishes_passing_manifest(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "review-report.md").mkdir()

    with pytest.raises(cli_module.QAFailure, match="QA evidence"):
        cli_module._publish_qa_evidence(
            passing_qa_result("https://fixture.example/new"),
            empty_master_review(),
            output_dir,
        )

    assert not (output_dir / "manifest.json").exists()


def test_qa_evidence_transaction_restores_previous_pair_on_manifest_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    cli_module._publish_qa_evidence(
        passing_qa_result("https://fixture.example/old"),
        empty_master_review(),
        output_dir,
    )
    manifest = output_dir / "manifest.json"
    report = output_dir / "review-report.md"
    old_pair = (manifest.read_bytes(), report.read_bytes())
    real_replace = cli_module.os.replace
    failed = False

    def fail_new_manifest_once(source_path: object, destination_path: object) -> None:
        nonlocal failed
        if Path(destination_path) == manifest and not failed:
            failed = True
            raise OSError("injected manifest publication failure")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(cli_module.os, "replace", fail_new_manifest_once)

    with pytest.raises(cli_module.QAFailure, match="QA evidence"):
        cli_module._publish_qa_evidence(
            passing_qa_result("https://fixture.example/new"),
            empty_master_review(),
            output_dir,
        )

    assert (manifest.read_bytes(), report.read_bytes()) == old_pair
