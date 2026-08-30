"""Canonical PDF-only semantic-review input binding."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any


TERMINOLOGY_POLICY_ID = "english-technical-first-use-ko-gloss"
TERMINOLOGY_POLICY_VERSION = "1.0"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ZONE = re.compile(r"zone-\d{3}\Z")
_REPARSE_POINT = 0x400


class PdfSemanticReviewError(ValueError):
    """PDF semantic-review inputs or their digest are unsafe or inconsistent."""


@dataclass(frozen=True, slots=True)
class PdfSemanticInputFile:
    path: str
    byte_length: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "byte_length": self.byte_length,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PdfSemanticInputFile:
        if set(value) != {"path", "byte_length", "sha256"}:
            raise PdfSemanticReviewError("semantic input file fields are not exact")
        path = value["path"]
        byte_length = value["byte_length"]
        sha256 = value["sha256"]
        if not isinstance(path, str) or not path:
            raise PdfSemanticReviewError("semantic input file path is invalid")
        if type(byte_length) is not int or byte_length < 0:
            raise PdfSemanticReviewError("semantic input file byte length is invalid")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise PdfSemanticReviewError("semantic input file SHA-256 is invalid")
        return cls(path=path, byte_length=byte_length, sha256=sha256)


@dataclass(frozen=True, slots=True)
class PdfSemanticReviewInput:
    schema_version: str
    semantic_input_sha256: str
    terminology_policy: dict[str, str]
    files: tuple[PdfSemanticInputFile, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "semantic_input_sha256": self.semantic_input_sha256,
            "terminology_policy": dict(self.terminology_policy),
            "files": [record.to_dict() for record in self.files],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PdfSemanticReviewInput:
        if set(value) != {
            "schema_version",
            "semantic_input_sha256",
            "terminology_policy",
            "files",
        }:
            raise PdfSemanticReviewError("semantic review input fields are not exact")
        if value["schema_version"] != "1.0":
            raise PdfSemanticReviewError("semantic review input schema is unsupported")
        digest = value["semantic_input_sha256"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise PdfSemanticReviewError("semantic review input digest is invalid")
        policy = value["terminology_policy"]
        expected_policy = _terminology_policy()
        if not isinstance(policy, Mapping) or dict(policy) != expected_policy:
            raise PdfSemanticReviewError("semantic review terminology policy is invalid")
        files = value["files"]
        if not isinstance(files, list):
            raise PdfSemanticReviewError("semantic review input files must be an array")
        parsed = tuple(
            PdfSemanticInputFile.from_dict(_mapping(item, "semantic input file"))
            for item in files
        )
        if [item.path for item in parsed] != sorted(item.path for item in parsed):
            raise PdfSemanticReviewError("semantic review input files must be sorted")
        return cls("1.0", digest, expected_policy, parsed)


@dataclass(slots=True)
class PdfSemanticInputSnapshot:
    """Held exact semantic inputs and the digest derived from those same bytes."""

    run_anchor: Any
    root_files: dict[str, Any]
    directories: dict[str, Any]
    directory_files: dict[str, dict[str, Any]]
    payloads: dict[str, bytes]
    review_input: PdfSemanticReviewInput
    owns_run_anchor: bool

    def verify(self) -> None:
        import web_translator.pdf_assemble as anchored

        try:
            self.run_anchor.verify_visible()
            anchored._verify_anchored_evidence(self.run_anchor, self.root_files)
            for directory_name, directory in self.directories.items():
                directory.verify_visible()
                opened = self.directory_files[directory_name]
                if anchored._anchored_directory_names(directory) != sorted(opened):
                    raise PdfSemanticReviewError(
                        f"PDF semantic input directory changed child set: {directory_name}"
                    )
                anchored._verify_anchored_evidence(directory, opened)
            for relative, expected in self.payloads.items():
                if "/" in relative:
                    directory_name, name = relative.split("/", 1)
                    directory = self.directories[directory_name]
                    opened = self.directory_files[directory_name][name]
                else:
                    directory = self.run_anchor
                    opened = self.root_files[relative]
                current = anchored._read_opened_bytes(
                    opened,
                    directory.path / (name if "/" in relative else relative),
                    f"PDF semantic input {relative}",
                )
                if current != expected:
                    raise PdfSemanticReviewError(
                        f"PDF semantic input changed content: {relative}"
                    )
        except PdfSemanticReviewError:
            raise
        except anchored.PdfAssemblyError as error:
            raise PdfSemanticReviewError(str(error)) from error


@contextmanager
def hold_pdf_semantic_inputs(run: Path | Any) -> Iterator[PdfSemanticInputSnapshot]:
    """Hold every reviewed file and directory from snapshot through consumption."""
    import web_translator.pdf_assemble as anchored

    owns_run = isinstance(run, (str, os.PathLike, Path))
    run_anchor = None
    root_files: dict[str, Any] = {}
    directories: dict[str, Any] = {}
    directory_files: dict[str, dict[str, Any]] = {}
    yield_started = False
    try:
        run_anchor = (
            anchored._open_directory_anchor(Path(run), "PDF run")
            if owns_run
            else run
        )
        for name in ("segments.jsonl", "glossary.json"):
            root_files[name] = anchored._open_anchored_input_file(
                run_anchor, name, f"PDF semantic input {name}"
            )
        payloads = {
            name: anchored._read_opened_bytes(
                opened, run_anchor.path / name, f"PDF semantic input {name}"
            )
            for name, opened in root_files.items()
        }
        zone_ids: dict[str, set[str]] = {}
        for directory_name, suffix in (
            ("zones", ".json"),
            ("assignments", ".json"),
            ("translations", ".jsonl"),
        ):
            directory = anchored._open_existing_child_directory(
                run_anchor, directory_name, f"PDF {directory_name}"
            )
            directories[directory_name] = directory
            names = anchored._anchored_directory_names(directory)
            stems = {
                name[: -len(suffix)]
                for name in names
                if name.endswith(suffix)
                and _ZONE.fullmatch(name[: -len(suffix)])
            }
            if not names or len(stems) != len(names):
                raise PdfSemanticReviewError(
                    f"PDF {directory_name} must contain only zone-NNN{suffix} files"
                )
            zone_ids[directory_name] = stems
            opened_files: dict[str, Any] = {}
            directory_files[directory_name] = opened_files
            for name in names:
                opened = anchored._open_anchored_input_file(
                    directory, name, f"PDF semantic input {directory_name}/{name}"
                )
                opened_files[name] = opened
                relative = f"{directory_name}/{name}"
                payloads[relative] = anchored._read_opened_bytes(
                    opened, directory.path / name, f"PDF semantic input {relative}"
                )
        if len({frozenset(value) for value in zone_ids.values()}) != 1:
            raise PdfSemanticReviewError(
                "PDF zones, assignments, and translations must exactly cover the same zones"
            )
        files = tuple(
            PdfSemanticInputFile(
                path=path,
                byte_length=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
            for path, payload in sorted(payloads.items())
        )
        snapshot = PdfSemanticInputSnapshot(
            run_anchor=run_anchor,
            root_files=root_files,
            directories=directories,
            directory_files=directory_files,
            payloads=payloads,
            review_input=PdfSemanticReviewInput(
                "1.0", _semantic_digest(payloads), _terminology_policy(), files
            ),
            owns_run_anchor=owns_run,
        )
        snapshot.verify()
        yield_started = True
        yield snapshot
    except PdfSemanticReviewError:
        raise
    except anchored.PdfAssemblyError as error:
        if yield_started:
            raise
        raise PdfSemanticReviewError(str(error)) from error
    finally:
        for opened_files in directory_files.values():
            for opened in opened_files.values():
                anchored._close_opened_file(opened)
        for directory in directories.values():
            directory.close()
        for opened in root_files.values():
            anchored._close_opened_file(opened)
        if owns_run and run_anchor is not None:
            run_anchor.close()


def build_pdf_semantic_review_input(run_dir: Path) -> PdfSemanticReviewInput:
    """Snapshot every exact file reviewed by the PDF semantic master."""
    with hold_pdf_semantic_inputs(Path(run_dir)) as snapshot:
        return snapshot.review_input


def validate_pdf_semantic_review(
    run_dir: Path,
    review: Mapping[str, Any],
) -> PdfSemanticReviewInput:
    """Require one strict PDF review to match the current reviewed inputs."""
    with hold_pdf_semantic_inputs(Path(run_dir)) as snapshot:
        return validate_pdf_semantic_review_snapshot(snapshot, review)


def validate_pdf_semantic_review_snapshot(
    snapshot: PdfSemanticInputSnapshot,
    review: Mapping[str, Any],
) -> PdfSemanticReviewInput:
    """Validate review evidence against an already-held consumed snapshot."""
    expected_fields = {
        "semantic_input_sha256",
        "retries",
        "section_findings",
        "unresolved_required",
    }
    if set(review) != expected_fields:
        raise PdfSemanticReviewError("PDF semantic review fields are not exact")
    digest = review.get("semantic_input_sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise PdfSemanticReviewError("PDF semantic review digest is invalid")
    snapshot.verify()
    semantic_input = snapshot.review_input
    if digest != semantic_input.semantic_input_sha256:
        raise PdfSemanticReviewError(
            "PDF semantic review digest does not match current reviewed inputs"
        )
    return semantic_input


def _semantic_digest(payloads: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256(b"web-translator:pdf-semantic-review-input:v1\0")
    policy_payload = json.dumps(
        _terminology_policy(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    records = {**payloads, "@terminology-policy.json": policy_payload}
    for path, payload in sorted(records.items()):
        path_bytes = path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _terminology_policy() -> dict[str, str]:
    return {
        "policy_id": TERMINOLOGY_POLICY_ID,
        "policy_version": TERMINOLOGY_POLICY_VERSION,
    }


def _require_safe_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PdfSemanticReviewError(f"cannot inspect {label}: {error}") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    ):
        raise PdfSemanticReviewError(f"{label} is not a safe directory: {path}")


def _read_safe_file(run_dir: Path, relative: str) -> bytes:
    path = run_dir.joinpath(*relative.split("/"))
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise PdfSemanticReviewError(
                f"PDF semantic input is not a safe regular file: {relative}"
            )
        payload = path.read_bytes()
        after = path.lstat()
    except PdfSemanticReviewError:
        raise
    except OSError as error:
        raise PdfSemanticReviewError(
            f"cannot read PDF semantic input {relative}: {error}"
        ) from error
    if (metadata.st_dev, metadata.st_ino) != (after.st_dev, after.st_ino):
        raise PdfSemanticReviewError(
            f"PDF semantic input changed identity while reading: {relative}"
        )
    return payload


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PdfSemanticReviewError(f"{label} must be an object")
    return value  # type: ignore[return-value]


__all__ = [
    "PdfSemanticInputFile",
    "PdfSemanticReviewError",
    "PdfSemanticReviewInput",
    "build_pdf_semantic_review_input",
    "hold_pdf_semantic_inputs",
    "validate_pdf_semantic_review_snapshot",
    "validate_pdf_semantic_review",
]
