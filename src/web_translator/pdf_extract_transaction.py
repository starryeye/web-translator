"""Held-directory transaction for publishing PDF extraction artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import web_translator.pdf_assemble as anchored
from web_translator.pdf_extract import PdfExtractionError, extract_pdf
from web_translator.pdf_models import PdfContractError, PdfSourceRecord


PdfExtractor = Callable[[Path, Path, Path, Path], object]


def extract_pdf_transaction(
    run_dir: Path,
    *,
    extractor: PdfExtractor = extract_pdf,
) -> None:
    """Extract and no-clobber publish through one exact held run directory."""
    run_dir = Path(run_dir)
    run_anchor: anchored._DirectoryAnchor | None = None
    staging_anchor: anchored._DirectoryAnchor | None = None
    staged_media_anchor: anchored._DirectoryAnchor | None = None
    published_media_anchor: anchored._DirectoryAnchor | None = None
    source_files: dict[str, anchored._OpenedFile] = {}
    staged_files: dict[str, anchored._OpenedFile] = {}
    staged_media_files: dict[str, anchored._OpenedFile] = {}
    published_files: dict[str, anchored._PublishedFile] = {}
    published_media_files: dict[str, anchored._PublishedFile] = {}
    staging_name: str | None = None
    completed = False
    try:
        run_anchor = anchored._open_directory_anchor(run_dir, "run")
        for name in ("document.json", "segments.jsonl", "media"):
            try:
                anchored._require_anchored_name_absent(run_anchor, name)
            except anchored.PdfAssemblyError as error:
                raise PdfExtractionError(
                    f"PDF extraction destination already exists: {run_dir / name}"
                ) from error

        for name, label in (
            ("source.pdf", "PDF source"),
            ("source.json", "PDF source record"),
        ):
            source_files[name] = anchored._open_anchored_input_file(
                run_anchor,
                name,
                label,
            )
        anchored._verify_anchored_evidence(run_anchor, source_files)
        source_bytes = anchored._read_opened_bytes(
            source_files["source.pdf"],
            run_dir / "source.pdf",
            "PDF source",
        )
        source_record_bytes = anchored._read_opened_bytes(
            source_files["source.json"],
            run_dir / "source.json",
            "PDF source record",
        )
        source_record = _read_source_record(source_record_bytes)
        if (
            source_record.byte_length != len(source_bytes)
            or source_record.sha256 != hashlib.sha256(source_bytes).hexdigest()
        ):
            raise PdfExtractionError(
                "source.json byte length or SHA-256 does not match held source.pdf"
            )

        staging_name, staging_anchor = anchored._create_unique_child_directory(
            run_anchor,
            ".pdf-extracting-",
            "PDF extraction staging",
        )
        _write_private_file(staging_anchor, "source.pdf", source_bytes)
        _write_private_file(staging_anchor, "source.json", source_record_bytes)
        staging_path = staging_anchor.current_path()
        extractor(
            staging_path / "source.pdf",
            staging_path / "document.json",
            staging_path / "segments.jsonl",
            staging_path / "media",
        )

        run_anchor.verify_visible()
        staging_anchor.verify_visible()
        anchored._verify_anchored_evidence(run_anchor, source_files)
        _verify_source_snapshots(run_dir, source_files, source_bytes, source_record_bytes)
        for name, label in (
            ("document.json", "staged PDF document"),
            ("segments.jsonl", "staged PDF segments"),
        ):
            opened = anchored._open_anchored_input_file(staging_anchor, name, label)
            staged_files[name] = opened
            # Windows no-clobber publication moves an exact owned handle.  Record
            # path-created extractor files as transaction-owned only after opening.
            staging_anchor.owned_files[name] = opened.identity
        staged_media_anchor = anchored._open_existing_child_directory(
            staging_anchor,
            "media",
            "staged PDF media",
        )
        media_names = anchored._anchored_directory_names(staged_media_anchor)
        for name in media_names:
            opened = anchored._open_anchored_input_file(
                staged_media_anchor,
                name,
                "staged PDF media artifact",
            )
            staged_media_files[name] = opened
            staged_media_anchor.owned_files[name] = opened.identity
        anchored._verify_anchored_evidence(staging_anchor, staged_files)
        anchored._verify_anchored_evidence(staged_media_anchor, staged_media_files)
        if anchored._anchored_directory_names(staged_media_anchor) != media_names:
            raise PdfExtractionError(
                "staged PDF media child set does not exactly match held artifacts"
            )
        if anchored._anchored_directory_names(staging_anchor) != [
            "document.json",
            "media",
            "segments.jsonl",
            "source.json",
            "source.pdf",
        ]:
            raise PdfExtractionError(
                "private PDF extraction staging contains unexpected entries"
            )

        for name in ("document.json", "segments.jsonl"):
            try:
                published_files[name] = anchored._publish_new_file(
                    staging_anchor,
                    name,
                    run_anchor,
                    name,
                )
            except anchored.PdfAssemblyError as error:
                raise PdfExtractionError(
                    f"PDF extraction destination already exists: {run_dir / name}"
                ) from error
        try:
            published_media_anchor = anchored._create_child_directory(
                run_anchor,
                "media",
                "published PDF media",
            )
        except (FileExistsError, anchored.PdfAssemblyError) as error:
            raise PdfExtractionError(
                f"PDF extraction destination already exists: {run_dir / 'media'}"
            ) from error
        for name in media_names:
            try:
                published_media_files[name] = anchored._publish_new_file(
                    staged_media_anchor,
                    name,
                    published_media_anchor,
                    name,
                )
            except anchored.PdfAssemblyError as error:
                raise PdfExtractionError(
                    f"PDF extraction media destination already exists: "
                    f"{run_dir / 'media' / name}"
                ) from error

        run_anchor.verify_visible()
        anchored._verify_anchored_evidence(run_anchor, source_files)
        _verify_source_snapshots(run_dir, source_files, source_bytes, source_record_bytes)
        anchored._verify_anchored_evidence(
            run_anchor,
            {name: staged_files[name] for name in ("document.json", "segments.jsonl")},
        )
        anchored._verify_anchored_evidence(
            published_media_anchor,
            staged_media_files,
        )
        if anchored._anchored_directory_names(published_media_anchor) != media_names:
            raise PdfExtractionError(
                "published PDF media child set does not exactly match held artifacts"
            )
        completed = True
    except PdfExtractionError:
        raise
    except (anchored.PdfAssemblyError, PdfContractError) as error:
        raise PdfExtractionError(
            f"cannot complete anchored PDF extraction: {error}"
        ) from error
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise PdfExtractionError(
            f"cannot complete anchored PDF extraction: {error}"
        ) from error
    finally:
        active_failure = not completed or sys.exc_info()[0] is not None
        if active_failure and run_anchor is not None:
            for name, published in reversed(tuple(published_files.items())):
                anchored._remove_owned_file(run_anchor, name, published)
            if published_media_anchor is not None:
                for name, published in reversed(tuple(published_media_files.items())):
                    anchored._remove_owned_file(
                        published_media_anchor,
                        name,
                        published,
                    )
                anchored._remove_owned_directory(
                    run_anchor,
                    "media",
                    published_media_anchor.identity,
                    child=published_media_anchor,
                )

        for opened in (
            *source_files.values(),
            *staged_files.values(),
            *staged_media_files.values(),
        ):
            anchored._close_opened_file(opened)
        for published in (*published_files.values(), *published_media_files.values()):
            anchored._close_published_file(published)

        if staged_media_anchor is not None:
            for name, opened in reversed(tuple(staged_media_files.items())):
                anchored._remove_owned_file(
                    staged_media_anchor,
                    name,
                    anchored._PublishedFile(opened.identity),
                )
            if staging_anchor is not None:
                anchored._remove_owned_directory(
                    staging_anchor,
                    "media",
                    staged_media_anchor.identity,
                    child=staged_media_anchor,
                )
            staged_media_anchor.close()
        if staging_anchor is not None:
            for name, opened in reversed(tuple(staged_files.items())):
                anchored._remove_owned_file(
                    staging_anchor,
                    name,
                    anchored._PublishedFile(opened.identity),
                )
            for name in ("source.json", "source.pdf"):
                identity = staging_anchor.owned_files.get(name)
                if identity is not None:
                    anchored._remove_owned_file(
                        staging_anchor,
                        name,
                        anchored._PublishedFile(identity),
                    )
            if run_anchor is not None and staging_name is not None:
                anchored._remove_owned_directory(
                    run_anchor,
                    staging_name,
                    staging_anchor.identity,
                    child=staging_anchor,
                )
            staging_anchor.close()
        if published_media_anchor is not None:
            published_media_anchor.close()
        if run_anchor is not None:
            run_anchor.close()


def _write_private_file(
    directory: anchored._DirectoryAnchor,
    name: str,
    payload: bytes,
) -> None:
    opened = anchored._create_anchored_binary_file(directory, name)
    try:
        opened.stream.write(payload)
        anchored._finalize_opened_file(opened, f"private extraction {name}")
    finally:
        anchored._close_opened_file(opened)


def _read_source_record(payload: bytes) -> PdfSourceRecord:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PdfExtractionError(f"cannot read held source.json: {error}") from error
    if not isinstance(value, Mapping):
        raise PdfExtractionError("held source.json must be a JSON object")
    try:
        return PdfSourceRecord.from_dict(value)
    except PdfContractError as error:
        raise PdfExtractionError(f"invalid held source.json: {error}") from error


def _verify_source_snapshots(
    run_dir: Path,
    source_files: Mapping[str, anchored._OpenedFile],
    source_bytes: bytes,
    source_record_bytes: bytes,
) -> None:
    for name, expected in (
        ("source.pdf", source_bytes),
        ("source.json", source_record_bytes),
    ):
        current = anchored._read_opened_bytes(
            source_files[name],
            run_dir / name,
            f"held {name}",
        )
        if hashlib.sha256(current).digest() != hashlib.sha256(expected).digest():
            raise PdfExtractionError(f"{name} content changed during extraction")


__all__ = ["extract_pdf_transaction"]
