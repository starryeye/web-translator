"""Deterministic machine- and human-readable QA evidence writers."""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
import re
import tempfile

from web_translator import __version__
from web_translator.models import (
    Finding,
    ManifestAsset,
    ManifestProvenance,
    MasterReview,
    QAResult,
)


def write_manifest(
    result: QAResult,
    path: Path,
    provenance: ManifestProvenance | None = None,
) -> None:
    """Write canonical JSON evidence for one QA run."""
    provenance = provenance or _fallback_provenance(result)
    payload = {
        **provenance.to_dict(),
        "browser_metrics": result.browser_metrics,
        "capture_metadata": result.capture_metadata,
        "qa_status": "passed" if result.passed else "failed",
        "required_findings": [
            _finding_payload(item) for item in sorted(result.required_findings, key=_finding_key)
        ],
        "screenshots": sorted(_portable_path(item) for item in result.screenshots),
        "source_url": result.source_url,
        "warnings": [
            _finding_payload(item) for item in sorted(result.warnings, key=_finding_key)
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write(Path(path), serialized)


def _fallback_provenance(result: QAResult) -> ManifestProvenance:
    """Keep the evidence writer usable for isolated QA unit results."""
    capture = result.capture_metadata
    asset_map = capture.get("asset_map", {})
    fingerprints = capture.get("fingerprints", {})
    critical = set(capture.get("critical_assets", []))
    assets: list[ManifestAsset] = []
    if isinstance(asset_map, dict) and isinstance(fingerprints, dict):
        for source, local_path in sorted(asset_map.items()):
            if not isinstance(source, str) or not isinstance(local_path, str):
                continue
            digest = fingerprints.get(local_path, "")
            if not isinstance(digest, str):
                digest = ""
            assets.append(
                ManifestAsset(
                    source=source,
                    local_path=local_path,
                    sha256=digest,
                    classification="critical" if local_path in critical else "optional",
                )
            )
    return ManifestProvenance(
        captured_at=str(capture.get("captured_at", "")),
        requested_url=str(capture.get("requested_url", result.source_url)),
        final_url=str(capture.get("final_url", result.source_url)),
        source_language="und",
        target_language="ko",
        terminology_policy_id="english-technical-first-use-ko-gloss",
        terminology_policy_version="1.0",
        tool_version=__version__,
        segment_count=0,
        target_segment_count=0,
        translated_segment_count=0,
        zone_count=0,
        assets=assets,
        missing_optional_assets=[
            item
            for item in capture.get("missing_optional_assets", [])
            if isinstance(item, str)
        ]
        if isinstance(capture.get("missing_optional_assets", []), list)
        else [],
        retries={},
    )


def write_review_report(result: QAResult, review: MasterReview, path: Path) -> None:
    """Write deterministic Markdown evidence for human review."""
    status = "PASS" if result.passed and not review.unresolved_required else "FAIL"
    lines = [
        "# Translation QA Review Report",
        "",
        f"- Status: **{status}**",
        f"- Source: {_markdown(result.source_url)}",
        f"- Screenshots: {len(result.screenshots)}",
        "",
        "## Automated required findings",
        "",
    ]
    lines.extend(
        _finding_lines(sorted(result.required_findings, key=_finding_key), empty="None.")
    )
    lines.extend(["", "## Warnings", ""])
    lines.extend(_finding_lines(sorted(result.warnings, key=_finding_key), empty="None."))
    lines.extend(["", "## Browser evidence", ""])
    if result.browser_metrics:
        lines.extend(
            [
                "| Viewport | Horizontal overflow | Broken images | Clipped text |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for viewport in sorted(result.browser_metrics):
            metric = result.browser_metrics[viewport]
            lines.append(
                "| "
                f"{_markdown(viewport)} | {str(bool(metric.get('horizontalOverflow'))).lower()} | "
                f"{len(metric.get('brokenImages', []))} | {int(metric.get('clippedText', 0))} |"
            )
    else:
        lines.append("No browser evidence recorded.")

    lines.extend(["", "## Master semantic review", "", "### Unresolved required items", ""])
    if review.unresolved_required:
        lines.extend(f"- {_markdown(item)}" for item in sorted(review.unresolved_required))
    else:
        lines.append("None.")

    lines.extend(["", "### Translation retries", ""])
    if review.retries:
        lines.extend(["| Zone | Retries |", "| --- | ---: |"])
        for zone_id in sorted(review.retries):
            lines.append(f"| {_markdown(zone_id)} | {review.retries[zone_id]} |")
    else:
        lines.append("None.")

    lines.extend(["", "### Section findings", ""])
    if review.section_findings:
        for zone_id in sorted(review.section_findings):
            lines.append(f"- **{_markdown(zone_id)}**")
            findings = sorted(review.section_findings[zone_id])
            lines.extend(f"  - {_markdown(item)}" for item in findings)
    else:
        lines.append("None.")
    lines.append("")
    _atomic_write(Path(path), "\n".join(lines))


def _finding_payload(finding: Finding) -> dict[str, object]:
    return {
        "code": finding.code,
        "evidence": finding.evidence,
        "message": finding.message,
        "severity": finding.severity,
    }


def _finding_key(finding: Finding) -> tuple[str, str, str, str]:
    evidence = json.dumps(
        finding.evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return finding.severity, finding.code, finding.message, evidence


def _finding_lines(findings: list[Finding], *, empty: str) -> list[str]:
    if not findings:
        return [empty]
    result: list[str] = []
    for finding in findings:
        result.append(f"- **{_markdown(finding.code)}**: {_markdown(finding.message)}")
        if finding.evidence:
            evidence = json.dumps(
                finding.evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            result.append(f"  - Evidence: {_markdown(evidence)}")
    return result


def _portable_path(path: Path) -> str:
    return Path(path).as_posix()


def _markdown(value: object) -> str:
    normalized = str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    escaped = html.escape(normalized, quote=False).replace("|", r"\|")
    longest_run = max((len(run) for run in re.findall(r"`+", escaped)), default=0)
    delimiter = "`" * (longest_run + 1)
    return f"{delimiter} {escaped} {delimiter}"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
