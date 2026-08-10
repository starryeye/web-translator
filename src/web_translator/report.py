"""Deterministic machine- and human-readable QA evidence writers."""

from __future__ import annotations

from pathlib import Path
import html
import json
import os
import tempfile

from web_translator.models import Finding, MasterReview, QAResult


def write_manifest(result: QAResult, path: Path) -> None:
    """Write canonical JSON evidence for one QA run."""
    payload = {
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
            result.append(f"  - Evidence: <code>{_markdown(evidence)}</code>")
    return result


def _portable_path(path: Path) -> str:
    return Path(path).as_posix()


def _markdown(value: object) -> str:
    escaped = html.escape(str(value), quote=False).replace("\n", " ")
    for character in ("\\", "`", "*", "_", "[", "]", "|"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


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
