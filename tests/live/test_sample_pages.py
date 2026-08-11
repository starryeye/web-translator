from pathlib import Path

import pytest

from web_translator.capture import capture_page
from web_translator.extract import extract_segments


SAMPLES = (
    "https://docs.spring.io/spring-ai/reference/concepts.html",
    "https://datatracker.ietf.org/doc/html/rfc8693",
)


@pytest.mark.live
@pytest.mark.parametrize("url", SAMPLES)
def test_approved_sample_page_can_be_captured_and_extracted(
    tmp_path: Path, url: str
) -> None:
    run_dir = tmp_path / ("rfc-8693" if "rfc8693" in url else "spring-ai")
    captured = capture_page(url, run_dir)

    html = captured.source_html.read_text("utf-8")
    assert "<html" in html.lower()
    assert len(html) >= 1_000

    segments = extract_segments(captured.source_html, run_dir / "segments.jsonl")
    targets = [segment for segment in segments if segment.target]
    assert targets
    if "rfc8693" in url:
        assert len(targets) >= 100
