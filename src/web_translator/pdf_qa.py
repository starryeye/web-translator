"""Fail-closed automated QA preparation for staged translated PDFs."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
from typing import Any

import pdfplumber
from PIL import Image
from pypdf import PdfReader
from pypdf.generic import ArrayObject

import web_translator.pdf_assemble as assembly
from web_translator.models import Segment, Translation, read_segments_stream
from web_translator.pdf_flowables import PdfAssemblyError, PdfAssemblyLayout
from web_translator.pdf_media import (
    PdfMediaError,
    build_contact_sheets,
    render_pdf_pages,
)
from web_translator.pdf_models import PdfContractError, PdfDocument, PdfSourceRecord


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HANGUL = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")
_TOKEN = re.compile(r"⟦WT:\d{6}⟧")
_PAGE_NAME = re.compile(r"page-(\d{3})\.png\Z")
_RAW_PAGE_NAME = re.compile(r"page-\d+\.png\Z")
_CONTACT_NAME = re.compile(r"contact-sheet-(\d{3})\.png\Z")
_REVIEW_DIMENSIONS = {
    "semantic_fidelity",
    "qualification_preservation",
    "naturalness",
    "terminology",
    "boundary_consistency",
    "protected_content",
}


class PdfQAFailure(RuntimeError):
    """The staged PDF or its immutable run evidence failed automated QA."""


@dataclass(frozen=True, slots=True)
class PdfQAFinding:
    code: str
    evidence: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "evidence": self.evidence}


@dataclass(frozen=True, slots=True)
class PdfQAResult:
    schema_version: str
    staged_pdf_sha256: str
    findings: tuple[PdfQAFinding, ...]
    rendered_pages: tuple[Path, ...]
    rendered_page_hashes: dict[str, str]
    contact_sheet_pages: dict[str, list[int]]
    contact_sheet_hashes: dict[str, str]
    metrics: dict[str, int]
    passed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "contact_sheet_hashes": dict(self.contact_sheet_hashes),
            "contact_sheet_pages": {
                name: list(pages) for name, pages in self.contact_sheet_pages.items()
            },
            "findings": [finding.to_dict() for finding in self.findings],
            "metrics": dict(self.metrics),
            "passed": self.passed,
            "rendered_page_hashes": dict(self.rendered_page_hashes),
            "schema_version": self.schema_version,
            "staged_pdf_sha256": self.staged_pdf_sha256,
        }

    @classmethod
    def from_dict(
        cls, value: object, qa_pages_dir: Path
    ) -> PdfQAResult:
        fields = {
            "contact_sheet_hashes",
            "contact_sheet_pages",
            "findings",
            "metrics",
            "passed",
            "rendered_page_hashes",
            "schema_version",
            "staged_pdf_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise PdfQAFailure("pdf-qa fields must be exactly " + ", ".join(sorted(fields)))
        if value["schema_version"] != "1.0":
            raise PdfQAFailure("pdf-qa schema_version must be '1.0'")
        staged_hash = _qa_hash(value["staged_pdf_sha256"], "staged_pdf_sha256")
        if type(value["passed"]) is not bool:
            raise PdfQAFailure("pdf-qa passed must be a boolean")
        raw_findings = value["findings"]
        if not isinstance(raw_findings, list):
            raise PdfQAFailure("pdf-qa findings must be an array")
        findings: list[PdfQAFinding] = []
        for index, item in enumerate(raw_findings):
            if not isinstance(item, Mapping) or set(item) != {"code", "evidence"}:
                raise PdfQAFailure(f"pdf-qa findings[{index}] fields must be exactly code, evidence")
            code = item["code"]
            evidence = item["evidence"]
            if not isinstance(code, str) or not code or not isinstance(evidence, str) or not evidence.strip():
                raise PdfQAFailure(f"pdf-qa findings[{index}] must contain nonempty strings")
            findings.append(PdfQAFinding(code, evidence))
        if [item.code for item in findings] != sorted({item.code for item in findings}):
            raise PdfQAFailure("pdf-qa finding codes must be sorted and unique")
        page_hashes = _qa_hash_mapping(
            value["rendered_page_hashes"], _PAGE_NAME, "rendered_page_hashes"
        )
        expected_page_names = [
            f"page-{number:03d}.png" for number in range(1, len(page_hashes) + 1)
        ]
        if list(page_hashes) != expected_page_names:
            raise PdfQAFailure("pdf-qa rendered page coverage must be exact and sequential")
        contact_hashes = _qa_hash_mapping(
            value["contact_sheet_hashes"], _CONTACT_NAME, "contact_sheet_hashes"
        )
        expected_contact_names = [
            f"contact-sheet-{number:03d}.png"
            for number in range(1, math.ceil(len(page_hashes) / 12) + 1)
        ]
        if list(contact_hashes) != expected_contact_names:
            raise PdfQAFailure(
                "pdf-qa contact-sheet names must be exact and sequential"
            )
        raw_contacts = value["contact_sheet_pages"]
        if not isinstance(raw_contacts, Mapping) or list(raw_contacts) != list(contact_hashes):
            raise PdfQAFailure("pdf-qa contact-sheet hashes and coverage must match exactly")
        contacts: dict[str, list[int]] = {}
        for name, raw_pages in raw_contacts.items():
            if not isinstance(name, str) or not isinstance(raw_pages, list):
                raise PdfQAFailure("pdf-qa contact-sheet coverage is malformed")
            if any(type(page) is not int or page <= 0 for page in raw_pages):
                raise PdfQAFailure("pdf-qa contact-sheet coverage must use positive integers")
            contacts[name] = list(raw_pages)
        covered = [page for pages in contacts.values() for page in pages]
        if covered != list(range(1, len(page_hashes) + 1)) or any(
            len(pages) > 12 for pages in contacts.values()
        ):
            raise PdfQAFailure("pdf-qa contact-sheet coverage must cover every page exactly once")
        metric_fields = {
            "contact_sheet_count",
            "embedded_font_count",
            "figure_count",
            "link_count",
            "output_page_count",
            "rendered_page_count",
            "translated_block_count",
        }
        raw_metrics = value["metrics"]
        if not isinstance(raw_metrics, Mapping) or set(raw_metrics) != metric_fields:
            raise PdfQAFailure("pdf-qa metrics fields are not exact")
        if any(type(item) is not int or item < 0 for item in raw_metrics.values()):
            raise PdfQAFailure("pdf-qa metrics must be nonnegative integers")
        metrics = dict(raw_metrics)  # type: ignore[arg-type]
        if (
            metrics["rendered_page_count"] != len(page_hashes)
            or metrics["output_page_count"] != len(page_hashes)
            or metrics["contact_sheet_count"] != len(contacts)
        ):
            raise PdfQAFailure("pdf-qa metrics disagree with page/contact coverage")
        qa_pages_dir = Path(qa_pages_dir)
        return cls(
            schema_version="1.0",
            staged_pdf_sha256=staged_hash,
            findings=tuple(findings),
            rendered_pages=tuple(qa_pages_dir / name for name in page_hashes),
            rendered_page_hashes=page_hashes,
            contact_sheet_pages=contacts,
            contact_sheet_hashes=contact_hashes,
            metrics=metrics,
            passed=value["passed"],  # type: ignore[arg-type]
        )


@dataclass(slots=True)
class _PriorEvidence:
    pages: assembly._DirectoryAnchor
    record: assembly._OpenedFile
    page_files: dict[str, assembly._OpenedFile]
    pages_moved: bool = False
    record_moved: bool = False


def prepare_pdf_qa(run_dir: Path, output_dir: Path) -> PdfQAResult:
    """Validate staged output and atomically publish rendered QA evidence."""
    run_dir = Path(run_dir)
    output_dir = Path(output_dir)
    run_anchor: assembly._DirectoryAnchor | None = None
    staging_anchor: assembly._DirectoryAnchor | None = None
    staged_output_anchor: assembly._DirectoryAnchor | None = None
    qa_pages_anchor: assembly._DirectoryAnchor | None = None
    opened: dict[str, assembly._OpenedFile] = {}
    qa_artifacts: dict[str, assembly._OpenedFile] = {}
    staged_pdf: assembly._OpenedFile | None = None
    render_input: assembly._OpenedFile | None = None
    render_input_candidate: assembly._PublishedFile | None = None
    qa_json: assembly._OpenedFile | None = None
    qa_pages_publication_attempted = False
    published_json: assembly._PublishedFile | None = None
    json_publication_candidate: assembly._PublishedFile | None = None
    prior: _PriorEvidence | None = None
    completed = False
    temporary_name: str | None = None
    try:
        run_anchor = assembly._open_directory_anchor(run_dir, "run")
        _validate_locations(run_anchor, output_dir)
        prior = _open_prior_evidence(run_anchor)
        for name, label in (
            ("source.pdf", "PDF source"),
            ("source.json", "PDF source record"),
            ("document.json", "PDF document"),
            ("segments.jsonl", "PDF segments"),
            ("glossary.json", "PDF glossary"),
            ("review.json", "semantic review"),
            ("layout.json", "PDF layout"),
        ):
            opened[name] = assembly._open_anchored_input_file(run_anchor, name, label)
        staged_output_anchor = assembly._open_existing_child_directory(
            run_anchor, "staged-output", "staged PDF output"
        )
        staged_pdf = assembly._open_anchored_input_file(
            staged_output_anchor, "translated.pdf", "staged translated PDF"
        )
        assembly._verify_anchored_evidence(run_anchor, opened)
        document = _document(opened["document.json"], run_dir / "document.json")
        source = _source(opened["source.json"], run_dir / "source.json")
        segments = _segments(opened["segments.jsonl"], run_dir / "segments.jsonl")
        glossary = _glossary(opened["glossary.json"], run_dir / "glossary.json")
        layout = _layout(opened["layout.json"], run_dir / "layout.json")
        translations, translation_zone_ids = _read_translations(run_anchor)
        review = _review(
            opened["review.json"],
            run_dir / "review.json",
            translation_zone_ids,
        )
        _validate_source(document, source, opened["source.pdf"])
        normalized = _validate_contracts(
            document, segments, translations, glossary, review, layout, output_dir
        )
        pdf_bytes = assembly._read_opened_bytes(
            staged_pdf,
            run_dir / "staged-output" / "translated.pdf",
            "staged translated PDF",
        )
        staged_hash = hashlib.sha256(pdf_bytes).hexdigest()
        if layout.staged_pdf_sha256 != staged_hash:
            raise PdfQAFailure("layout staged PDF hash does not match translated.pdf")
        structure = _validate_pdf_structure(pdf_bytes, document, layout, normalized)
        _validate_figure_media(run_anchor, document, pdf_bytes)

        temporary_name, staging_anchor = assembly._create_unique_child_directory(
            run_anchor, ".pdf-qa-preparing-", "PDF QA preparation"
        )
        assembly._verify_anchored_evidence(
            staged_output_anchor, {"translated.pdf": staged_pdf}
        )
        _verify_staged_pdf_content(
            staged_pdf,
            staged_hash,
            run_dir / "staged-output" / "translated.pdf",
        )
        render_input = assembly._create_anchored_binary_file(
            staging_anchor, "render-input.pdf"
        )
        render_input_candidate = assembly._PublishedFile(render_input.identity)
        render_input.stream.write(pdf_bytes)
        assembly._finalize_opened_file(render_input, "held PDF QA render input")
        render_input.stream.close()
        assembly._verify_anchored_evidence(
            staged_output_anchor, {"translated.pdf": staged_pdf}
        )
        assembly._verify_anchored_evidence(
            staging_anchor, {"render-input.pdf": render_input}
        )
        qa_pages_anchor = assembly._create_child_directory(
            staging_anchor, "qa-pages", "rendered PDF QA pages"
        )
        raw_pages = render_pdf_pages(
            staging_anchor.current_path() / "render-input.pdf",
            qa_pages_anchor.current_path(),
            dpi=144,
            name_width=3,
            existing_destination_identity=qa_pages_anchor.identity,
        )
        qa_pages_anchor.verify_visible()
        raw_pages = [qa_pages_anchor.current_path() / path.name for path in raw_pages]
        assembly._verify_anchored_evidence(
            staged_output_anchor, {"translated.pdf": staged_pdf}
        )
        _verify_staged_pdf_content(
            staged_pdf,
            staged_hash,
            run_dir / "staged-output" / "translated.pdf",
        )
        assembly._verify_anchored_evidence(
            staging_anchor, {"render-input.pdf": render_input}
        )
        assembly._remove_owned_file(
            staging_anchor, "render-input.pdf", render_input_candidate
        )
        for path in raw_pages:
            qa_artifacts[path.name] = assembly._open_anchored_input_file(
                qa_pages_anchor,
                path.name,
                "rendered PDF QA page",
            )
        if len(raw_pages) != structure["page_count"]:
            raise PdfQAFailure("Poppler did not render every staged PDF page")
        _validate_rendered_pages(
            raw_pages,
            structure["page_count"],
            layout,
            normalized,
            structure["page_sizes"],
        )
        contacts = build_contact_sheets(raw_pages, qa_pages_anchor.current_path())
        _validate_contact_coverage(contacts, structure["page_count"])
        for name in contacts:
            qa_artifacts[name] = assembly._open_anchored_input_file(
                qa_pages_anchor,
                name,
                "PDF QA contact sheet",
            )
        assembly._verify_anchored_evidence(qa_pages_anchor, qa_artifacts)
        page_hashes = {
            path.name: hashlib.sha256(
                assembly._read_opened_bytes(
                    qa_artifacts[path.name],
                    qa_pages_anchor.path / path.name,
                    "rendered PDF QA page",
                )
            ).hexdigest()
            for path in raw_pages
        }
        contact_hashes = {
            name: hashlib.sha256(
                assembly._read_opened_bytes(
                    qa_artifacts[name],
                    qa_pages_anchor.path / name,
                    "PDF QA contact sheet",
                )
            ).hexdigest()
            for name in contacts
        }
        findings = tuple(
            PdfQAFinding(code, evidence)
            for code, evidence in sorted(
                {
                    "contract.coverage": f"Validated {len(normalized)} translated blocks.",
                    "contract.review": "Semantic review has no unresolved required finding.",
                    "layout.evidence": f"Validated {len(layout.flowables)} tracked flowables.",
                    "render.contact_sheets": f"Covered {structure['page_count']} rendered pages exactly once.",
                    "structure.fonts": "Embedded Regular and Bold Korean fonts have Unicode maps.",
                    "structure.links": f"Validated {structure['link_count']} PDF link annotations.",
                    "structure.pages": f"Reopened {structure['page_count']} unencrypted pages.",
                    "structure.text": f"Validated selectable text for {len(normalized)} translated blocks.",
                }.items()
            )
        )
        result = PdfQAResult(
            schema_version="1.0",
            staged_pdf_sha256=staged_hash,
            findings=findings,
            rendered_pages=tuple(run_dir / "qa-pages" / path.name for path in raw_pages),
            rendered_page_hashes=page_hashes,
            contact_sheet_pages=contacts,
            contact_sheet_hashes=contact_hashes,
            metrics={
                "contact_sheet_count": len(contacts),
                "embedded_font_count": structure["embedded_font_count"],
                "figure_count": sum(block.kind == "figure" for block in document.blocks),
                "link_count": structure["link_count"],
                "output_page_count": structure["page_count"],
                "rendered_page_count": len(raw_pages),
                "translated_block_count": len(normalized),
            },
            passed=True,
        )
        result = PdfQAResult.from_dict(result.to_dict(), run_dir / "qa-pages")
        qa_json = assembly._create_anchored_binary_file(staging_anchor, "pdf-qa.json")
        json_publication_candidate = assembly._PublishedFile(qa_json.identity)
        qa_json.stream.write(
            (
                json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
        )
        assembly._finalize_opened_file(qa_json, "PDF QA record")
        qa_json.stream.close()
        assembly._verify_anchored_evidence(qa_pages_anchor, qa_artifacts)
        if prior is not None:
            _move_prior_evidence_to_staging(run_anchor, staging_anchor, prior)
        assembly._verify_anchored_evidence(
            staged_output_anchor, {"translated.pdf": staged_pdf}
        )
        _verify_staged_pdf_content(
            staged_pdf,
            staged_hash,
            run_dir / "staged-output" / "translated.pdf",
        )
        qa_pages_publication_attempted = True
        _publish_new_directory(
            staging_anchor,
            "qa-pages",
            qa_pages_anchor,
            run_anchor,
            "qa-pages",
        )
        published_json = assembly._publish_new_file(
            staging_anchor, "pdf-qa.json", run_anchor, "pdf-qa.json"
        )
        completed = True
        if prior is not None:
            _discard_prior_evidence(staging_anchor, prior)
        return result
    except PdfQAFailure:
        raise
    except (PdfAssemblyError, PdfContractError, PdfMediaError) as error:
        raise PdfQAFailure(str(error)) from error
    except (OSError, ValueError, TypeError) as error:
        raise PdfQAFailure(f"cannot prepare PDF QA evidence: {error}") from error
    finally:
        if run_anchor is not None and not completed:
            assembly._remove_owned_file(
                run_anchor,
                "pdf-qa.json",
                published_json or json_publication_candidate,
            )
            if qa_pages_publication_attempted:
                _remove_qa_pages(
                    run_anchor,
                    "qa-pages",
                    qa_pages_anchor,
                    quarantine_parent=run_anchor,
                )
            if prior is not None and staging_anchor is not None:
                _restore_prior_evidence(run_anchor, staging_anchor, prior)
        for file in [
            qa_json,
            render_input,
            staged_pdf,
            *opened.values(),
            *qa_artifacts.values(),
        ]:
            assembly._close_opened_file(file)
        assembly._close_published_file(published_json)
        if prior is not None:
            for item in prior.page_files.values():
                assembly._close_opened_file(item)
            assembly._close_opened_file(prior.record)
            prior.pages.close()
        if staging_anchor is not None and not completed:
            _remove_qa_pages(
                staging_anchor,
                "qa-pages",
                qa_pages_anchor,
                quarantine_parent=run_anchor,
            )
        if qa_pages_anchor is not None:
            qa_pages_anchor.close()
        if staged_output_anchor is not None:
            staged_output_anchor.close()
        if staging_anchor is not None:
            assembly._remove_owned_file(
                staging_anchor,
                "render-input.pdf",
                render_input_candidate,
            )
            assembly._remove_owned_file(
                staging_anchor,
                "pdf-qa.json",
                published_json or json_publication_candidate,
            )
            if temporary_name is not None:
                assembly._remove_owned_directory(
                    run_anchor,
                    temporary_name,
                    staging_anchor.identity,
                    child=staging_anchor,
                )
            staging_anchor.close()
        if run_anchor is not None:
            run_anchor.close()


def _verify_staged_pdf_content(
    opened: assembly._OpenedFile,
    expected_sha256: str,
    path: Path,
) -> None:
    stream = opened.stream
    try:
        original_position = stream.tell()
    except (OSError, ValueError, TypeError) as error:
        raise PdfQAFailure(
            f"cannot inspect staged translated PDF content {path}: {error}"
        ) from error
    try:
        stream.seek(0)
        digest = hashlib.sha256()
        while True:
            chunk = stream.read(1024 * 1024)
            if not isinstance(chunk, bytes):
                raise TypeError("held staged PDF stream did not return bytes")
            if not chunk:
                break
            digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise PdfQAFailure(
                f"staged translated PDF content changed: {path}"
            )
    except PdfQAFailure:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise PdfQAFailure(
            f"cannot read staged translated PDF content {path}: {error}"
        ) from error
    finally:
        try:
            stream.seek(original_position)
        except (OSError, ValueError, TypeError) as error:
            raise PdfQAFailure(
                f"cannot restore staged translated PDF stream {path}: {error}"
            ) from error


def _validate_locations(run_anchor: assembly._DirectoryAnchor, output_dir: Path) -> None:
    assembly._reject_linked_ancestors(output_dir)
    run_resolved = run_anchor.current_path().resolve(strict=True)
    output_resolved = output_dir.resolve(strict=False)
    if output_resolved == run_resolved or run_resolved in output_resolved.parents:
        raise PdfQAFailure("reserved final output directory must be outside the run")
    if output_dir.exists() or output_dir.is_symlink():
        raise PdfQAFailure(f"reserved final output already exists: {output_dir}")
    present: set[str] = set()
    for name in ("qa-pages", "pdf-qa.json"):
        path = run_anchor.path / name
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if name == "qa-pages":
            safe = stat.S_ISDIR(metadata.st_mode) and not assembly._is_reparse_stat(metadata)
        else:
            safe = stat.S_ISREG(metadata.st_mode) and not assembly._is_reparse_stat(metadata)
        if not safe:
            raise PdfQAFailure(f"unsafe prior PDF QA evidence: {name}")
        present.add(name)
    if present not in (set(), {"qa-pages", "pdf-qa.json"}):
        raise PdfQAFailure("prior PDF QA evidence must be a complete coherent set")


def _open_prior_evidence(
    run_anchor: assembly._DirectoryAnchor,
) -> _PriorEvidence | None:
    try:
        assembly._require_anchored_name_absent(run_anchor, "qa-pages")
        assembly._require_anchored_name_absent(run_anchor, "pdf-qa.json")
        return None
    except PdfAssemblyError:
        pass
    pages = assembly._open_existing_child_directory(
        run_anchor, "qa-pages", "prior rendered PDF QA pages"
    )
    record: assembly._OpenedFile | None = None
    files: dict[str, assembly._OpenedFile] = {}
    try:
        record = assembly._open_anchored_input_file(
            run_anchor, "pdf-qa.json", "prior PDF QA record"
        )
        names = sorted(path.name for path in pages.current_path().iterdir())
        if not names or not any(_PAGE_NAME.fullmatch(name) for name in names):
            raise PdfQAFailure("prior qa-pages has no rendered pages")
        if any(
            _PAGE_NAME.fullmatch(name) is None
            and _CONTACT_NAME.fullmatch(name) is None
            for name in names
        ):
            raise PdfQAFailure("prior qa-pages contains unrelated or unsafe entries")
        for name in names:
            files[name] = assembly._open_anchored_input_file(
                pages, name, "prior rendered PDF QA artifact"
            )
        assembly._verify_anchored_evidence(pages, files)
        assembly._verify_anchored_evidence(
            run_anchor, {"pdf-qa.json": record}
        )
        return _PriorEvidence(pages=pages, record=record, page_files=files)
    except BaseException:
        for item in files.values():
            assembly._close_opened_file(item)
        assembly._close_opened_file(record)
        pages.close()
        raise


def _json(opened: assembly._OpenedFile, path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(assembly._read_opened_utf8(opened, path, label))
    except (json.JSONDecodeError, UnicodeError, OSError) as error:
        raise PdfQAFailure(f"cannot read {label}: {error}") from error
    if not isinstance(value, Mapping):
        raise PdfQAFailure(f"{label} must be a JSON object")
    return value


def _document(opened: assembly._OpenedFile, path: Path) -> PdfDocument:
    return PdfDocument.from_dict(_json(opened, path, "PDF document"))


def _source(opened: assembly._OpenedFile, path: Path) -> PdfSourceRecord:
    return PdfSourceRecord.from_dict(_json(opened, path, "PDF source record"))


def _segments(opened: assembly._OpenedFile, path: Path) -> list[Segment]:
    return read_segments_stream(
        io.StringIO(assembly._read_opened_utf8(opened, path, "PDF segments"))
    )


def _glossary(opened: assembly._OpenedFile, path: Path) -> dict[str, str]:
    value = _json(opened, path, "PDF glossary")
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise PdfQAFailure("PDF glossary must map strings to strings")
    return dict(value)  # type: ignore[arg-type]


def _review(
    opened: assembly._OpenedFile,
    path: Path,
    expected_zone_ids: set[str],
) -> Mapping[str, Any]:
    value = _json(opened, path, "semantic review")
    if set(value) != {"retries", "section_findings", "unresolved_required"}:
        raise PdfQAFailure("semantic review fields are not exact")
    unresolved = value["unresolved_required"]
    if not isinstance(unresolved, list) or any(not isinstance(item, str) for item in unresolved):
        raise PdfQAFailure("semantic review unresolved_required must be a string array")
    findings = value["section_findings"]
    if not isinstance(findings, Mapping) or not findings:
        raise PdfQAFailure("semantic review must cover every zone")
    retries = value["retries"]
    if not isinstance(retries, Mapping) or any(
        not isinstance(zone_id, str)
        or type(attempts) is not int
        or not 0 <= attempts <= 2
        for zone_id, attempts in retries.items()
    ):
        raise PdfQAFailure(
            "semantic review retries must map zones to integers from 0 through 2"
        )
    if set(retries) != expected_zone_ids or set(findings) != expected_zone_ids:
        raise PdfQAFailure(
            "semantic review findings and retries must exactly cover translation zones"
        )
    expected_unresolved: list[str] = []
    for zone_id, records in findings.items():
        if not isinstance(zone_id, str) or not isinstance(records, list):
            raise PdfQAFailure("semantic review findings are malformed")
        dimensions: set[str] = set()
        for record in records:
            if not isinstance(record, Mapping) or set(record) != {"dimension", "verdict", "evidence"}:
                raise PdfQAFailure("semantic review finding fields are not exact")
            dimension = record["dimension"]
            verdict = record["verdict"]
            evidence = record["evidence"]
            if dimension not in _REVIEW_DIMENSIONS or dimension in dimensions:
                raise PdfQAFailure("semantic review dimensions are incomplete or duplicated")
            if verdict not in {"pass", "required-fix"} or not isinstance(evidence, str) or not evidence.strip():
                raise PdfQAFailure("semantic review finding is invalid")
            dimensions.add(str(dimension))
            if verdict == "required-fix":
                expected_unresolved.append(f"{zone_id}:{dimension}")
        if dimensions != _REVIEW_DIMENSIONS:
            raise PdfQAFailure("semantic review dimensions are incomplete or duplicated")
    if list(unresolved) != sorted(expected_unresolved):
        raise PdfQAFailure("semantic review unresolved required findings disagree")
    if unresolved:
        raise PdfQAFailure("semantic review has unresolved required findings")
    return value


def _layout(opened: assembly._OpenedFile, path: Path) -> PdfAssemblyLayout:
    try:
        return PdfAssemblyLayout.from_dict(_json(opened, path, "PDF layout"))
    except PdfAssemblyError as error:
        raise PdfQAFailure(f"invalid PDF layout: {error}") from error


def _read_translations(
    run_anchor: assembly._DirectoryAnchor,
) -> tuple[dict[str, Translation], set[str]]:
    directory = assembly._open_existing_child_directory(
        run_anchor, "translations", "PDF translations"
    )
    opened: list[assembly._OpenedFile] = []
    try:
        names = sorted(path.name for path in directory.current_path().iterdir())
        if not names or any(re.fullmatch(r"zone-\d{3}\.jsonl", name) is None for name in names):
            raise PdfQAFailure("PDF translations must contain only zone-NNN.jsonl files")
        result: dict[str, Translation] = {}
        for name in names:
            item = assembly._open_anchored_input_file(directory, name, "PDF translation")
            opened.append(item)
            text = assembly._read_opened_utf8(item, directory.path / name, "PDF translation")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    record = Translation.from_dict(json.loads(line))
                except (json.JSONDecodeError, ValueError, TypeError) as error:
                    raise PdfQAFailure(f"invalid PDF translation {name}:{line_number}: {error}") from error
                if record.segment_id in result:
                    raise PdfQAFailure(f"duplicate PDF translation ID: {record.segment_id}")
                result[record.segment_id] = record
        assembly._verify_anchored_evidence(
            directory, {name: item for name, item in zip(names, opened, strict=True)}
        )
        return result, {Path(name).stem for name in names}
    finally:
        for item in opened:
            assembly._close_opened_file(item)
        directory.close()


def _validate_source(
    document: PdfDocument,
    source: PdfSourceRecord,
    source_pdf: assembly._OpenedFile,
) -> None:
    payload = assembly._read_opened_bytes(source_pdf, Path("source.pdf"), "PDF source")
    if source.sha256 != hashlib.sha256(payload).hexdigest():
        raise PdfQAFailure("source PDF hash does not match source.json")
    if source.byte_length != len(payload) or source.sha256 != document.source_sha256:
        raise PdfQAFailure("source/document PDF evidence does not agree")


def _validate_contracts(
    document: PdfDocument,
    segments: Sequence[Segment],
    translations: Mapping[str, Translation],
    glossary: Mapping[str, str],
    review: Mapping[str, Any],
    layout: PdfAssemblyLayout,
    output_dir: Path,
) -> list[tuple[Any, Segment, str]]:
    del review
    try:
        normalized = assembly._normalize_pdf_translations(
            document, segments, translations, glossary
        )
        assembly._validate_rich_relationships(document)
    except PdfAssemblyError as error:
        raise PdfQAFailure(f"PDF contract QA failed: {error}") from error
    for _block, segment, _text in normalized:
        expected = Counter(token.token for token in segment.protected)
        actual = Counter(_TOKEN.findall(translations[segment.id].text))
        if expected != actual:
            raise PdfQAFailure(f"protected-token multiset changed for {segment.id}")
    if layout.reserved_output_dir != str(output_dir):
        raise PdfQAFailure("layout reserved output directory does not match the request")
    visible_ids = {
        block.id
        for block in document.blocks
        if block.kind not in {"header", "footer", "page-number"}
    }
    emitted_ids = {item.block_id for item in layout.flowables}
    if not visible_ids.issubset(emitted_ids):
        raise PdfQAFailure("layout does not cover every required PDF block")
    return normalized


def _validate_figure_media(
    run_anchor: assembly._DirectoryAnchor,
    document: PdfDocument,
    pdf_bytes: bytes,
) -> None:
    figures = [block for block in document.blocks if block.kind == "figure"]
    if not figures:
        return
    directory = assembly._open_existing_child_directory(
        run_anchor, "media", "PDF figure media"
    )
    opened: dict[str, assembly._OpenedFile] = {}
    try:
        expected: Counter[tuple[tuple[int, int], str, str]] = Counter()
        for figure in figures:
            if figure.media_path is None:
                raise PdfQAFailure(f"figure media is missing for {figure.id}")
            name = Path(figure.media_path).name
            if Path(figure.media_path).parts != ("media", name):
                raise PdfQAFailure(f"unsafe figure media path for {figure.id}")
            if name in opened:
                raise PdfQAFailure("figure media paths must be unique")
            item = assembly._open_anchored_input_file(
                directory, name, f"figure media for {figure.id}"
            )
            opened[name] = item
            payload = assembly._read_opened_bytes(
                item, directory.path / name, f"figure media for {figure.id}"
            )
            try:
                with Image.open(io.BytesIO(payload)) as image:
                    image.load()
                    if image.format != "PNG":
                        raise PdfQAFailure(f"figure media is not PNG: {figure.id}")
                    normalized = image.convert("RGB")
                    signature = (
                        normalized.size,
                        normalized.mode,
                        hashlib.sha256(normalized.tobytes()).hexdigest(),
                    )
            except PdfQAFailure:
                raise
            except (OSError, ValueError) as error:
                raise PdfQAFailure(f"cannot read figure media for {figure.id}: {error}") from error
            expected[signature] += 1
        assembly._verify_anchored_evidence(directory, opened)
        actual: Counter[tuple[tuple[int, int], str, str]] = Counter()
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=True)
        for page in reader.pages:
            for embedded in page.images:
                image = embedded.image.convert("RGB")
                actual[
                    (
                        image.size,
                        image.mode,
                        hashlib.sha256(image.tobytes()).hexdigest(),
                    )
                ] += 1
        missing = expected - actual
        if missing:
            raise PdfQAFailure(
                "figure media does not match embedded staged PDF image evidence"
            )
    except PdfQAFailure:
        raise
    except Exception as error:
        raise PdfQAFailure(f"cannot validate figure media: {error}") from error
    finally:
        for item in opened.values():
            assembly._close_opened_file(item)
        directory.close()


def _validate_pdf_structure(
    pdf_bytes: bytes,
    document: PdfDocument,
    layout: PdfAssemblyLayout,
    normalized: Sequence[tuple[Any, Segment, str]],
) -> dict[str, Any]:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=True)
    except Exception as error:
        raise PdfQAFailure(f"cannot reopen staged translated PDF: {error}") from error
    if reader.is_encrypted:
        raise PdfQAFailure("staged translated PDF must not be encrypted")
    if not reader.pages:
        raise PdfQAFailure("staged translated PDF has no pages")
    font_faces: set[str] = set()
    link_count = 0
    internal_link_count = 0
    expected_uris = {block.uri for block in document.blocks if block.uri is not None}
    expected_internal_links = sum(
        block.destination is not None for block in document.blocks
    )
    actual_uris: set[str] = set()
    validated_font_programs: set[str] = set()
    page_sizes: list[tuple[float, float]] = []
    link_annotations: list[
        tuple[int, tuple[float, float, float, float], str | None, object | None]
    ] = []
    for page_number, page in enumerate(reader.pages, start=1):
        page_box = page.mediabox
        page_sizes.append(
            (
                float(page_box.right) - float(page_box.left),
                float(page_box.top) - float(page_box.bottom),
            )
        )
        try:
            contents = page.get_contents()
            if contents is None or not contents.get_data():
                raise PdfQAFailure(f"page {page_number} has no valid content stream")
            resources = page["/Resources"].get_object()
            font_resources = resources.get("/Font")
            fonts = {} if font_resources is None else font_resources.get_object()
        except PdfQAFailure:
            raise
        except Exception as error:
            raise PdfQAFailure(f"page {page_number} has invalid resources or streams: {error}") from error
        for font_reference in fonts.values():
            font = font_reference.get_object()
            base = str(font.get("/BaseFont", ""))
            if "NotoSansCJKKR-Regular" in base:
                font_faces.add("Regular")
            if "NotoSansCJKKR-Bold" in base:
                font_faces.add("Bold")
            if "NotoSansCJKKR" in base:
                unicode_map = font.get("/ToUnicode")
                if unicode_map is None or not unicode_map.get_object().get_data():
                    raise PdfQAFailure(f"embedded Korean font {base} is missing /ToUnicode")
                _require_embedded_font(font, base, validated_font_programs)
        annotations = page.get("/Annots", [])
        for reference in annotations:
            annotation = reference.get_object()
            if annotation.get("/Subtype") != "/Link":
                continue
            link_count += 1
            rectangle = _annotation_rectangle(annotation.get("/Rect"))
            action = annotation.get("/A")
            destination = annotation.get("/Dest")
            if action is not None:
                action = action.get_object()
                if action.get("/S") != "/URI" or not isinstance(action.get("/URI"), str):
                    raise PdfQAFailure("external PDF link has an invalid URI action")
                uri = str(action["/URI"])
                try:
                    assembly._safe_uri(uri, f"page-{page_number}")
                except PdfAssemblyError as error:
                    raise PdfQAFailure(str(error)) from error
                actual_uris.add(uri)
                link_annotations.append((page_number, rectangle, uri, None))
            elif destination is None or not _valid_destination(reader, destination):
                raise PdfQAFailure(
                    "internal PDF link does not resolve to an output destination"
                )
            else:
                internal_link_count += 1
                link_annotations.append(
                    (page_number, rectangle, None, destination)
                )
    if font_faces != {"Regular", "Bold"}:
        missing = sorted({"Regular", "Bold"} - font_faces)
        raise PdfQAFailure("missing embedded Korean font face: " + ", ".join(missing))
    if not expected_uris.issubset(actual_uris):
        raise PdfQAFailure("required external URI annotation is missing")
    if internal_link_count < expected_internal_links:
        raise PdfQAFailure("required internal link annotation is missing")
    if any(item.page_number > len(reader.pages) for item in layout.flowables):
        raise PdfQAFailure("layout refers to a nonexistent output page")
    for item in layout.flowables:
        page = reader.pages[item.page_number - 1]
        page_box = page.mediabox
        page_width = float(page_box.right) - float(page_box.left)
        page_height = float(page_box.top) - float(page_box.bottom)
        for name, box in (("frame", item.frame), ("bounds", item.bounds)):
            x, y, width, height = box
            if (
                x < -1e-6
                or y < -1e-6
                or x + width > page_width + 1e-6
                or y + height > page_height + 1e-6
            ):
                raise PdfQAFailure(
                    f"layout {name} falls outside PDF page bounds for {item.block_id}"
                )
    layout_by_block: dict[str, list[Any]] = {}
    for item in layout.flowables:
        layout_by_block.setdefault(item.block_id, []).append(item)
    for block in document.blocks:
        source_items = layout_by_block.get(block.id, [])
        if block.uri is not None and not any(
            uri == block.uri
            and page_number == item.page_number
            and _rectangle_intersects_bounds(rectangle, item.bounds)
            for page_number, rectangle, uri, _destination in link_annotations
            for item in source_items
        ):
            raise PdfQAFailure(
                f"required external URI annotation is missing for block {block.id}"
            )
        if block.destination is not None and not any(
            destination is not None
            and page_number == item.page_number
            and _rectangle_intersects_bounds(rectangle, item.bounds)
            for page_number, rectangle, _uri, destination in link_annotations
            for item in source_items
        ):
            raise PdfQAFailure(
                f"required internal link annotation is missing for block {block.id}"
            )
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            block_text: dict[str, list[str]] = {}
            for item in layout.flowables:
                page = pdf.pages[item.page_number - 1]
                x, y, width, height = item.bounds
                crop = (
                    max(0.0, x - 1.0),
                    max(0.0, page.height - (y + height) - 1.0),
                    min(page.width, x + width + 1.0),
                    min(page.height, page.height - y + 1.0),
                )
                extracted = page.crop(crop, strict=False).extract_text(
                    x_tolerance=3,
                    y_tolerance=3,
                )
                block_text.setdefault(item.block_id, []).append(extracted or "")
    except Exception as error:
        raise PdfQAFailure(f"cannot extract selectable text from staged PDF: {error}") from error
    if any(character in text for character in ("\ufffd", "\x00", "\u25a1")):
        raise PdfQAFailure("rendered PDF contains glyph replacement boxes")
    for block, segment, translated in normalized:
        expected = _normalize_text(translated)
        selected = _normalize_text("\n".join(block_text.get(block.id, [])))
        if expected not in selected:
            raise PdfQAFailure(
                f"translated block is not selectable in the staged PDF: {segment.id}"
            )
        if _HANGUL.search(translated) and _HANGUL.search(selected) is None:
            raise PdfQAFailure(f"translated Korean block is unreadable: {block.id}")
    return {
        "embedded_font_count": len(font_faces),
        "link_count": link_count,
        "page_count": len(reader.pages),
        "page_sizes": tuple(page_sizes),
    }


def _require_embedded_font(
    font: Mapping[str, Any],
    base: str,
    validated_programs: set[str],
) -> None:
    descendants = font.get("/DescendantFonts", [])
    candidates = [item.get_object() for item in descendants] or [font]
    for candidate in candidates:
        descriptor = candidate.get("/FontDescriptor")
        if descriptor is None:
            continue
        font_file = descriptor.get_object().get("/FontFile2")
        if font_file is None:
            continue
        try:
            payload = font_file.get_object().get_data()
        except Exception as error:
            raise PdfQAFailure(
                f"embedded Korean font {base} has an unreadable font program"
            ) from error
        digest = hashlib.sha256(payload).hexdigest()
        if digest not in validated_programs:
            _validate_true_type_program(payload, base)
            validated_programs.add(digest)
        return
    raise PdfQAFailure(f"Korean font {base} is not embedded")


def _validate_true_type_program(payload: bytes, base: str) -> None:
    failure = f"embedded Korean font {base} has an invalid TrueType font program"
    if len(payload) < 12 or payload[:4] != b"\x00\x01\x00\x00":
        raise PdfQAFailure(failure)
    table_count = struct.unpack_from(">H", payload, 4)[0]
    if table_count == 0 or table_count > 4096 or len(payload) < 12 + 16 * table_count:
        raise PdfQAFailure(failure)
    tables: dict[bytes, tuple[int, int, int]] = {}
    for index in range(table_count):
        offset = 12 + index * 16
        tag, checksum, table_offset, table_length = struct.unpack_from(
            ">4sIII", payload, offset
        )
        if (
            tag in tables
            or table_length == 0
            or table_offset < 12 + 16 * table_count
            or table_offset + table_length > len(payload)
        ):
            raise PdfQAFailure(failure)
        table_data = bytearray(payload[table_offset : table_offset + table_length])
        if tag == b"head" and len(table_data) >= 12:
            table_data[8:12] = b"\x00\x00\x00\x00"
        table_data.extend(b"\x00" * (-len(table_data) % 4))
        words = struct.unpack(f">{len(table_data) // 4}I", table_data)
        if sum(words) & 0xFFFFFFFF != checksum:
            raise PdfQAFailure(failure)
        tables[tag] = (table_offset, table_length, checksum)
    required = {b"cmap", b"glyf", b"head", b"loca", b"maxp", b"name"}
    if not required.issubset(tables):
        raise PdfQAFailure(failure)
    head_offset, head_length, _head_checksum = tables[b"head"]
    maxp_offset, maxp_length, _maxp_checksum = tables[b"maxp"]
    cmap_offset, cmap_length, _cmap_checksum = tables[b"cmap"]
    if (
        head_length < 16
        or payload[head_offset + 12 : head_offset + 16] != b"_\x0f<\xf5"
        or maxp_length < 6
        or struct.unpack_from(">H", payload, maxp_offset + 4)[0] == 0
        or cmap_length < 4
        or struct.unpack_from(">H", payload, cmap_offset)[0] != 0
    ):
        raise PdfQAFailure(failure)


def _annotation_rectangle(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, (ArrayObject, list)) or len(value) != 4:
        raise PdfQAFailure("PDF link annotation has an invalid rectangle")
    try:
        left, bottom, right, top = (float(item) for item in value)
    except (TypeError, ValueError) as error:
        raise PdfQAFailure("PDF link annotation has an invalid rectangle") from error
    if (
        not all(math.isfinite(item) for item in (left, bottom, right, top))
        or right <= left
        or top <= bottom
    ):
        raise PdfQAFailure("PDF link annotation has an invalid rectangle")
    return left, bottom, right, top


def _rectangle_intersects_bounds(
    rectangle: tuple[float, float, float, float],
    bounds: tuple[float, float, float, float],
) -> bool:
    left, bottom, right, top = rectangle
    x, y, width, height = bounds
    return (
        min(right, x + width) - max(left, x) > 1e-6
        and min(top, y + height) - max(bottom, y) > 1e-6
    )


def _valid_destination(reader: PdfReader, destination: object) -> bool:
    value = destination.get_object() if hasattr(destination, "get_object") else destination
    if isinstance(value, str):
        return value in reader.named_destinations
    if isinstance(value, (ArrayObject, list)) and value:
        try:
            return reader.get_page_number(value[0].get_object()) >= 0
        except Exception:
            return False
    return False


def _validate_rendered_pages(
    pages: Sequence[Path],
    page_count: int,
    layout: PdfAssemblyLayout,
    normalized: Sequence[tuple[Any, Segment, str]],
    page_sizes: Sequence[tuple[float, float]],
) -> None:
    if [path.name for path in pages] != [
        f"page-{number:03d}.png" for number in range(1, page_count + 1)
    ]:
        raise PdfQAFailure("rendered page names or coverage are not exact")
    korean_block_ids = {
        block.id for block, _segment, translated in normalized if _HANGUL.search(translated)
    }
    korean_flowables: dict[int, list[Any]] = {}
    for item in layout.flowables:
        if item.block_id in korean_block_ids:
            korean_flowables.setdefault(item.page_number, []).append(item)
    for page_number, path in enumerate(pages, start=1):
        try:
            with Image.open(path) as image:
                image.load()
                if image.width <= 0 or image.height <= 0:
                    raise PdfQAFailure(f"rendered page {page_number} has invalid dimensions")
                extrema = image.convert("L").getextrema()
                if extrema == (255, 255):
                    raise PdfQAFailure(f"unintended blank output page: {page_number}")
                page_width, page_height = page_sizes[page_number - 1]
                for item in korean_flowables.get(page_number, []):
                    crop = _rendered_flowable_crop(
                        image,
                        item.bounds,
                        page_width,
                        page_height,
                    )
                    try:
                        crop_extrema = crop.getextrema()
                        if crop_extrema[1] - crop_extrema[0] < 24:
                            raise PdfQAFailure(
                                "rendered Korean block has no visible glyph evidence: "
                                f"{item.block_id}"
                            )
                        if _looks_like_replacement_boxes(crop):
                            raise PdfQAFailure(
                                "rendered Korean block contains known tofu replacement boxes: "
                                f"{item.block_id}"
                            )
                    finally:
                        crop.close()
        except PdfQAFailure:
            raise
        except (OSError, ValueError) as error:
            raise PdfQAFailure(f"cannot inspect rendered page {page_number}: {error}") from error


def _rendered_flowable_crop(
    image: Image.Image,
    bounds: tuple[float, float, float, float],
    page_width: float,
    page_height: float,
) -> Image.Image:
    x, y, width, height = bounds
    scale_x = image.width / page_width
    scale_y = image.height / page_height
    left = max(0, math.floor((x - 1.0) * scale_x))
    top = max(0, math.floor((page_height - y - height - 1.0) * scale_y))
    right = min(image.width, math.ceil((x + width + 1.0) * scale_x))
    bottom = min(image.height, math.ceil((page_height - y + 1.0) * scale_y))
    if right <= left or bottom <= top:
        raise PdfQAFailure("rendered Korean block has invalid raster bounds")
    return image.crop((left, top, right, bottom)).convert("L")


def _looks_like_replacement_boxes(image: Image.Image) -> bool:
    width, height = image.size
    pixels = list(image.tobytes())
    background = Counter(pixels).most_common(1)[0][0]
    mask = bytearray(abs(value - background) >= 48 for value in pixels)
    visited = bytearray(width * height)
    components: list[tuple[int, int, int, int, int, int]] = []
    for start, ink in enumerate(mask):
        if not ink or visited[start]:
            continue
        queue: deque[int] = deque([start])
        visited[start] = 1
        min_x = max_x = start % width
        min_y = max_y = start // width
        points: list[int] = []
        while queue:
            point = queue.popleft()
            points.append(point)
            point_x = point % width
            point_y = point // width
            min_x = min(min_x, point_x)
            max_x = max(max_x, point_x)
            min_y = min(min_y, point_y)
            max_y = max(max_y, point_y)
            for neighbor_y in range(max(0, point_y - 1), min(height, point_y + 2)):
                for neighbor_x in range(max(0, point_x - 1), min(width, point_x + 2)):
                    neighbor = neighbor_y * width + neighbor_x
                    if mask[neighbor] and not visited[neighbor]:
                        visited[neighbor] = 1
                        queue.append(neighbor)
        component_width = max_x - min_x + 1
        component_height = max_y - min_y + 1
        if component_width < 5 or component_height < 5:
            continue
        border_pixels = sum(
            point % width in {min_x, max_x}
            or point // width in {min_y, max_y}
            for point in points
        )
        perimeter = 2 * component_width + 2 * component_height - 4
        components.append(
            (
                component_width,
                component_height,
                len(points),
                border_pixels,
                perimeter,
                max(0, (component_width - 2) * (component_height - 2)),
            )
        )
    boxes = [
        component
        for component in components
        if component[3] / component[4] >= 0.75
        and component[2] - component[3] <= max(2, round(component[5] * 0.1))
    ]
    if len(boxes) < 3 or len(boxes) != len(components):
        return False
    widths = [component[0] for component in boxes]
    heights = [component[1] for component in boxes]
    return max(widths) - min(widths) <= 1 and max(heights) - min(heights) <= 1


def _validate_contact_coverage(mapping: Mapping[str, list[int]], page_count: int) -> None:
    expected_names = [
        f"contact-sheet-{number:03d}.png"
        for number in range(1, math.ceil(page_count / 12) + 1)
    ]
    if list(mapping) != expected_names:
        raise PdfQAFailure("contact-sheet names are not deterministic")
    covered = [page for pages in mapping.values() for page in pages]
    if covered != list(range(1, page_count + 1)) or any(len(pages) > 12 for pages in mapping.values()):
        raise PdfQAFailure("contact sheets do not cover every page exactly once")


def _move_prior_evidence_to_staging(
    run_anchor: assembly._DirectoryAnchor,
    staging_anchor: assembly._DirectoryAnchor,
    prior: _PriorEvidence,
) -> None:
    _move_directory(
        run_anchor,
        "qa-pages",
        prior.pages,
        staging_anchor,
        "prior-qa-pages",
    )
    prior.pages_moved = True
    _move_file(
        run_anchor,
        "pdf-qa.json",
        prior.record.identity,
        staging_anchor,
        "prior-pdf-qa.json",
    )
    prior.record_moved = True


def _restore_prior_evidence(
    run_anchor: assembly._DirectoryAnchor,
    staging_anchor: assembly._DirectoryAnchor,
    prior: _PriorEvidence,
) -> None:
    if prior.record_moved:
        try:
            assembly._require_anchored_name_absent(run_anchor, "pdf-qa.json")
            _move_file(
                staging_anchor,
                "prior-pdf-qa.json",
                prior.record.identity,
                run_anchor,
                "pdf-qa.json",
            )
            prior.record_moved = False
        except (PdfAssemblyError, PdfQAFailure):
            pass
    if prior.pages_moved:
        try:
            assembly._require_anchored_name_absent(run_anchor, "qa-pages")
            _move_directory(
                staging_anchor,
                "prior-qa-pages",
                prior.pages,
                run_anchor,
                "qa-pages",
            )
            prior.pages_moved = False
        except (PdfAssemblyError, PdfQAFailure):
            pass


def _discard_prior_evidence(
    staging_anchor: assembly._DirectoryAnchor,
    prior: _PriorEvidence,
) -> None:
    if prior.record_moved:
        assembly._remove_owned_file(
            staging_anchor,
            "prior-pdf-qa.json",
            assembly._PublishedFile(prior.record.identity),
        )
        prior.record_moved = False
    if prior.pages_moved:
        for name, item in prior.page_files.items():
            assembly._remove_owned_file(
                prior.pages, name, assembly._PublishedFile(item.identity)
            )
        assembly._remove_owned_directory(
            staging_anchor,
            "prior-qa-pages",
            prior.pages.identity,
            child=prior.pages,
        )
        prior.pages_moved = False


def _move_file(
    source_parent: assembly._DirectoryAnchor,
    source_name: str,
    source_identity: tuple[int, int],
    destination_parent: assembly._DirectoryAnchor,
    destination_name: str,
) -> None:
    handle: int | None = None
    try:
        if source_parent.descriptor is not None and destination_parent.descriptor is not None:
            os.rename(
                source_name,
                destination_name,
                src_dir_fd=source_parent.descriptor,
                dst_dir_fd=destination_parent.descriptor,
            )
        elif os.name == "nt":
            handle = assembly._windows_open_relative_file(
                assembly._windows_anchor_handle(source_parent), source_name
            )
            if assembly._windows_file_identity(handle, require_regular=True) != source_identity:
                raise PdfQAFailure("prior PDF QA record changed identity")
            assembly._windows_rename_open_file(
                handle,
                assembly._windows_anchor_handle(destination_parent),
                destination_name,
            )
        else:
            raise PdfQAFailure("safe anchored PDF QA record move is unavailable")
        assembly._verify_anchored_input_identity(
            destination_parent, destination_name, source_identity
        )
    except FileExistsError as error:
        raise PdfQAFailure(
            f"PDF QA regeneration destination already exists: {destination_name}"
        ) from error
    except (NotImplementedError, OSError) as error:
        raise PdfQAFailure(f"cannot move prior PDF QA record: {error}") from error
    finally:
        if handle is not None:
            assembly.pdf_acquire_module._close_windows_handle(handle)


def _move_directory(
    source_parent: assembly._DirectoryAnchor,
    source_name: str,
    source: assembly._DirectoryAnchor,
    destination_parent: assembly._DirectoryAnchor,
    destination_name: str,
) -> None:
    try:
        if source_parent.descriptor is not None and destination_parent.descriptor is not None:
            os.rename(
                source_name,
                destination_name,
                src_dir_fd=source_parent.descriptor,
                dst_dir_fd=destination_parent.descriptor,
            )
        elif os.name == "nt":
            assembly._windows_rename_open_file(
                assembly._windows_anchor_handle(source),
                assembly._windows_anchor_handle(destination_parent),
                destination_name,
            )
        else:
            raise PdfQAFailure("safe anchored PDF QA directory move is unavailable")
        result = assembly._anchored_entry_stat(destination_parent, destination_name)
        if (
            not stat.S_ISDIR(result.st_mode)
            or assembly._is_reparse_stat(result)
            or (result.st_dev, result.st_ino) != source.identity
        ):
            raise PdfQAFailure("prior PDF QA directory changed identity")
    except FileExistsError as error:
        raise PdfQAFailure(
            f"PDF QA regeneration destination already exists: {destination_name}"
        ) from error
    except (NotImplementedError, OSError) as error:
        raise PdfQAFailure(f"cannot move prior PDF QA directory: {error}") from error


def _publish_new_directory(
    source_parent: assembly._DirectoryAnchor,
    source_name: str,
    source: assembly._DirectoryAnchor,
    destination_parent: assembly._DirectoryAnchor,
    destination_name: str,
) -> None:
    try:
        _require_anchored_directory_identity(
            source_parent,
            source_name,
            source.identity,
            "PDF QA publication source",
        )
        if source_parent.descriptor is not None and destination_parent.descriptor is not None:
            os.rename(
                source_name,
                destination_name,
                src_dir_fd=source_parent.descriptor,
                dst_dir_fd=destination_parent.descriptor,
            )
        elif os.name == "nt":
            assembly._windows_rename_open_file(
                assembly._windows_anchor_handle(source),
                assembly._windows_anchor_handle(destination_parent),
                destination_name,
            )
        else:
            raise PdfQAFailure("safe anchored QA directory publication is unavailable")
        try:
            _require_anchored_directory_identity(
                destination_parent,
                destination_name,
                source.identity,
                "published PDF QA directory",
            )
        except PdfQAFailure:
            _quarantine_raced_directory(
                source_parent,
                destination_parent,
                destination_name,
            )
            raise
        destination_parent.verify_visible()
    except FileExistsError as error:
        raise PdfQAFailure(f"PDF QA destination already exists: {destination_name}") from error
    except (NotImplementedError, OSError) as error:
        raise PdfQAFailure(f"cannot publish PDF QA directory: {error}") from error


def _require_anchored_directory_identity(
    parent: assembly._DirectoryAnchor,
    name: str,
    expected: tuple[int, int],
    context: str,
) -> None:
    try:
        result = assembly._anchored_entry_stat(parent, name)
    except PdfAssemblyError as error:
        raise PdfQAFailure(f"{context} changed identity") from error
    if (
        not stat.S_ISDIR(result.st_mode)
        or assembly._is_reparse_stat(result)
        or (result.st_dev, result.st_ino) != expected
    ):
        raise PdfQAFailure(f"{context} changed identity")


def _quarantine_raced_directory(
    staging_parent: assembly._DirectoryAnchor,
    public_parent: assembly._DirectoryAnchor,
    public_name: str,
) -> None:
    quarantine_name = f".pdf-qa-raced-{os.urandom(16).hex()}"
    raced_anchor: assembly._DirectoryAnchor | None = None
    try:
        if staging_parent.descriptor is not None and public_parent.descriptor is not None:
            os.rename(
                public_name,
                quarantine_name,
                src_dir_fd=public_parent.descriptor,
                dst_dir_fd=staging_parent.descriptor,
            )
        elif os.name == "nt":
            raced_anchor = assembly._open_existing_child_directory(
                public_parent,
                public_name,
                "raced published PDF QA directory",
            )
            assembly._windows_rename_open_file(
                assembly._windows_anchor_handle(raced_anchor),
                assembly._windows_anchor_handle(staging_parent),
                quarantine_name,
            )
    except (PdfAssemblyError, NotImplementedError, OSError):
        return
    finally:
        if raced_anchor is not None:
            raced_anchor.close()


def _remove_qa_pages(
    run_anchor: assembly._DirectoryAnchor,
    name: str,
    pages_anchor: assembly._DirectoryAnchor | None,
    *,
    quarantine_parent: assembly._DirectoryAnchor | None,
) -> None:
    if pages_anchor is None:
        return
    try:
        if run_anchor.descriptor is not None:
            if quarantine_parent is None or quarantine_parent.descriptor is None:
                return
            quarantine_name = f".pdf-qa-raced-{os.urandom(16).hex()}"
            os.rename(
                name,
                quarantine_name,
                src_dir_fd=run_anchor.descriptor,
                dst_dir_fd=quarantine_parent.descriptor,
            )
            _require_anchored_directory_identity(
                quarantine_parent,
                quarantine_name,
                pages_anchor.identity,
                "privately moved failed PDF QA render directory",
            )
            try:
                os.rmdir(quarantine_name, dir_fd=quarantine_parent.descriptor)
            except OSError:
                pass
            return
        if os.name != "nt":
            return
        _require_anchored_directory_identity(
            run_anchor,
            name,
            pages_anchor.identity,
            "failed PDF QA render directory",
        )
        try:
            assembly._windows_delete_open_file(
                assembly._windows_anchor_handle(pages_anchor)
            )
        except (OSError, PdfAssemblyError):
            pass
        try:
            _require_anchored_directory_identity(
                run_anchor,
                name,
                pages_anchor.identity,
                "failed PDF QA render directory",
            )
        except (PdfAssemblyError, PdfQAFailure):
            return
        if quarantine_parent is not None:
            _move_directory(
                run_anchor,
                name,
                pages_anchor,
                quarantine_parent,
                f".pdf-qa-raced-{os.urandom(16).hex()}",
            )
    except (OSError, PdfAssemblyError, PdfQAFailure):
        return


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _qa_hash(value: object, context: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PdfQAFailure(f"pdf-qa {context} must be lowercase SHA-256")
    return value


def _qa_hash_mapping(
    value: object,
    name_pattern: re.Pattern[str],
    context: str,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise PdfQAFailure(f"pdf-qa {context} must be an object")
    result: dict[str, str] = {}
    for name, digest in value.items():
        if not isinstance(name, str) or name_pattern.fullmatch(name) is None:
            raise PdfQAFailure(f"pdf-qa {context} has an invalid artifact name")
        result[name] = _qa_hash(digest, f"{context}[{name!r}]")
    if list(result) != sorted(result):
        raise PdfQAFailure(f"pdf-qa {context} must be sorted")
    return result


__all__ = ["PdfQAFailure", "PdfQAFinding", "PdfQAResult", "prepare_pdf_qa"]
