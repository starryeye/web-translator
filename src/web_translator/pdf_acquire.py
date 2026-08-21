"""Fail-closed acquisition of one local or public PDF source."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
from itertools import chain
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import BinaryIO, Callable, Iterator, Literal, Protocol
from urllib.parse import urlsplit

import httpx

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
    for operation in (os.link, os.mkdir, os.open, os.rmdir, os.stat, os.unlink)
) and os.listdir in os.supports_fd
MetadataWriter = Callable[[PdfSourceRecord, Path], None]


class _DirectoryPathAnchor(Protocol):
    def current_path(self) -> Path: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class _RunDirectory:
    path: Path
    descriptor: int | None
    identity: tuple[int, int]
    path_anchor: _DirectoryPathAnchor | None = None
    published: dict[str, tuple[int, int]] = field(default_factory=dict)
    publication_paths: dict[str, list[Path]] = field(default_factory=dict)


@dataclass(slots=True)
class _StagingDirectory:
    name: str
    descriptor: int | None
    identity: tuple[int, int]
    files: dict[str, tuple[int, int] | None] = field(default_factory=dict)


@dataclass(slots=True)
class _PosixDirectoryPathAnchor:
    descriptor: int

    def current_path(self) -> Path:
        try:
            if sys.platform == "darwin":
                import fcntl

                value = fcntl.fcntl(
                    self.descriptor,
                    fcntl.F_GETPATH,
                    bytes(1024),
                )
                raw_path = value.split(b"\0", 1)[0]
            elif sys.platform.startswith("linux"):
                raw_path = os.fsencode(
                    os.readlink(f"/proc/self/fd/{self.descriptor}")
                )
            else:
                raise NotImplementedError(
                    "directory-handle path resolution is unavailable"
                )
        except (AttributeError, NotImplementedError, OSError, ValueError) as error:
            raise PdfAcquireError(
                f"safe PDF directory anchor unavailable: {error}"
            ) from error
        return Path(os.fsdecode(raw_path))

    def close(self) -> None:
        try:
            os.close(self.descriptor)
        except OSError:
            return


@dataclass(slots=True)
class _WindowsDirectoryPathAnchor:
    handle: int

    def current_path(self) -> Path:
        return _windows_final_path(self.handle)

    def close(self) -> None:
        _close_windows_handle(self.handle)


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
        if run.path_anchor is not None:
            run.path_anchor.close()


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
        with _staging_directory(run) as staging:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(source, flags)
            opened = os.fstat(descriptor)
            if not _same_identity(initial, opened):
                raise PdfAcquireError(f"local PDF changed identity before copy: {source}")
            with os.fdopen(descriptor, "rb", closefd=True) as input_stream:
                descriptor = None
                with _open_staged_output(run, staging, "source.pdf") as output_stream:
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
        with _staging_directory(run) as staging:
            _write_staged_bytes(run, staging, "source.pdf", content)
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


@contextmanager
def _staging_directory(run: _RunDirectory) -> Iterator[_StagingDirectory]:
    staging = _create_staging_directory(run)
    failed = False
    try:
        yield staging
    except BaseException:
        failed = True
        raise
    finally:
        try:
            _cleanup_staging_directory(run, staging)
        except PdfAcquireError:
            if not failed:
                raise


def _create_staging_directory(run: _RunDirectory) -> _StagingDirectory:
    for _ in range(100):
        name = f".pdf-acquiring-{secrets.token_hex(8)}"
        descriptor: int | None = None
        created = False
        identity: tuple[int, int] | None = None
        try:
            _verify_run_identity(run)
            if _supports_descriptor_relative_operations():
                assert run.descriptor is not None
                os.mkdir(name, 0o700, dir_fd=run.descriptor)
                created = True
                result = os.stat(
                    name, dir_fd=run.descriptor, follow_symlinks=False
                )
                identity = (result.st_dev, result.st_ino)
                descriptor = os.open(
                    name,
                    _directory_open_flags(),
                    dir_fd=run.descriptor,
                )
                opened = os.fstat(descriptor)
                if not _same_identity(result, opened):
                    raise PdfAcquireError(
                        "PDF staging directory changed identity while opening"
                    )
            else:
                path = _fallback_current_run_path(run) / name
                os.mkdir(path, 0o700)
                created = True
                result = _require_safe_directory(path)
                identity = (result.st_dev, result.st_ino)
            staging = _StagingDirectory(name, descriptor, identity)
            _verify_staging_identity(run, staging)
            return staging
        except FileExistsError:
            if descriptor is not None:
                os.close(descriptor)
            if created and identity is not None:
                _remove_staging_root(run, name, identity)
            continue
        except PdfAcquireError:
            if descriptor is not None:
                os.close(descriptor)
            if created and identity is not None:
                _remove_staging_root(run, name, identity)
            raise
        except (NotImplementedError, OSError) as error:
            if descriptor is not None:
                os.close(descriptor)
            if created and identity is not None:
                _remove_staging_root(run, name, identity)
            raise PdfAcquireError(
                f"cannot create PDF staging directory in {run.path}: {error}"
            ) from error
    raise PdfAcquireError(f"cannot allocate PDF staging directory in {run.path}")


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


@contextmanager
def _open_staged_output(
    run: _RunDirectory, staging: _StagingDirectory, name: str
) -> Iterator[BinaryIO]:
    _verify_run_identity(run)
    _verify_staging_identity(run, staging)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        if _supports_descriptor_relative_operations():
            assert staging.descriptor is not None
            descriptor = os.open(name, flags, 0o600, dir_fd=staging.descriptor)
        else:
            destination = _fallback_staging_path(run, staging) / name
            _require_absent_destination(destination)
            descriptor = os.open(destination, flags, 0o600)
        result = os.fstat(descriptor)
        if not stat.S_ISREG(result.st_mode) or _is_reparse_point(result):
            raise PdfAcquireError(f"PDF staging path is not a regular file: {name}")
        staging.files[name] = (result.st_dev, result.st_ino)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            yield stream
    except FileExistsError as error:
        raise PdfAcquireError(f"PDF staging destination already exists: {name}") from error
    except PdfAcquireError:
        raise
    except (NotImplementedError, OSError) as error:
        raise PdfAcquireError(f"cannot stage PDF artifact {name}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_staged_bytes(
    run: _RunDirectory,
    staging: _StagingDirectory,
    name: str,
    content: bytes,
) -> None:
    with _open_staged_output(run, staging, name) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_staged_file(
    staging: _StagingDirectory,
    source_name: str,
    destination: Path,
    run: _RunDirectory,
) -> tuple[int, int]:
    _verify_run_identity(run)
    source = _staged_file_stat(run, staging, source_name)
    source_identity = (source.st_dev, source.st_ino)
    if staging.files.get(source_name) != source_identity:
        raise PdfAcquireError(
            f"PDF staging artifact changed identity before publication: {source_name}"
        )
    run.published[destination.name] = source_identity
    try:
        if _supports_descriptor_relative_operations():
            assert run.descriptor is not None
            assert staging.descriptor is not None
            os.link(
                source_name,
                destination.name,
                src_dir_fd=staging.descriptor,
                dst_dir_fd=run.descriptor,
                follow_symlinks=False,
            )
        else:
            current_run_path = _fallback_current_run_path(run)
            source_path = _fallback_staging_path(run, staging) / source_name
            current_destination = current_run_path / destination.name
            run.publication_paths.setdefault(destination.name, []).append(
                current_run_path
            )
            _require_absent_destination(current_destination)
            os.link(source_path, current_destination)
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
    staging: _StagingDirectory,
    run: _RunDirectory,
    record: PdfSourceRecord,
    metadata_writer: MetadataWriter | None,
) -> None:
    if metadata_writer is not None:
        # The private staging name is cleanup-owned even if the writer aborts
        # before its inode can be recorded.
        staging.files["source.json"] = None
        try:
            _verify_run_identity(run)
            staged_metadata = _staging_path(run, staging) / "source.json"
            metadata_writer(record, staged_metadata)
            result = _staged_file_stat(run, staging, "source.json")
            path_result = _regular_file_lstat(staged_metadata)
            if not _same_identity(result, path_result):
                raise PdfAcquireError(
                    "PDF metadata changed identity while it was staged"
                )
            staging.files["source.json"] = (result.st_dev, result.st_ino)
            _verify_run_identity(run)
        except PdfAcquireError:
            raise
        except (NotImplementedError, OSError) as error:
            raise PdfAcquireError(f"cannot stage PDF metadata: {error}") from error

    source_destination = run.path / "source.pdf"
    metadata_destination = run.path / "source.json"
    try:
        _assert_run_contents(run.path, staging, set(), run)
        _publish_staged_file(staging, "source.pdf", source_destination, run)
        if metadata_writer is None:
            return
        _assert_run_contents(run.path, staging, {"source.pdf"}, run)
        if not _matches_identity_at(run, "source.pdf", run.published["source.pdf"]):
            raise PdfAcquireError("PDF source changed identity during publication")
        _publish_staged_file(staging, "source.json", metadata_destination, run)
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
        anchor = _open_fallback_run_anchor(run_dir)
        try:
            run = _RunDirectory(
                run_dir,
                None,
                (result.st_dev, result.st_ino),
                path_anchor=anchor,
            )
            _fallback_current_run_path(run)
            _assert_run_contents(run_dir, None, set(), run)
            return run
        except BaseException:
            anchor.close()
            raise

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
    staging: _StagingDirectory | None,
    allowed: set[str],
    run: _RunDirectory | None = None,
) -> None:
    if run is not None:
        _verify_run_identity(run)
    try:
        if run is not None and _supports_descriptor_relative_operations():
            assert run.descriptor is not None
            names = set(os.listdir(run.descriptor))
        elif run is not None:
            names = {entry.name for entry in _fallback_current_run_path(run).iterdir()}
        else:
            _reject_linked_ancestors(run_dir)
            _require_safe_directory(run_dir)
            names = {entry.name for entry in run_dir.iterdir()}
    except (NotImplementedError, OSError) as error:
        raise PdfAcquireError(f"cannot inspect PDF run directory {run_dir}: {error}") from error
    expected = set(allowed)
    if staging is not None:
        if run is None:
            raise PdfAcquireError("PDF staging validation requires a run anchor")
        _verify_staging_identity(run, staging)
        expected.add(staging.name)
    if names != expected:
        raise PdfAcquireError(f"PDF run directory must be empty: {run_dir}")


def _fallback_current_run_path(run: _RunDirectory) -> Path:
    if run.path_anchor is None:
        raise PdfAcquireError(
            f"safe PDF directory anchor unavailable for {run.path}"
        )
    current_path = run.path_anchor.current_path()
    _reject_linked_ancestors(current_path)
    current = _require_safe_directory(current_path)
    if (current.st_dev, current.st_ino) != run.identity:
        raise PdfAcquireError(f"PDF run directory changed identity: {run.path}")
    return current_path


def _staging_path(run: _RunDirectory, staging: _StagingDirectory) -> Path:
    if _supports_descriptor_relative_operations():
        _verify_run_identity(run)
        _verify_staging_identity(run, staging)
        return run.path / staging.name
    return _fallback_staging_path(run, staging)


def _fallback_staging_path(
    run: _RunDirectory, staging: _StagingDirectory
) -> Path:
    path = _fallback_current_run_path(run) / staging.name
    result = _require_safe_directory(path)
    if (result.st_dev, result.st_ino) != staging.identity:
        raise PdfAcquireError("PDF staging directory changed identity")
    return path


def _verify_staging_identity(
    run: _RunDirectory, staging: _StagingDirectory
) -> None:
    try:
        if _supports_descriptor_relative_operations():
            assert run.descriptor is not None
            result = os.stat(
                staging.name,
                dir_fd=run.descriptor,
                follow_symlinks=False,
            )
        else:
            result = os.lstat(_fallback_current_run_path(run) / staging.name)
    except (NotImplementedError, OSError) as error:
        raise PdfAcquireError(
            f"cannot inspect PDF staging directory: {error}"
        ) from error
    if (
        not stat.S_ISDIR(result.st_mode)
        or _is_reparse_point(result)
        or (result.st_dev, result.st_ino) != staging.identity
    ):
        raise PdfAcquireError("PDF staging directory changed identity")


def _staged_file_stat(
    run: _RunDirectory, staging: _StagingDirectory, name: str
) -> os.stat_result:
    _verify_staging_identity(run, staging)
    try:
        if _supports_descriptor_relative_operations():
            assert staging.descriptor is not None
            result = os.stat(name, dir_fd=staging.descriptor, follow_symlinks=False)
        else:
            result = os.lstat(_fallback_staging_path(run, staging) / name)
    except (NotImplementedError, OSError) as error:
        raise PdfAcquireError(f"cannot inspect PDF staging artifact {name}: {error}") from error
    if not stat.S_ISREG(result.st_mode) or _is_reparse_point(result):
        raise PdfAcquireError(f"PDF staging artifact is not a regular file: {name}")
    return result


def _cleanup_staging_directory(
    run: _RunDirectory, staging: _StagingDirectory
) -> None:
    cleanup_failed = False
    for name, identity in reversed(tuple(staging.files.items())):
        if identity is not None:
            try:
                result = _staged_file_stat(run, staging, name)
            except PdfAcquireError:
                continue
            if (result.st_dev, result.st_ino) != identity:
                continue
        try:
            if _supports_descriptor_relative_operations():
                assert staging.descriptor is not None
                os.unlink(name, dir_fd=staging.descriptor)
            else:
                (_fallback_staging_path(run, staging) / name).unlink()
        except FileNotFoundError:
            continue
        except (NotImplementedError, OSError):
            cleanup_failed = True

    if staging.descriptor is not None:
        os.close(staging.descriptor)
        staging.descriptor = None

    try:
        _remove_staging_root(run, staging.name, staging.identity)
    except PdfAcquireError:
        cleanup_failed = True
    if cleanup_failed:
        raise PdfAcquireError("cannot safely clean PDF staging directory")


def _remove_staging_root(
    run: _RunDirectory, name: str, identity: tuple[int, int]
) -> None:
    try:
        if _supports_descriptor_relative_operations():
            assert run.descriptor is not None
            result = os.stat(name, dir_fd=run.descriptor, follow_symlinks=False)
            if (
                not stat.S_ISDIR(result.st_mode)
                or _is_reparse_point(result)
                or (result.st_dev, result.st_ino) != identity
            ):
                return
            os.rmdir(name, dir_fd=run.descriptor)
        else:
            path = _fallback_current_run_path(run) / name
            result = os.lstat(path)
            if (
                not stat.S_ISDIR(result.st_mode)
                or _is_reparse_point(result)
                or (result.st_dev, result.st_ino) != identity
            ):
                return
            os.rmdir(path)
    except FileNotFoundError:
        return
    except (NotImplementedError, OSError) as error:
        raise PdfAcquireError(
            f"cannot remove PDF staging directory {name}: {error}"
        ) from error


def _open_run_directory(path: Path) -> int:
    try:
        descriptor = os.open(path, _directory_open_flags())
    except OSError as error:
        raise PdfAcquireError(f"cannot open PDF run directory {path}: {error}") from error
    result = os.fstat(descriptor)
    current = _require_safe_directory(path)
    if (result.st_dev, result.st_ino) != (current.st_dev, current.st_ino):
        os.close(descriptor)
        raise PdfAcquireError(f"PDF run directory changed identity: {path}")
    return descriptor


def _open_fallback_run_anchor(path: Path) -> _DirectoryPathAnchor:
    if os.name == "nt":
        return _open_windows_run_anchor(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _directory_open_flags())
        anchor = _PosixDirectoryPathAnchor(descriptor)
        current = anchor.current_path()
        opened = os.fstat(descriptor)
        result = _require_safe_directory(current)
        if not _same_identity(opened, result):
            raise PdfAcquireError(f"PDF run directory changed identity: {path}")
        return anchor
    except PdfAcquireError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (NotImplementedError, OSError) as error:
        if descriptor is not None:
            os.close(descriptor)
        raise PdfAcquireError(
            f"safe PDF directory anchor unavailable for {path}: {error}"
        ) from error


def _open_windows_run_anchor(path: Path) -> _WindowsDirectoryPathAnchor:
    handle: int | None = None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        raw_handle = create_file(
            str(path.absolute()),
            0,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if raw_handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        handle = int(raw_handle)
        anchor = _WindowsDirectoryPathAnchor(handle)
        anchor.current_path()
        return anchor
    except PdfAcquireError:
        if handle is not None:
            _close_windows_handle(handle)
        raise
    except (AttributeError, NotImplementedError, OSError) as error:
        if handle is not None:
            _close_windows_handle(handle)
        raise PdfAcquireError(
            f"safe PDF directory anchor unavailable for {path}: {error}"
        ) from error


def _windows_final_path(handle: int) -> Path:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_final_path = kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        get_final_path.restype = wintypes.DWORD
        size = 512
        while size <= 32768:
            buffer = ctypes.create_unicode_buffer(size)
            length = get_final_path(handle, buffer, size, 0)
            if length == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            if length < size:
                return Path(buffer.value)
            size = length + 1
        raise OSError("resolved Windows directory path exceeds 32768 characters")
    except (AttributeError, NotImplementedError, OSError) as error:
        raise PdfAcquireError(f"cannot resolve PDF directory anchor: {error}") from error


def _close_windows_handle(handle: int) -> None:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        close_handle(handle)
    except (AttributeError, OSError):
        return


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
            result = os.lstat(_fallback_current_run_path(run) / name)
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
        for candidate in _fallback_cleanup_paths(run, name):
            destination = candidate / name
            try:
                _reject_linked_ancestors(candidate)
                _require_safe_directory(candidate)
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
    run.publication_paths.clear()


def _fallback_cleanup_paths(run: _RunDirectory, name: str) -> list[Path]:
    candidates = [run.path, *run.publication_paths.get(name, [])]
    try:
        candidates.insert(0, _fallback_current_run_path(run))
    except PdfAcquireError:
        pass
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
