#!/usr/bin/env python3
"""Download and extract the exact Windows Poppler CI archive within fixed bounds."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from typing import Any
from urllib.request import urlopen
import zipfile


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_EXTRACTION_CHUNK_BYTES = 1024 * 1024


class PopplerBootstrapError(RuntimeError):
    """The pinned Poppler archive failed its download or extraction contract."""


@dataclass(frozen=True, slots=True)
class ArchiveContract:
    url: str
    expected_size: int
    expected_sha256: str
    root_prefix: str
    max_entries: int
    max_uncompressed_bytes: int
    required_relative_files: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.url.startswith("https://"):
            raise ValueError("archive URL must use HTTPS")
        if self.expected_size <= 0:
            raise ValueError("archive expected size must be positive")
        if _SHA256.fullmatch(self.expected_sha256) is None:
            raise ValueError("archive SHA-256 must be lowercase hexadecimal")
        if (
            not self.root_prefix.endswith("/")
            or self.root_prefix.startswith("/")
            or "\\" in self.root_prefix
        ):
            raise ValueError("archive root prefix must be a relative POSIX directory")
        if self.max_entries <= 0 or self.max_uncompressed_bytes <= 0:
            raise ValueError("archive resource bounds must be positive")
        if not self.required_relative_files:
            raise ValueError("archive must require executable files")


PINNED_POPPLER = ArchiveContract(
    url=(
        "https://github.com/oschwartz10612/poppler-windows/releases/download/"
        "v26.02.0-0/Release-26.02.0-0.zip"
    ),
    expected_size=16_107_283,
    expected_sha256=(
        "993e4a94376ed712fafc7058d724ea0b943d118bbd2305cd9ed55174eb85cda5"
    ),
    root_prefix="poppler-26.02.0/",
    # The pinned archive has 462 entries and 57,081,822 uncompressed bytes.
    max_entries=512,
    max_uncompressed_bytes=64 * 1024 * 1024,
    required_relative_files=(
        "Library/bin/pdfinfo.exe",
        "Library/bin/pdftoppm.exe",
    ),
)


def download_verified_archive(
    destination: Path,
    *,
    url: str,
    expected_size: int,
    expected_sha256: str,
    opener: Callable[..., Any] = urlopen,
) -> None:
    """Stream exactly ``expected_size`` bytes and verify SHA-256 before returning."""
    destination = Path(destination)
    if expected_size <= 0:
        raise PopplerBootstrapError("pinned archive size must be positive")
    if _SHA256.fullmatch(expected_sha256) is None:
        raise PopplerBootstrapError("pinned archive SHA-256 is invalid")
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with opener(url, timeout=120) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except (TypeError, ValueError) as error:
                    raise PopplerBootstrapError(
                        "Poppler response has an invalid Content-Length"
                    ) from error
                if declared_size > expected_size:
                    raise PopplerBootstrapError(
                        "Poppler download exceeds pinned size"
                    )
                if declared_size < expected_size:
                    raise PopplerBootstrapError("Poppler download is truncated")

            digest = hashlib.sha256()
            received = 0
            with destination.open("xb") as stream:
                created = True
                while received < expected_size:
                    chunk = response.read(
                        min(_DOWNLOAD_CHUNK_BYTES, expected_size - received)
                    )
                    if not chunk:
                        raise PopplerBootstrapError("Poppler download is truncated")
                    if not isinstance(chunk, bytes):
                        raise PopplerBootstrapError(
                            "Poppler download returned non-byte content"
                        )
                    stream.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                if response.read(1):
                    raise PopplerBootstrapError(
                        "Poppler download exceeds pinned size"
                    )
                stream.flush()
                os.fsync(stream.fileno())
            if digest.hexdigest() != expected_sha256:
                raise PopplerBootstrapError("Poppler archive SHA-256 mismatch")
    except PopplerBootstrapError:
        if created:
            destination.unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as error:
        if created:
            destination.unlink(missing_ok=True)
        raise PopplerBootstrapError(
            f"cannot download pinned Poppler archive: {error}"
        ) from error


def install_pinned_poppler(
    destination: Path,
    *,
    contract: ArchiveContract = PINNED_POPPLER,
    opener: Callable[..., Any] = urlopen,
) -> Path:
    """Verify the fixed archive, validate all ZIP metadata, then extract it."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise PopplerBootstrapError(
            f"Poppler installation destination already exists: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    archive_path = temporary / "poppler.zip"
    extraction_root = temporary / "extracted"
    try:
        download_verified_archive(
            archive_path,
            url=contract.url,
            expected_size=contract.expected_size,
            expected_sha256=contract.expected_sha256,
            opener=opener,
        )
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                members = _validated_members(archive, contract)
                extraction_root.mkdir()
                _extract_members(archive, members, extraction_root)
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            raise PopplerBootstrapError(
                f"cannot validate pinned Poppler ZIP: {error}"
            ) from error

        for relative in contract.required_relative_files:
            required = extraction_root / contract.root_prefix / relative
            if not required.is_file() or required.stat().st_size == 0:
                raise PopplerBootstrapError(
                    f"pinned Poppler ZIP is missing {relative}"
                )
        if destination.exists() or destination.is_symlink():
            raise PopplerBootstrapError(
                f"Poppler installation destination already exists: {destination}"
            )
        extraction_root.rename(destination)
        return destination / contract.root_prefix / "Library" / "bin"
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _validated_members(
    archive: zipfile.ZipFile,
    contract: ArchiveContract,
) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > contract.max_entries:
        raise PopplerBootstrapError("Poppler ZIP entry count exceeds limit")
    seen: set[str] = set()
    total_uncompressed = 0
    required_names = {
        f"{contract.root_prefix}{relative}"
        for relative in contract.required_relative_files
    }
    for member in members:
        name = member.filename
        trimmed = name[:-1] if name.endswith("/") else name
        segments = trimmed.split("/")
        if (
            not name
            or "\x00" in name
            or "\\" in name
            or PurePosixPath(name).is_absolute()
            or any(segment in {"", ".", ".."} for segment in segments)
        ):
            raise PopplerBootstrapError(
                f"Poppler ZIP contains an unsafe path: {name!r}"
            )
        if not name.startswith(contract.root_prefix):
            raise PopplerBootstrapError(
                f"Poppler ZIP member is outside pinned root: {name!r}"
            )
        if name in seen:
            raise PopplerBootstrapError(
                f"Poppler ZIP contains a duplicate member: {name!r}"
            )
        seen.add(name)
        unix_mode = (member.external_attr >> 16) & 0o170000
        if unix_mode == stat.S_IFLNK:
            raise PopplerBootstrapError(
                f"Poppler ZIP contains a symbolic link: {name!r}"
            )
        if member.flag_bits & 0x1:
            raise PopplerBootstrapError(
                f"Poppler ZIP contains an encrypted member: {name!r}"
            )
        total_uncompressed += member.file_size
        if total_uncompressed > contract.max_uncompressed_bytes:
            raise PopplerBootstrapError(
                "Poppler ZIP uncompressed size exceeds limit"
            )
    missing = sorted(required_names - seen)
    if missing:
        raise PopplerBootstrapError(
            f"Poppler ZIP is missing required files: {', '.join(missing)}"
        )
    return members


def _extract_members(
    archive: zipfile.ZipFile,
    members: Sequence[zipfile.ZipInfo],
    destination: Path,
) -> None:
    for member in members:
        relative = PurePosixPath(member.filename)
        target = destination.joinpath(*relative.parts)
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with archive.open(member, "r") as source, target.open("xb") as output:
            while True:
                chunk = source.read(_EXTRACTION_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > member.file_size:
                    raise PopplerBootstrapError(
                        f"Poppler ZIP member exceeds declared size: {member.filename}"
                    )
                output.write(chunk)
        if written != member.file_size:
            raise PopplerBootstrapError(
                f"Poppler ZIP member is truncated: {member.filename}"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the fixed, resource-bounded Windows Poppler CI archive."
    )
    parser.add_argument("--destination-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        poppler_bin = install_pinned_poppler(arguments.destination_root)
    except PopplerBootstrapError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(poppler_bin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
