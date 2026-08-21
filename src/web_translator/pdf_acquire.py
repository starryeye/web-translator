"""Fail-closed acquisition of one local or public PDF source."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from itertools import chain
import os
from pathlib import Path
import stat
import tempfile
from typing import BinaryIO, Literal
from urllib.parse import urlsplit

import httpx

from web_translator.assets import atomic_write
from web_translator.network import (
    NetworkBudget,
    NetworkError,
    build_public_client,
    fetch_limited,
)
from web_translator.paths import validate_public_url
from web_translator.pdf_models import PdfSourceRecord


MAX_PDF_BYTES = 50 * 1024 * 1024
PDF_SIGNATURE = b"%PDF-"
MAX_REDIRECTS = 5
MAX_DOWNLOAD_SECONDS = 120.0
_REPARSE_POINT = 0x400
_CHUNK_SIZE = 1024 * 1024
_GENERIC_BINARY_TYPES = {"application/octet-stream", "binary/octet-stream"}


class PdfAcquireError(RuntimeError):
    """A PDF source could not be acquired safely."""


def acquire_pdf(
    source: str,
    run_dir: Path,
    *,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> PdfSourceRecord:
    """Copy one validated source PDF into *run_dir* as ``source.pdf``."""
    parsed = urlsplit(source)
    if parsed.scheme in {"http", "https"}:
        return _acquire_public_pdf(source, run_dir, transport=transport, now=now)
    if parsed.scheme:
        raise PdfAcquireError("PDF source must be a local path or public HTTP(S) URL")
    return _acquire_local_pdf(Path(source), run_dir, now=now)


def _acquire_local_pdf(
    source: Path, run_dir: Path, *, now: datetime | None
) -> PdfSourceRecord:
    initial = _regular_file_lstat(source)
    _prepare_destination(run_dir)
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        opened = os.fstat(descriptor)
        if not _same_identity(initial, opened):
            raise PdfAcquireError(f"local PDF changed identity before copy: {source}")
        with os.fdopen(descriptor, "rb", closefd=True) as input_stream:
            descriptor = None
            with tempfile.NamedTemporaryFile(
                dir=run_dir, prefix=".source.pdf.", suffix=".tmp", delete=False
            ) as output_stream:
                temporary_name = output_stream.name
                digest, size = _copy_and_hash_pdf(input_stream, output_stream, source)

        final = _regular_file_lstat(source)
        if not _same_identity(opened, final):
            raise PdfAcquireError(f"local PDF changed identity during copy: {source}")
        _publish_staged_pdf(Path(temporary_name), run_dir / "source.pdf")
    except PdfAcquireError:
        raise
    except OSError as error:
        raise PdfAcquireError(f"cannot acquire local PDF {source}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)

    return _source_record(
        input_kind="local",
        requested_source=source.name,
        final_source=source.name,
        content_type="application/pdf",
        byte_length=size,
        sha256=digest,
        now=now,
        redirects=[],
        warnings=[],
    )


def _acquire_public_pdf(
    source: str,
    run_dir: Path,
    *,
    transport: httpx.BaseTransport | None,
    now: datetime | None,
) -> PdfSourceRecord:
    try:
        requested = str(validate_public_url(source))
        _prepare_destination(run_dir)
        budget = NetworkBudget(
            max_bytes=MAX_PDF_BYTES,
            max_redirects=MAX_REDIRECTS,
            deadline_seconds=MAX_DOWNLOAD_SECONDS,
            error_prefix="PDF resource budget exceeded",
        )
        with build_public_client(budget=budget, transport=transport) as client:
            response, content = fetch_limited(
                client, requested, MAX_PDF_BYTES, "PDF source"
            )
        final = str(validate_public_url(str(response.url)))
    except (NetworkError, ValueError, OSError) as error:
        raise PdfAcquireError(str(error)) from error

    _require_pdf_signature(content, source)
    content_type = _normalized_content_type(response)
    if content_type != "application/pdf" and content_type not in _GENERIC_BINARY_TYPES:
        raise PdfAcquireError(
            "public PDF must use application/pdf or a generic binary content type"
        )
    try:
        atomic_write(run_dir / "source.pdf", content)
    except OSError as error:
        raise PdfAcquireError(f"cannot publish public PDF source: {error}") from error

    return _source_record(
        input_kind="public",
        requested_source=requested,
        final_source=final,
        content_type=content_type,
        byte_length=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        now=now,
        redirects=[str(item.url) for item in response.history],
        warnings=(
            [f"generic-content-type: {content_type}"]
            if content_type in _GENERIC_BINARY_TYPES
            else []
        ),
    )


def _regular_file_lstat(path: Path) -> os.stat_result:
    try:
        result = os.lstat(path)
    except OSError as error:
        raise PdfAcquireError(f"cannot inspect local PDF {path}: {error}") from error
    if stat.S_ISLNK(result.st_mode) or _is_reparse_point(result):
        raise PdfAcquireError(f"local PDF is a link or reparse point: {path}")
    if not stat.S_ISREG(result.st_mode):
        raise PdfAcquireError(f"local PDF must be a readable regular file: {path}")
    return result


def _is_reparse_point(result: os.stat_result) -> bool:
    return bool(getattr(result, "st_file_attributes", 0) & _REPARSE_POINT)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _copy_and_hash_pdf(
    input_stream: BinaryIO, output_stream: BinaryIO, source: Path
) -> tuple[str, int]:
    first = input_stream.read(len(PDF_SIGNATURE))
    _require_pdf_signature(first, str(source))
    digest = hashlib.sha256()
    size = 0
    for chunk in chain((first,), iter(lambda: input_stream.read(_CHUNK_SIZE), b"")):
        size += len(chunk)
        if size > MAX_PDF_BYTES:
            raise PdfAcquireError(
                f"local PDF exceeds the {MAX_PDF_BYTES}-byte size limit: {source}"
            )
        digest.update(chunk)
        output_stream.write(chunk)
    output_stream.flush()
    os.fsync(output_stream.fileno())
    return digest.hexdigest(), size


def _publish_staged_pdf(temporary: Path, destination: Path) -> None:
    try:
        with temporary.open("rb") as stream:
            atomic_write(destination, stream.read())
    except FileExistsError as error:
        raise PdfAcquireError(f"PDF destination already exists: {destination}") from error


def _prepare_destination(run_dir: Path) -> None:
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise PdfAcquireError(
            f"cannot create PDF run directory {run_dir}: {error}"
        ) from error
    if not run_dir.is_dir():
        raise PdfAcquireError(f"PDF run path is not a directory: {run_dir}")
    destination = run_dir / "source.pdf"
    try:
        result = os.lstat(destination)
    except FileNotFoundError:
        return
    except OSError as error:
        raise PdfAcquireError(
            f"cannot inspect PDF destination {destination}: {error}"
        ) from error
    if stat.S_ISLNK(result.st_mode) or _is_reparse_point(result):
        raise PdfAcquireError(
            f"PDF destination is a link or reparse point: {destination}"
        )
    raise PdfAcquireError(f"PDF destination already exists: {destination}")


def _require_pdf_signature(content: bytes, source: str) -> None:
    if not content.startswith(PDF_SIGNATURE):
        raise PdfAcquireError(f"source does not have a PDF signature: {source}")


def _normalized_content_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").partition(";")[0].strip().lower()


def _source_record(
    *,
    input_kind: Literal["local", "public"],
    requested_source: str,
    final_source: str,
    content_type: str,
    byte_length: int,
    sha256: str,
    now: datetime | None,
    redirects: list[str],
    warnings: list[str],
) -> PdfSourceRecord:
    acquired = now or datetime.now(UTC)
    acquired_at = (
        acquired.astimezone(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    return PdfSourceRecord(
        schema_version="1.0",
        input_kind=input_kind,
        requested_source=requested_source,
        final_source=final_source,
        content_type=content_type,
        byte_length=byte_length,
        sha256=sha256,
        acquired_at=acquired_at,
        redirects=redirects,
        warnings=warnings,
    )
