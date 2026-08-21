"""Deterministic pure helpers for PDF word, line, and text-block layout."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import math
import re

from web_translator.pdf_models import PdfBlock, PdfBlockKind, PdfBlockStyle


_VERTICAL_OVERLAP = 0.60
_MINIMUM_GUTTER = 18.0
_WORD_GAP_FONT_MULTIPLIER = 1.5
_PARAGRAPH_GAP_FONT_MULTIPLIER = 1.6
_LIST_PATTERN = re.compile(
    r"^(?:[\u2022\u2023\u25e6\u2043\u2219*+-]|"
    r"\d+(?:\.\d+)*[.)]|[A-Za-z][.)]|[ivxlcdmIVXLCDM]+[.)])\s+"
)
_HEADING_NUMBER_PATTERN = re.compile(r"^(?P<number>\d+(?:\.\d+)*)[.)]?\s+")
_PAGE_NUMBER_PATTERN = re.compile(r"\d+\Z")
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
        return self.bold and _LIST_PATTERN.match(self.text) is None

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
        dynamic_gap = min(
            _MINIMUM_GUTTER - 1e-6,
            max(self.size, word.size) * _WORD_GAP_FONT_MULTIPLIER,
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
    return [
        line.normalized()
        for line in sorted(lines, key=lambda item: (item.top, item.x0, item.text))
    ]


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
        if not line.is_heading and line.width <= page_width * 0.60
    ]
    centers = sorted({line.center_x for line in candidates})
    gutters: list[tuple[float, float]] = []
    for left_center, right_center in zip(centers, centers[1:]):
        split = (left_center + right_center) / 2.0
        left = [line for line in candidates if line.center_x <= split]
        right = [line for line in candidates if line.center_x > split]
        if not left or not right:
            continue
        gutter = (max(line.x1 for line in left), min(line.x0 for line in right))
        if gutter[1] - gutter[0] + 1e-9 < minimum_width:
            continue
        if not any(
            math.isclose(gutter[0], existing[0], abs_tol=0.01)
            and math.isclose(gutter[1], existing[1], abs_tol=0.01)
            for existing in gutters
        ):
            gutters.append(gutter)
    if len(gutters) > 1:
        raise PdfExtractionError("ambiguous column evidence")
    return gutters[0] if gutters else None


def order_page_lines(lines: Sequence[PdfLine], page_width: float) -> list[PdfLine]:
    """Order a single page top-to-bottom or by an unambiguous two-column gutter."""
    gutter = find_clear_gutter(lines, page_width, minimum_width=_MINIMUM_GUTTER)
    if gutter is None:
        return sorted(lines, key=lambda item: (item.top, item.x0, item.text))
    conflicting = [
        line for line in lines if line.crosses(gutter) and not line.is_heading
    ]
    if conflicting:
        evidence = ", ".join(repr(line.text) for line in conflicting[:3])
        raise PdfExtractionError(f"conflicting column evidence: {evidence}")
    return order_column_regions(lines, gutter)


def order_column_regions(
    lines: Sequence[PdfLine], gutter: tuple[float, float]
) -> list[PdfLine]:
    """Order each region between spanning headings left-column then right-column."""
    spanning = sorted(
        (line for line in lines if line.crosses(gutter) and line.is_heading),
        key=lambda item: (item.top, item.x0, item.text),
    )
    ordinary = [line for line in lines if line not in spanning]
    result: list[PdfLine] = []
    previous_bottom = -math.inf
    for heading in spanning:
        region = [
            line
            for line in ordinary
            if line.top >= previous_bottom and line.top < heading.top
        ]
        result.extend(_order_two_columns(region, gutter))
        result.append(heading)
        previous_bottom = heading.bottom
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
    normalized = _classify_repeated_bands(normalized)

    sizes: Counter[int] = Counter()
    for lines in normalized:
        for line in lines:
            if line.kind is None and _LIST_PATTERN.match(line.text) is None:
                sizes[round(line.size)] += line.character_count
    body_size = max(sizes, key=lambda size: (sizes[size], -size)) if sizes else 0
    heading_sizes = sorted(
        {size for size in sizes if size >= body_size + 1}, reverse=True
    )

    result: list[list[PdfLine]] = []
    for lines in normalized:
        classified: list[PdfLine] = []
        for line in lines:
            if line.kind is not None:
                classified.append(line)
                continue
            rounded_size = round(line.size)
            heading_candidate = rounded_size in heading_sizes or line.bold
            if heading_candidate and _LIST_PATTERN.match(line.text) is None:
                number = _HEADING_NUMBER_PATTERN.match(line.text)
                if number is not None:
                    level = number.group("number").count(".") + 1
                elif rounded_size in heading_sizes:
                    level = heading_sizes.index(rounded_size) + 1
                else:
                    level = len(heading_sizes) + 1
                classified.append(replace(line, kind="heading", heading_level=level))
            elif _LIST_PATTERN.match(line.text) is not None:
                classified.append(replace(line, kind="list-item"))
            else:
                classified.append(replace(line, kind="paragraph"))
        result.append(classified)
    return result


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
    if _LIST_PATTERN.match(line.text) is not None:
        return "list-item", line
    if line.is_heading:
        return "heading", line
    return "paragraph", line


def merge_contiguous_paragraph_lines(
    classified: Sequence[tuple[PdfBlockKind, PdfLine]],
) -> list[tuple[PdfBlockKind, tuple[PdfLine, ...]]]:
    """Merge only geometrically contiguous paragraph lines."""
    merged: list[tuple[PdfBlockKind, tuple[PdfLine, ...]]] = []
    for kind, line in classified:
        if (
            kind == "paragraph"
            and merged
            and merged[-1][0] == "paragraph"
            and _paragraphs_are_contiguous(merged[-1][1][-1], line)
        ):
            previous_kind, previous_lines = merged[-1]
            merged[-1] = (previous_kind, (*previous_lines, line))
        else:
            merged.append((kind, (line,)))
    return merged


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
