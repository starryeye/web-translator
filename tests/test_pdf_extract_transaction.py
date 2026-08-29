from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

import pytest

from tests.pdf_fixtures import make_text_pdf
from web_translator.pdf_acquire import acquire_pdf
from web_translator.pdf_extract import PdfExtractionError
from web_translator.pdf_extract_transaction import extract_pdf_transaction


Extractor = Callable[[Path, Path, Path, Path], object]


def _acquired_run(tmp_path: Path) -> Path:
    source = make_text_pdf(tmp_path / "source.pdf")
    run_dir = tmp_path / "run"

    def write_metadata(record: object, path: Path) -> None:
        path.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n",  # type: ignore[attr-defined]
            encoding="utf-8",
        )

    acquire_pdf(str(source), run_dir, metadata_writer=write_metadata)
    return run_dir


def _write_staged_outputs(
    _source: Path,
    document: Path,
    segments: Path,
    media: Path,
) -> None:
    document.write_text('{"document":"staged"}\n', encoding="utf-8")
    segments.write_text('{"segment":"staged"}\n', encoding="utf-8")
    media.mkdir()
    (media / "figure-0001.png").write_bytes(b"staged figure")


def test_pdf_extract_transaction_publishes_only_complete_held_outputs(
    tmp_path: Path,
) -> None:
    run_dir = _acquired_run(tmp_path)

    extract_pdf_transaction(run_dir, extractor=_write_staged_outputs)

    assert (run_dir / "document.json").read_text(encoding="utf-8") == (
        '{"document":"staged"}\n'
    )
    assert (run_dir / "segments.jsonl").read_text(encoding="utf-8") == (
        '{"segment":"staged"}\n'
    )
    assert (run_dir / "media" / "figure-0001.png").read_bytes() == b"staged figure"
    assert not list(run_dir.glob(".pdf-extracting-*"))


def test_pdf_extract_transaction_rejects_run_replacement_and_preserves_racer(
    tmp_path: Path,
) -> None:
    run_dir = _acquired_run(tmp_path)
    held_run = tmp_path / "held-run"

    def replace_run(
        source: Path,
        document: Path,
        segments: Path,
        media: Path,
    ) -> None:
        _write_staged_outputs(source, document, segments, media)
        run_dir.rename(held_run)
        run_dir.mkdir()
        (run_dir / "keep.txt").write_text("racer", encoding="utf-8")

    with pytest.raises(PdfExtractionError, match="run.*changed identity"):
        extract_pdf_transaction(run_dir, extractor=replace_run)

    assert (run_dir / "keep.txt").read_text(encoding="utf-8") == "racer"
    assert not (run_dir / "document.json").exists()


def test_pdf_extract_transaction_rejects_source_replacement_after_held_read(
    tmp_path: Path,
) -> None:
    run_dir = _acquired_run(tmp_path)
    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes((run_dir / "source.pdf").read_bytes() + b"\n")
    replacement_bytes = replacement.read_bytes()

    def replace_source(
        source: Path,
        document: Path,
        segments: Path,
        media: Path,
    ) -> None:
        _write_staged_outputs(source, document, segments, media)
        os.replace(replacement, run_dir / "source.pdf")

    with pytest.raises(PdfExtractionError, match="changed identity"):
        extract_pdf_transaction(run_dir, extractor=replace_source)

    assert (run_dir / "source.pdf").read_bytes() == replacement_bytes
    assert not (run_dir / "document.json").exists()


@pytest.mark.parametrize("racer_name", ["document.json", "segments.jsonl", "media"])
def test_pdf_extract_transaction_never_clobbers_late_destination_racers(
    tmp_path: Path,
    racer_name: str,
) -> None:
    run_dir = _acquired_run(tmp_path)

    def race_destination(
        source: Path,
        document: Path,
        segments: Path,
        media: Path,
    ) -> None:
        _write_staged_outputs(source, document, segments, media)
        racer = run_dir / racer_name
        if racer_name == "media":
            racer.mkdir()
            (racer / "keep.txt").write_text("foreign media", encoding="utf-8")
        else:
            racer.write_text(f"foreign {racer_name}", encoding="utf-8")

    with pytest.raises(PdfExtractionError, match="already exists"):
        extract_pdf_transaction(run_dir, extractor=race_destination)

    if racer_name == "media":
        assert (run_dir / "media" / "keep.txt").read_text(encoding="utf-8") == (
            "foreign media"
        )
    else:
        assert (run_dir / racer_name).read_text(encoding="utf-8") == (
            f"foreign {racer_name}"
        )
    for name in {"document.json", "segments.jsonl", "media"} - {racer_name}:
        assert not (run_dir / name).exists()


def test_pdf_extract_transaction_preserves_existing_destination_set(
    tmp_path: Path,
) -> None:
    run_dir = _acquired_run(tmp_path)
    (run_dir / "document.json").write_text("old document", encoding="utf-8")
    (run_dir / "segments.jsonl").write_text("old segments", encoding="utf-8")
    (run_dir / "media").mkdir()
    (run_dir / "media" / "keep.txt").write_text("old media", encoding="utf-8")

    with pytest.raises(PdfExtractionError, match="already exists"):
        extract_pdf_transaction(run_dir, extractor=_write_staged_outputs)

    assert (run_dir / "document.json").read_text(encoding="utf-8") == "old document"
    assert (run_dir / "segments.jsonl").read_text(encoding="utf-8") == "old segments"
    assert (run_dir / "media" / "keep.txt").read_text(encoding="utf-8") == "old media"
