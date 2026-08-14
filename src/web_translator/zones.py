"""Section-aware partitioning for independent translation work."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from web_translator.models import Segment


class ZoneContractError(ValueError):
    """Segments or zone parameters violate the planning contract."""


MAX_TARGET_ZONES = 64


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
    segments: Sequence[Segment],
    max_chars: int = 12_000,
    target_zones: int | None = None,
) -> list[Zone]:
    """Return source-ordered assignments whose targets and context fit the bound.

    Complete sections stay together when they fit. Oversized sections split only
    between indivisible segments or table rows; an indivisible item that cannot
    fit fails closed instead of silently defeating ``max_chars``.
    """
    if type(max_chars) is not int or max_chars <= 0:
        raise ZoneContractError("max_chars must be a positive integer")
    if target_zones is not None and (
        type(target_zones) is not int
        or target_zones <= 0
        or target_zones > MAX_TARGET_ZONES
    ):
        raise ZoneContractError(
            f"target_zones must be an integer from 1 through {MAX_TARGET_ZONES}"
        )

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

    pack_items: list[list[int]] = []
    atomic_items: list[list[int]] = []
    section_boundaries: set[int] = set()
    for section in sections:
        units = _atomic_units(section, ordered)
        for unit in units:
            unit_chars = _indices_chars(unit, ordered)
            if unit_chars > max_chars:
                _raise_oversized_unit(unit, ordered)
            atomic_items.append(unit)
        section_boundaries.add(len(atomic_items))
        section_chars = _indices_chars(section, ordered)
        if section_chars <= max_chars:
            pack_items.append(section)
            continue
        for unit in units:
            pack_items.append(unit)

    if target_zones is None:
        packed_sections = _greedy_pack(pack_items, ordered, max_chars)
    else:
        greedy_atomic = _greedy_pack(atomic_items, ordered, max_chars)
        minimum_zones = len(greedy_atomic)
        if minimum_zones > target_zones:
            packed_sections = greedy_atomic
        else:
            desired_zones = min(len(atomic_items), target_zones)
            packed_sections = _balanced_pack(
                atomic_items,
                ordered,
                desired_zones,
                max_chars,
                section_boundaries,
            )

    zones: list[Zone] = []
    for zone_number, indices in enumerate(packed_sections, start=1):
        targets = [ordered[index] for index in indices]
        context_before, context_after = _bounded_context(
            ordered, indices, max_chars - _indices_chars(indices, ordered)
        )
        zones.append(
            Zone(
                id=f"zone-{zone_number:03d}",
                heading_path=list(targets[0].heading_path),
                target_ids=[item.id for item in targets],
                context_before_ids=[ordered[index].id for index in context_before],
                context_after_ids=[ordered[index].id for index in context_after],
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


def _raise_oversized_unit(unit: Sequence[int], ordered: Sequence[Segment]) -> None:
    identifiers = ", ".join(ordered[index].id for index in unit)
    row_key = _row_key(ordered[unit[0]].semantic_type)
    if row_key is not None:
        raise ZoneContractError(
            f"indivisible table row {row_key} exceeds max_chars: {identifiers}"
        )
    raise ZoneContractError(f"indivisible segment {identifiers} exceeds max_chars")


def _greedy_pack(
    items: Sequence[Sequence[int]],
    ordered: Sequence[Segment],
    max_chars: int,
) -> list[list[int]]:
    packed: list[list[int]] = []
    current_indices: list[int] = []
    current_chars = 0
    for item in items:
        item_chars = _indices_chars(item, ordered)
        if current_indices and current_chars + item_chars > max_chars:
            packed.append(current_indices)
            current_indices = []
            current_chars = 0
        current_indices.extend(item)
        current_chars += item_chars
    if current_indices:
        packed.append(current_indices)
    return packed


def _balanced_pack(
    items: Sequence[Sequence[int]],
    ordered: Sequence[Segment],
    zone_count: int,
    max_chars: int,
    section_boundaries: set[int],
) -> list[list[int]]:
    """Partition ordered atomic items while minimizing the slowest zone."""
    if not items:
        return []
    weights = [_indices_chars(item, ordered) for item in items]
    prefix = [0]
    for weight in weights:
        prefix.append(prefix[-1] + weight)
    total = prefix[-1]
    capacity = _minimum_balanced_capacity(weights, zone_count, max_chars)
    minimum_from = _minimum_zones_from(prefix, capacity)

    ranges: list[tuple[int, int]] = []
    start = 0
    remaining_total = total
    for zones_left in range(zone_count, 1, -1):
        maximum_end = len(items) - (zones_left - 1)
        best_end: int | None = None
        best_objective: tuple[int, int] | None = None
        for end in range(start + 1, maximum_end + 1):
            weight = prefix[end] - prefix[start]
            if weight > capacity:
                break
            if minimum_from[end] > zones_left - 1:
                continue
            objective = (
                abs(weight * zones_left - remaining_total),
                int(end not in section_boundaries),
            )
            if best_objective is None or objective < best_objective:
                best_objective = objective
                best_end = end
        if best_end is None:
            raise ZoneContractError(
                "cannot satisfy target_zones without exceeding max_chars"
            )
        ranges.append((start, best_end))
        remaining_total -= prefix[best_end] - prefix[start]
        start = best_end
    ranges.append((start, len(items)))
    return [
        [index for item in items[start:end] for index in item]
        for start, end in ranges
    ]


def _minimum_balanced_capacity(
    weights: Sequence[int], zone_count: int, max_chars: int
) -> int:
    total = sum(weights)
    lower = max(max(weights), (total + zone_count - 1) // zone_count)
    upper = max_chars
    while lower < upper:
        candidate = (lower + upper) // 2
        if _required_zone_count(weights, candidate) <= zone_count:
            upper = candidate
        else:
            lower = candidate + 1
    return lower


def _required_zone_count(weights: Sequence[int], capacity: int) -> int:
    count = 0
    current = 0
    has_items = False
    for weight in weights:
        if has_items and current + weight > capacity:
            count += 1
            current = 0
            has_items = False
        current += weight
        has_items = True
    return count + int(has_items)


def _minimum_zones_from(prefix: Sequence[int], capacity: int) -> list[int]:
    result = [0] * len(prefix)
    for start in range(len(prefix) - 2, -1, -1):
        end = bisect_right(prefix, prefix[start] + capacity, lo=start + 1) - 1
        if end <= start:
            raise ZoneContractError("an atomic zone item exceeds max_chars")
        result[start] = 1 + result[end]
    return result


def _indices_chars(indices: Sequence[int], ordered: Sequence[Segment]) -> int:
    return sum(len(ordered[index].source_text) for index in indices)


def _row_key(semantic_type: str) -> str | None:
    marker = ":row:"
    if marker not in semantic_type:
        return None
    base, row = semantic_type.rsplit(marker, 1)
    if base not in {"table_cell", "table_header"} or not row:
        return None
    return row


def _atomic_units(section: list[int], ordered: Sequence[Segment]) -> list[list[int]]:
    row_positions: dict[str, list[int]] = {}
    for position, index in enumerate(section):
        row_key = _row_key(ordered[index].semantic_type)
        if row_key is not None:
            row_positions.setdefault(row_key, []).append(position)
    interval_ends = {
        positions[0]: positions[-1] for positions in row_positions.values()
    }

    units: list[list[int]] = []
    position = 0
    while position < len(section):
        end = interval_ends.get(position, position)
        cursor = position
        while cursor <= end:
            end = max(end, interval_ends.get(cursor, cursor))
            cursor += 1
        units.append(section[position : end + 1])
        position = end + 1
    return units


def _bounded_context(
    ordered: Sequence[Segment], target_indices: Sequence[int], budget: int
) -> tuple[list[int], list[int]]:
    if budget <= 0:
        return [], []
    first_index = target_indices[0]
    last_index = target_indices[-1]
    before_candidates = list(range(max(0, first_index - 2), first_index))[::-1]
    after_candidates = list(
        range(last_index + 1, min(len(ordered), last_index + 3))
    )
    selected_before: list[int] = []
    selected_after: list[int] = []
    for offset in range(2):
        for candidates, selected in (
            (before_candidates, selected_before),
            (after_candidates, selected_after),
        ):
            if offset >= len(candidates):
                continue
            index = candidates[offset]
            size = len(ordered[index].source_text)
            if size <= budget:
                selected.append(index)
                budget -= size
    return sorted(selected_before), selected_after
