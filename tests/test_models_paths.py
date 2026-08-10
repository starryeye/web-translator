from datetime import UTC, datetime
import json
from pathlib import Path
import re

import pytest

from web_translator.models import ProtectedToken, Segment, read_segments, write_segments
from web_translator.paths import create_run_paths, validate_public_url


def test_only_public_http_urls_are_accepted() -> None:
    assert str(validate_public_url("https://example.com/docs?a=1")) == "https://example.com/docs?a=1"
    for value in ("file:///C:/secret", "ftp://example.com/a", "http://localhost/a", "http://127.0.0.1/a"):
        with pytest.raises(ValueError):
            validate_public_url(value)


def test_run_paths_are_unique_and_windows_safe(tmp_path: Path) -> None:
    workspace = tmp_path / "\ud55c\uae00 workspace"
    now = datetime(2026, 8, 10, 12, 34, 56, tzinfo=UTC)
    paths = create_run_paths(workspace, "https://docs.example.com/a page", now)
    assert paths.output_dir.name == "docs.example.com-a-page-20260810-123456"
    assert paths.work_dir.is_relative_to(workspace / ".web-translator/runs")


def test_segment_jsonl_round_trip(tmp_path: Path) -> None:
    segment = Segment(
        id="seg-000001", locator="[data-wt-segment='seg-000001']",
        semantic_type="paragraph", heading_path=["Overview"],
        source_text="Use \u27e6WT:0\u27e7.",
        protected=[ProtectedToken(token="\u27e6WT:0\u27e7", kind="code", value="<code>JWT</code>")],
        context_ids=[], target=True,
    )
    path = tmp_path / "segments.jsonl"
    write_segments(path, [segment])
    assert read_segments(path) == [segment]


@pytest.mark.parametrize(
    "url",
    [
        "https:///missing-host",
        "http://::1/",
        "http://10.0.0.1/",
        "http://169.254.1.1/",
        "http://224.0.0.1/",
        "http://0.0.0.0/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://127.1/",
    ],
)
def test_private_or_malformed_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        validate_public_url(url)


@pytest.mark.parametrize("url", ["http://user@example.com/", "http://user:pass@example.com/"])
def test_credential_bearing_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        validate_public_url(url)


@pytest.mark.parametrize(
    ("record", "field"),
    [
        ("not an object", "record"),
        (
            {
                "id": "seg-000001",
                "locator": "[data-wt-segment='seg-000001']",
                "semantic_type": "paragraph",
                "heading_path": ["Overview", 1],
                "source_text": "Text",
                "protected": [],
                "context_ids": [],
                "target": True,
            },
            "Segment.heading_path[1]",
        ),
        (
            {
                "id": "seg-000001",
                "locator": "[data-wt-segment='seg-000001']",
                "semantic_type": "paragraph",
                "heading_path": [],
                "source_text": "Text",
                "protected": [{"token": "⟦WT:0⟧", "kind": 1, "value": "<code>JWT</code>"}],
                "context_ids": [],
                "target": True,
            },
            "Segment.protected[0].kind",
        ),
        (
            {
                "id": "seg-000001",
                "locator": "[data-wt-segment='seg-000001']",
                "semantic_type": "paragraph",
                "heading_path": [],
                "source_text": "Text",
                "protected": [],
                "context_ids": [],
                "target": "true",
            },
            "Segment.target",
        ),
    ],
)
def test_segment_jsonl_rejects_invalid_record_shapes(tmp_path: Path, record: object, field: str) -> None:
    path = tmp_path / "segments.jsonl"
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(field)):
        read_segments(path)


def test_existing_output_directory_is_not_overwritten(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 12, 34, 56, tzinfo=UTC)
    run_id = "example.com-docs-20260810-123456"
    output_dir = tmp_path / "translated-pages" / run_id
    output_dir.mkdir(parents=True)
    (output_dir / "keep.txt").write_text("existing output", encoding="utf-8")

    paths = create_run_paths(tmp_path, "https://example.com/docs", now)

    assert paths.output_dir != output_dir
    assert (output_dir / "keep.txt").read_text(encoding="utf-8") == "existing output"
    assert paths.work_dir.is_dir()
    assert not paths.output_dir.exists()
