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
from typing import Any

from langdetect import DetectorFactory, PROFILES_DIRECTORY
from langdetect.lang_detect_exception import LangDetectException

from web_translator import __version__
from web_translator.models import Segment, SegmentContractError, read_segments_stream
from web_translator.pdf_flowables import PdfAssemblyError, PdfAssemblyLayout
from web_translator.pdf_models import (
    PdfBlock,
    PdfContractError,
    PdfDocument,
    PdfLayoutReview,
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
    block_counts = dict(sorted(Counter(block.kind for block in document.blocks).items()))
    target_segments = [segment for segment in segments if segment.target]
    return {
        "automated_qa": qa.to_dict(),
        "block_counts": block_counts,
        "inspection": {
            "page_count": document.page_count,
            "pages": [page.to_dict() for page in document.pages],
            "scan_candidate_pages": list(document.scan_candidate_pages),
            "selectable_characters": document.selectable_characters,
            "source_sha256": document.source_sha256,
        },
        "languages": {
            "source": _detect_source_language(segments),
            "target": "ko",
        },
        "output": {
            "embedded_font_count": metrics["embedded_font_count"],
            "figure_count": metrics["figure_count"],
            "link_count": metrics["link_count"],
            "minimum_font_size": layout.minimum_font_size,
            "page_count": metrics["output_page_count"],
            "sha256": qa.staged_pdf_sha256,
            "translated_block_count": metrics["translated_block_count"],
        },
        "qa_status": "passed" if qa.passed and not visual_review.unresolved_required else "failed",
        "schema_version": "1.0",
        "source": source.to_dict(),
        "terminology": {
            "glossary": dict(sorted(glossary.items())),
            "policy_id": _TERMINOLOGY_POLICY_ID,
            "policy_version": _TERMINOLOGY_POLICY_VERSION,
        },
        "tool_version": __version__,
        "translation": {
            "segment_count": len(segments),
            "semantic_review": semantic_review,
            "target_segment_count": len(target_segments),
            "translated_block_count": metrics["translated_block_count"],
            "zone_count": len(semantic_review["retries"]),
            "retries": semantic_review["retries"],
        },
        "visual_review": visual_review.to_dict(),
        "warnings": sorted(source.warnings),
    }


def write_pdf_manifest(
    run_dir: Path,
    path: Path,
    *,
    evidence: PdfReportEvidence | None = None,
) -> dict[str, object]:
    """Write canonical PDF manifest JSON and return its payload."""
    payload = build_pdf_manifest(run_dir, evidence=evidence)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write(Path(path), serialized)
    return payload


def render_pdf_review_report(manifest: Mapping[str, object]) -> str:
    """Render deterministic Markdown from a validated PDF manifest."""
    status = "PASS" if manifest.get("qa_status") == "passed" else "FAIL"
    source = _mapping(manifest, "source", "manifest")
    output = _mapping(manifest, "output", "manifest")
    automated = _mapping(manifest, "automated_qa", "manifest")
    translation = _mapping(manifest, "translation", "manifest")
    semantic = _mapping(translation, "semantic_review", "manifest.translation")
    visual = _mapping(manifest, "visual_review", "manifest")
    lines = [
        "# PDF Translation QA Review Report",
        "",
        f"- Status: **{status}**",
        f"- Source: {_markdown(source.get('final_source', ''))}",
        f"- Output pages: {output.get('page_count', 0)}",
        f"- Output SHA-256: `{output.get('sha256', '')}`",
        "",
        "## Automated PDF QA",
        "",
    ]
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
    lines.append("")
    return "\n".join(lines)


def write_pdf_review_report(
    manifest: Mapping[str, object], path: Path
) -> None:
    """Write deterministic human-readable PDF review evidence."""
    _atomic_write(Path(path), render_pdf_review_report(manifest))


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
    if set(review) != {"retries", "section_findings", "unresolved_required"}:
        raise PdfQAFailure("semantic review fields are not exact")
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
