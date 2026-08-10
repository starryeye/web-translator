"""Document-wide, boundary-aware normalization of technical terminology."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
import re

from web_translator.models import ProtectedToken, Translation


_TOKEN_PATTERN = re.compile(r"⟦WT:\d{6}⟧")
_KOREAN_PATTERN = re.compile(r"[가-힣]")
_IDENTIFIER_CHARACTER = r"A-Za-z0-9_"


class TerminologyError(ValueError):
    """A glossary or translation cannot be normalized safely."""


def normalize_first_use(
    ordered: Sequence[Translation],
    glossary: Mapping[str, str],
    *,
    protected_by_segment: Mapping[str, Sequence[ProtectedToken]] | None = None,
) -> list[Translation]:
    """Keep English terms and add one canonical Korean gloss at first use.

    Records must already be in document order. Protected placeholders are opaque,
    so terms contained by code, URLs, identifiers, and inline tag tokens are never
    inspected or rewritten here.
    """
    records = list(ordered)
    if any(not isinstance(record, Translation) for record in records):
        raise TerminologyError("ordered records must contain Translation values")
    terms = _validated_glossary(glossary)
    if not terms:
        return records

    alternatives = "|".join(re.escape(term) for term, _ in terms)
    term_pattern = re.compile(
        rf"(?<![{_IDENTIFIER_CHARACTER}])(?P<term>{alternatives})"
        rf"(?![{_IDENTIFIER_CHARACTER}])"
    )
    canonical = dict(terms)
    seen: set[str] = set()
    token_metadata = _validated_token_metadata(protected_by_segment)

    normalized: list[Translation] = []
    for record in records:
        transparent_tokens = {
            token.token
            for token in token_metadata.get(record.segment_id, ())
            if token.kind == "tag"
        }
        normalized.append(
            replace(
                record,
                text=_normalize_record(
                    record.text,
                    term_pattern,
                    canonical,
                    seen,
                    transparent_tokens,
                ),
            )
        )
    return normalized


def _normalize_record(
    text: str,
    term_pattern: re.Pattern[str],
    canonical: Mapping[str, str],
    seen: set[str],
    transparent_tokens: set[str],
) -> str:
    """Normalize visible characters while retaining opaque tokens verbatim.

    Removing tokens in the matching projection makes paired inline tag markers
    transparent, so a term such as ``Spring <em>AI</em>`` is still one visual
    occurrence. A full code/URL/identifier token contributes no characters and
    its protected value remains uninspected.
    """
    visible_characters: list[str] = []
    original_positions: list[int] = []
    cursor = 0
    for placeholder in _TOKEN_PATTERN.finditer(text):
        for position in range(cursor, placeholder.start()):
            visible_characters.append(text[position])
            original_positions.append(position)
        if placeholder.group() not in transparent_tokens:
            visible_characters.append("\x00")
            original_positions.append(placeholder.start())
        cursor = placeholder.end()
    for position in range(cursor, len(text)):
        visible_characters.append(text[position])
        original_positions.append(position)
    projected = "".join(visible_characters)

    removed_positions: set[int] = set()
    insertions: dict[int, list[str]] = {}
    ignored_ranges: list[tuple[int, int]] = []
    for match in term_pattern.finditer(projected):
        if any(start <= match.start() < end for start, end in ignored_ranges):
            continue
        term = match.group("term")
        gloss = canonical[term]
        suffix_end = _gloss_suffix_end(projected, match.end(), gloss)
        if suffix_end > match.end():
            ignored_ranges.append((match.end(), suffix_end))
        removed_positions.update(
            original_positions[index] for index in range(match.end(), suffix_end)
        )
        if term not in seen:
            seen.add(term)
            insertion_position = (
                original_positions[match.end()]
                if match.end() < len(original_positions)
                else len(text)
            )
            insertions.setdefault(insertion_position, []).append(f"({gloss})")

    rebuilt: list[str] = []
    for position, character in enumerate(text):
        rebuilt.extend(insertions.get(position, ()))
        if position not in removed_positions:
            rebuilt.append(character)
    rebuilt.extend(insertions.get(len(text), ()))
    return "".join(rebuilt)


def _gloss_suffix_end(text: str, start: int, canonical_gloss: str) -> int:
    position = start
    parenthetical = re.compile(r"[ \t]*\((?P<content>[^()\r\n]*)\)")
    while True:
        match = parenthetical.match(text, position)
        if match is None:
            return position
        content = match.group("content").strip()
        if not _looks_like_gloss(content, canonical_gloss):
            return position
        position = match.end()


def _looks_like_gloss(content: str, canonical_gloss: str) -> bool:
    """Recognize only the canonical gloss, never infer from Korean grammar.

    A short Korean parenthetical can be either a gloss or a qualification. The
    current glossary contract has no variant metadata, so destructive inference
    would lose meaning. Future structured variant metadata can extend this exact
    comparison without reintroducing a language-shape heuristic.
    """
    compact_content = re.sub(r"[\s·_-]+", "", content)
    compact_canonical = re.sub(r"[\s·_-]+", "", canonical_gloss)
    return compact_content == compact_canonical


def _validated_token_metadata(
    protected_by_segment: Mapping[str, Sequence[ProtectedToken]] | None,
) -> dict[str, tuple[ProtectedToken, ...]]:
    if protected_by_segment is None:
        return {}
    if not isinstance(protected_by_segment, Mapping):
        raise TerminologyError("protected token metadata must be a mapping")
    result: dict[str, tuple[ProtectedToken, ...]] = {}
    for segment_id, values in protected_by_segment.items():
        if not isinstance(segment_id, str):
            raise TerminologyError("protected token metadata keys must be strings")
        if isinstance(values, (str, bytes)):
            raise TerminologyError("protected token metadata values must be sequences")
        tokens = tuple(values)
        if any(not isinstance(token, ProtectedToken) for token in tokens):
            raise TerminologyError("protected token metadata must contain ProtectedToken values")
        result[segment_id] = tokens
    return result


def _validated_glossary(glossary: Mapping[str, str]) -> list[tuple[str, str]]:
    if not isinstance(glossary, Mapping):
        raise TerminologyError("glossary must be a mapping")
    terms: list[tuple[str, str]] = []
    for term, gloss in glossary.items():
        if not isinstance(term, str) or not isinstance(gloss, str):
            raise TerminologyError("glossary terms and glosses must be strings")
        if not term or term != term.strip():
            raise TerminologyError("glossary term must be non-empty and trimmed")
        if not gloss or gloss != gloss.strip():
            raise TerminologyError("glossary gloss must be non-empty and trimmed")
        if _KOREAN_PATTERN.search(gloss) is None:
            raise TerminologyError(f"gloss for {term!r} must contain Korean text")
        if any(character in gloss for character in "()\r\n"):
            raise TerminologyError(f"gloss for {term!r} cannot contain parentheses or newlines")
        terms.append((term, gloss))
    terms.sort(key=lambda item: (-len(item[0]), item[0]))
    return terms
