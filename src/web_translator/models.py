"""Immutable data contracts and JSONL serialization for translation runs."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
        return cls(token=data["token"], kind=data["kind"], value=data["value"])


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
        return cls(
            id=data["id"],
            locator=data["locator"],
            semantic_type=data["semantic_type"],
            heading_path=list(data["heading_path"]),
            source_text=data["source_text"],
            protected=[ProtectedToken.from_dict(token) for token in data["protected"]],
            context_ids=list(data["context_ids"]),
            target=data["target"],
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
        return cls(
            segment_id=data["segment_id"],
            text=data["text"],
            notes=data.get("notes"),
            glossary_observations=dict(data.get("glossary_observations", {})),
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
        return cls(
            run_id=data["run_id"],
            work_dir=Path(data["work_dir"]),
            output_dir=Path(data["output_dir"]),
        )


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
        return [
            Segment.from_dict(json.loads(line))
            for line in stream
            if line.strip()
        ]
