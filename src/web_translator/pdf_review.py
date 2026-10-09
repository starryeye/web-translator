"""Canonical PDF-only semantic-review input binding."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from web_translator.pdf_models import PdfContractError, PdfDocument
from web_translator.pdf_figure_review import (
    FIGURE_TEXT_INPUT_NAME, PdfFigureReviewError, validate_pdf_figure_text_review,
)
from web_translator.models import SegmentContractError, read_segments_stream
from web_translator.pdf_unit_bindings import (
    PDF_UNIT_BINDING_NAME, PdfUnitBindingError, _binding_from_payloads,
)


TERMINOLOGY_POLICY_ID = "korean-first-technical-terms"
TERMINOLOGY_POLICY_VERSION = "2.0"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ZONE = re.compile(r"zone-\d{3}\Z")
_REPARSE_POINT = 0x400


class PdfSemanticReviewError(ValueError):
    """PDF semantic-review inputs or their digest are unsafe or inconsistent."""


def _master_review_from_bytes(payload: bytes) -> Mapping[str, Any]:
    """Reject ambiguous duplicate fields before interpreting master decisions."""
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise PdfSemanticReviewError(f"PDF master review contains duplicate field: {key}")
            result[key] = value
        return result
    try:
        return _mapping(json.loads(payload, object_pairs_hook=unique), "PDF master review")
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PdfSemanticReviewError(str(error)) from error


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
        if [item.path for item in parsed] != sorted({item.path for item in parsed}):
            raise PdfSemanticReviewError("semantic review input files must be sorted and unique")
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
            if (FIGURE_TEXT_INPUT_NAME not in self.payloads
                    and FIGURE_TEXT_INPUT_NAME in anchored._anchored_directory_names(self.run_anchor)):
                raise PdfSemanticReviewError("PDF figure inventory appeared after semantic snapshot")
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
        logical_units = False
        if "document.json" not in anchored._anchored_directory_names(run_anchor):
            raise PdfSemanticReviewError("PDF semantic review requires a native 1.2 document")
        if "document.json" in anchored._anchored_directory_names(run_anchor):
            opened = anchored._open_anchored_input_file(run_anchor, "document.json", "PDF document")
            root_files["document.json"] = opened
            document_bytes = anchored._read_opened_bytes(opened, run_anchor.path / "document.json", "PDF document")
            document = PdfDocument.from_dict(json.loads(document_bytes))
            from web_translator.pdf_unit_bindings import require_assignable_pdf
            require_assignable_pdf(document)
            logical_units = document.schema_version == "1.2"
            if logical_units:
                payloads["document.json"] = document_bytes
                opened = anchored._open_anchored_input_file(run_anchor, "source.json", "PDF source record")
                root_files["source.json"] = opened
                payloads["source.json"] = anchored._read_opened_bytes(opened, run_anchor.path / "source.json", "PDF source record")
            inventory_present = FIGURE_TEXT_INPUT_NAME in anchored._anchored_directory_names(run_anchor)
            if any(block.kind == "figure" for block in document.blocks) and not inventory_present:
                raise PdfSemanticReviewError("PDF figures require figure-text-input.json; run pdf-figure-review-input and inspect every source figure")
            if inventory_present:
                opened = anchored._open_anchored_input_file(run_anchor, FIGURE_TEXT_INPUT_NAME, "PDF figure inventory")
                root_files[FIGURE_TEXT_INPUT_NAME] = opened
                payloads[FIGURE_TEXT_INPUT_NAME] = anchored._read_opened_bytes(opened, run_anchor.path / FIGURE_TEXT_INPUT_NAME, "PDF figure inventory")
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
            package_names = [name for name in names if not (
                logical_units and directory_name == "assignments" and name == PDF_UNIT_BINDING_NAME
            )]
            stems = {
                name[: -len(suffix)]
                for name in names
                if name.endswith(suffix)
                and _ZONE.fullmatch(name[: -len(suffix)])
            }
            if not package_names or len(stems) != len(package_names):
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
        if logical_units:
            _binding_from_payloads(payloads)
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
    except (PdfUnitBindingError, PdfContractError, UnicodeError, json.JSONDecodeError) as error:
        raise PdfSemanticReviewError(str(error)) from error
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
    if not expected_fields <= set(review) <= expected_fields | {"preserved_names", "figure_text_review"}:
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
    inventory = snapshot.payloads.get(FIGURE_TEXT_INPUT_NAME)
    if inventory is not None or "figure_text_review" in review:
        if inventory is None or "figure_text_review" not in review:
            raise PdfSemanticReviewError("PDF figure inventory and figure_text_review must be supplied together; inspect every source figure")
        try:
            validate_pdf_figure_text_review(review["figure_text_review"], inventory_bytes=inventory)
        except PdfFigureReviewError as error:
            raise PdfSemanticReviewError(str(error)) from error
    if "preserved_names" in review:
        names = parse_pdf_preserved_names(review["preserved_names"])
        try:
            segments = {item.id: item for item in read_segments_stream(
                io.StringIO(snapshot.payloads["segments.jsonl"].decode("utf-8"))
            )}
            translations = {}
            for path, payload in snapshot.payloads.items():
                if path.startswith("translations/"):
                    for line in payload.decode("utf-8").splitlines():
                        if line.strip():
                            record = json.loads(line)
                            translations[record["segment_id"]] = record["text"]
        except (ValueError, KeyError, TypeError, SegmentContractError) as error:
            raise PdfSemanticReviewError("cannot validate preserved name inputs") from error
        for segment_id, records in names.items():
            segment = segments.get(segment_id)
            translation = translations.get(segment_id)
            if segment is None or not segment.target or not isinstance(translation, str):
                raise PdfSemanticReviewError("preserved name refers to an unknown translation target")
            source = segment.source_text
            for token in segment.protected:
                source = source.replace(token.token, token.value)
                translation = translation.replace(token.token, token.value)
            for record in records:
                pattern = preserved_name_pattern(record["text"])
                if pattern.search(source) is None or pattern.search(translation) is None:
                    raise PdfSemanticReviewError("preserved name is not exact in source and translation")
    return semantic_input


def preserved_name_pattern(text: str) -> re.Pattern[str]:
    """Keep exact name spelling while allowing attached Korean particles."""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(text)}(?![A-Za-z0-9_])")


def parse_pdf_preserved_names(value: Any) -> dict[str, list[dict[str, str]]]:
    """Validate explicit master semantic evidence; never infer names from case."""
    if not isinstance(value, Mapping):
        raise PdfSemanticReviewError("preserved names must map segment IDs to reviewed records")
    result: dict[str, list[dict[str, str]]] = {}
    for segment_id, records in value.items():
        if not isinstance(segment_id, str) or re.fullmatch(r"seg-\d{6}", segment_id) is None:
            raise PdfSemanticReviewError("preserved name segment ID is invalid")
        if not isinstance(records, list) or not records or len(records) > 64:
            raise PdfSemanticReviewError("preserved name records must be a nonempty bounded array")
        seen = set()
        canonical = []
        for record in records:
            if not isinstance(record, Mapping) or set(record) != {"text", "reason"}:
                raise PdfSemanticReviewError("preserved name fields must be text and reason")
            text, reason = record["text"], record["reason"]
            if (
                not isinstance(text, str) or not text.strip() or text != text.strip()
                or len(text) > 160 or any(ord(character) < 32 for character in text)
                or text in seen or not isinstance(reason, str) or not reason.strip()
            ):
                raise PdfSemanticReviewError("preserved name text/reason is invalid or duplicated")
            seen.add(text)
            canonical.append({"text": text, "reason": reason})
        result[segment_id] = canonical
    return result


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
