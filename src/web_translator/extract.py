"""Semantic DOM extraction for captured source pages."""

from __future__ import annotations

from pathlib import Path
import re

from bs4 import BeautifulSoup, Tag

from web_translator.models import Segment, write_segments
from web_translator.protection import protect_fragment


_ELIGIBLE_TAGS = {
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
_EXCLUDED_TAGS = {"script", "style", "noscript", "pre", "code", "svg", "math"}
_TOKEN_PATTERN = re.compile(r"⟦WT:\d{6}⟧")
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
}


def extract_segments(source_html: Path, segments_path: Path) -> list[Segment]:
    """Mark eligible blocks, persist the DOM, and write their segment manifest."""
    source_html = Path(source_html)
    soup = BeautifulSoup(source_html.read_text(encoding="utf-8"), "lxml")
    for marked in soup.select("[data-wt-segment]"):
        del marked["data-wt-segment"]

    candidates = [tag for tag in soup.find_all(_ELIGIBLE_TAGS) if _is_candidate(tag)]
    drafts: list[tuple[Tag, list[str], str]] = []
    heading_stack: list[str] = []
    for candidate in candidates:
        name = candidate.name.lower()
        if _is_heading(name):
            level = int(name[1])
            heading_stack[level - 1 :] = []
        own_text, _ = protect_fragment(_fragment_without_eligible_descendants(candidate))
        if _has_translatable_text(own_text):
            semantic_type = "heading" if _is_heading(name) else _SEMANTIC_TYPES[name]
            drafts.append((candidate, list(heading_stack), semantic_type))
        if _is_heading(name):
            while len(heading_stack) < level - 1:
                heading_stack.append("")
            heading_stack.append(candidate.get_text(" ", strip=True))

    ids = [f"seg-{index:06d}" for index in range(1, len(drafts) + 1)]
    for segment_id, (candidate, _, _) in zip(ids, drafts, strict=True):
        candidate["data-wt-segment"] = segment_id

    segments: list[Segment] = []
    for index, (candidate, heading_path, semantic_type) in enumerate(drafts):
        segment_id = ids[index]
        source_text, protected = protect_fragment(candidate.decode_contents())
        context_ids = ids[max(0, index - 1) : index] + ids[index + 1 : index + 2]
        segments.append(
            Segment(
                id=segment_id,
                locator=f"[data-wt-segment='{segment_id}']",
                semantic_type=semantic_type,
                heading_path=[heading for heading in heading_path if heading],
                source_text=source_text,
                protected=list(protected),
                context_ids=context_ids,
                target=True,
            )
        )

    source_html.write_text(str(soup), encoding="utf-8", newline="\n")
    write_segments(Path(segments_path), segments)
    return segments


def _is_candidate(tag: Tag) -> bool:
    for parent in tag.parents:
        if not isinstance(parent, Tag):
            continue
        parent_name = parent.name.lower()
        if parent_name in _EXCLUDED_TAGS or _translate_disabled(parent):
            return False
    return not _translate_disabled(tag)


def _fragment_without_eligible_descendants(tag: Tag) -> str:
    fragment = BeautifulSoup(tag.decode_contents(), "html.parser")
    for descendant in fragment.find_all(_ELIGIBLE_TAGS):
        descendant.decompose()
    return str(fragment)


def _translate_disabled(tag: Tag) -> bool:
    return str(tag.get("translate", "")).strip().lower() == "no"


def _has_translatable_text(source_text: str) -> bool:
    return bool(_TOKEN_PATTERN.sub("", source_text).strip())


def _is_heading(name: str) -> bool:
    return len(name) == 2 and name[0] == "h" and name[1] in "123456"
