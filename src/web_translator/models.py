"""Immutable data contracts and JSONL serialization for translation runs."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TextIO


class SegmentContractError(ValueError):
    """A persisted segment JSONL record violates the segment contract."""


@dataclass(frozen=True, slots=True)
class ProtectedToken:
    """A source fragment temporarily replaced before translation."""

    token: str
    kind: str
    value: str

    def to_dict(self) -> dict[str, str]:
        return {"token": self.token, "kind": self.kind, "value": self.value}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProtectedToken:
        return _protected_token_from_dict(data, "ProtectedToken")


@dataclass(frozen=True, slots=True)
class Segment:
    """A translatable fragment of a captured web page."""

    id: str
    locator: str
    semantic_type: str
    heading_path: list[str]
    source_text: str
    protected: list[ProtectedToken]
    context_ids: list[str]
    target: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "locator": self.locator,
            "semantic_type": self.semantic_type,
            "heading_path": self.heading_path,
            "source_text": self.source_text,
            "protected": [token.to_dict() for token in self.protected],
            "context_ids": self.context_ids,
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Segment:
        data = _require_mapping(data, "Segment")
        return cls(
            id=_require_string(data, "id", "Segment"),
            locator=_require_string(data, "locator", "Segment"),
            semantic_type=_require_string(data, "semantic_type", "Segment"),
            heading_path=_require_string_list(data, "heading_path", "Segment"),
            source_text=_require_string(data, "source_text", "Segment"),
            protected=[
                _protected_token_from_dict(token, f"Segment.protected[{index}]")
                for index, token in enumerate(_require_list(data, "protected", "Segment"))
            ],
            context_ids=_require_string_list(data, "context_ids", "Segment"),
            target=_require_bool(data, "target", "Segment"),
        )


@dataclass(frozen=True, slots=True)
class Translation:
    """A translated segment and optional reviewer observations."""

    segment_id: str
    text: str
    notes: str | None = None
    glossary_observations: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "text": self.text,
            "notes": self.notes,
            "glossary_observations": self.glossary_observations,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Translation:
        data = _require_mapping(data, "Translation")
        observations = _require_mapping_value(data, "glossary_observations", "Translation", default={})
        return cls(
            segment_id=_require_string(data, "segment_id", "Translation"),
            text=_require_string(data, "text", "Translation"),
            notes=_require_optional_string(data, "notes", "Translation"),
            glossary_observations={
                _require_string_value(key, f"Translation.glossary_observations key"): _require_string_value(
                    value, f"Translation.glossary_observations[{key!r}]"
                )
                for key, value in observations.items()
            },
        )


@dataclass(frozen=True, slots=True)
class RunPaths:
    """The directories allocated to one translation run."""

    run_id: str
    work_dir: Path
    output_dir: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "work_dir": str(self.work_dir),
            "output_dir": str(self.output_dir),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunPaths:
        data = _require_mapping(data, "RunPaths")
        return cls(
            run_id=_require_string(data, "run_id", "RunPaths"),
            work_dir=Path(_require_string(data, "work_dir", "RunPaths")),
            output_dir=Path(_require_string(data, "output_dir", "RunPaths")),
        )


@dataclass(frozen=True, slots=True)
class Finding:
    """One deterministic QA observation."""

    code: str
    severity: Literal["required", "warning"]
    message: str
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SemanticReviewFinding:
    """One typed master-review verdict with written semantic evidence."""

    dimension: Literal[
        "semantic_fidelity",
        "qualification_preservation",
        "naturalness",
        "terminology",
        "boundary_consistency",
        "protected_content",
    ]
    verdict: Literal["pass", "required-fix"]
    evidence: str


@dataclass(frozen=True, slots=True)
class MasterReview:
    """Semantic review evidence supplied by the master agent."""

    unresolved_required: list[str]
    retries: dict[str, int]
    section_findings: dict[str, list[str]]
    semantic_findings: dict[str, list[SemanticReviewFinding]] = field(
        default_factory=dict
    )


@dataclass(frozen=True, slots=True)
class QAInputs:
    """All deterministic evidence consumed by the QA gate."""

    source_html: Path
    output_html: Path
    source_url: str
    source_segment_ids: set[str]
    translated_segment_ids: set[str]
    critical_assets: list[Path]
    optional_assets: list[Path]
    screenshot_dir: Path
    master_review: MasterReview
    protected_tokens: dict[str, list[ProtectedToken]] = field(default_factory=dict)
    translated_texts: dict[str, str] = field(default_factory=dict)
    capture_metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QAResult:
    """The complete automated acceptance result and its evidence."""

    passed: bool
    required_findings: list[Finding]
    warnings: list[Finding]
    screenshots: list[Path]
    source_url: str = ""
    browser_metrics: dict[str, dict[str, object]] = field(default_factory=dict)
    capture_metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ManifestAsset:
    """One captured asset with source provenance and integrity evidence."""

    source: str
    local_path: str
    sha256: str
    classification: Literal["critical", "optional"]

    def to_dict(self) -> dict[str, str]:
        return {
            "classification": self.classification,
            "local_path": self.local_path,
            "sha256": self.sha256,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ManifestProvenance:
    """Typed, cross-validated provenance for one completed translation run."""

    captured_at: str
    requested_url: str
    final_url: str
    source_language: str
    target_language: str
    terminology_policy_id: str
    terminology_policy_version: str
    tool_version: str
    segment_count: int
    target_segment_count: int
    translated_segment_count: int
    zone_count: int
    assets: list[ManifestAsset]
    missing_optional_assets: list[str]
    retries: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "assets": {
                "captured": [asset.to_dict() for asset in self.assets],
                "missing_optional": list(self.missing_optional_assets),
            },
            "capture": {
                "captured_at": self.captured_at,
                "final_url": self.final_url,
                "requested_url": self.requested_url,
            },
            "coverage": {
                "segments": self.segment_count,
                "target_segments": self.target_segment_count,
                "translated_segments": self.translated_segment_count,
                "zones": self.zone_count,
            },
            "languages": {
                "source": self.source_language,
                "target": self.target_language,
            },
            "retries": dict(self.retries),
            "schema_version": "1.0",
            "terminology_policy": {
                "id": self.terminology_policy_id,
                "version": self.terminology_policy_version,
            },
            "tool": {"name": "web-translator", "version": self.tool_version},
        }


def write_segments(path: Path, segments: Iterable[Segment]) -> None:
    """Write segments as UTF-8 JSON Lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for segment in segments:
            stream.write(json.dumps(segment.to_dict(), ensure_ascii=False))
            stream.write("\n")


def read_segments(path: Path) -> list[Segment]:
    """Read UTF-8 JSON Lines segments written by :func:`write_segments`."""
    with path.open(encoding="utf-8") as stream:
        return read_segments_stream(stream)


def read_segments_stream(stream: TextIO) -> list[Segment]:
    """Read strict UTF-8-decoded JSON Lines from an already-open stream."""
    segments: list[Segment] = []
    for line_number, line in enumerate(stream, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise SegmentContractError(
                f"segments JSONL line {line_number}: invalid JSON"
            ) from error
        try:
            segments.append(Segment.from_dict(record))
        except ValueError as error:
            raise SegmentContractError(
                f"segments JSONL line {line_number}: {error}"
            ) from error
    return segments


def _protected_token_from_dict(data: object, context: str) -> ProtectedToken:
    data = _require_mapping(data, context)
    return ProtectedToken(
        token=_require_string(data, "token", context),
        kind=_require_string(data, "kind", context),
        value=_require_string(data, "value", context),
    )


def _require_mapping(data: object, context: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{context} record must be an object")
    return data


def _require_value(data: Mapping[str, Any], field: str, context: str) -> Any:
    if field not in data:
        raise ValueError(f"{context}.{field} is required")
    return data[field]


def _require_string(data: Mapping[str, Any], field: str, context: str) -> str:
    return _require_string_value(_require_value(data, field, context), f"{context}.{field}")


def _require_string_value(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{context} must be a string")
    return value


def _require_optional_string(data: Mapping[str, Any], field: str, context: str) -> str | None:
    value = data.get(field)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{context}.{field} must be a string or null")
    return value


def _require_bool(data: Mapping[str, Any], field: str, context: str) -> bool:
    value = _require_value(data, field, context)
    if type(value) is not bool:
        raise ValueError(f"{context}.{field} must be a boolean")
    return value


def _require_list(data: Mapping[str, Any], field: str, context: str) -> list[Any]:
    value = _require_value(data, field, context)
    if not isinstance(value, list):
        raise ValueError(f"{context}.{field} must be an array")
    return value


def _require_string_list(data: Mapping[str, Any], field: str, context: str) -> list[str]:
    values = _require_list(data, field, context)
    return [
        _require_string_value(value, f"{context}.{field}[{index}]")
        for index, value in enumerate(values)
    ]


def _require_mapping_value(
    data: Mapping[str, Any], field: str, context: str, *, default: Mapping[str, Any]
) -> Mapping[str, Any]:
    value = data.get(field, default)
    if not isinstance(value, Mapping):
        raise ValueError(f"{context}.{field} must be an object")
    return value
