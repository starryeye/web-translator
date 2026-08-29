"""Canonical PDF-only semantic-review input binding."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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


def build_pdf_semantic_review_input(run_dir: Path) -> PdfSemanticReviewInput:
    """Snapshot every exact file reviewed by the PDF semantic master."""
    run_dir = Path(os.path.abspath(run_dir))
    _require_safe_directory(run_dir, "PDF run")
    payloads: dict[str, bytes] = {
        "segments.jsonl": _read_safe_file(run_dir, "segments.jsonl"),
        "glossary.json": _read_safe_file(run_dir, "glossary.json"),
    }
    zone_ids: dict[str, set[str]] = {}
    for directory_name, suffix in (
        ("zones", ".json"),
        ("assignments", ".json"),
        ("translations", ".jsonl"),
    ):
        directory = run_dir / directory_name
        _require_safe_directory(directory, f"PDF {directory_name}")
        try:
            names = sorted(path.name for path in directory.iterdir())
        except OSError as error:
            raise PdfSemanticReviewError(
                f"cannot inspect PDF {directory_name}: {error}"
            ) from error
        stems = {
            name[: -len(suffix)]
            for name in names
            if name.endswith(suffix) and _ZONE.fullmatch(name[: -len(suffix)])
        }
        if not names or len(stems) != len(names):
            raise PdfSemanticReviewError(
                f"PDF {directory_name} must contain only zone-NNN{suffix} files"
            )
        zone_ids[directory_name] = stems
        for name in names:
            relative = f"{directory_name}/{name}"
            payloads[relative] = _read_safe_file(run_dir, relative)
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
    digest = _semantic_digest(payloads)
    return PdfSemanticReviewInput("1.0", digest, _terminology_policy(), files)


def validate_pdf_semantic_review(
    run_dir: Path,
    review: Mapping[str, Any],
) -> PdfSemanticReviewInput:
    """Require one strict PDF review to match the current reviewed inputs."""
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
    semantic_input = build_pdf_semantic_review_input(run_dir)
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
    "validate_pdf_semantic_review",
]
