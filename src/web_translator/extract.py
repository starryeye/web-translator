"""Semantic DOM extraction for captured source pages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from web_translator.models import (
    ProtectedToken,
    Segment,
    SegmentContractError,
    read_segments,
    write_segments,
)
from web_translator.protection import protect_fragment


_BLOCK_TAGS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "li",
    "dt",
    "dd",
    "th",
    "td",
    "caption",
    "figcaption",
    "label",
    "summary",
}
_STANDALONE_TEXT_TAGS = {"a", "button", "legend", "option"}
_FLOW_CONTAINER_TAGS = {
    "address",
    "article",
    "aside",
    "body",
    "div",
    "footer",
    "header",
    "main",
    "nav",
    "section",
}
_EXCLUDED_TAGS = {"script", "style", "noscript", "pre", "code", "svg", "math"}
TRANSLATABLE_ATTRIBUTES = (
    "alt",
    "aria-label",
    "aria-description",
    "title",
    "placeholder",
)
_TOKEN_PATTERN = re.compile(r"⟦WT:\d{6}⟧")
_LOCATION_PREFIX = "<!--wt-location:"
_SEMANTIC_TYPES = {
    "p": "paragraph",
    "li": "list_item",
    "dt": "term",
    "dd": "definition",
    "th": "table_header",
    "td": "table_cell",
    "caption": "caption",
    "figcaption": "figure_caption",
    "label": "label",
    "summary": "summary",
    "a": "link",
    "button": "button",
    "legend": "legend",
    "option": "option",
}


class ExtractionError(RuntimeError):
    """Captured source cannot be read or extraction outputs cannot be written."""


@dataclass(frozen=True, slots=True)
class _Draft:
    element: Tag
    heading_path: list[str]
    semantic_type: str
    has_content: bool
    attributes: tuple[str, ...]


def extract_segments(source_html: Path, segments_path: Path) -> list[Segment]:
    """Mark every eligible location and persist its exact translation manifest."""
    source_html = Path(source_html)
    try:
        original = source_html.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ExtractionError(f"source HTML cannot be read as UTF-8: {error}") from error
    soup = BeautifulSoup(original, "lxml")
    if any(
        "data-wt-segment" in style.get_text().lower()
        for style in soup.find_all("style")
        if isinstance(style, Tag)
    ):
        raise ExtractionError(
            "source CSS uses reserved selector data-wt-segment; extraction aborted"
        )
    preexisting_markers = [
        tag for tag in soup.select("[data-wt-segment]") if isinstance(tag, Tag)
    ]
    if preexisting_markers:
        previous = _previous_extraction(preexisting_markers, Path(segments_path))
        if previous is not None:
            return previous
        raise ExtractionError(
            "source HTML uses reserved attribute data-wt-segment; extraction aborted"
        )
    if _LOCATION_PREFIX in original:
        raise ExtractionError(
            "source HTML uses reserved web-translator location marker; extraction aborted"
        )

    content_owners = _content_owners(soup)
    attribute_names = _attribute_targets(soup)
    target_elements = content_owners | set(attribute_names)
    row_numbers = {
        id(row): index
        for index, row in enumerate(
            (
                tag
                for tag in soup.find_all("tr")
                if isinstance(tag, Tag) and _is_candidate(tag)
            ),
            start=1,
        )
    }

    drafts: list[_Draft] = []
    heading_stack: list[str] = []
    for element in soup.find_all(True):
        if not isinstance(element, Tag):
            continue
        name = element.name.lower()
        heading_level = int(name[1]) if _is_heading(name) and _is_candidate(element) else None
        if heading_level is not None:
            heading_stack[heading_level - 1 :] = []

        if id(element) in target_elements:
            fragment = _fragment_without_owned_descendants(element, content_owners)
            protected_fragment, _ = protect_fragment(fragment)
            has_content = id(element) in content_owners and _has_translatable_text(
                protected_fragment
            )
            attributes = tuple(attribute_names.get(id(element), ()))
            if has_content or attributes:
                semantic_type = _semantic_type(element, row_numbers)
                if attributes:
                    semantic_type = (
                        f"located:{semantic_type}" if has_content else "located:attributes"
                    )
                drafts.append(
                    _Draft(
                        element=element,
                        heading_path=[value for value in heading_stack if value],
                        semantic_type=semantic_type,
                        has_content=has_content,
                        attributes=attributes,
                    )
                )

        if heading_level is not None:
            while len(heading_stack) < heading_level - 1:
                heading_stack.append("")
            heading_stack.append(element.get_text(" ", strip=True))

    ids = [f"seg-{index:06d}" for index in range(1, len(drafts) + 1)]
    for segment_id, draft in zip(ids, drafts, strict=True):
        draft.element["data-wt-segment"] = segment_id

    segments: list[Segment] = []
    for index, draft in enumerate(drafts):
        segment_id = ids[index]
        raw_payload = _location_payload(draft)
        source_text, raw_protected = protect_fragment(raw_payload)
        protected = [
            ProtectedToken(token.token, "location", token.value)
            if _is_location_boundary(token.value)
            else token
            for token in raw_protected
        ]
        if not _has_translatable_text(source_text):
            raise ExtractionError(
                f"eligible human-readable location produced no target: {segment_id}"
            )
        context_ids = ids[max(0, index - 1) : index] + ids[index + 1 : index + 2]
        segments.append(
            Segment(
                id=segment_id,
                locator=f"[data-wt-segment='{segment_id}']",
                semantic_type=draft.semantic_type,
                heading_path=draft.heading_path,
                source_text=source_text,
                protected=list(protected),
                context_ids=context_ids,
                target=True,
            )
        )

    try:
        source_html.write_text(str(soup), encoding="utf-8", newline="\n")
        write_segments(Path(segments_path), segments)
    except (OSError, UnicodeError) as error:
        raise ExtractionError(f"extraction outputs cannot be written: {error}") from error
    return segments


def _is_location_boundary(value: str) -> bool:
    return value.startswith(_LOCATION_PREFIX) and value.endswith("-->")


def _previous_extraction(
    markers: list[Tag], segments_path: Path
) -> list[Segment] | None:
    if not segments_path.is_file():
        return None
    try:
        segments = read_segments(segments_path)
    except (OSError, UnicodeError, SegmentContractError):
        return None
    marker_ids = [str(marker.get("data-wt-segment", "")) for marker in markers]
    expected_ids = [segment.id for segment in segments]
    if marker_ids != expected_ids or len(marker_ids) != len(set(marker_ids)):
        return None
    if any(
        segment.locator != f"[data-wt-segment='{segment.id}']"
        for segment in segments
    ):
        return None
    return segments


def _content_owners(soup: BeautifulSoup) -> set[int]:
    owners: set[int] = set()
    for node in soup.find_all(string=True):
        if not isinstance(node, NavigableString) or isinstance(node, Comment):
            continue
        parent = node.parent
        if not isinstance(parent, Tag) or not _is_candidate(parent):
            continue
        protected, _ = protect_fragment(str(node))
        if not _has_translatable_text(protected):
            continue
        owner = _text_owner(parent)
        if owner is not None:
            owners.add(id(owner))
    return owners


def _text_owner(parent: Tag) -> Tag | None:
    ancestors = [
        node
        for node in (parent, *parent.parents)
        if isinstance(node, Tag) and _is_candidate(node)
    ]
    for names in (_BLOCK_TAGS, _STANDALONE_TEXT_TAGS, _FLOW_CONTAINER_TAGS):
        owner = next((node for node in ancestors if node.name.lower() in names), None)
        if owner is not None:
            return owner
    return parent


def _attribute_targets(soup: BeautifulSoup) -> dict[int, tuple[str, ...]]:
    result: dict[int, tuple[str, ...]] = {}
    for element in soup.find_all(True):
        if not isinstance(element, Tag) or not _is_candidate(element):
            continue
        names = tuple(
            name
            for name in TRANSLATABLE_ATTRIBUTES
            if element.has_attr(name) and _is_translatable_value(element.get(name))
        )
        if names:
            result[id(element)] = names
    return result


def _is_translatable_value(value: object) -> bool:
    if not isinstance(value, str):
        return False
    protected, _ = protect_fragment(value)
    return _has_translatable_text(protected)


def _fragment_without_owned_descendants(tag: Tag, owners: set[int]) -> str:
    fragment = BeautifulSoup(tag.decode_contents(), "html.parser")
    original_descendants = [
        node
        for node in tag.find_all(True)
        if isinstance(node, Tag) and id(node) in owners
    ]
    for original in original_descendants:
        relative = _relative_tag_path(tag, original)
        current: Tag | BeautifulSoup = fragment
        for child_index in relative:
            children = [child for child in current.children if isinstance(child, Tag)]
            if child_index >= len(children):
                current = fragment
                break
            current = children[child_index]
        if isinstance(current, Tag) and current is not fragment:
            current.decompose()
    return str(fragment)


def _relative_tag_path(ancestor: Tag, descendant: Tag) -> list[int]:
    path: list[int] = []
    current = descendant
    while current is not ancestor:
        parent = current.parent
        if not isinstance(parent, Tag):
            return []
        siblings = [child for child in parent.children if isinstance(child, Tag)]
        path.append(siblings.index(current))
        current = parent
    return list(reversed(path))


def _location_payload(draft: _Draft) -> str:
    if not draft.attributes:
        return draft.element.decode_contents()
    parts: list[str] = []
    for name in draft.attributes:
        value = draft.element.get(name)
        if not isinstance(value, str):
            raise ExtractionError(f"translatable attribute {name} must be a string")
        parts.extend((f"<!--wt-location:attribute:{name}-->", value))
    if draft.has_content:
        parts.extend(
            (
                "<!--wt-location:content-->",
                draft.element.decode_contents(),
            )
        )
    return "".join(parts)


def _semantic_type(element: Tag, row_numbers: dict[int, int]) -> str:
    name = element.name.lower()
    base = "heading" if _is_heading(name) else _SEMANTIC_TYPES.get(name, "prose")
    if name not in {"th", "td"}:
        return base
    row = element.find_parent("tr")
    row_number = row_numbers.get(id(row)) if isinstance(row, Tag) else None
    if row_number is None:
        raise ExtractionError("table cell has no eligible table row")
    return f"{base}:row:{row_number:06d}"


def _is_candidate(tag: Tag) -> bool:
    for node in (tag, *tag.parents):
        if not isinstance(node, Tag):
            continue
        if node.name.lower() in _EXCLUDED_TAGS or _translate_disabled(node):
            return False
    return True


def _translate_disabled(tag: Tag) -> bool:
    return str(tag.get("translate", "")).strip().lower() == "no"


def _has_translatable_text(source_text: str) -> bool:
    return bool(_TOKEN_PATTERN.sub("", source_text).strip())


def _is_heading(name: str) -> bool:
    return len(name) == 2 and name[0] == "h" and name[1] in "123456"
