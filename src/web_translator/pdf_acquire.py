"""Fail-closed acquisition of one local or public PDF source."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
from itertools import chain
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import BinaryIO, Callable, Literal
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
_DESCRIPTOR_RELATIVE_OPERATIONS_SUPPORTED = all(
    operation in os.supports_dir_fd
    for operation in (os.link, os.stat, os.unlink)
)
MetadataWriter = Callable[[PdfSourceRecord, Path], None]


@dataclass(slots=True)
class _RunDirectory:
    path: Path
    descriptor: int | None
    identity: tuple[int, int]
    published: dict[str, tuple[int, int]] = field(default_factory=dict)


class PdfAcquireError(RuntimeError):
    """A PDF source could not be acquired safely."""


def acquire_pdf(
    source: str,
    run_dir: Path,
    *,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
    metadata_writer: MetadataWriter | None = None,
) -> PdfSourceRecord:
    """Copy one validated source PDF into *run_dir* as ``source.pdf``."""
    run = _prepare_destination(run_dir)
    try:
        if _is_windows_local_path(source):
            record = _acquire_local_pdf(
                Path(source), run, now=now, metadata_writer=metadata_writer
            )
        else:
            parsed = urlsplit(source)
            if parsed.scheme in {"http", "https"}:
                record = _acquire_public_pdf(
                    source,
                    run,
                    transport=transport,
                    now=now,
                    metadata_writer=metadata_writer,
                )
            elif parsed.scheme:
                raise PdfAcquireError(
                    "PDF source must be a local path or public HTTP(S) URL"
                )
            else:
                record = _acquire_local_pdf(
                    Path(source), run, now=now, metadata_writer=metadata_writer
                )
        _verify_run_identity(run)
        return record
    except BaseException:
        _rollback_published(run)
        raise
    finally:
        if run.descriptor is not None:
            os.close(run.descriptor)


def _acquire_local_pdf(
    source: Path,
    run: _RunDirectory,
    *,
    now: datetime | None,
    metadata_writer: MetadataWriter | None,
) -> PdfSourceRecord:
    initial = _regular_file_lstat(source)
    descriptor: int | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix=".pdf-acquiring-", dir=run.path.parent
        ) as name:
            staging = Path(name)
            flags = (
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(source, flags)
            opened = os.fstat(descriptor)
            if not _same_identity(initial, opened):
                raise PdfAcquireError(f"local PDF changed identity before copy: {source}")
            staged_source = staging / "source.pdf"
            with os.fdopen(descriptor, "rb", closefd=True) as input_stream:
                descriptor = None
                with staged_source.open("wb") as output_stream:
                    digest, size = _copy_and_hash_pdf(input_stream, output_stream, source)

            final = _regular_file_lstat(source)
            if not _same_identity(opened, final):
                raise PdfAcquireError(f"local PDF changed identity during copy: {source}")
            record = _source_record(
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
            _publish_artifacts(staging, run, record, metadata_writer)
    except PdfAcquireError:
        raise
    except OSError as error:
        raise PdfAcquireError(f"cannot acquire local PDF {source}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return record


def _acquire_public_pdf(
    source: str,
    run: _RunDirectory,
    *,
    transport: httpx.BaseTransport | None,
    now: datetime | None,
    metadata_writer: MetadataWriter | None,
) -> PdfSourceRecord:
    try:
        requested = str(validate_public_url(source))
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
    record = _source_record(
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
    try:
        with tempfile.TemporaryDirectory(
            prefix=".pdf-acquiring-", dir=run.path.parent
        ) as name:
            staging = Path(name)
            atomic_write(staging / "source.pdf", content)
            _publish_artifacts(staging, run, record, metadata_writer)
    except OSError as error:
        raise PdfAcquireError(f"cannot publish public PDF source: {error}") from error
    return record


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


def _publish_staged_file(
    temporary: Path, destination: Path, run: _RunDirectory
) -> tuple[int, int]:
    _verify_run_identity(run)
    source = _regular_file_lstat(temporary)
    source_identity = (source.st_dev, source.st_ino)
    run.published[destination.name] = source_identity
    try:
        if _supports_descriptor_relative_operations():
            assert run.descriptor is not None
            os.link(temporary, destination.name, dst_dir_fd=run.descriptor)
        else:
            _require_absent_destination(destination)
            os.link(temporary, destination)
    except FileExistsError as error:
        raise PdfAcquireError(f"PDF destination already exists: {destination}") from error
    except (NotImplementedError, OSError) as error:
        raise PdfAcquireError(
            f"safe PDF publication unavailable: {destination}: {error}"
        ) from error
    if not _matches_identity_at(run, destination.name, source_identity):
        raise PdfAcquireError(
            f"PDF destination changed identity during publication: {destination}"
        )
    _verify_run_identity(run)
    return source_identity


def _publish_artifacts(
    staging: Path,
    run: _RunDirectory,
    record: PdfSourceRecord,
    metadata_writer: MetadataWriter | None,
) -> None:
    staged_source = staging / "source.pdf"
    staged_metadata = staging / "source.json"
    if metadata_writer is not None:
        try:
            metadata_writer(record, staged_metadata)
            _regular_file_lstat(staged_metadata)
        except (NotImplementedError, OSError) as error:
            raise PdfAcquireError(f"cannot stage PDF metadata: {error}") from error

    source_destination = run.path / "source.pdf"
    metadata_destination = run.path / "source.json"
    try:
        _assert_run_contents(run.path, staging, set(), run)
        _publish_staged_file(staged_source, source_destination, run)
        if metadata_writer is None:
            return
        _assert_run_contents(run.path, staging, {"source.pdf"}, run)
        if not _matches_identity_at(run, "source.pdf", run.published["source.pdf"]):
            raise PdfAcquireError("PDF source changed identity during publication")
        _publish_staged_file(staged_metadata, metadata_destination, run)
        if not _matches_identity_at(run, "source.pdf", run.published["source.pdf"]):
            raise PdfAcquireError("PDF source changed identity during publication")
        if not _matches_identity_at(run, "source.json", run.published["source.json"]):
            raise PdfAcquireError("PDF metadata changed identity during publication")
    except BaseException:
        _rollback_published(run)
        raise


def _prepare_destination(run_dir: Path) -> _RunDirectory:
    try:
        _reject_linked_ancestors(run_dir)
        if run_dir.exists():
            _require_safe_directory(run_dir)
        else:
            run_dir.mkdir(parents=True)
            _reject_linked_ancestors(run_dir)
            _require_safe_directory(run_dir)
    except OSError as error:
        raise PdfAcquireError(
            f"cannot create PDF run directory {run_dir}: {error}"
        ) from error
    if not _supports_descriptor_relative_operations():
        result = _require_safe_directory(run_dir)
        run = _RunDirectory(run_dir, None, (result.st_dev, result.st_ino))
        _assert_run_contents(run_dir, None, set(), run)
        return run

    descriptor = _open_run_directory(run_dir)
    try:
        result = os.fstat(descriptor)
        run = _RunDirectory(run_dir, descriptor, (result.st_dev, result.st_ino))
        _assert_run_contents(run_dir, None, set(), run)
        return run
    except BaseException:
        os.close(descriptor)
        raise


def _is_windows_local_path(source: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", source) or source.startswith("\\\\"))


def _require_safe_directory(path: Path) -> os.stat_result:
    try:
        result = os.lstat(path)
    except OSError as error:
        raise PdfAcquireError(f"cannot inspect PDF run directory {path}: {error}") from error
    if stat.S_ISLNK(result.st_mode) or _is_reparse_point(result):
        raise PdfAcquireError(f"PDF run directory is a link or reparse point: {path}")
    if not stat.S_ISDIR(result.st_mode):
        raise PdfAcquireError(f"PDF run path is not a directory: {path}")
    return result


def _reject_linked_ancestors(path: Path) -> None:
    candidate = path.absolute()
    while not os.path.lexists(candidate):
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    while True:
        _require_safe_directory(candidate)
        parent = candidate.parent
        if parent == candidate:
            return
        candidate = parent


def _assert_run_contents(
    run_dir: Path,
    staging: Path | None,
    allowed: set[str],
    run: _RunDirectory | None = None,
) -> None:
    if run is not None:
        _verify_run_identity(run)
    _reject_linked_ancestors(run_dir)
    _require_safe_directory(run_dir)
    try:
        names = {entry.name for entry in run_dir.iterdir()}
    except OSError as error:
        raise PdfAcquireError(f"cannot inspect PDF run directory {run_dir}: {error}") from error
    expected = set(allowed)
    if staging is not None:
        _require_safe_directory(staging)
    if names != expected:
        raise PdfAcquireError(f"PDF run directory must be empty: {run_dir}")


def _open_run_directory(path: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PdfAcquireError(f"cannot open PDF run directory {path}: {error}") from error
    result = os.fstat(descriptor)
    current = _require_safe_directory(path)
    if (result.st_dev, result.st_ino) != (current.st_dev, current.st_ino):
        os.close(descriptor)
        raise PdfAcquireError(f"PDF run directory changed identity: {path}")
    return descriptor


def _supports_descriptor_relative_operations() -> bool:
    return _DESCRIPTOR_RELATIVE_OPERATIONS_SUPPORTED


def _require_absent_destination(destination: Path) -> None:
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


def _verify_run_identity(run: _RunDirectory) -> None:
    current = _require_safe_directory(run.path)
    if (current.st_dev, current.st_ino) != run.identity:
        raise PdfAcquireError(f"PDF run directory changed identity: {run.path}")


def _matches_identity_at(
    run: _RunDirectory, name: str, identity: tuple[int, int]
) -> bool:
    try:
        if _supports_descriptor_relative_operations():
            assert run.descriptor is not None
            result = os.stat(name, dir_fd=run.descriptor, follow_symlinks=False)
        else:
            result = os.lstat(run.path / name)
    except (PdfAcquireError, OSError, NotImplementedError):
        return False
    return (
        stat.S_ISREG(result.st_mode)
        and not _is_reparse_point(result)
        and (result.st_dev, result.st_ino) == identity
    )


def _rollback_published(run: _RunDirectory) -> None:
    for name, identity in reversed(tuple(run.published.items())):
        if _supports_descriptor_relative_operations():
            if not _matches_identity_at(run, name, identity):
                continue
            try:
                assert run.descriptor is not None
                os.unlink(name, dir_fd=run.descriptor)
            except (OSError, NotImplementedError):
                continue
            continue
        for candidate in _fallback_run_paths(run):
            destination = candidate / name
            try:
                result = _regular_file_lstat(destination)
            except PdfAcquireError:
                continue
            if (result.st_dev, result.st_ino) != identity:
                continue
            try:
                destination.unlink()
            except OSError:
                continue
    run.published.clear()


def _fallback_run_paths(run: _RunDirectory) -> list[Path]:
    candidates = [run.path]
    try:
        for sibling in run.path.parent.iterdir():
            try:
                result = _require_safe_directory(sibling)
            except PdfAcquireError:
                continue
            if (result.st_dev, result.st_ino) == run.identity:
                candidates.append(sibling)
    except (PdfAcquireError, OSError):
        return candidates
    return list(dict.fromkeys(candidates))


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
