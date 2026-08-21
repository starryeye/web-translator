from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import socket
import stat

import httpx
import pytest

import web_translator.pdf_acquire as acquire_module
from web_translator.pdf_acquire import PdfAcquireError, acquire_pdf


FIXED_TIME = datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC)
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(
        host: str, port: int, *args: object, **kwargs: object
    ) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def test_acquire_local_pdf_copies_to_fresh_inode_and_records_private_provenance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "입력 자료" / "보고서.pdf"
    source.parent.mkdir()
    source.write_bytes(PDF)
    run_dir = tmp_path / "실행 공간"

    record = acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    copied = run_dir / "source.pdf"
    assert copied.read_bytes() == PDF
    assert not os.path.samefile(copied, source)
    assert record.input_kind == "local"
    assert record.requested_source == "보고서.pdf"
    assert record.final_source == "보고서.pdf"
    assert record.content_type == "application/pdf"
    assert record.byte_length == len(PDF)
    assert record.acquired_at == "2026-08-21T01:02:03Z"
    assert record.redirects == []
    assert record.warnings == []


@pytest.mark.parametrize("kind", ["link", "directory"])
def test_acquire_local_pdf_rejects_links_and_non_regular_files(
    tmp_path: Path, kind: str
) -> None:
    source = tmp_path / "source.pdf"
    if kind == "link":
        target = tmp_path / "target.pdf"
        target.write_bytes(PDF)
        source.symlink_to(target)
    else:
        source.mkdir()

    with pytest.raises(PdfAcquireError, match="regular file|link or reparse"):
        acquire_pdf(str(source), tmp_path / "run", now=FIXED_TIME)


def test_acquire_local_pdf_rejects_non_pdf_signature_without_publishing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"not a PDF")
    run_dir = tmp_path / "run"

    with pytest.raises(PdfAcquireError, match="PDF signature"):
        acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    assert not (run_dir / "source.pdf").exists()


def test_acquire_local_pdf_rejects_file_when_identity_changes_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    original_fstat = acquire_module.os.fstat

    def changed_fstat(fd: int) -> os.stat_result:
        result = original_fstat(fd)
        values = list(result)
        values[stat.ST_INO] = result.st_ino + 1
        return os.stat_result(values)

    monkeypatch.setattr(acquire_module.os, "fstat", changed_fstat)

    with pytest.raises(PdfAcquireError, match="changed identity"):
        acquire_pdf(str(source), tmp_path / "run", now=FIXED_TIME)


def test_acquire_public_pdf_records_redirect_chain_and_accepts_generic_binary_type(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "start.example":
            return httpx.Response(302, headers={"location": "https://cdn.example/report.pdf"})
        return httpx.Response(
            200,
            content=PDF,
            headers={"content-type": "application/octet-stream"},
        )

    record = acquire_pdf(
        "https://start.example/report.pdf",
        tmp_path / "run",
        transport=httpx.MockTransport(handler),
        now=FIXED_TIME,
    )

    assert calls == ["https://start.example/report.pdf", "https://cdn.example/report.pdf"]
    assert record.input_kind == "public"
    assert record.requested_source == "https://start.example/report.pdf"
    assert record.final_source == "https://cdn.example/report.pdf"
    assert record.content_type == "application/octet-stream"
    assert record.redirects == ["https://start.example/report.pdf"]
    assert record.warnings == ["generic-content-type: application/octet-stream"]
    assert (tmp_path / "run" / "source.pdf").read_bytes() == PDF


def test_acquire_public_pdf_rejects_private_redirect_target_before_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[str] = []

    def resolve(
        host: str, port: int, *args: object, **kwargs: object
    ) -> list[tuple[object, ...]]:
        address = "10.0.0.7" if host == "private.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://private.example/report.pdf"})

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    with pytest.raises(PdfAcquireError, match="non-public DNS"):
        acquire_pdf(
            "https://start.example/report.pdf",
            tmp_path / "run",
            transport=httpx.MockTransport(handler),
            now=FIXED_TIME,
        )

    assert requested == ["https://start.example/report.pdf"]


def test_acquire_public_pdf_measures_streamed_content_not_lie_in_content_length(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=PDF,
            headers={"content-type": "application/pdf", "content-length": "1"},
        )

    record = acquire_pdf(
        "https://example.com/report.pdf",
        tmp_path / "run",
        transport=httpx.MockTransport(handler),
        now=FIXED_TIME,
    )

    assert record.byte_length == len(PDF)
    assert record.warnings == []
    assert (tmp_path / "run" / "source.pdf").read_bytes() == PDF


def test_acquire_public_pdf_stops_streaming_above_fifty_mebibytes(
    tmp_path: Path,
) -> None:
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield PDF
            yield b"x" * (50 * 1024 * 1024)

        def close(self) -> None:
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=Stream(),
            headers={"content-type": "application/pdf"},
        )

    with pytest.raises(PdfAcquireError, match="size limit|downloaded bytes"):
        acquire_pdf(
            "https://example.com/report.pdf",
            tmp_path / "run",
            transport=httpx.MockTransport(handler),
            now=FIXED_TIME,
        )

    assert not (tmp_path / "run" / "source.pdf").exists()
