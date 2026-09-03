"""Deterministic pure helpers for PDF word, line, and text-block layout."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import math
from numbers import Real
from pathlib import Path
import re

import pdfplumber
from pypdf import PdfReader

from web_translator.pdf_models import (
    PdfBlock,
    PdfBlockKind,
    PdfBlockStyle,
    PdfLinkEvidence,
    PdfTableCell,
    font_size_bucket,
)


_VERTICAL_OVERLAP = 0.60
_MINIMUM_GUTTER = 18.0
_WORD_GAP_FONT_MULTIPLIER = 1.5
_PARAGRAPH_GAP_FONT_MULTIPLIER = 1.6
_LIST_MARKER_PATTERN = re.compile(
    r"^\s*(?P<marker>[\u2022\u2023\u25e6\u2043\u2219*+-]|"
    r"\d+(?:\.\d+)*[.)]|[A-Za-z][.)]|[ivxlcdmIVXLCDM]+[.)])"
    r"\s+(?P<body>.*)\Z"
)
_NUMBERED_LIST_PATTERN = re.compile(
    r"^(?P<number>\d+(?:\.\d+)*)(?P<marker>[.)])\s+"
)
_HEADING_NUMBER_PATTERN = re.compile(r"^(?P<number>\d+(?:\.\d+)*)[.)]?\s+")
_PAGE_NUMBER_PATTERN = re.compile(r"\d+\Z")
_RUNNING_PAGE_TOKEN_PATTERN = re.compile(r"(?:\d+|[ivxlcdm]+)\Z", re.IGNORECASE)
_TOC_DOT_LEADER_PATTERN = re.compile(
    r"^\s*\d+(?:\.\d+)*[.)]?\s+\S.*(?:\.\s*){3,}\d+\s*\Z"
)
_TEXT_BLOCK_KINDS = {
    "heading",
    "paragraph",
    "list-item",
    "caption",
    "footnote",
    "header",
    "footer",
    "page-number",
}


class PdfExtractionError(RuntimeError):
    """A PDF cannot safely enter the translation extraction workflow."""


class PdfExtractionWarning(UserWarning):
    """Noncritical PDF evidence could not be mapped unambiguously."""


@dataclass(frozen=True, slots=True)
class TableDetectionResult:
    blocks: tuple[PdfBlock, ...]
    cells: tuple[PdfTableCell, ...]
    bboxes: tuple[tuple[float, float, float, float], ...]
    owned_character_count: int


@dataclass(frozen=True, slots=True)
class LinkEvidenceResult:
    blocks: tuple[PdfBlock, ...]
    links: tuple[PdfLinkEvidence, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PdfWord:
    """One validated word and its source character evidence."""

    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    fontname: str
    size: float
    character_count: int

    @classmethod
    def from_pdfplumber(cls, word: Mapping[str, object]) -> PdfWord:
        """Build a strict word from ``pdfplumber.extract_words`` output."""
        text = word.get("text")
        fontname = word.get("fontname")
        if not isinstance(text, str) or not text.strip():
            raise PdfExtractionError("PDF word text must be a nonempty string")
        if not isinstance(fontname, str) or not fontname:
            raise PdfExtractionError("PDF word fontname must be a nonempty string")
        x0 = _finite_number(word.get("x0"), "word x0")
        top = _finite_number(word.get("top"), "word top")
        x1 = _finite_number(word.get("x1"), "word x1")
        bottom = _finite_number(word.get("bottom"), "word bottom")
        size = _finite_number(word.get("size"), "word size")
        if x1 <= x0 or bottom <= top or size <= 0:
            raise PdfExtractionError("PDF word has invalid geometry or font size")
        chars = word.get("chars")
        if not isinstance(chars, list):
            raise PdfExtractionError("PDF word is missing source character evidence")
        character_count = 0
        for char in chars:
            if not isinstance(char, Mapping) or not isinstance(char.get("text"), str):
                raise PdfExtractionError("PDF word has invalid source character evidence")
            character_count += sum(
                1 for value in str(char["text"]) if not value.isspace()
            )
        if character_count == 0:
            character_count = sum(1 for value in text if not value.isspace())
        return cls(
            text=text,
            x0=x0,
            top=top,
            x1=x1,
            bottom=bottom,
            fontname=fontname,
            size=size,
            character_count=character_count,
        )

    @property
    def bold(self) -> bool:
        return any(
            marker in self.fontname.casefold()
            for marker in ("bold", "black", "heavy")
        )


@dataclass(frozen=True, slots=True)
class PdfLine:
    """An immutable source line retaining all word-assignment evidence."""

    words: tuple[PdfWord, ...]
    kind: PdfBlockKind | None = None
    heading_level: int | None = None
    page_width: float | None = None
    page_height: float | None = None
    spanning: bool = False

    @classmethod
    def from_word(cls, word: PdfWord) -> PdfLine:
        return cls(words=(word,))

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    @property
    def x0(self) -> float:
        return min(word.x0 for word in self.words)

    @property
    def top(self) -> float:
        return min(word.top for word in self.words)

    @property
    def x1(self) -> float:
        return max(word.x1 for word in self.words)

    @property
    def bottom(self) -> float:
        return max(word.bottom for word in self.words)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def is_spanning(self) -> bool:
        return self.spanning

    @property
    def size(self) -> float:
        weighted = sum(word.size * word.character_count for word in self.words)
        characters = sum(word.character_count for word in self.words)
        return weighted / max(1, characters)

    @property
    def bold(self) -> bool:
        bold_characters = sum(
            word.character_count for word in self.words if word.bold
        )
        return bold_characters >= max(1, math.ceil(self.character_count * 0.5))

    @property
    def character_count(self) -> int:
        return sum(word.character_count for word in self.words)

    @property
    def is_heading(self) -> bool:
        if self.kind is not None:
            return self.kind == "heading"
        return self.bold and split_list_marker(self.text) is None

    def vertical_overlap_ratio(self, other: PdfWord | PdfLine) -> float:
        overlap = max(0.0, min(self.bottom, other.bottom) - max(self.top, other.top))
        denominator = min(self.height, other.bottom - other.top)
        return 0.0 if denominator <= 0 else overlap / denominator

    def horizontal_gap(self, word: PdfWord) -> float:
        if word.x0 >= self.x1:
            return word.x0 - self.x1
        if self.x0 >= word.x1:
            return self.x0 - word.x1
        return 0.0

    def accepts(self, word: PdfWord) -> bool:
        monospaced = all(
            "mono" in candidate.fontname.casefold()
            for candidate in (*self.words, word)
        )
        dynamic_gap = (
            _MINIMUM_GUTTER - 1e-6
            if monospaced
            else min(
                _MINIMUM_GUTTER - 1e-6,
                max(self.size, word.size) * _WORD_GAP_FONT_MULTIPLIER,
            )
        )
        return self.horizontal_gap(word) <= dynamic_gap

    def with_word(self, word: PdfWord) -> PdfLine:
        return replace(self, words=(*self.words, word))

    def normalized(self) -> PdfLine:
        return replace(
            self,
            words=tuple(sorted(self.words, key=lambda item: (item.x0, item.top, item.text))),
        )

    def crosses(self, gutter: tuple[float, float]) -> bool:
        return self.x0 < gutter[0] and self.x1 > gutter[1]

    def with_page_geometry(self, width: float, height: float) -> PdfLine:
        return replace(self, page_width=width, page_height=height)


def group_words_into_lines(words: Sequence[Mapping[str, object]]) -> list[PdfLine]:
    """Assign every selectable word to one deterministic line."""
    candidates = [PdfWord.from_pdfplumber(word) for word in words]
    lines: list[PdfLine] = []
    for word in sorted(candidates, key=lambda item: (item.top, item.x0, item.text)):
        matching = [
            (index, line.vertical_overlap_ratio(word), -line.horizontal_gap(word))
            for index, line in enumerate(lines)
            if line.vertical_overlap_ratio(word) >= _VERTICAL_OVERLAP
            and line.accepts(word)
        ]
        if not matching:
            lines.append(PdfLine.from_word(word))
            continue
        index = max(matching, key=lambda item: (item[1], item[2], -item[0]))[0]
        lines[index] = lines[index].with_word(word)
    normalized = [
        line.normalized()
        for line in sorted(lines, key=lambda item: (item.top, item.x0, item.text))
    ]
    return _coalesce_line_fragments(normalized)


def _coalesce_line_fragments(lines: Sequence[PdfLine]) -> list[PdfLine]:
    result = list(lines)
    changed = True
    while changed:
        changed = False
        for left_index, left in enumerate(result):
            for right_index in range(left_index + 1, len(result)):
                right = result[right_index]
                if left.vertical_overlap_ratio(right) < _VERTICAL_OVERLAP:
                    continue
                if not any(left.accepts(word) for word in right.words):
                    continue
                merged = left
                for word in right.words:
                    merged = merged.with_word(word)
                result[left_index] = merged.normalized()
                result.pop(right_index)
                changed = True
                break
            if changed:
                break
    return sorted(result, key=lambda item: (item.top, item.x0, item.text))


def find_clear_gutter(
    lines: Sequence[PdfLine],
    page_width: float,
    minimum_width: float = _MINIMUM_GUTTER,
) -> tuple[float, float] | None:
    """Return the single evidenced two-column gutter, or fail if ambiguous."""
    if not math.isfinite(page_width) or page_width <= 0:
        raise PdfExtractionError("page width must be finite and positive")
    if not math.isfinite(minimum_width) or minimum_width <= 0:
        raise PdfExtractionError("minimum gutter width must be finite and positive")
    candidates = [
        line
        for line in lines
        if not line.is_heading
        and line.kind not in {"caption", "header", "footer", "page-number"}
    ]
    aligned_with = {
        line: {
            peer
            for peer in candidates
            if peer is not line
            and line.vertical_overlap_ratio(peer) >= _VERTICAL_OVERLAP
        }
        for line in candidates
    }
    evidence: list[tuple[tuple[float, float], bool]] = []
    boundary_pairs = sorted(
        {
            (left_boundary.x1, right_boundary.x0)
            for left_boundary in candidates
            for right_boundary in candidates
            if right_boundary.x0 - left_boundary.x1 >= minimum_width
        }
    )
    for left_edge, right_edge in boundary_pairs:
        left = [line for line in candidates if line.x1 <= left_edge]
        right = [line for line in candidates if line.x0 >= right_edge]
        if not left or not right:
            continue
        gutter = (max(line.x1 for line in left), min(line.x0 for line in right))
        if gutter[1] - gutter[0] < minimum_width:
            continue
        if not all(
            line.x1 <= gutter[0]
            or line.x0 >= gutter[1]
            or line.crosses(gutter)
            for line in candidates
        ):
            continue
        left_set = set(left)
        right_set = set(right)
        has_aligned_rows = (
            sum(bool(aligned_with[line] & right_set) for line in left) >= 2
            and sum(bool(aligned_with[line] & left_set) for line in right) >= 2
        )
        matching = next(
            (
                index
                for index, (existing, _) in enumerate(evidence)
                if math.isclose(gutter[0], existing[0], abs_tol=0.01)
                and math.isclose(gutter[1], existing[1], abs_tol=0.01)
            ),
            None,
        )
        if matching is None:
            evidence.append((gutter, has_aligned_rows))
        elif has_aligned_rows and not evidence[matching][1]:
            evidence[matching] = (evidence[matching][0], True)
    if not evidence:
        return None
    if len(evidence) > 1:
        raise PdfExtractionError("ambiguous column evidence")
    gutter, has_aligned_rows = evidence[0]
    return gutter if has_aligned_rows else None


def order_page_lines(
    lines: Sequence[PdfLine],
    page_width: float,
    *,
    spanning_bboxes: Sequence[tuple[float, float, float, float]] = (),
) -> list[PdfLine]:
    """Order a single page top-to-bottom or by an unambiguous two-column gutter."""
    gutter = find_clear_gutter(lines, page_width, minimum_width=_MINIMUM_GUTTER)
    if gutter is None:
        return sorted(lines, key=lambda item: (item.top, item.x0, item.text))
    edge_lines = [
        line
        for line in lines
        if line.kind in {"header", "footer", "page-number"}
        and _edge_band(line) is not None
    ]
    content_lines = [line for line in lines if line not in edge_lines]
    conflicting = [
        line
        for line in content_lines
        if line.crosses(gutter) and not _is_spanning_line(line)
    ]
    if conflicting:
        evidence = ", ".join(repr(line.text) for line in conflicting[:3])
        raise PdfExtractionError(f"conflicting column evidence: {evidence}")
    key = lambda item: (item.top, item.x0, item.text)
    top_edge = sorted(
        (line for line in edge_lines if _edge_band(line) == "top"), key=key
    )
    bottom_edge = sorted(
        (line for line in edge_lines if _edge_band(line) == "bottom"), key=key
    )
    return [
        *top_edge,
        *order_column_regions(
            content_lines,
            gutter,
            spanning_bboxes=spanning_bboxes,
        ),
        *bottom_edge,
    ]


def order_column_regions(
    lines: Sequence[PdfLine],
    gutter: tuple[float, float],
    *,
    spanning_bboxes: Sequence[tuple[float, float, float, float]] = (),
) -> list[PdfLine]:
    """Order each region between spanning content left-column then right-column."""
    spanning = sorted(
        (line for line in lines if line.crosses(gutter) and _is_spanning_line(line)),
        key=lambda item: (item.top, item.x0, item.text),
    )
    ordinary = [line for line in lines if line not in spanning]
    events: list[tuple[float, float, PdfLine | None]] = [
        (line.top, line.bottom, line) for line in spanning
    ]
    events.extend(
        (bbox[1], bbox[3], None)
        for bbox in spanning_bboxes
        if bbox[0] < gutter[0]
        and bbox[2] > gutter[1]
        and bbox[3] > bbox[1]
        and all(math.isfinite(value) for value in bbox)
    )
    result: list[PdfLine] = []
    previous_bottom = -math.inf
    for top, bottom, heading in sorted(
        events,
        key=lambda item: (
            item[0],
            0 if item[2] is not None else 1,
            item[2].x0 if item[2] is not None else 0.0,
        ),
    ):
        region = [
            line
            for line in ordinary
            if line.top >= previous_bottom and line.top < top
        ]
        result.extend(_order_two_columns(region, gutter))
        if heading is not None:
            result.append(heading)
        previous_bottom = max(previous_bottom, bottom)
    result.extend(
        _order_two_columns(
            [line for line in ordinary if line.top >= previous_bottom], gutter
        )
    )
    return result


def _order_two_columns(
    lines: Sequence[PdfLine], gutter: tuple[float, float]
) -> list[PdfLine]:
    left = [line for line in lines if line.center_x <= gutter[0]]
    right = [line for line in lines if line.center_x >= gutter[1]]
    unassigned = [line for line in lines if line not in left and line not in right]
    if unassigned:
        evidence = ", ".join(repr(line.text) for line in unassigned[:3])
        raise PdfExtractionError(f"ambiguous column evidence: {evidence}")
    key = lambda item: (item.top, item.x0, item.text)
    return [*sorted(left, key=key), *sorted(right, key=key)]


def _is_spanning_line(line: PdfLine) -> bool:
    return line.is_heading or line.is_spanning


def classify_document_lines(
    pages: Sequence[tuple[Sequence[PdfLine], float]],
) -> list[list[PdfLine]]:
    """Classify repeated bands, page numbers, headings, and lists document-wide."""
    normalized = [
        [
            line.with_page_geometry(
                line.page_width if line.page_width is not None else max(1.0, line.x1),
                height,
            )
            for line in lines
        ]
        for lines, height in pages
    ]
    normalized = _classify_page_numbers(normalized)
    normalized = _classify_running_bands(normalized)
    normalized = _classify_repeated_bands(normalized)
    normalized = _classify_toc_entries(normalized)

    sizes: Counter[int] = Counter()
    for lines in normalized:
        for line in lines:
            if line.kind is None:
                sizes[font_size_bucket(line.size)] += line.character_count
    body_size = max(sizes, key=lambda size: (sizes[size], -size)) if sizes else 0
    heading_sizes = sorted(
        {size for size in sizes if size >= body_size + 1}, reverse=True
    )

    result: list[list[PdfLine]] = []
    for lines in normalized:
        classified: list[PdfLine] = []
        for index, line in enumerate(lines):
            if line.kind is not None:
                classified.append(line)
                continue
            rounded_size = font_size_bucket(line.size)
            heading_candidate = rounded_size in heading_sizes or line.bold
            list_marker = split_list_marker(line.text)
            number = _HEADING_NUMBER_PATTERN.match(line.text)
            numbered_heading = (
                heading_candidate
                and number is not None
                and _has_heading_spacing(lines, index)
                and not _has_tight_numbered_list_peer(lines, index)
                and not line.is_spanning
            )
            if (
                heading_candidate
                and not line.is_spanning
                and (list_marker is None or numbered_heading)
            ):
                if number is not None:
                    level = number.group("number").count(".") + 1
                elif rounded_size in heading_sizes:
                    level = heading_sizes.index(rounded_size) + 1
                else:
                    level = len(heading_sizes) + 1
                classified.append(replace(line, kind="heading", heading_level=level))
            elif list_marker is not None:
                classified.append(replace(line, kind="list-item"))
            else:
                classified.append(replace(line, kind="paragraph"))
        result.append(classified)
    return result


def _has_heading_spacing(lines: Sequence[PdfLine], index: int) -> bool:
    line = lines[index]
    gaps: list[float] = []
    neighboring_sizes: list[float] = [line.size]
    if index > 0:
        previous = lines[index - 1]
        gaps.append(max(0.0, line.top - previous.bottom))
        neighboring_sizes.append(previous.size)
    if index + 1 < len(lines):
        following = lines[index + 1]
        gaps.append(max(0.0, following.top - line.bottom))
        neighboring_sizes.append(following.size)
    if not gaps:
        return False
    threshold = max(neighboring_sizes) * 0.75
    return any(gap >= threshold for gap in gaps)


def _has_tight_numbered_list_peer(
    lines: Sequence[PdfLine], index: int
) -> bool:
    line = lines[index]
    family = _numbered_list_family(line)
    if family is None:
        return False
    same_band_peers = [
        peer
        for peer_index, peer in enumerate(lines)
        if peer_index != index
        and _numbered_list_family(peer) == family
        and abs(line.x0 - peer.x0) <= max(line.size, peer.size) * 0.5
    ]
    previous = max(
        (peer for peer in same_band_peers if peer.top < line.top),
        key=lambda peer: (peer.bottom, peer.top, peer.text),
        default=None,
    )
    following = min(
        (peer for peer in same_band_peers if peer.top > line.top),
        key=lambda peer: (peer.top, peer.bottom, peer.text),
        default=None,
    )
    for peer in (previous, following):
        if peer is None:
            continue
        if peer.top < line.top:
            gap = max(0.0, line.top - peer.bottom)
        else:
            gap = max(0.0, peer.top - line.bottom)
        if gap < max(line.size, peer.size) * 0.75:
            return True
    return False


def _numbered_list_family(line: PdfLine) -> tuple[int, str] | None:
    marker = _NUMBERED_LIST_PATTERN.match(line.text)
    if marker is None:
        return None
    depth = marker.group("number").count(".") + 1
    return (depth, marker.group("marker"))


def _classify_page_numbers(pages: list[list[PdfLine]]) -> list[list[PdfLine]]:
    groups: dict[tuple[str, int], list[tuple[int, int, int]]] = defaultdict(list)
    for page_index, lines in enumerate(pages):
        for line_index, line in enumerate(lines):
            if line.page_height is None or line.page_width is None:
                continue
            band = _edge_band(line)
            if band is None or _PAGE_NUMBER_PATTERN.fullmatch(line.text.strip()) is None:
                continue
            horizontal_bucket = round((line.center_x / line.page_width) * 20)
            groups[(band, horizontal_bucket)].append(
                (page_index, line_index, int(line.text.strip()))
            )
    page_number_locations: set[tuple[int, int]] = set()
    for entries in groups.values():
        ordered = sorted(entries)
        if len(ordered) < 2 or len({entry[0] for entry in ordered}) != len(ordered):
            continue
        if all(
            next_value - value == next_page - page
            for (page, _, value), (next_page, _, next_value) in zip(
                ordered, ordered[1:]
            )
        ):
            page_number_locations.update((page, line) for page, line, _ in ordered)
    return [
        [
            replace(line, kind="page-number")
            if (page_index, line_index) in page_number_locations
            else line
            for line_index, line in enumerate(lines)
        ]
        for page_index, lines in enumerate(pages)
    ]


def _classify_repeated_bands(pages: list[list[PdfLine]]) -> list[list[PdfLine]]:
    eligible_pages = sum(bool(lines) for lines in pages)
    threshold = max(2, math.ceil(eligible_pages * 0.60))
    occurrences: dict[tuple[str, str], set[int]] = defaultdict(set)
    for page_index, lines in enumerate(pages):
        for line in lines:
            band = _edge_band(line)
            if band is None or line.kind == "page-number":
                continue
            occurrences[(band, _normalized_text(line.text))].add(page_index)
    repeated = {
        key for key, page_numbers in occurrences.items() if len(page_numbers) >= threshold
    }
    return [
        [
            replace(
                line,
                kind=("header" if _edge_band(line) == "top" else "footer"),
            )
            if line.kind is None
            and (_edge_band(line), _normalized_text(line.text)) in repeated
            else line
            for line in lines
        ]
        for lines in pages
    ]


def _classify_running_bands(pages: list[list[PdfLine]]) -> list[list[PdfLine]]:
    occurrences: dict[
        tuple[str, str], list[tuple[int, int, int, str]]
    ] = defaultdict(list)
    for page_index, lines in enumerate(pages):
        for line_index, line in enumerate(lines):
            band = _edge_band(line)
            evidence = _running_band_evidence(line)
            if line.kind is not None or band is None or evidence is None:
                continue
            label, value, side = evidence
            page_width = line.page_width
            if page_width is None or (
                side == "left" and line.center_x >= page_width / 2.0
            ) or (
                side == "right" and line.center_x <= page_width / 2.0
            ):
                continue
            occurrences[(band, label)].append((page_index, line_index, value, side))
    running_band_locations = {
        (page_index, line_index)
        for locations in occurrences.values()
        if _is_sequential_running_band(locations)
        for page_index, line_index, _value, _side in locations
    }
    return [
        [
            replace(
                line,
                kind=("header" if _edge_band(line) == "top" else "footer"),
            )
            if (page_index, line_index) in running_band_locations
            else line
            for line_index, line in enumerate(lines)
        ]
        for page_index, lines in enumerate(pages)
    ]


def _running_band_label(line: PdfLine) -> str | None:
    evidence = _running_band_evidence(line)
    return evidence[0] if evidence is not None else None


def _running_band_evidence(line: PdfLine) -> tuple[str, int, str] | None:
    parts = [part.strip() for part in line.text.split("|")]
    if len(parts) != 2 or any(not part for part in parts):
        return None
    page_tokens = [
        _RUNNING_PAGE_TOKEN_PATTERN.fullmatch(part) is not None for part in parts
    ]
    if sum(page_tokens) != 1:
        return None
    token_index = page_tokens.index(True)
    value = _page_token_value(parts[token_index])
    if value is None:
        return None
    return (
        _normalized_text(parts[1 - token_index]),
        value,
        "left" if token_index == 0 else "right",
    )


def _page_token_value(token: str) -> int | None:
    if token.isdecimal():
        value = int(token)
        return value if value > 0 else None
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    normalized = token.casefold()
    total = 0
    previous = 0
    for character in reversed(normalized):
        value = values.get(character)
        if value is None:
            return None
        total += -value if value < previous else value
        previous = max(previous, value)
    return total if 0 < total <= 3999 and _roman_page_token(total) == normalized else None


def _roman_page_token(value: int) -> str:
    parts: list[str] = []
    for amount, token in (
        (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"),
        (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
        (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
    ):
        while value >= amount:
            parts.append(token)
            value -= amount
    return "".join(parts)


def _is_sequential_running_band(
    locations: Sequence[tuple[int, int, int, str]],
) -> bool:
    ordered = sorted(locations)
    if len({page_index for page_index, *_rest in ordered}) < 2:
        return False
    for left, right in zip(ordered, ordered[1:]):
        page_delta = right[0] - left[0]
        if page_delta <= 0 or right[2] - left[2] != page_delta:
            return False
    sides = [location[3] for location in ordered]
    same_side = len(set(sides)) == 1
    alternating = all(
        (right[3] != left[3]) == ((right[0] - left[0]) % 2 == 1)
        for left, right in zip(ordered, ordered[1:])
    )
    return same_side or alternating


def _classify_toc_entries(pages: list[list[PdfLine]]) -> list[list[PdfLine]]:
    evidence: set[tuple[int, int]] = set()
    for page_index, lines in enumerate(pages):
        matches = [
            (line_index, line)
            for line_index, line in enumerate(lines)
            if line.kind is None and _TOC_DOT_LEADER_PATTERN.fullmatch(line.text)
        ]
        if len(matches) >= 2:
            evidence.update((page_index, index) for index, _line in matches)
    if not evidence:
        return pages
    return [
        [
            replace(line, spanning=True)
            if (page_index, line_index) in evidence
            else line
            for line_index, line in enumerate(lines)
        ]
        for page_index, lines in enumerate(pages)
    ]


def _edge_band(line: PdfLine) -> str | None:
    if line.page_height is None:
        return None
    if line.top <= line.page_height * 0.10:
        return "top"
    if line.bottom >= line.page_height * 0.90:
        return "bottom"
    return None


def _normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def classify_line(line: PdfLine) -> tuple[PdfBlockKind, PdfLine]:
    """Classify one already-normalized line without mutating it."""
    if line.kind is not None:
        return line.kind, line
    if split_list_marker(line.text) is not None:
        return "list-item", line
    if line.is_heading:
        return "heading", line
    return "paragraph", line


def split_list_marker(text: str) -> tuple[str, str] | None:
    """Return one canonical extractor-supported list marker and its body."""
    match = _LIST_MARKER_PATTERN.match(text)
    if match is None:
        return None
    return match.group("marker"), match.group("body")


def merge_contiguous_paragraph_lines(
    classified: Sequence[tuple[PdfBlockKind, PdfLine]],
) -> list[tuple[PdfBlockKind, tuple[PdfLine, ...]]]:
    """Merge only geometrically contiguous paragraph lines."""
    merged: list[tuple[PdfBlockKind, tuple[PdfLine, ...]]] = []
    for index, (kind, line) in enumerate(classified):
        if (
            kind == "paragraph"
            and merged
            and merged[-1][0] == "paragraph"
            and _paragraphs_are_contiguous(merged[-1][1][-1], line)
            and not _has_side_by_side_successor(classified, index)
        ):
            previous_kind, previous_lines = merged[-1]
            merged[-1] = (previous_kind, (*previous_lines, line))
        else:
            merged.append((kind, (line,)))
    return merged


def _has_side_by_side_successor(
    classified: Sequence[tuple[PdfBlockKind, PdfLine]], index: int
) -> bool:
    if index + 1 >= len(classified):
        return False
    line = classified[index][1]
    following = classified[index + 1][1]
    return (
        line.vertical_overlap_ratio(following) >= _VERTICAL_OVERLAP
        and (line.x1 <= following.x0 or following.x1 <= line.x0)
    )


def _paragraphs_are_contiguous(previous: PdfLine, current: PdfLine) -> bool:
    vertical_gap = current.top - previous.bottom
    maximum_gap = max(previous.size, current.size) * _PARAGRAPH_GAP_FONT_MULTIPLIER
    horizontal_overlap = max(
        0.0, min(previous.x1, current.x1) - max(previous.x0, current.x0)
    )
    smaller_width = min(previous.width, current.width)
    aligned = abs(previous.x0 - current.x0) <= max(previous.size, current.size)
    return (
        -1e-9 <= vertical_gap <= maximum_gap
        and (aligned or horizontal_overlap >= smaller_width * 0.50)
    )


def build_text_blocks(lines: Sequence[PdfLine], page_number: int) -> list[PdfBlock]:
    """Build strict page-local text blocks with stable IDs."""
    classified = [classify_line(line) for line in lines]
    merged = merge_contiguous_paragraph_lines(classified)
    blocks: list[PdfBlock] = []
    for index, (kind, block_lines) in enumerate(merged):
        if kind not in _TEXT_BLOCK_KINDS:
            raise PdfExtractionError(f"unsupported text block kind: {kind}")
        next_top = merged[index + 1][1][0].top if index + 1 < len(merged) else None
        bottom = max(line.bottom for line in block_lines)
        space_after = max(0.0, next_top - bottom) if next_top is not None else 0.0
        blocks.append(
            PdfBlock(
                id=f"pdf:page-{page_number:04d}:block-{index + 1:04d}",
                page_number=page_number,
                order=index,
                kind=kind,
                bbox=(
                    min(line.x0 for line in block_lines),
                    min(line.top for line in block_lines),
                    max(line.x1 for line in block_lines),
                    bottom,
                ),
                style=PdfBlockStyle(
                    font_size=max(line.size for line in block_lines),
                    bold=any(line.bold for line in block_lines),
                    alignment=_alignment(block_lines),
                    indentation=min(line.x0 for line in block_lines),
                    space_after=space_after,
                ),
                source_text=" ".join(line.text for line in block_lines),
            )
        )
    return blocks


def _alignment(lines: Sequence[PdfLine]) -> str:
    page_width = next((line.page_width for line in lines if line.page_width), None)
    if page_width is None:
        return "left"
    x0 = min(line.x0 for line in lines)
    x1 = max(line.x1 for line in lines)
    tolerance = max(6.0, max(line.size for line in lines))
    if abs(((x0 + x1) / 2.0) - page_width / 2.0) <= tolerance:
        return "center"
    if page_width - x1 <= page_width * 0.05:
        return "right"
    return "left"


def detect_tables(page: object, *, page_number: int) -> TableDetectionResult:
    """Detect fixed table grids with strict selectable-character ownership."""
    try:
        explicit = list(
            page.find_tables(  # type: ignore[attr-defined]
                {
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                }
            )
        )
        explicit = [table for table in explicit if _usable_table(table, "lines")]
        explicit_bboxes = [
            tuple(float(value) for value in table.bbox) for table in explicit
        ]
        text_page = (
            page.filter(  # type: ignore[attr-defined]
                lambda item: not _mapping_center_inside_any_bbox(
                    item, explicit_bboxes
                )
            )
            if explicit_bboxes
            else page
        )
        text_candidates = [
            table
            for table in text_page.find_tables(  # type: ignore[attr-defined]
                {
                    "vertical_strategy": "text",
                    "horizontal_strategy": "text",
                    "min_words_vertical": 2,
                    "min_words_horizontal": 1,
                }
            )
            if _usable_table(table, "text")
            and not any(
                _bbox_intersection(
                    tuple(float(value) for value in table.bbox),
                    tuple(float(value) for value in ruled.bbox),
                )
                > 0
                for ruled in explicit
            )
        ]
        candidates = [
            (table, "lines") for table in explicit
        ] + [
            (table, "text") for table in text_candidates
        ]
    except PdfExtractionError:
        raise
    except Exception as error:
        raise PdfExtractionError(
            f"cannot detect tables on page {page_number}: {error}"
        ) from error

    ordered = sorted(
        candidates,
        key=lambda item: (
            float(item[0].bbox[1]),
            float(item[0].bbox[0]),
            float(item[0].bbox[3]),
            float(item[0].bbox[2]),
        ),
    )
    _reject_overlapping_tables([table for table, _ in ordered], page_number)
    blocks: list[PdfBlock] = []
    cells: list[PdfTableCell] = []
    owned_characters = 0
    table_bboxes: list[tuple[float, float, float, float]] = []
    for table_index, (table, strategy) in enumerate(ordered, start=1):
        table_id = f"pdf:page-{page_number:04d}:table-{table_index:04d}"
        table_blocks, table_cells, character_count = _convert_table(
            page,
            table,
            strategy=strategy,
            table_id=table_id,
            page_number=page_number,
        )
        blocks.extend(table_blocks)
        cells.extend(table_cells)
        owned_characters += character_count
        table_bboxes.append(tuple(float(value) for value in table.bbox))
    return TableDetectionResult(
        blocks=tuple(blocks),
        cells=tuple(cells),
        bboxes=tuple(table_bboxes),
        owned_character_count=owned_characters,
    )


def detect_footnotes(
    blocks: Sequence[PdfBlock],
    characters: Sequence[Mapping[str, object]],
    *,
    page_height: float,
) -> list[PdfBlock]:
    """Pair clear superscript markers with smaller-font page-edge bodies."""
    if not blocks:
        return []
    body_sizes = sorted(
        block.style.font_size
        for block in blocks
        if block.kind not in {"figure", "table-cell"} and block.source_text.strip()
    )
    if not body_sizes:
        return list(blocks)
    median_size = body_sizes[len(body_sizes) // 2]
    bodies: list[tuple[PdfBlock, str]] = []
    for block in blocks:
        marker = _leading_footnote_marker(block.source_text)
        if (
            marker is not None
            and block.bbox[1] >= page_height * 0.75
            and block.style.font_size <= median_size * 0.85 + 1e-9
        ):
            bodies.append((block, marker))

    replacements: dict[str, PdfBlock] = {}
    removed: set[str] = set()
    claimed_owners: dict[str, str] = {}
    for body, marker in bodies:
        owners = [
            block
            for block in blocks
            if block.id != body.id
            and block.bbox[1] < body.bbox[1]
            and any(
                _normalized_marker(str(character.get("text", ""))) == marker
                and _character_inside_bbox(character, block.bbox)
                and _character_size(character) <= block.style.font_size * 0.85 + 1e-9
                for character in characters
            )
        ]
        trailing_owners = [
            block
            for block in blocks
            if block.id != body.id
            and block.bbox[1] < body.bbox[1]
            and _trailing_marker(block.source_text) == marker
            and _trailing_marker_characters(block, marker, characters) is not None
        ]
        if len(trailing_owners) == 1 and (not owners or len(owners) > 1):
            owners = trailing_owners
        standalone_markers = [
            block
            for block in blocks
            if block.id != body.id
            and block.bbox[1] < body.bbox[1]
            and _normalized_marker(block.source_text) == marker
            and block.style.font_size <= median_size * 0.85 + 1e-9
        ]
        if len(owners) == 1 and len(standalone_markers) == 1:
            owner = owners[0]
            marker_block = standalone_markers[0]
            if (
                _vertical_overlap_ratio(owner.bbox, marker_block.bbox) >= 0.50
                and _horizontal_bbox_gap(owner.bbox, marker_block.bbox)
                <= median_size * 1.5
            ):
                owners = [
                    replace(
                        owner,
                        bbox=(
                            min(owner.bbox[0], marker_block.bbox[0]),
                            min(owner.bbox[1], marker_block.bbox[1]),
                            max(owner.bbox[2], marker_block.bbox[2]),
                            max(owner.bbox[3], marker_block.bbox[3]),
                        ),
                        source_text=(
                            owner.source_text
                            if _trailing_marker(owner.source_text) == marker
                            else f"{owner.source_text.rstrip()} {marker}"
                        ),
                    )
                ]
                removed.add(marker_block.id)
        if not owners and standalone_markers:
            if len(standalone_markers) > 1:
                raise PdfExtractionError(
                    f"ambiguous footnote marker {marker!r} on page {body.page_number}"
                )
            marker_block = standalone_markers[0]
            neighbors = [
                block
                for block in blocks
                if block.id not in {body.id, marker_block.id}
                and block.kind not in {"caption", "figure", "table-cell"}
                and _vertical_overlap_ratio(block.bbox, marker_block.bbox) >= 0.50
                and _horizontal_bbox_gap(block.bbox, marker_block.bbox)
                <= median_size * 1.5
            ]
            if len(neighbors) > 1:
                raise PdfExtractionError(
                    f"ambiguous footnote marker {marker!r} on page {body.page_number}"
                )
            if neighbors:
                owner = neighbors[0]
                owners = [
                    replace(
                        owner,
                        bbox=(
                            min(owner.bbox[0], marker_block.bbox[0]),
                            min(owner.bbox[1], marker_block.bbox[1]),
                            max(owner.bbox[2], marker_block.bbox[2]),
                            max(owner.bbox[3], marker_block.bbox[3]),
                        ),
                        source_text=f"{owner.source_text.rstrip()} {marker}",
                    )
                ]
                removed.add(marker_block.id)
        if len(owners) == 1 and _trailing_marker(owners[0].source_text) == marker:
            owners = [_trim_footnote_marker_extent(owners[0], characters)]
        if len(owners) > 1:
            raise PdfExtractionError(
                f"ambiguous footnote marker {marker!r} on page {body.page_number}"
            )
        if not owners:
            continue
        owner = owners[0]
        if owner.id in claimed_owners and claimed_owners[owner.id] != body.id:
            raise PdfExtractionError(
                f"ambiguous footnote owner {owner.id} on page {body.page_number}"
            )
        current_owner = replacements.get(owner.id, owner)
        if (
            current_owner.destination is not None
            and current_owner.destination != body.id
        ):
            raise PdfExtractionError(
                f"ambiguous footnote destination for block {owner.id}"
            )
        claimed_owners[owner.id] = body.id
        replacements[body.id] = replace(body, kind="footnote")
        replacements[owner.id] = replace(current_owner, destination=body.id)
    return [
        replacements.get(block.id, block)
        for block in blocks
        if block.id not in removed
    ]


def pair_figure_captions(blocks: Sequence[PdfBlock]) -> list[PdfBlock]:
    """Pair figures and explicit captions only when the relation is unique."""
    figures = [block for block in blocks if block.kind == "figure"]
    captions = [
        block
        for block in blocks
        if block.kind in {"paragraph", "caption"}
        and re.match(r"^\s*(?:figure|fig\.)\s*\d+\b", block.source_text, re.I)
    ]
    figure_by_id = {figure.id: figure for figure in figures}
    candidates = {
        caption.id: sorted(
            (
                figure.id
                for figure in figures
                if caption.page_number == figure.page_number
                and _caption_distance(figure.bbox, caption.bbox) <= 36.0
                and _horizontal_overlap_ratio(figure.bbox, caption.bbox) >= 0.25
            ),
            key=lambda figure_id: (
                _caption_distance(
                    figure_by_id[figure_id].bbox,
                    caption.bbox,
                ),
                figure_id,
            ),
        )
        for caption in captions
    }
    orphaned = sorted(
        caption.id for caption in captions if not candidates[caption.id]
    )
    if orphaned:
        raise PdfExtractionError(
            "orphan figure-caption evidence: " + ", ".join(orphaned)
        )
    claimed = _find_caption_matching(candidates)
    if claimed is None or any(
        _find_caption_matching(candidates, forbidden=(caption_id, figure_id))
        is not None
        for caption_id, figure_id in claimed.items()
    ):
        evidence = next(iter(sorted(candidates)), "unknown-caption")
        raise PdfExtractionError(
            f"ambiguous figure-caption pairing for {evidence}"
        )
    replacements: dict[str, PdfBlock] = {}
    by_id = {block.id: block for block in blocks}
    for caption_id, figure_id in claimed.items():
        replacements[figure_id] = replace(by_id[figure_id], caption_id=caption_id)
        replacements[caption_id] = replace(
            by_id[caption_id], kind="caption", caption_id=figure_id
        )
    return [replacements.get(block.id, block) for block in blocks]


def _find_caption_matching(
    candidates: Mapping[str, Sequence[str]],
    *,
    forbidden: tuple[str, str] | None = None,
) -> dict[str, str] | None:
    figure_owners: dict[str, str] = {}

    def assign(caption_id: str, visited: set[str]) -> bool:
        for figure_id in candidates[caption_id]:
            if forbidden == (caption_id, figure_id) or figure_id in visited:
                continue
            visited.add(figure_id)
            owner = figure_owners.get(figure_id)
            if owner is None or assign(owner, visited):
                figure_owners[figure_id] = caption_id
                return True
        return False

    for caption_id in sorted(candidates, key=lambda item: (len(candidates[item]), item)):
        if not assign(caption_id, set()):
            return None
    return {caption_id: figure_id for figure_id, caption_id in figure_owners.items()}


def extract_link_evidence(
    source_pdf: Path,
    blocks: Sequence[PdfBlock],
) -> LinkEvidenceResult:
    """Attach clear URI/internal annotations and return unresolved warnings."""
    try:
        reader = PdfReader(Path(source_pdf), strict=True)
        with pdfplumber.open(Path(source_pdf)) as layout_document:
            visible_characters = [
                tuple(
                    character
                    for character in page.chars
                    if isinstance(character, Mapping)
                    and str(character.get("text", ""))
                )
                for page in layout_document.pages
            ]
    except Exception as error:
        raise PdfExtractionError(f"cannot inspect PDF link annotations: {error}") from error
    if len(visible_characters) != len(reader.pages):
        raise PdfExtractionError("PDF readers disagree on link-evidence page count")
    replacements = {block.id: block for block in blocks}
    links: list[PdfLinkEvidence] = []
    warnings: list[str] = []
    by_page: dict[int, list[PdfBlock]] = defaultdict(list)
    for block in blocks:
        by_page[block.page_number].append(block)

    for page_index, page in enumerate(reader.pages):
        page_number = page_index + 1
        annotations = page.get("/Annots", ())
        for annotation_index, reference in enumerate(annotations, start=1):
            try:
                annotation = reference.get_object()
                if str(annotation.get("/Subtype", "")) != "/Link":
                    continue
                source_bbox = _annotation_bbox(annotation.get("/Rect"), page)
                owners = _blocks_intersecting_bbox(by_page.get(page_number, ()), source_bbox)
            except Exception:
                warnings.append(
                    f"page {page_number} link {annotation_index}: malformed annotation"
                )
                continue
            visible_label = _visible_link_label(
                visible_characters[page_index], source_bbox
            )
            if not visible_label:
                raise PdfExtractionError(
                    f"page {page_number} link {annotation_index}: "
                    "missing visible link text"
                )
            if not owners and visible_label:
                raise PdfExtractionError(
                    f"page {page_number} link {annotation_index}: "
                    "visible link text has no emitted owner"
                )
            if not owners and any(
                (character_bbox := _character_bbox(character)) is not None
                and str(character.get("text", "")).strip()
                and _bbox_intersection(character_bbox, source_bbox) > 0
                for character in visible_characters[page_index]
            ):
                raise PdfExtractionError(
                    f"page {page_number} link {annotation_index}: "
                    "visible link text has no emitted owner"
                )
            uri = _annotation_uri(annotation)
            raw_destination = _annotation_destination(annotation)
            missing_destination = uri is None and raw_destination is None
            target = (
                None
                if uri is not None or missing_destination
                else _map_internal_destination(reader, raw_destination, by_page)
            )
            destination = (
                None
                if uri is not None
                else "unresolved:missing-destination"
                if missing_destination
                else target.id
                if target is not None
                else _display_internal_destination(raw_destination)
            )
            owner = owners[0] if len(owners) == 1 else None
            source_span = (
                _unique_source_span(owner.source_text, visible_label)
                if owner is not None
                else None
            )
            reason: str | None = None
            if owner is None:
                reason = "visible source intersects multiple blocks"
            elif source_span is None:
                reason = "visible label is not unique within source block"
            elif missing_destination:
                reason = "link destination is missing or malformed"
            elif uri is None and target is None:
                reason = "internal destination is unresolved"
            reconstructed = reason is None
            link = PdfLinkEvidence(
                id=f"pdf:page-{page_number:04d}:link-{annotation_index:04d}",
                page_number=page_number,
                source_block_id=owner.id if owner is not None else None,
                source_span=source_span,
                bounds=source_bbox,
                visible_label=visible_label,
                uri=uri,
                destination=destination,
                reconstructed=reconstructed,
                reason=reason,
            )
            links.append(link)
            if reason is not None:
                warnings.append(
                    f"page {page_number} link {annotation_index}: "
                    f"unreconstructed link label={visible_label!r} "
                    f"destination={(uri or destination)!r} reason={reason}"
                )

    links_by_block: dict[str, list[PdfLinkEvidence]] = defaultdict(list)
    for link in links:
        if link.reconstructed and link.source_block_id is not None:
            links_by_block[link.source_block_id].append(link)
    for block_id, block_links in links_by_block.items():
        block = replacements[block_id]
        if len(block_links) != 1 or block.uri is not None or block.destination is not None:
            continue
        link = block_links[0]
        replacements[block_id] = replace(
            block,
            uri=link.uri,
            destination=link.destination,
        )
    return LinkEvidenceResult(
        blocks=tuple(replacements[block.id] for block in blocks),
        links=tuple(links),
        warnings=tuple(sorted(set(warnings))),
    )


def _visible_link_label(
    characters: Sequence[Mapping[str, object]],
    bbox: tuple[float, float, float, float],
) -> str:
    raw = "".join(
        str(character.get("text", ""))
        for character in characters
        if _character_center_inside_bbox(character, bbox)
    )
    return " ".join(raw.split())


def _unique_source_span(text: str, label: str) -> tuple[int, int] | None:
    start = text.find(label)
    if start < 0 or text.find(label, start + 1) >= 0:
        return None
    return (start, start + len(label))


def _display_internal_destination(destination: object) -> str:
    if isinstance(destination, str) and destination:
        return destination
    if isinstance(destination, Sequence) and not isinstance(destination, (str, bytes)):
        rendered = ", ".join(str(item) for item in destination[1:])
        return f"internal:[{rendered}]"
    rendered = str(destination)
    return rendered if rendered else "internal:unresolved"


def _usable_table(table: object, strategy: str) -> bool:
    rows = list(getattr(table, "rows", ()))
    if len(rows) < 2:
        return False
    columns = max((len(getattr(row, "cells", ())) for row in rows), default=0)
    if columns < 2:
        return False
    extracted = table.extract()
    occupied = [
        row
        for row in extracted
        if any(isinstance(value, str) and value.strip() for value in row)
    ]
    if strategy == "text":
        if len(occupied) < 2 or any(len(row) != columns for row in occupied):
            return False
        present = [
            [isinstance(value, str) and bool(value.strip()) for value in row]
            for row in occupied
        ]
        if any(sum(row) < 2 for row in present):
            return False
        if any(sum(row[column] for row in present) < 2 for column in range(columns)):
            return False
        occupied_indexes = [
            index
            for index, row in enumerate(extracted)
            if any(isinstance(value, str) and value.strip() for value in row)
        ]
        cells = [
            tuple(float(value) for value in cell)
            for index in occupied_indexes
            for cell in rows[index].cells
            if cell is not None
        ]
        table_bbox = tuple(float(value) for value in table.bbox)
        for character in getattr(table.page, "chars", ()):
            if not str(character.get("text", "")).strip() or not (
                _character_center_inside_bbox(character, table_bbox)
            ):
                continue
            if sum(_character_fully_inside_bbox(character, cell) for cell in cells) != 1:
                return False
    return True


def _reject_overlapping_tables(tables: Sequence[object], page_number: int) -> None:
    for index, left in enumerate(tables):
        left_bbox = tuple(float(value) for value in left.bbox)
        for right in tables[index + 1 :]:
            right_bbox = tuple(float(value) for value in right.bbox)
            if _bbox_intersection(left_bbox, right_bbox) > 0:
                raise PdfExtractionError(
                    f"ambiguous overlapping tables on page {page_number}"
                )


def _convert_table(
    page: object,
    table: object,
    *,
    strategy: str,
    table_id: str,
    page_number: int,
) -> tuple[list[PdfBlock], list[PdfTableCell], int]:
    table_bbox = tuple(float(value) for value in table.bbox)
    rows = list(table.rows)
    extracted = list(table.extract())
    if strategy == "text":
        selected_rows = [
            (raw_index, row)
            for raw_index, row in enumerate(rows)
            if any(
                isinstance(value, str) and value.strip()
                for value in extracted[raw_index]
            )
        ]
    else:
        selected_rows = list(enumerate(rows))
    actual_cells = [
        tuple(float(value) for value in cell)
        for _, row in selected_rows
        for cell in row.cells
        if cell is not None
    ]
    unique_cells = list(dict.fromkeys(actual_cells))
    characters = [
        character
        for character in getattr(page, "chars", ())
        if str(character.get("text", "")).strip()
        and _character_center_inside_bbox(character, table_bbox)
    ]
    owned: dict[tuple[float, float, float, float], list[Mapping[str, object]]] = {
        cell: [] for cell in unique_cells
    }
    for character in characters:
        owners = [
            cell for cell in unique_cells if _character_fully_inside_bbox(character, cell)
        ]
        if len(owners) != 1:
            raise PdfExtractionError(
                "ambiguous table character ownership on page "
                f"{page_number} in {table_id}"
            )
        owned[owners[0]].append(character)

    x_edges = sorted({value for cell in unique_cells for value in (cell[0], cell[2])})
    y_edges = sorted({value for cell in unique_cells for value in (cell[1], cell[3])})
    blocks: list[PdfBlock] = []
    cells: list[PdfTableCell] = []
    seen: set[tuple[float, float, float, float]] = set()
    for logical_row, (raw_row, row) in enumerate(selected_rows):
        for raw_column, raw_cell in enumerate(row.cells):
            if raw_cell is None:
                continue
            bbox = tuple(float(value) for value in raw_cell)
            if bbox in seen:
                continue
            seen.add(bbox)
            if strategy == "text":
                row_index = logical_row
                column_index = raw_column
                row_span = 1
                column_span = 1
            else:
                column_index = _edge_index(x_edges, bbox[0])
                row_index = _edge_index(y_edges, bbox[1])
                column_span = _edge_index(x_edges, bbox[2]) - column_index
                row_span = _edge_index(y_edges, bbox[3]) - row_index
            identifier = (
                f"{table_id}:row-{row_index + 1:04d}:cell-{column_index + 1:04d}"
            )
            text = ""
            if raw_row < len(extracted) and raw_column < len(extracted[raw_row]):
                value = extracted[raw_row][raw_column]
                if isinstance(value, str):
                    text = re.sub(r"\s+", " ", value).strip()
            cell_chars = owned.get(bbox, [])
            font_size = max(
                (_character_size(character) for character in cell_chars),
                default=10.0,
            )
            bold = any(
                any(
                    marker in str(character.get("fontname", "")).casefold()
                    for marker in ("bold", "black", "heavy")
                )
                for character in cell_chars
            )
            block = PdfBlock(
                id=identifier,
                page_number=page_number,
                order=0,
                kind="table-cell",
                bbox=bbox,
                style=PdfBlockStyle(
                    font_size=font_size,
                    bold=bold,
                    alignment="left",
                    indentation=bbox[0],
                    space_after=0.0,
                ),
                source_text=text,
                table_id=table_id,
                row=row_index,
                column=column_index,
                row_span=row_span,
                column_span=column_span,
            )
            blocks.append(block)
            cells.append(
                PdfTableCell(
                    id=identifier,
                    table_id=table_id,
                    page_number=page_number,
                    row=row_index,
                    column=column_index,
                    row_span=row_span,
                    column_span=column_span,
                    is_header=row_index == 0,
                    block_id=identifier,
                )
            )
    key = lambda block: (block.row or 0, block.column or 0)
    return (
        sorted(blocks, key=key),
        sorted(cells, key=lambda cell: (cell.row, cell.column)),
        len(characters),
    )


def _edge_index(edges: Sequence[float], value: float) -> int:
    for index, edge in enumerate(edges):
        if math.isclose(edge, value, abs_tol=0.01):
            return index
    raise PdfExtractionError("table cell edge does not belong to the fixed grid")


def _leading_footnote_marker(text: str) -> str | None:
    match = re.match(
        r"^\s*((?:[\[(]?(?:\d{1,3}|[*†‡])[\].)]?)|"
        r"(?:[ivxlcdm]{1,6}[.)]|\([ivxlcdm]{1,6}\)|\[[ivxlcdm]{1,6}\]))\s+",
        text,
        re.IGNORECASE,
    )
    return _normalized_marker(match.group(1)) if match is not None else None


def _normalized_marker(text: str) -> str:
    return re.sub(r"[\s\[\]().]", "", text).casefold()


def _trailing_marker(text: str) -> str | None:
    tokens = text.rstrip().rsplit(maxsplit=1)
    return _normalized_marker(tokens[-1]) if tokens else None


def _trailing_marker_characters(
    owner: PdfBlock,
    marker: str,
    characters: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...] | None:
    candidates = sorted(
        (
            character
            for character in characters
            if str(character.get("text", "")).strip()
            and _character_fully_inside_bbox(character, owner.bbox)
            and _character_size(character) <= owner.style.font_size * 0.85 + 1e-9
        ),
        key=lambda character: (
            float(character.get("top", 0.0)),
            float(character.get("x0", 0.0)),
        ),
    )
    if not candidates:
        return None
    owner_characters = [
        character
        for character in characters
        if str(character.get("text", "")).strip()
        and _character_fully_inside_bbox(character, owner.bbox)
        and _character_bbox(character) is not None
    ]
    if not owner_characters:
        return None
    baseline_bboxes = [
        bbox
        for character in owner_characters
        if (bbox := _character_bbox(character)) is not None
        and _character_size(character) > owner.style.font_size * 0.85 + 1e-9
    ]
    if not baseline_bboxes:
        return None
    baseline_rows: list[list[tuple[float, float, float, float]]] = []
    for bbox in sorted(baseline_bboxes, key=lambda item: (item[1] + item[3], item[0])):
        center = (bbox[1] + bbox[3]) / 2.0
        if baseline_rows:
            previous_center = sum(
                (item[1] + item[3]) / 2.0 for item in baseline_rows[-1]
            ) / len(baseline_rows[-1])
        else:
            previous_center = math.inf
        if abs(center - previous_center) <= owner.style.font_size * 0.25:
            baseline_rows[-1].append(bbox)
        else:
            baseline_rows.append([bbox])
    row_centers = [
        sum((bbox[1] + bbox[3]) / 2.0 for bbox in row) / len(row)
        for row in baseline_rows
    ]
    final_row_index = max(range(len(baseline_rows)), key=row_centers.__getitem__)
    for start in range(len(candidates)):
        selected: list[Mapping[str, object]] = []
        rendered = ""
        for character in candidates[start:]:
            if selected:
                previous_bbox = _character_bbox(selected[-1])
                current_bbox = _character_bbox(character)
                if previous_bbox is None or current_bbox is None:
                    break
                if _vertical_overlap_ratio(previous_bbox, current_bbox) < 0.50:
                    break
                if current_bbox[0] - previous_bbox[2] > owner.style.font_size * 0.50:
                    break
            selected.append(character)
            rendered += _normalized_marker(str(character.get("text", "")))
            if rendered == marker:
                selected_bboxes = [
                    bbox
                    for item in selected
                    if (bbox := _character_bbox(item)) is not None
                ]
                if not selected_bboxes:
                    break
                selected_center = sum(
                    (bbox[1] + bbox[3]) / 2.0 for bbox in selected_bboxes
                ) / len(selected_bboxes)
                nearest_row_index = min(
                    range(len(baseline_rows)),
                    key=lambda index: abs(selected_center - row_centers[index]),
                )
                final_row = baseline_rows[final_row_index]
                row_right = max(bbox[2] for bbox in final_row)
                marker_left = min(bbox[0] for bbox in selected_bboxes)
                horizontal_gap = marker_left - row_right
                if (
                    nearest_row_index == final_row_index
                    and abs(selected_center - row_centers[final_row_index])
                    <= owner.style.font_size * 1.5
                    and -owner.style.font_size * 0.50
                    <= horizontal_gap
                    <= owner.style.font_size * 1.50
                ):
                    return tuple(selected)
                break
            if not marker.startswith(rendered):
                break
    return None


def _trim_footnote_marker_extent(
    owner: PdfBlock,
    characters: Sequence[Mapping[str, object]],
) -> PdfBlock:
    content_bottoms = [
        float(character["bottom"])
        for character in characters
        if str(character.get("text", "")).strip()
        and _character_fully_inside_bbox(character, owner.bbox)
        and not (
            _character_size(character) <= owner.style.font_size * 0.85 + 1e-9
        )
        and isinstance(character.get("bottom"), Real)
        and math.isfinite(float(character["bottom"]))
    ]
    if not content_bottoms:
        return owner
    content_bottom = max(content_bottoms)
    if content_bottom >= owner.bbox[3] or content_bottom <= owner.bbox[1]:
        return owner
    return replace(
        owner,
        bbox=(owner.bbox[0], owner.bbox[1], owner.bbox[2], content_bottom),
    )


def _character_size(character: Mapping[str, object]) -> float:
    try:
        size = float(character.get("size", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return size if math.isfinite(size) else 0.0


def _character_bbox(
    character: Mapping[str, object],
) -> tuple[float, float, float, float] | None:
    try:
        bbox = (
            float(character["x0"]),
            float(character["top"]),
            float(character["x1"]),
            float(character["bottom"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in bbox):
        return None
    return bbox


def _character_center_inside_bbox(
    character: Mapping[str, object],
    bbox: tuple[float, float, float, float],
) -> bool:
    char_bbox = _character_bbox(character)
    if char_bbox is None:
        return False
    x = (char_bbox[0] + char_bbox[2]) / 2.0
    y = (char_bbox[1] + char_bbox[3]) / 2.0
    return (
        bbox[0] - 1e-6 <= x <= bbox[2] + 1e-6
        and bbox[1] - 1e-6 <= y <= bbox[3] + 1e-6
    )


def _mapping_center_inside_any_bbox(
    value: object,
    bboxes: Sequence[tuple[float, float, float, float]],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    char_bbox = _character_bbox(value)
    if char_bbox is None:
        return False
    x = (char_bbox[0] + char_bbox[2]) / 2.0
    y = (char_bbox[1] + char_bbox[3]) / 2.0
    return any(
        bbox[0] - 1e-6 <= x <= bbox[2] + 1e-6
        and bbox[1] - 1e-6 <= y <= bbox[3] + 1e-6
        for bbox in bboxes
    )


def _character_fully_inside_bbox(
    character: Mapping[str, object],
    bbox: tuple[float, float, float, float],
) -> bool:
    char_bbox = _character_bbox(character)
    if char_bbox is None:
        return False
    return (
        char_bbox[0] >= bbox[0] - 1e-6
        and char_bbox[1] >= bbox[1] - 1e-6
        and char_bbox[2] <= bbox[2] + 1e-6
        and char_bbox[3] <= bbox[3] + 1e-6
    )


def _character_inside_bbox(
    character: Mapping[str, object],
    bbox: tuple[float, float, float, float],
) -> bool:
    return _character_center_inside_bbox(character, bbox)


def _caption_distance(
    figure: tuple[float, float, float, float],
    caption: tuple[float, float, float, float],
) -> float:
    if caption[1] >= figure[3]:
        return caption[1] - figure[3]
    if figure[1] >= caption[3]:
        return figure[1] - caption[3]
    return 0.0


def _horizontal_overlap_ratio(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    return overlap / max(1e-9, min(left[2] - left[0], right[2] - right[0]))


def _vertical_overlap_ratio(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    overlap = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return overlap / max(1e-9, min(left[3] - left[1], right[3] - right[1]))


def _horizontal_bbox_gap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(left[0] - right[2], right[0] - left[2], 0.0)


def _annotation_bbox(
    value: object, page: object
) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 4
    ):
        raise ValueError("invalid link rectangle")
    x0, y0, x1, y1 = (float(item) for item in value)
    points = [
        _pdf_point_to_plumber(page, x, y)
        for x, y in ((x0, y0), (x0, y1), (x1, y0), (x1, y1))
    ]
    return _points_bbox(points)


def _blocks_intersecting_bbox(
    blocks: Sequence[PdfBlock],
    bbox: tuple[float, float, float, float],
) -> list[PdfBlock]:
    return [block for block in blocks if _bbox_intersection(block.bbox, bbox) > 0]


def _annotation_uri(annotation: Mapping[str, object]) -> str | None:
    action = annotation.get("/A")
    if not isinstance(action, Mapping) or str(action.get("/S", "")) != "/URI":
        return None
    uri = action.get("/URI")
    return str(uri) if uri is not None and str(uri) else None


def _annotation_destination(annotation: Mapping[str, object]) -> object:
    if "/Dest" in annotation:
        return annotation["/Dest"]
    action = annotation.get("/A")
    if isinstance(action, Mapping) and str(action.get("/S", "")) == "/GoTo":
        return action.get("/D")
    return None


def _map_internal_destination(
    reader: PdfReader,
    destination: object,
    by_page: Mapping[int, Sequence[PdfBlock]],
) -> PdfBlock | None:
    if destination is None:
        return None
    if isinstance(destination, str):
        destination = reader.named_destinations.get(destination)
        if destination is None:
            return None
    if hasattr(destination, "page"):
        try:
            page_index = reader.get_destination_page_number(destination)  # type: ignore[arg-type]
        except Exception:
            return None
        mode = str(getattr(destination, "typ", ""))
        values = {
            "/Left": getattr(destination, "left", None),
            "/Bottom": getattr(destination, "bottom", None),
            "/Right": getattr(destination, "right", None),
            "/Top": getattr(destination, "top", None),
        }
    else:
        if (
            not isinstance(destination, Sequence)
            or isinstance(destination, (str, bytes))
            or not destination
        ):
            return None
        try:
            page_object = destination[0]
            if hasattr(page_object, "get_object"):
                page_object = page_object.get_object()
            page_index = reader.get_page_number(page_object)
        except Exception:
            return None
        mode = str(destination[1]) if len(destination) > 1 else "/Fit"
        arguments = list(destination[2:])
        values = _destination_array_values(mode, arguments)
    candidates = sorted(by_page.get(page_index + 1, ()), key=lambda block: block.order)
    if not candidates:
        return None
    page = reader.pages[page_index]
    try:
        media_box = page.mediabox
        page_left, page_right = sorted(
            (float(media_box.left), float(media_box.right))
        )
        page_bottom, page_top = sorted(
            (float(media_box.bottom), float(media_box.top))
        )
    except (AttributeError, TypeError, ValueError):
        return None

    left = _destination_number(values.get("/Left"))
    right = _destination_number(values.get("/Right"))
    top = _destination_number(values.get("/Top"))
    bottom = _destination_number(values.get("/Bottom"))
    if mode in {"/Fit", "/FitB"}:
        matches = candidates if len(candidates) == 1 else []
    elif mode in {"/FitH", "/FitBH"}:
        if top is None:
            return candidates[0] if len(candidates) == 1 else None
        matches = _blocks_intersecting_segment(
            candidates,
            _pdf_point_to_plumber(page, page_left, top),
            _pdf_point_to_plumber(page, page_right, top),
        )
    elif mode in {"/FitV", "/FitBV"}:
        if left is None:
            return candidates[0] if len(candidates) == 1 else None
        matches = _blocks_intersecting_segment(
            candidates,
            _pdf_point_to_plumber(page, left, page_bottom),
            _pdf_point_to_plumber(page, left, page_top),
        )
    elif mode == "/FitR":
        if None in {left, bottom, right, top}:
            return None
        target_bbox = _points_bbox(
            [
                _pdf_point_to_plumber(page, x, y)
                for x, y in (
                    (left, bottom),
                    (left, top),
                    (right, bottom),
                    (right, top),
                )
            ]
        )
        matches = _blocks_intersecting_bbox(candidates, target_bbox)
    elif mode == "/XYZ":
        if left is not None and top is not None:
            point = _pdf_point_to_plumber(page, left, top)
            matches = [
                block
                for block in candidates
                if block.bbox[0] - 1e-6 <= point[0] <= block.bbox[2] + 1e-6
                and block.bbox[1] - 1e-6 <= point[1] <= block.bbox[3] + 1e-6
            ]
        elif left is not None:
            matches = _blocks_intersecting_segment(
                candidates,
                _pdf_point_to_plumber(page, left, page_bottom),
                _pdf_point_to_plumber(page, left, page_top),
            )
        elif top is not None:
            matches = _blocks_intersecting_segment(
                candidates,
                _pdf_point_to_plumber(page, page_left, top),
                _pdf_point_to_plumber(page, page_right, top),
            )
        else:
            matches = candidates if len(candidates) == 1 else []
    else:
        return None
    return matches[0] if len(matches) == 1 else None


def _pdf_point_to_plumber(
    page: object,
    x: float,
    y: float,
) -> tuple[float, float]:
    """Map a raw PDF point to pdfplumber's rotated, top-origin coordinates."""
    try:
        media_box = page.mediabox  # type: ignore[attr-defined]
        x0, x1 = sorted((float(media_box.left), float(media_box.right)))
        y0, y1 = sorted((float(media_box.bottom), float(media_box.top)))
        rotation = int(page.get("/Rotate", 0)) % 360  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("invalid PDF page geometry") from error
    if rotation == 0:
        a, b, c, d, e, f = (1.0, 0.0, 0.0, 1.0, -x0, -y0)
        normalized = (x0, y0, x1, y1)
    elif rotation == 90:
        a, b, c, d, e, f = (0.0, -1.0, 1.0, 0.0, -y0, x1)
        normalized = (y0, x0, y1, x1)
    elif rotation == 180:
        a, b, c, d, e, f = (-1.0, 0.0, 0.0, -1.0, x1, y1)
        normalized = (x0, y0, x1, y1)
    elif rotation == 270:
        a, b, c, d, e, f = (0.0, 1.0, -1.0, 0.0, y1, -x0)
        normalized = (y0, x0, y1, x1)
    else:
        raise ValueError("PDF page rotation must be a multiple of 90 degrees")
    media_height = normalized[3] - normalized[1]
    media_x0 = normalized[0]
    media_top = media_height - normalized[3]
    transformed_x = a * x + c * y + e
    transformed_y = b * x + d * y + f
    return (
        transformed_x + media_x0,
        media_height - transformed_y + media_top,
    )


def _points_bbox(
    points: Sequence[tuple[float, float]],
) -> tuple[float, float, float, float]:
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _blocks_intersecting_segment(
    blocks: Sequence[PdfBlock],
    start: tuple[float, float],
    end: tuple[float, float],
) -> list[PdfBlock]:
    x0, x1 = sorted((start[0], end[0]))
    top, bottom = sorted((start[1], end[1]))
    if abs(x1 - x0) <= 1e-6:
        return [
            block
            for block in blocks
            if block.bbox[0] - 1e-6 <= x0 <= block.bbox[2] + 1e-6
            and min(block.bbox[3], bottom) >= max(block.bbox[1], top) - 1e-6
        ]
    if abs(bottom - top) <= 1e-6:
        return [
            block
            for block in blocks
            if block.bbox[1] - 1e-6 <= top <= block.bbox[3] + 1e-6
            and min(block.bbox[2], x1) >= max(block.bbox[0], x0) - 1e-6
        ]
    return []


def _destination_array_values(
    mode: str, arguments: Sequence[object]
) -> dict[str, object]:
    if mode == "/XYZ":
        names = ("/Left", "/Top")
    elif mode in {"/FitH", "/FitBH"}:
        names = ("/Top",)
    elif mode in {"/FitV", "/FitBV"}:
        names = ("/Left",)
    elif mode == "/FitR":
        names = ("/Left", "/Bottom", "/Right", "/Top")
    else:
        names = ()
    return dict(zip(names, arguments, strict=False))


def _destination_number(value: object) -> float | None:
    if value is None or value.__class__.__name__ == "NullObject":
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bbox_intersection(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _finite_number(value: object, context: str) -> float:
    if isinstance(value, bool):
        raise PdfExtractionError(f"{context} must be a finite number")
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise PdfExtractionError(f"{context} must be a finite number") from error
    if not math.isfinite(numeric):
        raise PdfExtractionError(f"{context} must be a finite number")
    return numeric
