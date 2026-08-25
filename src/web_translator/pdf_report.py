"""Deterministic manifest and review-report writers for finalized PDFs."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from langdetect import DetectorFactory, PROFILES_DIRECTORY
from langdetect.lang_detect_exception import LangDetectException

from web_translator import __version__
from web_translator.models import Segment, SegmentContractError, read_segments
from web_translator.pdf_flowables import PdfAssemblyError, PdfAssemblyLayout
from web_translator.pdf_models import PdfContractError, PdfDocument, PdfSourceRecord
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


def build_pdf_manifest(run_dir: Path) -> dict[str, object]:
    """Build one stable, PDF-specific manifest from reviewed run evidence."""
    run_dir = Path(run_dir)
    qa = _read_pdf_qa(run_dir / "pdf-qa.json")
    visual_review = read_pdf_layout_review(
        run_dir / "pdf-layout-review.json", run_dir / "pdf-qa.json"
    )
    source = _source_record(run_dir / "source.json")
    document = _document(run_dir / "document.json")
    layout = _layout(run_dir / "layout.json")
    try:
        segments = read_segments(run_dir / "segments.jsonl")
    except (OSError, UnicodeError, SegmentContractError) as error:
        raise PdfQAFailure(f"cannot read PDF segments for report: {error}") from error
    glossary = _string_mapping(run_dir / "glossary.json", "PDF glossary")
    semantic_review = _semantic_review(run_dir)
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


def write_pdf_manifest(run_dir: Path, path: Path) -> dict[str, object]:
    """Write canonical PDF manifest JSON and return its payload."""
    payload = build_pdf_manifest(run_dir)
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


def _source_record(path: Path) -> PdfSourceRecord:
    try:
        return PdfSourceRecord.from_dict(_read_json(path, "PDF source record"))
    except PdfContractError as error:
        raise PdfQAFailure(f"invalid PDF source record: {error}") from error


def _document(path: Path) -> PdfDocument:
    try:
        return PdfDocument.from_dict(_read_json(path, "PDF document"))
    except PdfContractError as error:
        raise PdfQAFailure(f"invalid PDF document: {error}") from error


def _layout(path: Path) -> PdfAssemblyLayout:
    try:
        return PdfAssemblyLayout.from_dict(_read_json(path, "PDF layout"))
    except PdfAssemblyError as error:
        raise PdfQAFailure(f"invalid PDF layout: {error}") from error


def _semantic_review(run_dir: Path) -> dict[str, object]:
    review = _read_json(run_dir / "review.json", "semantic review")
    if set(review) != {"retries", "section_findings", "unresolved_required"}:
        raise PdfQAFailure("semantic review fields are not exact")
    zone_dir = run_dir / "zones"
    try:
        zone_names = sorted(path.name for path in zone_dir.iterdir())
    except OSError as error:
        raise PdfQAFailure(f"cannot read PDF zones for report: {error}") from error
    if not zone_names or any(_ZONE_FILE.fullmatch(name) is None for name in zone_names):
        raise PdfQAFailure("PDF zones must contain only zone-NNN.json files")
    zone_ids = {Path(name).stem for name in zone_names}
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


def _string_mapping(path: Path, label: str) -> dict[str, str]:
    value = _read_json(path, label)
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise PdfQAFailure(f"{label} must map strings to strings")
    return dict(value)  # type: ignore[arg-type]


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
