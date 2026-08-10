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

    Records must already be in document order. Placeholder values are never
    inspected. Code, URL, identifier, and other full-value placeholders are opaque
    barriers; paired tag-boundary placeholders are transparent only when their
    exact ``ProtectedToken`` metadata is supplied.
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

    Removing tag tokens in the matching projection makes paired inline markers
    transparent, so a term such as ``Spring <em>AI</em>`` is still one visual
    occurrence. A full code/URL/identifier token contributes an opaque barrier,
    and its protected value remains uninspected.
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
        exact_gloss_ranges = _exact_gloss_ranges(projected, match.end(), gloss)
        ignored_ranges.extend(exact_gloss_ranges)
        for start, end in exact_gloss_ranges:
            removed_positions.update(
                original_positions[index] for index in range(start, end)
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


def _exact_gloss_ranges(
    text: str, start: int, canonical_gloss: str
) -> list[tuple[int, int]]:
    """Return exact canonical entries in one contiguous parenthetical suffix."""
    position = start
    exact: list[tuple[int, int]] = []
    saw_group = False
    previous_was_exact = False
    while True:
        whitespace_start = position
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text) or text[position] != "(":
            return exact
        opening = position
        closing = _balanced_parenthetical_end(text, opening)
        if closing is None:
            return exact
        is_exact = _looks_like_gloss(
            text[opening + 1 : closing - 1], canonical_gloss
        )
        if is_exact:
            removal_start = (
                whitespace_start
                if not saw_group or previous_was_exact
                else opening
            )
            exact.append((removal_start, closing))
        saw_group = True
        previous_was_exact = is_exact
        position = closing


def _balanced_parenthetical_end(text: str, opening: int) -> int | None:
    depth = 0
    for position in range(opening, len(text)):
        character = text[position]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return position + 1
    return None


def _looks_like_gloss(content: str, canonical_gloss: str) -> bool:
    """Recognize only the canonical gloss, never infer from Korean grammar.

    A short Korean parenthetical can be either a gloss or a qualification. The
    current glossary contract has no variant metadata, so destructive inference
    would lose meaning. Future structured variant metadata can extend this exact
    comparison without reintroducing a language-shape heuristic.
    """
    return content == canonical_gloss


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
