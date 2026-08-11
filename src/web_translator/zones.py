"""Section-aware partitioning for independent translation work."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from web_translator.models import Segment


class ZoneContractError(ValueError):
    """Segments or zone parameters violate the planning contract."""


@dataclass(frozen=True, slots=True)
class Zone:
    """One immutable translation assignment plus read-only neighbor context."""

    id: str
    heading_path: list[str]
    target_ids: list[str]
    context_before_ids: list[str]
    context_after_ids: list[str]
    attempt: int = 0
    expected_tokens: Mapping[str, tuple[str, ...]] = field(
        kw_only=True, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Detach token expectations from caller-owned mutable containers."""
        if not isinstance(self.expected_tokens, Mapping):
            raise ZoneContractError("Zone.expected_tokens must be a mapping")
        normalized: dict[str, tuple[str, ...]] = {}
        for segment_id, tokens in self.expected_tokens.items():
            if not isinstance(segment_id, str):
                raise ZoneContractError("Zone.expected_tokens keys must be strings")
            if not isinstance(tokens, (list, tuple)) or any(
                not isinstance(token, str) for token in tokens
            ):
                raise ZoneContractError(
                    "Zone.expected_tokens values must be string sequences"
                )
            normalized[segment_id] = tuple(tokens)
        if set(normalized) != set(self.target_ids):
            raise ZoneContractError(
                "Zone.expected_tokens must exactly cover the target IDs"
            )
        object.__setattr__(self, "expected_tokens", MappingProxyType(normalized))


def build_zones(
    segments: Sequence[Segment], max_chars: int = 12_000
) -> list[Zone]:
    """Return complete heading sections as exact, source-ordered assignments.

    ``max_chars`` is a soft limit. A section is never split merely to satisfy
    it, because a section may contain an indivisible table or prose block.
    """
    if type(max_chars) is not int or max_chars <= 0:
        raise ZoneContractError("max_chars must be a positive integer")

    ordered = list(segments)
    ids = [item.id for item in ordered]
    duplicates = sorted(
        segment_id for segment_id, count in Counter(ids).items() if count > 1
    )
    if duplicates:
        raise ZoneContractError(f"duplicate segment IDs: {', '.join(duplicates)}")
    if any(not isinstance(item.id, str) for item in ordered):
        raise ZoneContractError("segment IDs must be strings")
    if any(not isinstance(item.source_text, str) for item in ordered):
        raise ZoneContractError("segment source_text must be a string")

    sections: list[list[int]] = []
    current: list[int] = []
    for index, item in enumerate(ordered):
        if item.semantic_type == "heading" and current:
            sections.append(current)
            current = []
        if item.target:
            current.append(index)
    if current:
        sections.append(current)

    packed_sections: list[list[int]] = []
    current_indices: list[int] = []
    current_chars = 0
    for section in sections:
        section_chars = sum(len(ordered[index].source_text) for index in section)
        if section_chars > max_chars:
            if current_indices:
                packed_sections.append(current_indices)
                current_indices = []
                current_chars = 0
            packed_sections.append(section)
            continue
        if current_indices and current_chars + section_chars > max_chars:
            packed_sections.append(current_indices)
            current_indices = []
            current_chars = 0
        current_indices.extend(section)
        current_chars += section_chars
    if current_indices:
        packed_sections.append(current_indices)

    zones: list[Zone] = []
    for zone_number, indices in enumerate(packed_sections, start=1):
        first_index = indices[0]
        last_index = indices[-1]
        targets = [ordered[index] for index in indices]
        zones.append(
            Zone(
                id=f"zone-{zone_number:03d}",
                heading_path=list(targets[0].heading_path),
                target_ids=[item.id for item in targets],
                context_before_ids=[
                    item.id for item in ordered[max(0, first_index - 2) : first_index]
                ],
                context_after_ids=[
                    item.id
                    for item in ordered[last_index + 1 : last_index + 3]
                ],
                expected_tokens={
                    item.id: tuple(token.token for token in item.protected)
                    for item in targets
                },
            )
        )

    assigned = [segment_id for zone in zones for segment_id in zone.target_ids]
    expected = [item.id for item in ordered if item.target]
    if assigned != expected or len(assigned) != len(set(assigned)):
        raise ZoneContractError("zone target IDs do not form an exact partition")
    return zones
