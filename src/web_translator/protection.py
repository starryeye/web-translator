"""Exact placeholder protection for non-translatable source fragments."""

from __future__ import annotations

from collections.abc import Sequence
import re

from web_translator.models import ProtectedToken


class ProtectionError(ValueError):
    """Protected placeholders do not form an exact, restorable set."""


_TOKEN_PATTERN = re.compile(r"⟦WT:\d{6}⟧")
_TAG_PARTS = r'''(?:[^<>"']+|"[^"]*"|'[^']*')*'''
_TAG_PATTERN = rf"</?[A-Za-z]{_TAG_PARTS}>|<!--.*?-->"
_OPENING_TAG_PATTERN = re.compile(r"<([A-Za-z][\w:-]*)\b", re.IGNORECASE)
_CLOSING_TAG_PATTERN = re.compile(r"</\s*([A-Za-z][\w:-]*)\b", re.IGNORECASE)
_TRANSLATE_NO_ATTRIBUTE = re.compile(
    r"\btranslate\s*=\s*(?:\"\s*no\s*\"|'\s*no\s*'|no(?=\s|/?>))",
    re.IGNORECASE,
)
_CODE_LIKE_TAGS = {"code", "kbd", "samp", "var", "pre"}
_EXCLUDED_TAGS = {"script", "style", "noscript", "svg", "math"}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_URL_PATTERN = r"https?://[^\s<>\"']+"
_COMMAND_WORD = (
    r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|'''
    r"--?[A-Za-z0-9][\w.-]*(?:=[^\s;,.!?]+)?|[A-Za-z0-9_./:@+~<>=-]+)"
)
_COMMAND_TAIL = rf"(?:\s+{_COMMAND_WORD})*"
_COMMAND_PATTERN = (
    rf"(?-i:\b(?:"
    rf"(?:npm|pnpm|yarn)\s+(?:add|build|ci|exec|install|publish|remove|run|test|update){_COMMAND_TAIL}"
    rf"|npx\s+{_COMMAND_WORD}{_COMMAND_TAIL}"
    rf"|pip3?\s+(?:check|download|freeze|install|list|show|uninstall|wheel){_COMMAND_TAIL}"
    rf"|python3?\s+(?:-m\s+{_COMMAND_WORD}|{_COMMAND_WORD}\.py){_COMMAND_TAIL}"
    rf"|git\s+(?:add|branch|checkout|clone|commit|config|diff|fetch|init|log|merge|pull|push|rebase|"
    rf"restore|show|status|switch|tag){_COMMAND_TAIL}"
    rf"|(?:curl|wget)\s+(?=--?[A-Za-z]|https?://){_COMMAND_WORD}{_COMMAND_TAIL}"
    rf"|docker\s+(?:build|compose|exec|images|logs|ps|pull|push|run|stop){_COMMAND_TAIL}"
    rf"|kubectl\s+(?:apply|create|delete|describe|exec|get|logs|rollout){_COMMAND_TAIL}"
    rf"|cargo\s+(?:add|build|check|clippy|install|publish|run|test){_COMMAND_TAIL}"
    rf"))"
)
_KEYWORD_PATTERN = (
    r"(?-i:\b(?:MUST\s+NOT|SHALL\s+NOT|SHOULD\s+NOT|NOT\s+RECOMMENDED|MUST|REQUIRED|"
    r"SHALL|SHOULD|RECOMMENDED|MAY|OPTIONAL)\b)"
)
_INLINE_PATTERN = re.compile(
    rf"(?P<literal_token>{_TOKEN_PATTERN.pattern})"
    rf"|(?P<tag>{_TAG_PATTERN})"
    rf"|(?P<url>{_URL_PATTERN})"
    rf"|(?P<command>{_COMMAND_PATTERN})"
    rf"|(?P<keyword>{_KEYWORD_PATTERN})",
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)


def protect_fragment(html: str) -> tuple[str, list[ProtectedToken]]:
    """Replace exact protected spans in *html* with deterministic placeholders."""
    tokens: list[ProtectedToken] = []
    rendered: list[str] = []
    position = 0
    next_index = 0
    used_placeholders = set(_TOKEN_PATTERN.findall(html))
    for start, end, kind in _protected_spans(html):
        rendered.append(html[position:start])
        placeholder = f"⟦WT:{next_index:06d}⟧"
        while placeholder in used_placeholders:
            next_index += 1
            placeholder = f"⟦WT:{next_index:06d}⟧"
        used_placeholders.add(placeholder)
        next_index += 1
        tokens.append(ProtectedToken(token=placeholder, kind=kind, value=html[start:end]))
        rendered.append(placeholder)
        position = end
    rendered.append(html[position:])
    return "".join(rendered), tokens


def _protected_spans(html: str) -> list[tuple[int, int, str]]:
    complete = _complete_element_spans(html)
    spans: list[tuple[int, int, str]] = []
    position = 0
    for start, end, kind in complete:
        spans.extend(_inline_spans(html, position, start))
        spans.append((start, end, kind))
        position = end
    spans.extend(_inline_spans(html, position, len(html)))
    return spans


def _complete_element_spans(html: str) -> list[tuple[int, int, str]]:
    stack: list[tuple[str, int, str | None]] = []
    complete: list[tuple[int, int, str]] = []
    for tag_match in re.finditer(_TAG_PATTERN, html, re.IGNORECASE | re.DOTALL):
        markup = tag_match.group()
        closing = _CLOSING_TAG_PATTERN.match(markup)
        if closing is not None:
            name = closing.group(1).lower()
            matching = next(
                (index for index in range(len(stack) - 1, -1, -1) if stack[index][0] == name),
                None,
            )
            if matching is None:
                continue
            _, start, kind = stack[matching]
            del stack[matching:]
            if kind is not None:
                complete.append((start, tag_match.end(), kind))
            continue

        opening = _OPENING_TAG_PATTERN.match(markup)
        if opening is None:
            continue
        name = opening.group(1).lower()
        if name in _VOID_TAGS or markup.rstrip().endswith("/>"):
            continue
        kind: str | None = None
        if name in _CODE_LIKE_TAGS:
            kind = "code"
        elif name in _EXCLUDED_TAGS or _TRANSLATE_NO_ATTRIBUTE.search(markup):
            kind = "excluded"
        stack.append((name, tag_match.start(), kind))

    complete.sort(key=lambda span: (span[0], -span[1]))
    outermost: list[tuple[int, int, str]] = []
    for span in complete:
        if outermost and span[1] <= outermost[-1][1]:
            continue
        outermost.append(span)
    return outermost


def _inline_spans(html: str, start: int, end: int) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in _INLINE_PATTERN.finditer(html[start:end]):
        kind = match.lastgroup or "tag"
        span_start = start + match.start()
        span_end = start + match.end()
        if kind == "url":
            span_end = _trim_url_end(html, span_start, span_end)
        spans.append((span_start, span_end, kind))
    return spans


def _trim_url_end(html: str, start: int, end: int) -> int:
    pairs = {")": "(", "]": "[", "}": "{"}
    while end > start:
        final = html[end - 1]
        if final in ".,;:!?":
            end -= 1
            continue
        opening = pairs.get(final)
        if opening is not None:
            value = html[start:end]
            if value.count(final) > value.count(opening):
                end -= 1
                continue
        break
    return end


def restore_tokens(text: str, tokens: Sequence[ProtectedToken]) -> str:
    """Restore tokens only when *text* contains each expected token exactly once."""
    expected: dict[str, ProtectedToken] = {}
    for protected in tokens:
        if _TOKEN_PATTERN.fullmatch(protected.token) is None:
            raise ProtectionError(f"invalid protected token: {protected.token!r}")
        if protected.token in expected:
            raise ProtectionError(f"duplicate protected token definition: {protected.token}")
        expected[protected.token] = protected

    occurrences = _TOKEN_PATTERN.findall(text)
    foreign = sorted(set(occurrences) - expected.keys())
    if foreign:
        raise ProtectionError(f"foreign protected token: {foreign[0]}")
    for placeholder in expected:
        count = occurrences.count(placeholder)
        if count != 1:
            raise ProtectionError(
                f"protected token {placeholder} must occur exactly once; found {count}"
            )

    return _TOKEN_PATTERN.sub(lambda match: expected[match.group()].value, text)
