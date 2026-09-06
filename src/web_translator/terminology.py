"""Document-wide, boundary-aware normalization of technical terminology."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
import re
from typing import Callable, Literal

from web_translator.models import ProtectedToken, Translation


_TOKEN_PATTERN = re.compile(r"⟦WT:\d{6}⟧")
_KOREAN_PATTERN = re.compile(r"[가-힣]")
_IDENTIFIER_CHARACTER = r"A-Za-z0-9_"


class TerminologyError(ValueError):
    """A glossary or translation cannot be normalized safely."""


TerminologyDisplayPolicy = Literal["english-first", "korean-first"]


def normalize_terminology(
    ordered: Sequence[Translation],
    glossary: Mapping[str, str],
    *,
    policy: TerminologyDisplayPolicy,
    protected_by_segment: Mapping[str, Sequence[ProtectedToken]] | None = None,
) -> list[Translation]:
    """Render canonical glossary terms according to one explicit display policy."""
    if policy not in {"english-first", "korean-first"}:
        raise TerminologyError(f"unsupported terminology display policy: {policy}")
    first_format: Callable[[str, str], str] = (
        (lambda term, gloss: f"{term}({gloss})")
        if policy == "english-first"
        else (lambda term, gloss: f"{gloss}({term})")
    )
    later_format: Callable[[str, str], str] = (
        (lambda term, _gloss: term)
        if policy == "english-first"
        else (lambda _term, gloss: gloss)
    )
    return _normalize_policy_records(
        ordered,
        glossary,
        protected_by_segment=protected_by_segment,
        first_format=first_format,
        later_format=later_format,
    )


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
    return normalize_terminology(
        ordered,
        glossary,
        policy="english-first",
        protected_by_segment=protected_by_segment,
    )


def _normalize_policy_records(
    ordered: Sequence[Translation],
    glossary: Mapping[str, str],
    *,
    protected_by_segment: Mapping[str, Sequence[ProtectedToken]] | None,
    first_format: Callable[[str, str], str],
    later_format: Callable[[str, str], str],
) -> list[Translation]:
    records = list(ordered)
    if any(not isinstance(record, Translation) for record in records):
        raise TerminologyError("ordered records must contain Translation values")
    terms = _validated_glossary(glossary)
    if not terms:
        return records

    replaceable_terms = [
        (term, gloss)
        for term, gloss in terms
        if not any(character.isdigit() for character in term)
    ]
    term_pattern: re.Pattern[str] | None = None
    if replaceable_terms:
        alternatives = "|".join(re.escape(term) for term, _ in replaceable_terms)
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
                    first_format,
                    later_format,
                ),
            )
        )
    return normalized


def _normalize_record(
    text: str,
    term_pattern: re.Pattern[str] | None,
    canonical: Mapping[str, str],
    seen: set[str],
    transparent_tokens: set[str],
    first_format: Callable[[str, str], str],
    later_format: Callable[[str, str], str],
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

    pair_candidates: list[tuple[int, int, str]] = []
    for term, gloss in canonical.items():
        if any(character.isdigit() for character in term):
            continue
        pair = f"{gloss}({term})"
        start = 0
        while (found := projected.find(pair, start)) >= 0:
            pair_candidates.append((found, found + len(pair), term))
            start = found + 1
    pair_candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    pairs: list[tuple[int, int, str]] = []
    for candidate in pair_candidates:
        if any(
            candidate[0] < end and start < candidate[1]
            for start, end, _term in pairs
        ):
            continue
        pairs.append(candidate)

    canonical_gloss_ranges: list[tuple[int, int]] = []
    for gloss in canonical.values():
        start = 0
        while (found := projected.find(gloss, start)) >= 0:
            canonical_gloss_ranges.append((found, found + len(gloss)))
            start = found + 1

    candidates: list[
        tuple[int, int, str, list[tuple[int, int]], Literal["pair", "term"]]
    ] = [
        (start, end, term, [], "pair") for start, end, term in pairs
    ]
    ignored_ranges: list[tuple[int, int]] = []
    if term_pattern is not None:
        for match in term_pattern.finditer(projected):
            match_range = (match.start(), match.end())
            if any(
                match.start() < end and start < match.end()
                for start, end, _term in pairs
            ):
                continue
            if any(
                match.start() < end and start < match.end()
                for start, end in canonical_gloss_ranges
            ):
                continue
            if any(start <= match.start() < end for start, end in ignored_ranges):
                continue
            term = match.group("term")
            exact_gloss_ranges = _exact_gloss_ranges(
                projected, match.end(), canonical[term]
            )
            ignored_ranges.extend(exact_gloss_ranges)
            candidates.append(
                (*match_range, term, exact_gloss_ranges, "term")
            )

    replacements: list[tuple[int, int, str]] = []
    for start, end, term, gloss_ranges, kind in sorted(candidates):
        gloss = canonical[term]
        first_occurrence = term not in seen
        seen.add(term)
        original_start, original_end = _original_span(
            original_positions, start, end, len(text)
        )
        source_fragment = text[original_start:original_end]
        if kind == "pair":
            replacement = (
                first_format(term, gloss)
                if first_occurrence
                else later_format(term, gloss)
            )
        else:
            replacement = (
                first_format(source_fragment, gloss)
                if first_occurrence
                else later_format(source_fragment, gloss)
            )
        replacement = _retain_placeholders(source_fragment, replacement)
        replacements.append((original_start, original_end, replacement))
        for gloss_start, gloss_end in gloss_ranges:
            removed_start, removed_end = _original_span(
                original_positions, gloss_start, gloss_end, len(text)
            )
            replacements.append((removed_start, removed_end, ""))

    if not replacements:
        return text
    replacements.sort()
    for previous, current in zip(replacements, replacements[1:]):
        if previous[1] > current[0]:
            raise TerminologyError("terminology replacements overlap")

    rebuilt: list[str] = []
    cursor = 0
    for start, end, replacement in replacements:
        rebuilt.append(text[cursor:start])
        rebuilt.append(replacement)
        cursor = end
    rebuilt.append(text[cursor:])
    return "".join(rebuilt)


def _original_span(
    original_positions: Sequence[int],
    start: int,
    end: int,
    original_length: int,
) -> tuple[int, int]:
    original_start = original_positions[start]
    original_end = (
        original_positions[end] if end < len(original_positions) else original_length
    )
    return original_start, original_end


def _retain_placeholders(source: str, replacement: str) -> str:
    """Keep transparent boundary tokens exact when the selected form omits English."""
    missing = [
        match.group()
        for match in _TOKEN_PATTERN.finditer(source)
        if match.group() not in replacement
    ]
    return replacement + "".join(missing)


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
