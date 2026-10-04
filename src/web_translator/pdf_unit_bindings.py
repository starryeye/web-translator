"""Exact-byte assignment bindings for native logical-unit PDF extractions."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from web_translator.pdf_models import PdfContractError, PdfDocument, PdfSourceRecord


PDF_UNIT_BINDING_NAME = ".pdf-unit-binding.json"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ZONE_FILE = re.compile(r"zone-\d{3}\.json\Z")


class PdfUnitBindingError(ValueError):
    """A logical-unit assignment binding is missing, stale, or unsafe."""


@dataclass(frozen=True, slots=True)
class PdfUnitBinding:
    schema_version: str
    document_sha256: str
    segments_sha256: str
    zones: Mapping[str, str]
    assignments: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.schema_version != "1.0":
            raise PdfUnitBindingError("PDF binding schema must be 1.0")
        for digest in (self.document_sha256, self.segments_sha256):
            _require_digest(digest)
        for name in ("zones", "assignments"):
            values = getattr(self, name)
            if not isinstance(values, Mapping) or not values:
                raise PdfUnitBindingError(f"PDF binding {name} must be a nonempty map")
            for filename, digest in values.items():
                if not isinstance(filename, str) or not _ZONE_FILE.fullmatch(filename):
                    raise PdfUnitBindingError(f"PDF binding has invalid {name} filename")
                _require_digest(digest)
            object.__setattr__(self, name, MappingProxyType(dict(values)))
        if self.zones.keys() != self.assignments.keys():
            raise PdfUnitBindingError("PDF binding zones and assignments must exactly match")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version,
                "document_sha256": self.document_sha256,
                "segments_sha256": self.segments_sha256,
                "zones": dict(self.zones), "assignments": dict(self.assignments)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PdfUnitBinding:
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version", "document_sha256", "segments_sha256", "zones", "assignments"
        }:
            raise PdfUnitBindingError("PDF binding fields must be exact")
        return cls(**value)


def _require_digest(value: object) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise PdfUnitBindingError("PDF binding SHA-256 digest is malformed")


def build_pdf_unit_binding(
    document_bytes: bytes, segments_bytes: bytes,
    zone_payloads: Mapping[str, bytes], assignment_payloads: Mapping[str, bytes],
) -> PdfUnitBinding:
    digest = lambda payload: hashlib.sha256(payload).hexdigest()
    return PdfUnitBinding(
        "1.0", digest(document_bytes), digest(segments_bytes),
        {name: digest(payload) for name, payload in zone_payloads.items()},
        {name: digest(payload) for name, payload in assignment_payloads.items()},
    )


def require_assignable_pdf(document: PdfDocument) -> None:
    if document.schema_version != "1.2" or document.extracted_schema_version != "1.2":
        raise PdfUnitBindingError("PDF assignments require native 1.2 extraction")
    required = [finding.code for finding in document.flow_findings if finding.severity == "required"]
    if required:
        raise PdfUnitBindingError("PDF assignments have required flow findings: " + ", ".join(required))


def _binding_from_payloads(payloads: Mapping[str, bytes]) -> PdfUnitBinding:
    """Validate a binding against bytes already held by its consuming reader."""
    try:
        document = PdfDocument.from_dict(json.loads(payloads["document.json"]))
        source = PdfSourceRecord.from_dict(json.loads(payloads["source.json"]))
        if source.sha256 != document.source_sha256:
            raise PdfUnitBindingError("PDF binding source/document SHA-256 mismatch")
        require_assignable_pdf(document)
        actual = PdfUnitBinding.from_dict(json.loads(payloads[f"assignments/{PDF_UNIT_BINDING_NAME}"]))
        expected = build_pdf_unit_binding(
            payloads["document.json"], payloads["segments.jsonl"],
            {name.removeprefix("zones/"): value for name, value in payloads.items() if name.startswith("zones/")},
            {name.removeprefix("assignments/"): value for name, value in payloads.items()
             if name.startswith("assignments/") and name != f"assignments/{PDF_UNIT_BINDING_NAME}"},
        )
        if actual != expected:
            raise PdfUnitBindingError("PDF binding does not match current exact input bytes")
        return actual
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, PdfContractError) as error:
        raise PdfUnitBindingError(f"invalid PDF binding inputs: {error}") from error


@dataclass(slots=True)
class _PdfUnitInputs:
    run_anchor: Any
    roots: dict[str, Any]
    directories: dict[str, Any]
    files: dict[str, dict[str, Any]]
    payloads: dict[str, bytes]
    document: PdfDocument

    def verify(self) -> None:
        import web_translator.pdf_assemble as anchored
        try:
            anchored._verify_anchored_evidence(self.run_anchor, self.roots)
            for name, directory in self.directories.items():
                if anchored._anchored_directory_names(directory) != sorted(self.files[name]):
                    raise PdfUnitBindingError(f"PDF binding directory changed child set: {name}")
                anchored._verify_anchored_evidence(directory, self.files[name])
            for relative, payload in self.payloads.items():
                parts = relative.split("/")
                directory = self.run_anchor if len(parts) == 1 else self.directories[parts[0]]
                opened = self.roots[relative] if len(parts) == 1 else self.files[parts[0]][parts[1]]
                if anchored._read_opened_bytes(opened, directory.path / parts[-1], "PDF binding input") != payload:
                    raise PdfUnitBindingError(f"PDF binding input changed content: {relative}")
        except (anchored.PdfAssemblyError, OSError) as error:
            raise PdfUnitBindingError(f"PDF binding: {error}") from error


@contextmanager
def _hold_pdf_unit_inputs(run: Path | Any, directories: tuple[str, ...]) -> Iterator[_PdfUnitInputs]:
    """Retain source handles before packages exist, or through bound consumption."""
    import web_translator.pdf_assemble as anchored
    yield_started = False
    try:
        with ExitStack() as stack:
            root = run
            if isinstance(run, (str, os.PathLike)):
                root = anchored._open_directory_anchor(Path(run), "PDF binding run")
                stack.callback(root.close)
            roots: dict[str, Any] = {}
            children: dict[str, Any] = {}
            files: dict[str, dict[str, Any]] = {}
            payloads: dict[str, bytes] = {}
            for name in ("source.json", "document.json", "segments.jsonl"):
                opened = anchored._open_anchored_input_file(root, name, "PDF binding input")
                stack.callback(anchored._close_opened_file, opened)
                roots[name] = opened
                payloads[name] = anchored._read_opened_bytes(opened, root.path / name, "PDF binding input")
            document = PdfDocument.from_dict(json.loads(payloads["document.json"]))
            source = PdfSourceRecord.from_dict(json.loads(payloads["source.json"]))
            if source.sha256 != document.source_sha256:
                raise PdfUnitBindingError("PDF binding source/document SHA-256 mismatch")
            for name in directories:
                directory = anchored._open_existing_child_directory(root, name, "PDF binding directory")
                stack.callback(directory.close)
                children[name] = directory
                files[name] = {}
                names = anchored._anchored_directory_names(directory)
                package_names = [item for item in names if not (name == "assignments" and item == PDF_UNIT_BINDING_NAME)]
                if not package_names or any(not _ZONE_FILE.fullmatch(item) for item in package_names):
                    raise PdfUnitBindingError(f"PDF binding {name} must contain only zone-NNN.json files")
                for filename in names:
                    opened = anchored._open_anchored_input_file(directory, filename, "PDF binding input")
                    stack.callback(anchored._close_opened_file, opened)
                    files[name][filename] = opened
                    payloads[f"{name}/{filename}"] = anchored._read_opened_bytes(opened, directory.path / filename, "PDF binding input")
            snapshot = _PdfUnitInputs(root, roots, children, files, payloads, document)
            snapshot.verify()
            yield_started = True
            yield snapshot
            snapshot.verify()
    except (anchored.PdfAssemblyError, OSError, PdfContractError, UnicodeError, json.JSONDecodeError) as error:
        if yield_started:
            raise
        raise PdfUnitBindingError(f"PDF binding: {error}") from error


@contextmanager
def hold_pdf_unit_binding(run: Path | Any) -> Iterator[PdfUnitBinding]:
    """Hold and verify exact bound inputs throughout the caller's consumption."""
    with _hold_pdf_unit_inputs(run, ("zones", "assignments")) as snapshot:
        yield _binding_from_payloads(snapshot.payloads)


@contextmanager
def _publish_pdf_unit_assignments(
    inputs: _PdfUnitInputs, temporary: Path,
) -> Iterator[PdfUnitBinding]:
    """Hold staged packages through no-clobber publication and owned rollback.

    The caller writes the yielded binding into the held temporary directory.
    Successful exit validates that file and publishes the whole directory.
    """
    import web_translator.pdf_assemble as anchored
    from web_translator.pdf_qa import PdfQAFailure, _rename_anchored_directory_no_replace

    try:
        with ExitStack() as stack:
            parent = anchored._open_existing_child_directory(
                inputs.run_anchor, temporary.parent.name, "assignment staging parent"
            )
            stack.callback(parent.close)
            staged = anchored._open_existing_child_directory(
                parent, temporary.name, "staged assignments"
            )
            stack.callback(staged.close)
            opened_files: dict[str, Any] = {}
            payloads = dict(inputs.payloads)
            expected_names = sorted(inputs.files["zones"])
            if anchored._anchored_directory_names(staged) != expected_names:
                raise PdfUnitBindingError("staged assignment filenames do not match zones")
            for name in expected_names:
                opened = anchored._open_anchored_input_file(staged, name, "staged assignment")
                stack.callback(anchored._close_opened_file, opened)
                opened_files[name] = opened
                payloads[f"assignments/{name}"] = anchored._read_opened_bytes(
                    opened, staged.path / name, "staged assignment"
                )
            snapshot = _PdfUnitInputs(
                inputs.run_anchor, inputs.roots,
                {**inputs.directories, "assignments": staged},
                {**inputs.files, "assignments": opened_files},
                payloads, inputs.document,
            )
            snapshot.verify()
            binding = build_pdf_unit_binding(
                payloads["document.json"], payloads["segments.jsonl"],
                {name: payloads[f"zones/{name}"] for name in expected_names},
                {name: payloads[f"assignments/{name}"] for name in expected_names},
            )
            completed = False
            publication_handle: int | None = None
            try:
                yield binding
                opened = anchored._open_anchored_input_file(
                    staged, PDF_UNIT_BINDING_NAME, "staged PDF binding"
                )
                stack.callback(anchored._close_opened_file, opened)
                opened_files[PDF_UNIT_BINDING_NAME] = opened
                payloads[f"assignments/{PDF_UNIT_BINDING_NAME}"] = anchored._read_opened_bytes(
                    opened, staged.path / PDF_UNIT_BINDING_NAME, "staged PDF binding"
                )
                _binding_from_payloads(payloads)
                snapshot.verify()
                parent.verify_visible()
                anchored._require_anchored_name_absent(inputs.run_anchor, "assignments")
                publication_handle = _rename_anchored_directory_no_replace(
                    parent, temporary.name, staged.identity, inputs.run_anchor, "assignments",
                    retain_windows_handle=True,
                )
                if publication_handle is not None:
                    stack.callback(anchored.pdf_acquire_module._close_windows_handle, publication_handle)
                staged.path = inputs.run_anchor.path / "assignments"
                snapshot.verify()
                completed = True
            finally:
                if not completed:
                    # A rename may succeed and then raise: inspect ownership, not
                    # a success flag. Never remove a racer's directory or files.
                    staged.path = inputs.run_anchor.path / "assignments"
                    try:
                        staged.verify_visible()
                    except anchored.PdfAssemblyError:
                        pass
                    else:
                        for name, opened in opened_files.items():
                            anchored._close_opened_file(opened)
                            anchored._remove_owned_file(
                                staged, name, anchored._PublishedFile(opened.identity)
                            )
                        if publication_handle is not None:
                            # The retained rename handle has DELETE access; the
                            # ordinary held-directory reader deliberately does not.
                            if not anchored._anchored_directory_names(staged):
                                anchored._windows_delete_open_file(publication_handle)
                        else:
                            anchored._remove_owned_directory(
                                inputs.run_anchor, "assignments", staged.identity, child=staged
                            )
    except (anchored.PdfAssemblyError, PdfQAFailure, OSError) as error:
        raise PdfUnitBindingError(f"cannot publish PDF assignment binding: {error}") from error
