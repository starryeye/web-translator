"""Deterministic manifest and review-report writers for finalized PDFs."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, BinaryIO

from langdetect import DetectorFactory, PROFILES_DIRECTORY
from langdetect.lang_detect_exception import LangDetectException

from web_translator import __version__
from web_translator.models import Segment, SegmentContractError, read_segments_stream
from web_translator.pdf_flowables import PdfAssemblyError, PdfAssemblyLayout
from web_translator.pdf_models import (
    PdfBlock,
    PdfContractError,
    PdfDocument,
    PdfLinkEvidence,
    PdfLayoutReview,
    PdfPage,
    PdfSourceRecord,
)
from web_translator.pdf_qa import (
    PdfQAFailure,
    PdfQAResult,
    read_pdf_layout_review,
)


_SEMANTIC_DIMENSIONS = {
    "semantic_fidelity",
    "qualification_preservation",
    "naturalness",
    "terminology",
    "boundary_consistency",
    "protected_content",
}
_ZONE_FILE = re.compile(r"zone-\d{3}\.json\Z")
_TERMINOLOGY_POLICY_ID = "english-technical-first-use-ko-gloss"
_TERMINOLOGY_POLICY_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class PdfReportEvidence:
    """Validated immutable inputs used by both final report writers."""

    source: PdfSourceRecord
    document: PdfDocument
    segments: tuple[Segment, ...]
    glossary: dict[str, str]
    semantic_review: dict[str, object]
    layout: PdfAssemblyLayout
    qa: PdfQAResult
    visual_review: PdfLayoutReview


@dataclass(frozen=True, slots=True)
class PdfManifestInput:
    kind: str
    name: str | None
    requested_url: str | None
    final_url: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
        }

    @classmethod
    def from_dict(cls, value: object) -> PdfManifestInput:
        data = _exact_report_mapping(
            value,
            "input",
            {"kind", "name", "requested_url", "final_url"},
        )
        kind = _report_string(data, "kind", "input")
        if kind not in {"local", "public"}:
            raise PdfQAFailure("input.kind must be local or public")
        name = _report_optional_string(data, "name", "input")
        requested_url = _report_optional_string(data, "requested_url", "input")
        final_url = _report_optional_string(data, "final_url", "input")
        if kind == "local":
            if not name or requested_url is not None or final_url is not None:
                raise PdfQAFailure(
                    "local input requires a name and null requested/final URLs"
                )
        elif name is not None or not requested_url or not final_url:
            raise PdfQAFailure(
                "public input requires requested/final URLs and a null name"
            )
        return cls(kind, name, requested_url, final_url)


@dataclass(frozen=True, slots=True)
class PdfManifestSource:
    sha256: str
    byte_length: int
    page_count: int
    selectable_characters: int
    scan_candidate_pages: tuple[int, ...]
    pages: tuple[PdfPage, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "page_count": self.page_count,
            "selectable_characters": self.selectable_characters,
            "scan_candidate_pages": list(self.scan_candidate_pages),
            "pages": [page.to_dict() for page in self.pages],
        }

    @classmethod
    def from_dict(cls, value: object) -> PdfManifestSource:
        data = _exact_report_mapping(
            value,
            "source",
            {
                "sha256",
                "byte_length",
                "page_count",
                "selectable_characters",
                "scan_candidate_pages",
                "pages",
            },
        )
        digest = _report_sha256(data, "sha256", "source")
        byte_length = _report_nonnegative_int(data, "byte_length", "source")
        page_count = _report_positive_int(data, "page_count", "source")
        selectable = _report_nonnegative_int(
            data, "selectable_characters", "source"
        )
        candidates = tuple(
            _report_positive_int_list(
                data.get("scan_candidate_pages"), "source.scan_candidate_pages"
            )
        )
        if list(candidates) != sorted(set(candidates)) or any(
            page > page_count for page in candidates
        ):
            raise PdfQAFailure(
                "source.scan_candidate_pages must be sorted unique source pages"
            )
        raw_pages = data.get("pages")
        if not isinstance(raw_pages, list):
            raise PdfQAFailure("source.pages must be an array")
        try:
            pages = tuple(PdfPage.from_dict(_report_mapping(item, "source.pages")) for item in raw_pages)
        except PdfContractError as error:
            raise PdfQAFailure(f"invalid source page evidence: {error}") from error
        if [page.number for page in pages] != list(range(1, page_count + 1)):
            raise PdfQAFailure("source.pages must exactly cover the source page count")
        return cls(digest, byte_length, page_count, selectable, candidates, pages)


@dataclass(frozen=True, slots=True)
class PdfManifestExtraction:
    layout_validation_counts: dict[str, int]
    warnings: tuple[str, ...]
    unreconstructed_links: tuple[PdfLinkEvidence, ...]

    _COUNT_FIELDS = {
        "blocks",
        "translation_targets",
        "tables",
        "table_cells",
        "reconstructed_links",
        "unreconstructed_links",
    }

    def to_dict(self) -> dict[str, object]:
        return {
            "layout_validation_counts": dict(self.layout_validation_counts),
            "warnings": list(self.warnings),
            "unreconstructed_links": [
                link.to_dict() for link in self.unreconstructed_links
            ],
        }

    @classmethod
    def from_dict(cls, value: object) -> PdfManifestExtraction:
        data = _exact_report_mapping(
            value,
            "extraction",
            {"layout_validation_counts", "warnings", "unreconstructed_links"},
        )
        counts_data = _exact_report_mapping(
            data.get("layout_validation_counts"),
            "extraction.layout_validation_counts",
            cls._COUNT_FIELDS,
        )
        counts = {
            field: _report_nonnegative_int(
                counts_data, field, "extraction.layout_validation_counts"
            )
            for field in sorted(cls._COUNT_FIELDS)
        }
        warnings = tuple(_report_string_list(data.get("warnings"), "extraction.warnings"))
        if list(warnings) != sorted(set(warnings)):
            raise PdfQAFailure("extraction.warnings must be sorted and unique")
        raw_links = data.get("unreconstructed_links")
        if not isinstance(raw_links, list):
            raise PdfQAFailure("extraction.unreconstructed_links must be an array")
        try:
            links = tuple(
                PdfLinkEvidence.from_dict(
                    _report_mapping(item, "extraction.unreconstructed_links")
                )
                for item in raw_links
            )
        except PdfContractError as error:
            raise PdfQAFailure(
                f"invalid unreconstructed link provenance: {error}"
            ) from error
        if any(link.reconstructed for link in links):
            raise PdfQAFailure(
                "extraction.unreconstructed_links cannot contain reconstructed links"
            )
        if counts["unreconstructed_links"] != len(links):
            raise PdfQAFailure(
                "extraction unreconstructed-link count disagrees with its evidence"
            )
        return cls(counts, warnings, links)


@dataclass(frozen=True, slots=True)
class PdfManifestCounts:
    values: dict[str, int]

    FIELDS = {
        "headings",
        "paragraphs",
        "lists",
        "tables",
        "cells",
        "figures",
        "captions",
        "footnotes",
        "segments",
        "zones",
    }

    def to_dict(self) -> dict[str, object]:
        return dict(self.values)

    @classmethod
    def from_dict(cls, value: object) -> PdfManifestCounts:
        data = _exact_report_mapping(value, "counts", cls.FIELDS)
        return cls(
            {
                field: _report_nonnegative_int(data, field, "counts")
                for field in sorted(cls.FIELDS)
            }
        )


@dataclass(frozen=True, slots=True)
class PdfManifestTranslation:
    source_language: str
    target_language: str
    terminology: dict[str, object]
    retries: dict[str, int]
    master_semantic_review: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "source_language": self.source_language,
            "target_language": self.target_language,
            "terminology": _json_copy(self.terminology),
            "retries": dict(self.retries),
            "master_semantic_review": _json_copy(self.master_semantic_review),
        }

    @classmethod
    def from_dict(cls, value: object) -> PdfManifestTranslation:
        data = _exact_report_mapping(
            value,
            "translation",
            {
                "source_language",
                "target_language",
                "terminology",
                "retries",
                "master_semantic_review",
            },
        )
        source_language = _report_string(data, "source_language", "translation")
        target_language = _report_string(data, "target_language", "translation")
        if target_language != "ko":
            raise PdfQAFailure("translation.target_language must be ko")
        terminology_data = _exact_report_mapping(
            data.get("terminology"),
            "translation.terminology",
            {"policy_id", "policy_version", "glossary"},
        )
        policy_id = _report_string(
            terminology_data, "policy_id", "translation.terminology"
        )
        policy_version = _report_string(
            terminology_data, "policy_version", "translation.terminology"
        )
        glossary = _report_string_mapping(
            terminology_data.get("glossary"), "translation.terminology.glossary"
        )
        retries_data = _report_mapping(data.get("retries"), "translation.retries")
        retries = {
            key: _report_nonnegative_int_value(value, f"translation.retries[{key!r}]")
            for key, value in retries_data.items()
            if isinstance(key, str)
        }
        if len(retries) != len(retries_data) or list(retries) != sorted(retries):
            raise PdfQAFailure("translation.retries keys must be sorted strings")
        master_value = _report_mapping(
            data.get("master_semantic_review"),
            "translation.master_semantic_review",
        )
        master = _semantic_review_from_value(
            master_value,
            set(retries),
            retries,
        )
        if master["retries"] != retries:
            raise PdfQAFailure(
                "translation retries disagree with master semantic review"
            )
        return cls(
            source_language,
            target_language,
            {
                "policy_id": policy_id,
                "policy_version": policy_version,
                "glossary": glossary,
            },
            retries,
            master,
        )


@dataclass(frozen=True, slots=True)
class PdfManifestOutput:
    page_count: int
    sha256: str
    embedded_fonts: tuple[str, ...]
    link_count: int
    figure_count: int
    qa_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "page_count": self.page_count,
            "sha256": self.sha256,
            "embedded_fonts": list(self.embedded_fonts),
            "link_count": self.link_count,
            "figure_count": self.figure_count,
            "qa_status": self.qa_status,
        }

    @classmethod
    def from_dict(cls, value: object) -> PdfManifestOutput:
        data = _exact_report_mapping(
            value,
            "output",
            {
                "page_count",
                "sha256",
                "embedded_fonts",
                "link_count",
                "figure_count",
                "qa_status",
            },
        )
        fonts = tuple(_report_string_list(data.get("embedded_fonts"), "output.embedded_fonts"))
        if not fonts or list(fonts) != sorted(set(fonts)):
            raise PdfQAFailure("output.embedded_fonts must be sorted and unique")
        status = _report_string(data, "qa_status", "output")
        if status not in {"passed", "failed"}:
            raise PdfQAFailure("output.qa_status must be passed or failed")
        return cls(
            _report_positive_int(data, "page_count", "output"),
            _report_sha256(data, "sha256", "output"),
            fonts,
            _report_nonnegative_int(data, "link_count", "output"),
            _report_nonnegative_int(data, "figure_count", "output"),
            status,
        )


@dataclass(frozen=True, slots=True)
class PdfManifestQA:
    rendered_page_hashes: dict[str, str]
    layout_findings: dict[str, object]
    contact_sheet_coverage: dict[str, object]
    automated: dict[str, object]
    visual: dict[str, object]
    warnings: tuple[str, ...]
    final_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "rendered_page_hashes": dict(self.rendered_page_hashes),
            "layout_findings": _json_copy(self.layout_findings),
            "contact_sheet_coverage": _json_copy(self.contact_sheet_coverage),
            "automated": _json_copy(self.automated),
            "visual": _json_copy(self.visual),
            "warnings": list(self.warnings),
            "final_status": self.final_status,
        }

    @classmethod
    def from_dict(cls, value: object) -> PdfManifestQA:
        data = _exact_report_mapping(
            value,
            "qa",
            {
                "rendered_page_hashes",
                "layout_findings",
                "contact_sheet_coverage",
                "automated",
                "visual",
                "warnings",
                "final_status",
            },
        )
        rendered = _report_sha256_mapping(
            data.get("rendered_page_hashes"), "qa.rendered_page_hashes"
        )
        if list(rendered) != [
            f"page-{number:03d}.png" for number in range(1, len(rendered) + 1)
        ]:
            raise PdfQAFailure("qa.rendered_page_hashes must cover sequential pages")
        findings = _report_mapping(data.get("layout_findings"), "qa.layout_findings")
        contacts = _report_mapping(
            data.get("contact_sheet_coverage"), "qa.contact_sheet_coverage"
        )
        automated_value = _exact_report_mapping(
            data.get("automated"),
            "qa.automated",
            {
                "contact_sheet_hashes",
                "contact_sheet_pages",
                "findings",
                "metrics",
                "passed",
                "rendered_page_hashes",
                "schema_version",
                "staged_pdf_sha256",
            },
        )
        automated_result = PdfQAResult.from_dict(automated_value, Path("."))
        automated = automated_result.to_dict()
        visual_data = _exact_report_mapping(
            data.get("visual"),
            "qa.visual",
            {
                "schema_version",
                "staged_pdf_sha256",
                "pages_reviewed",
                "contact_sheets_reviewed",
                "findings",
                "unresolved_required",
            },
        )
        try:
            visual_record = PdfLayoutReview.from_dict(visual_data)
            visual = visual_record.to_dict()
        except PdfContractError as error:
            raise PdfQAFailure(f"invalid qa.visual evidence: {error}") from error
        if findings != visual["findings"]:
            raise PdfQAFailure("qa.layout_findings disagree with qa.visual")
        if contacts != visual["contact_sheets_reviewed"]:
            raise PdfQAFailure("qa.contact_sheet_coverage disagrees with qa.visual")
        if rendered != automated.get("rendered_page_hashes"):
            raise PdfQAFailure("qa rendered-page hashes disagree with automated QA")
        warnings = tuple(_report_string_list(data.get("warnings"), "qa.warnings"))
        if list(warnings) != sorted(set(warnings)):
            raise PdfQAFailure("qa.warnings must be sorted and unique")
        status = _report_string(data, "final_status", "qa")
        if status not in {"passed", "failed"}:
            raise PdfQAFailure("qa.final_status must be passed or failed")
        expected_status = (
            "passed"
            if automated_result.passed and not visual_record.unresolved_required
            else "failed"
        )
        if status != expected_status:
            raise PdfQAFailure(
                "qa.final_status disagrees with automated and visual QA evidence"
            )
        return cls(
            rendered,
            _json_copy(findings),
            _json_copy(contacts),
            _json_copy(automated),
            visual,
            warnings,
            status,
        )


@dataclass(frozen=True, slots=True)
class PdfFinalManifest:
    """Exact typed schema shared by the two final provenance artifacts."""

    schema_version: str
    tool_version: str
    input: PdfManifestInput
    source: PdfManifestSource
    extraction: PdfManifestExtraction
    counts: PdfManifestCounts
    translation: PdfManifestTranslation
    output: PdfManifestOutput
    qa: PdfManifestQA

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "tool_version": self.tool_version,
            "input": self.input.to_dict(),
            "source": self.source.to_dict(),
            "extraction": self.extraction.to_dict(),
            "counts": self.counts.to_dict(),
            "translation": self.translation.to_dict(),
            "output": self.output.to_dict(),
            "qa": self.qa.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> PdfFinalManifest:
        data = _exact_report_mapping(
            value,
            "manifest",
            {
                "schema_version",
                "tool_version",
                "input",
                "source",
                "extraction",
                "counts",
                "translation",
                "output",
                "qa",
            },
        )
        schema_version = _report_string(data, "schema_version", "manifest")
        if schema_version != "1.0":
            raise PdfQAFailure("manifest.schema_version must be 1.0")
        tool_version = _report_string(data, "tool_version", "manifest")
        if not tool_version:
            raise PdfQAFailure("manifest.tool_version must be nonempty")
        input_record = PdfManifestInput.from_dict(data.get("input"))
        source = PdfManifestSource.from_dict(data.get("source"))
        extraction = PdfManifestExtraction.from_dict(data.get("extraction"))
        counts = PdfManifestCounts.from_dict(data.get("counts"))
        translation = PdfManifestTranslation.from_dict(data.get("translation"))
        output = PdfManifestOutput.from_dict(data.get("output"))
        qa = PdfManifestQA.from_dict(data.get("qa"))
        if output.page_count != len(qa.rendered_page_hashes):
            raise PdfQAFailure("output page count disagrees with rendered-page hashes")
        if output.sha256 != qa.automated.get("staged_pdf_sha256"):
            raise PdfQAFailure("output SHA-256 disagrees with automated QA")
        if output.qa_status != qa.final_status:
            raise PdfQAFailure("output QA status disagrees with final QA status")
        if not set(extraction.warnings).issubset(qa.warnings):
            raise PdfQAFailure("final QA warnings omit extraction warnings")
        return cls(
            schema_version,
            tool_version,
            input_record,
            source,
            extraction,
            counts,
            translation,
            output,
            qa,
        )


def build_pdf_report_evidence(
    *,
    source_pdf: bytes,
    source_value: Mapping[str, Any],
    document_value: Mapping[str, Any],
    segments_text: str,
    glossary_value: Mapping[str, Any],
    review_value: Mapping[str, Any],
    zone_values: Mapping[str, Mapping[str, Any]],
    layout: PdfAssemblyLayout,
    qa: PdfQAResult,
    visual_review: PdfLayoutReview,
) -> PdfReportEvidence:
    """Parse and cross-validate one already captured report-evidence snapshot."""
    try:
        source = PdfSourceRecord.from_dict(source_value)
        document = PdfDocument.from_dict(document_value)
    except PdfContractError as error:
        raise PdfQAFailure(f"invalid PDF report evidence: {error}") from error
    try:
        segments = tuple(read_segments_stream(io.StringIO(segments_text)))
    except (SegmentContractError, ValueError) as error:
        raise PdfQAFailure(f"invalid PDF segments for report: {error}") from error
    glossary = _string_mapping_value(glossary_value, "PDF glossary")
    zone_targets, zone_attempts = _zone_snapshot(zone_values)
    semantic_review = _semantic_review_from_value(
        review_value, set(zone_values), zone_attempts
    )
    source_hash = hashlib.sha256(source_pdf).hexdigest()
    if (
        source.sha256 != source_hash
        or document.source_sha256 != source_hash
        or source.byte_length != len(source_pdf)
    ):
        raise PdfQAFailure(
            "PDF source SHA and byte length must agree across source, document, and bytes"
        )
    target_segments = [segment for segment in segments if segment.target]
    target_ids = [segment.id for segment in target_segments]
    assigned_ids = [
        segment_id
        for zone_id in sorted(zone_targets)
        for segment_id in zone_targets[zone_id]
    ]
    if assigned_ids != target_ids or len(assigned_ids) != len(set(assigned_ids)):
        raise PdfQAFailure("PDF zones must exactly partition target segments in order")
    blocks_by_segment: dict[str, PdfBlock] = {}
    for block in document.blocks:
        if block.segment_id is None:
            continue
        if block.segment_id in blocks_by_segment:
            raise PdfQAFailure("PDF document contains duplicate segment mappings")
        blocks_by_segment[block.segment_id] = block
    for segment in target_segments:
        block = blocks_by_segment.get(segment.id)
        if block is None or block.id != segment.locator:
            raise PdfQAFailure(
                "PDF document and segment manifest target mappings disagree"
            )
    metrics = qa.metrics
    if metrics["translated_block_count"] != len(target_segments):
        raise PdfQAFailure(
            "automated QA translated block count disagrees with target segments"
        )
    figure_count = sum(block.kind == "figure" for block in document.blocks)
    if metrics["figure_count"] != figure_count:
        raise PdfQAFailure(
            "automated QA figure count disagrees with PDF document"
        )
    visible_ids = {
        block.id
        for block in document.blocks
        if block.kind not in {"header", "footer", "page-number"}
    }
    flowable_ids = {item.block_id for item in layout.flowables}
    if not visible_ids.issubset(flowable_ids):
        raise PdfQAFailure("PDF layout does not cover every reportable document block")
    if any(item.page_number > metrics["output_page_count"] for item in layout.flowables):
        raise PdfQAFailure("PDF layout page evidence exceeds automated QA page count")
    return PdfReportEvidence(
        source=source,
        document=document,
        segments=segments,
        glossary=glossary,
        semantic_review=semantic_review,
        layout=layout,
        qa=qa,
        visual_review=visual_review,
    )


def build_pdf_manifest(
    run_dir: Path, *, evidence: PdfReportEvidence | None = None
) -> dict[str, object]:
    """Build one stable, PDF-specific manifest from reviewed run evidence."""
    run_dir = Path(run_dir)
    evidence = evidence or _read_pdf_report_evidence(run_dir)
    qa = evidence.qa
    visual_review = evidence.visual_review
    source = evidence.source
    document = evidence.document
    layout = evidence.layout
    segments = evidence.segments
    glossary = evidence.glossary
    semantic_review = evidence.semantic_review
    metrics = dict(qa.metrics)
    block_counts = Counter(block.kind for block in document.blocks)
    target_segments = [segment for segment in segments if segment.target]
    tables = {
        block.table_id for block in document.blocks if block.table_id is not None
    }
    unreconstructed = tuple(link for link in layout.links if not link.reconstructed)
    extraction_warnings = set(document.extraction_warnings)
    for link in unreconstructed:
        destination = link.uri or link.destination or "unresolved:missing-destination"
        if not any(
            link.visible_label in warning
            and destination in warning
            and str(link.reason) in warning
            for warning in extraction_warnings
        ):
            extraction_warnings.add(
                f"unreconstructed link label={link.visible_label!r} "
                f"destination={destination!r} reason={link.reason}"
            )
    all_warnings = tuple(sorted(set(source.warnings) | extraction_warnings))
    status = (
        "passed"
        if qa.passed and not visual_review.unresolved_required
        else "failed"
    )
    if source.input_kind == "local":
        input_record = PdfManifestInput(
            kind="local",
            name=Path(source.final_source).name,
            requested_url=None,
            final_url=None,
        )
    else:
        input_record = PdfManifestInput(
            kind="public",
            name=None,
            requested_url=source.requested_source,
            final_url=source.final_source,
        )
    manifest = PdfFinalManifest(
        schema_version="1.0",
        tool_version=__version__,
        input=input_record,
        source=PdfManifestSource(
            sha256=document.source_sha256,
            byte_length=source.byte_length,
            page_count=document.page_count,
            selectable_characters=document.selectable_characters,
            scan_candidate_pages=tuple(document.scan_candidate_pages),
            pages=tuple(document.pages),
        ),
        extraction=PdfManifestExtraction(
            layout_validation_counts={
                "blocks": len(document.blocks),
                "translation_targets": len(target_segments),
                "tables": len(tables),
                "table_cells": len(document.table_cells),
                "reconstructed_links": sum(
                    link.reconstructed for link in layout.links
                ),
                "unreconstructed_links": len(unreconstructed),
            },
            warnings=tuple(sorted(extraction_warnings)),
            unreconstructed_links=unreconstructed,
        ),
        counts=PdfManifestCounts(
            {
                "headings": block_counts["heading"],
                "paragraphs": block_counts["paragraph"],
                "lists": block_counts["list-item"],
                "tables": len(tables),
                "cells": len(document.table_cells),
                "figures": block_counts["figure"],
                "captions": block_counts["caption"],
                "footnotes": block_counts["footnote"],
                "segments": len(segments),
                "zones": len(semantic_review["retries"]),
            }
        ),
        translation=PdfManifestTranslation(
            source_language=_detect_source_language(segments),
            target_language="ko",
            terminology={
                "glossary": dict(sorted(glossary.items())),
                "policy_id": _TERMINOLOGY_POLICY_ID,
                "policy_version": _TERMINOLOGY_POLICY_VERSION,
            },
            retries=dict(semantic_review["retries"]),  # type: ignore[arg-type]
            master_semantic_review=semantic_review,
        ),
        output=PdfManifestOutput(
            page_count=metrics["output_page_count"],
            sha256=qa.staged_pdf_sha256,
            embedded_fonts=("NotoSansCJKKR-Bold", "NotoSansCJKKR-Regular"),
            link_count=metrics["link_count"],
            figure_count=metrics["figure_count"],
            qa_status=status,
        ),
        qa=PdfManifestQA(
            rendered_page_hashes=dict(qa.rendered_page_hashes),
            layout_findings=_json_copy(visual_review.findings),
            contact_sheet_coverage=_json_copy(
                visual_review.contact_sheets_reviewed
            ),
            automated=qa.to_dict(),
            visual=visual_review.to_dict(),
            warnings=all_warnings,
            final_status=status,
        ),
    )
    return PdfFinalManifest.from_dict(manifest.to_dict()).to_dict()


def write_pdf_manifest(
    run_dir: Path,
    path: Path | BinaryIO,
    *,
    evidence: PdfReportEvidence | None = None,
) -> dict[str, object]:
    """Write canonical PDF manifest JSON and return its payload."""
    payload = build_pdf_manifest(run_dir, evidence=evidence)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _write_text_target(path, serialized)
    return payload


def render_pdf_review_report(manifest: Mapping[str, object]) -> str:
    """Render deterministic Markdown from a validated PDF manifest."""
    canonical = PdfFinalManifest.from_dict(manifest).to_dict()
    input_record = _mapping(canonical, "input", "manifest")
    source = _mapping(canonical, "source", "manifest")
    extraction = _mapping(canonical, "extraction", "manifest")
    counts = _mapping(canonical, "counts", "manifest")
    output = _mapping(canonical, "output", "manifest")
    qa = _mapping(canonical, "qa", "manifest")
    automated = _mapping(qa, "automated", "manifest.qa")
    translation = _mapping(canonical, "translation", "manifest")
    semantic = _mapping(
        translation, "master_semantic_review", "manifest.translation"
    )
    visual = _mapping(qa, "visual", "manifest.qa")
    status = "PASS" if qa.get("final_status") == "passed" else "FAIL"
    source_label = input_record.get("name") or input_record.get("final_url") or ""
    lines = [
        "# PDF Translation QA Review Report",
        "",
        f"- Status: **{status}**",
        f"- Input kind: {_markdown(input_record.get('kind', ''))}",
        f"- Source: {_markdown(source_label)}",
        f"- Source SHA-256: `{source.get('sha256', '')}`",
        f"- Source bytes/pages: {source.get('byte_length', 0)} / {source.get('page_count', 0)}",
        f"- Output pages: {output.get('page_count', 0)}",
        f"- Output SHA-256: `{output.get('sha256', '')}`",
        "",
        "## Extraction and document counts",
        "",
    ]
    layout_counts = extraction.get("layout_validation_counts", {})
    if isinstance(layout_counts, Mapping):
        for name in sorted(layout_counts, key=str):
            lines.append(f"- {_markdown(name)}: {layout_counts[name]}")
    for name in sorted(counts, key=str):
        lines.append(f"- count.{_markdown(name)}: {counts[name]}")
    lines.extend([
        "",
        "## Automated PDF QA",
        "",
    ])
    findings = automated.get("findings", [])
    if isinstance(findings, list) and findings:
        for finding in sorted(
            findings,
            key=lambda item: str(item.get("code", "")) if isinstance(item, Mapping) else "",
        ):
            if isinstance(finding, Mapping):
                lines.append(
                    f"- **{_markdown(finding.get('code', ''))}**: "
                    f"{_markdown(finding.get('evidence', ''))}"
                )
    else:
        lines.append("None.")

    lines.extend(["", "## Semantic review", "", "### Retries", ""])
    retries = semantic.get("retries", {})
    if isinstance(retries, Mapping) and retries:
        lines.extend(["| Zone | Retries |", "| --- | ---: |"])
        for zone_id in sorted(retries, key=str):
            lines.append(f"| {_markdown(zone_id)} | {retries[zone_id]} |")
    else:
        lines.append("None.")
    lines.extend(["", "### Findings", ""])
    section_findings = semantic.get("section_findings", {})
    if isinstance(section_findings, Mapping) and section_findings:
        for zone_id in sorted(section_findings, key=str):
            lines.append(f"- **{_markdown(zone_id)}**")
            records = section_findings[zone_id]
            if isinstance(records, list):
                for finding in sorted(
                    records,
                    key=lambda item: str(item.get("dimension", ""))
                    if isinstance(item, Mapping)
                    else "",
                ):
                    if isinstance(finding, Mapping):
                        lines.append(
                            "  - "
                            f"**{_markdown(finding.get('dimension', ''))}** "
                            f"({_markdown(finding.get('verdict', ''))}): "
                            f"{_markdown(finding.get('evidence', ''))}"
                        )

    lines.extend(["", "## Visual layout review", ""])
    pages = visual.get("pages_reviewed", [])
    contacts = visual.get("contact_sheets_reviewed", {})
    lines.append(
        "- Pages reviewed: "
        + ", ".join(str(page) for page in pages)
        if isinstance(pages, list)
        else "- Pages reviewed:"
    )
    if isinstance(contacts, Mapping):
        for name in sorted(contacts, key=str):
            covered = contacts[name]
            rendered = ", ".join(str(page) for page in covered) if isinstance(covered, list) else ""
            lines.append(f"- Contact sheet {_markdown(name)}: pages {rendered}")
    lines.extend(["", "### Findings", ""])
    visual_findings = visual.get("findings", {})
    if isinstance(visual_findings, Mapping):
        for dimension in sorted(visual_findings, key=str):
            finding = visual_findings[dimension]
            if isinstance(finding, Mapping):
                lines.append(
                    f"- **{_markdown(dimension)}** "
                    f"({_markdown(finding.get('verdict', ''))}): "
                    f"{_markdown(finding.get('evidence', ''))}"
                )
    unresolved = visual.get("unresolved_required", [])
    lines.extend(["", "### Unresolved required items", ""])
    if isinstance(unresolved, list) and unresolved:
        lines.extend(f"- {_markdown(item)}" for item in unresolved)
    else:
        lines.append("None.")
    lines.extend(["", "## Warnings and unreconstructed links", ""])
    warnings = qa.get("warnings", [])
    if isinstance(warnings, list) and warnings:
        lines.extend(f"- {_markdown(item)}" for item in warnings)
    else:
        lines.append("None.")
    unreconstructed = extraction.get("unreconstructed_links", [])
    if isinstance(unreconstructed, list):
        for link in unreconstructed:
            if not isinstance(link, Mapping):
                continue
            destination = link.get("uri") or link.get("destination") or ""
            lines.append(
                "- Link "
                f"{_markdown(link.get('id', ''))}: label="
                f"{_markdown(link.get('visible_label', ''))}; destination="
                f"{_markdown(destination)}; reason={_markdown(link.get('reason', ''))}"
            )
    lines.extend(
        [
            "",
            "## Complete canonical provenance",
            "",
            "```json",
            json.dumps(canonical, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def write_pdf_review_report(
    manifest: Mapping[str, object], path: Path | BinaryIO
) -> None:
    """Write deterministic human-readable PDF review evidence."""
    _write_text_target(path, render_pdf_review_report(manifest))


def _write_text_target(path: Path | BinaryIO, content: str) -> None:
    if hasattr(path, "write"):
        path.write(content.encode("utf-8"))  # type: ignore[union-attr]
        return
    _atomic_write(Path(path), content)


def _read_pdf_qa(path: Path) -> PdfQAResult:
    value = _read_json(path, "automated PDF QA")
    try:
        return PdfQAResult.from_dict(value, path.parent / "qa-pages")
    except PdfQAFailure:
        raise
    except (TypeError, ValueError) as error:
        raise PdfQAFailure(f"invalid automated PDF QA: {error}") from error


def _layout(path: Path) -> PdfAssemblyLayout:
    try:
        return PdfAssemblyLayout.from_dict(_read_json(path, "PDF layout"))
    except PdfAssemblyError as error:
        raise PdfQAFailure(f"invalid PDF layout: {error}") from error


def _read_pdf_report_evidence(run_dir: Path) -> PdfReportEvidence:
    qa = _read_pdf_qa(run_dir / "pdf-qa.json")
    visual_review = read_pdf_layout_review(
        run_dir / "pdf-layout-review.json", run_dir / "pdf-qa.json"
    )
    try:
        source_pdf = (run_dir / "source.pdf").read_bytes()
        segments_text = (run_dir / "segments.jsonl").read_text(encoding="utf-8")
        zone_entries = sorted((run_dir / "zones").iterdir(), key=lambda path: path.name)
    except (OSError, UnicodeError) as error:
        raise PdfQAFailure(f"cannot read PDF report evidence: {error}") from error
    zone_values: dict[str, Mapping[str, Any]] = {}
    for path in zone_entries:
        if _ZONE_FILE.fullmatch(path.name) is None:
            raise PdfQAFailure(f"unexpected PDF zone evidence: {path.name}")
        zone_values[path.stem] = _read_json(path, "PDF zone")
    return build_pdf_report_evidence(
        source_pdf=source_pdf,
        source_value=_read_json(run_dir / "source.json", "PDF source record"),
        document_value=_read_json(run_dir / "document.json", "PDF document"),
        segments_text=segments_text,
        glossary_value=_read_json(run_dir / "glossary.json", "PDF glossary"),
        review_value=_read_json(run_dir / "review.json", "semantic review"),
        zone_values=zone_values,
        layout=_layout(run_dir / "layout.json"),
        qa=qa,
        visual_review=visual_review,
    )


def _zone_snapshot(
    zone_values: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, int]]:
    expected_names = [
        f"zone-{number:03d}" for number in range(1, len(zone_values) + 1)
    ]
    if list(zone_values) != expected_names:
        raise PdfQAFailure("PDF zone names must be exact and sequential")
    targets: dict[str, list[str]] = {}
    attempts: dict[str, int] = {}
    fields = {
        "attempt",
        "context_after_ids",
        "context_before_ids",
        "expected_tokens",
        "heading_path",
        "id",
        "target_ids",
    }
    for zone_id, value in zone_values.items():
        if set(value) != fields or value.get("id") != zone_id:
            raise PdfQAFailure(f"PDF zone fields or embedded ID are invalid: {zone_id}")
        target_ids = _string_list_value(value.get("target_ids"), f"{zone_id}.target_ids")
        if not target_ids or len(target_ids) != len(set(target_ids)):
            raise PdfQAFailure(f"PDF zone target IDs must be nonempty and unique: {zone_id}")
        _string_list_value(value.get("heading_path"), f"{zone_id}.heading_path")
        before = _string_list_value(
            value.get("context_before_ids"), f"{zone_id}.context_before_ids"
        )
        after = _string_list_value(
            value.get("context_after_ids"), f"{zone_id}.context_after_ids"
        )
        if set(target_ids) & set(before + after):
            raise PdfQAFailure(f"PDF zone context overlaps targets: {zone_id}")
        attempt = value.get("attempt")
        if type(attempt) is not int or not 0 <= attempt <= 2:
            raise PdfQAFailure(f"PDF zone attempt is invalid: {zone_id}")
        expected_tokens = value.get("expected_tokens")
        if not isinstance(expected_tokens, Mapping) or set(expected_tokens) != set(target_ids):
            raise PdfQAFailure(f"PDF zone token expectations are incomplete: {zone_id}")
        for segment_id, tokens in expected_tokens.items():
            _string_list_value(tokens, f"{zone_id}.expected_tokens[{segment_id!r}]")
        targets[zone_id] = target_ids
        attempts[zone_id] = attempt
    return targets, attempts


def _semantic_review_from_value(
    review: Mapping[str, Any],
    zone_ids: set[str],
    zone_attempts: Mapping[str, int],
) -> dict[str, object]:
    if set(review) != {
        "semantic_input_sha256",
        "retries",
        "section_findings",
        "unresolved_required",
    }:
        raise PdfQAFailure("semantic review fields are not exact")
    semantic_input_sha256 = review.get("semantic_input_sha256")
    if (
        not isinstance(semantic_input_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", semantic_input_sha256) is None
    ):
        raise PdfQAFailure("semantic review input digest is invalid")
    retries = review.get("retries")
    findings = review.get("section_findings")
    unresolved = review.get("unresolved_required")
    if not isinstance(retries, Mapping) or set(retries) != zone_ids or any(
        not isinstance(zone_id, str)
        or type(count) is not int
        or not 0 <= count <= 2
        for zone_id, count in retries.items()
    ):
        raise PdfQAFailure("semantic review retries do not exactly cover PDF zones")
    if any(retries[zone_id] != zone_attempts[zone_id] for zone_id in zone_ids):
        raise PdfQAFailure("semantic review retries disagree with PDF zone attempts")
    if not isinstance(findings, Mapping) or set(findings) != zone_ids:
        raise PdfQAFailure("semantic review findings do not exactly cover PDF zones")
    canonical_findings: dict[str, list[dict[str, str]]] = {}
    expected_unresolved: list[str] = []
    for zone_id in sorted(zone_ids):
        records = findings[zone_id]
        if not isinstance(records, list):
            raise PdfQAFailure("semantic review findings must be arrays")
        dimensions: set[str] = set()
        canonical_records: list[dict[str, str]] = []
        for record in records:
            if not isinstance(record, Mapping) or set(record) != {
                "dimension",
                "verdict",
                "evidence",
            }:
                raise PdfQAFailure("semantic review finding fields are not exact")
            dimension = record["dimension"]
            verdict = record["verdict"]
            evidence = record["evidence"]
            if (
                not isinstance(dimension, str)
                or dimension not in _SEMANTIC_DIMENSIONS
                or dimension in dimensions
            ):
                raise PdfQAFailure("semantic review dimensions are incomplete or duplicated")
            if verdict not in {"pass", "required-fix"}:
                raise PdfQAFailure("semantic review verdict is not supported")
            if not isinstance(evidence, str) or not evidence.strip():
                raise PdfQAFailure("semantic review evidence must be nonempty")
            dimensions.add(dimension)
            canonical_records.append(
                {"dimension": dimension, "evidence": evidence, "verdict": str(verdict)}
            )
            if verdict == "required-fix":
                expected_unresolved.append(f"{zone_id}:{dimension}")
        if dimensions != _SEMANTIC_DIMENSIONS:
            raise PdfQAFailure("semantic review dimensions are incomplete or duplicated")
        canonical_findings[zone_id] = sorted(
            canonical_records, key=lambda item: item["dimension"]
        )
    if (
        not isinstance(unresolved, list)
        or any(not isinstance(item, str) for item in unresolved)
        or unresolved != sorted(expected_unresolved)
    ):
        raise PdfQAFailure("semantic review unresolved required findings disagree")
    if unresolved:
        raise PdfQAFailure("semantic review has unresolved required findings")
    return {
        "semantic_input_sha256": semantic_input_sha256,
        "retries": {zone_id: retries[zone_id] for zone_id in sorted(zone_ids)},
        "section_findings": canonical_findings,
        "unresolved_required": [],
    }


def _string_mapping_value(value: Mapping[str, Any], label: str) -> dict[str, str]:
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise PdfQAFailure(f"{label} must map strings to strings")
    return dict(value)  # type: ignore[arg-type]


def _string_list_value(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PdfQAFailure(f"{label} must be a string array")
    return list(value)


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PdfQAFailure(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, Mapping):
        raise PdfQAFailure(f"{label} must be a JSON object")
    return value


def _detect_source_language(segments: Sequence[Segment]) -> str:
    text = "\n".join(segment.source_text for segment in segments if segment.target)[:100_000]
    if not text.strip() or re.search(r"[^\W\d_]", text, flags=re.UNICODE) is None:
        return "und"
    try:
        factory = DetectorFactory()
        factory.load_profile(PROFILES_DIRECTORY)
        factory.set_seed(0)
        detector = factory.create()
        detector.append(text)
        return detector.detect()
    except (LangDetectException, OSError, UnicodeError):
        return "und"


def _mapping(data: Mapping[str, object], key: str, context: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise PdfQAFailure(f"{context}.{key} must be an object")
    return value


def _markdown(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", r"\|")


def _report_mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise PdfQAFailure(f"{context} must be an object with string keys")
    return value  # type: ignore[return-value]


def _exact_report_mapping(
    value: object,
    context: str,
    fields: set[str],
) -> Mapping[str, Any]:
    data = _report_mapping(value, context)
    if set(data) != fields:
        raise PdfQAFailure(f"{context} fields must be exactly {sorted(fields)}")
    return data


def _report_string(data: Mapping[str, Any], field: str, context: str) -> str:
    value = data.get(field)
    if not isinstance(value, str):
        raise PdfQAFailure(f"{context}.{field} must be a string")
    return value


def _report_optional_string(
    data: Mapping[str, Any], field: str, context: str
) -> str | None:
    value = data.get(field)
    if value is not None and not isinstance(value, str):
        raise PdfQAFailure(f"{context}.{field} must be a string or null")
    return value


def _report_nonnegative_int_value(value: object, context: str) -> int:
    if type(value) is not int or value < 0:
        raise PdfQAFailure(f"{context} must be a nonnegative integer")
    return value


def _report_nonnegative_int(
    data: Mapping[str, Any], field: str, context: str
) -> int:
    return _report_nonnegative_int_value(data.get(field), f"{context}.{field}")


def _report_positive_int(
    data: Mapping[str, Any], field: str, context: str
) -> int:
    value = _report_nonnegative_int(data, field, context)
    if value == 0:
        raise PdfQAFailure(f"{context}.{field} must be positive")
    return value


def _report_positive_int_list(value: object, context: str) -> list[int]:
    if not isinstance(value, list):
        raise PdfQAFailure(f"{context} must be an array")
    result: list[int] = []
    for index, item in enumerate(value):
        if type(item) is not int or item <= 0:
            raise PdfQAFailure(f"{context}[{index}] must be a positive integer")
        result.append(item)
    return result


def _report_string_list(value: object, context: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PdfQAFailure(f"{context} must be a string array")
    return list(value)


def _report_string_mapping(value: object, context: str) -> dict[str, str]:
    data = _report_mapping(value, context)
    if any(not isinstance(item, str) for item in data.values()):
        raise PdfQAFailure(f"{context} must map strings to strings")
    return {key: item for key, item in data.items()}


def _report_sha256(data: Mapping[str, Any], field: str, context: str) -> str:
    value = _report_string(data, field, context)
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PdfQAFailure(f"{context}.{field} must be a lowercase SHA-256")
    return value


def _report_sha256_mapping(value: object, context: str) -> dict[str, str]:
    data = _report_mapping(value, context)
    result: dict[str, str] = {}
    for key, item in data.items():
        if not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None:
            raise PdfQAFailure(f"{context}[{key!r}] must be a lowercase SHA-256")
        result[key] = item
    return result


def _json_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        copied = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as error:
        raise PdfQAFailure(f"report evidence is not JSON serializable: {error}") from error
    if not isinstance(copied, dict):
        raise PdfQAFailure("report evidence must be a JSON object")
    return copied


def _atomic_write(path: Path, text: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
