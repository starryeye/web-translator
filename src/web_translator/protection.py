"""Exact placeholder protection for non-translatable source fragments."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
import re

from web_translator.models import ProtectedToken


class ProtectionError(ValueError):
    """Protected placeholders do not form an exact, restorable set."""


class _OpeningTagAttributeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.attributes: dict[str, str | None] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.attributes = {name.lower(): value for name, value in attrs}

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


@dataclass(frozen=True, slots=True)
class _ShellToken:
    value: str
    start: int
    end: int
    sentence_final: bool


_TOKEN_PATTERN = re.compile(r"⟦WT:\d{6}⟧")
_RAW_MARKUP_PATTERN = re.compile(r"<\s*(?:/?[A-Za-z]|!|\?)")
_TAG_PARTS = r'''(?:[^<>"']+|"[^"]*"|'[^']*')*'''
_TAG_PATTERN = rf"</?[A-Za-z]{_TAG_PARTS}>|<!--.*?-->"
_OPENING_TAG_PATTERN = re.compile(r"<([A-Za-z][\w:-]*)\b", re.IGNORECASE)
_CLOSING_TAG_PATTERN = re.compile(r"</\s*([A-Za-z][\w:-]*)\b", re.IGNORECASE)
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
_COMMAND_EXECUTABLE_PATTERN = re.compile(
    r"\b(?:npm|npx|pnpm|yarn|pip3?|python3?|git|curl|wget|docker|kubectl|cargo|make|go|dotnet|uv)\b"
)
_SHELL_TOKEN_PATTERN = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s;,!?]+''')
_ASSIGNMENT_PATTERN = re.compile(r"[A-Za-z_]\w*=.+")
_PATHLIKE_PATTERN = re.compile(r"(?:\./\.\.\.|.*[/:@+~<>=-].*|.+\.[A-Za-z0-9].*)")
_MAKE_START_TARGETS = {"all", "build", "clean", "install", "test"}
_GO_MOD_SUBCOMMANDS = {"download", "edit", "graph", "init", "tidy", "vendor", "verify"}
_DOCKER_COMPOSE_SUBCOMMANDS = {
    "build",
    "config",
    "create",
    "down",
    "exec",
    "logs",
    "pull",
    "push",
    "restart",
    "run",
    "start",
    "stop",
    "up",
}
_SUBCOMMANDS = {
    "npm": {"add", "build", "ci", "exec", "install", "publish", "remove", "run", "test", "update"},
    "pnpm": {"add", "build", "exec", "install", "publish", "remove", "run", "test", "update"},
    "yarn": {"add", "build", "install", "publish", "remove", "run", "test", "up"},
    "pip": {"check", "download", "freeze", "install", "list", "show", "uninstall", "wheel"},
    "git": {
        "add", "branch", "checkout", "clone", "commit", "config", "diff", "fetch", "init",
        "log", "merge", "pull", "push", "rebase", "restore", "show", "status", "switch", "tag",
    },
    "docker": {"build", "compose", "exec", "images", "logs", "ps", "pull", "push", "run", "stop"},
    "kubectl": {"apply", "create", "delete", "describe", "exec", "get", "logs", "rollout"},
    "cargo": {"add", "build", "check", "clippy", "install", "publish", "run", "test"},
    "go": {
        "build", "clean", "env", "fmt", "generate", "get", "install", "mod", "run", "test",
        "tool", "version", "vet",
    },
    "dotnet": {"add", "build", "clean", "format", "new", "pack", "publish", "restore", "run", "test", "tool"},
    "uv": {"add", "build", "export", "lock", "pip", "publish", "remove", "run", "sync", "tool", "venv"},
}
_REQUIRED_PLAIN_OPERANDS = {
    ("npm", "add"): 1, ("npm", "exec"): 1, ("npm", "remove"): 1, ("npm", "run"): 1,
    ("pnpm", "add"): 1, ("pnpm", "exec"): 1, ("pnpm", "publish"): 1,
    ("pnpm", "remove"): 1, ("pnpm", "run"): 1,
    ("yarn", "add"): 1, ("yarn", "publish"): 1,
    ("yarn", "remove"): 1, ("yarn", "run"): 1,
    ("pip", "download"): 1, ("pip", "install"): 1, ("pip", "show"): 1,
    ("pip", "uninstall"): 1, ("pip", "wheel"): 1,
    ("git", "add"): 1, ("git", "checkout"): 1, ("git", "clone"): 1,
    ("git", "restore"): 1, ("git", "switch"): 1,
    ("docker", "exec"): 1,
    ("docker", "logs"): 1, ("docker", "pull"): 1, ("docker", "push"): 1,
    ("docker", "run"): 1, ("docker", "stop"): 1,
    ("kubectl", "apply"): 1, ("kubectl", "create"): 1, ("kubectl", "delete"): 1,
    ("kubectl", "describe"): 1, ("kubectl", "exec"): 1, ("kubectl", "get"): 1,
    ("kubectl", "logs"): 1, ("kubectl", "rollout"): 1,
    ("cargo", "add"): 1, ("cargo", "install"): 1, ("cargo", "publish"): 1,
    ("uv", "add"): 1, ("uv", "pip"): 1, ("uv", "remove"): 1,
    ("uv", "run"): 1, ("uv", "tool"): 1,
}
_OPTIONAL_PLAIN_OPERANDS = {
    ("npm", "install"): 1,
    ("npm", "publish"): 1,
    ("pnpm", "install"): 1,
    ("yarn", "install"): 1,
}
_VALUED_OPTIONS = {
    "go": {"-count", "-cpu", "-parallel", "-run", "-timeout"},
    "dotnet": {"-c", "--configuration", "-f", "--framework", "-o", "--output"},
    "uv": {"-o", "--output-file", "--python"},
    "git": {"-m", "-n", "--author", "--file", "--format"},
    "make": {"-C", "-f", "-j", "--directory", "--file", "--jobs"},
    "docker": {"-f", "--file", "--project-directory", "--project-name"},
}
# Closed-class words that introduce an English clause or adjunct after a command.
# The argument parser consults them only after all required operands are present.
_PROSE_CONNECTORS = {"and", "but", "nor", "or", "so", "then", "yet"}
_PROSE_CLAUSE_MARKERS = {
    "after",
    "although",
    "because",
    "before",
    "if",
    "once",
    "since",
    "that",
    "to",
    "unless",
    "until",
    "when",
    "whenever",
    "where",
    "whereas",
    "while",
    "which",
    "who",
    "whose",
}
_PROSE_ADJUNCT_MARKERS = {
    "against",
    "around",
    "as",
    "at",
    "by",
    "despite",
    "during",
    "for",
    "from",
    "in",
    "into",
    "near",
    "on",
    "over",
    "through",
    "under",
    "using",
    "via",
    "with",
    "without",
}
_COMMAND_META_NOUNS = {"command", "commands", "example", "option", "options"}
_POLITE_BOUNDARIES = {"please"}
_GIT_GLOBAL_VALUED_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree"}
_KEYWORD_PATTERN = (
    r"(?-i:\b(?:MUST\s+NOT|SHALL\s+NOT|SHOULD\s+NOT|NOT\s+RECOMMENDED|MUST|REQUIRED|"
    r"SHALL|SHOULD|RECOMMENDED|MAY|OPTIONAL)\b)"
)
_STRUCTURAL_INLINE_PATTERN = re.compile(
    rf"(?P<literal_token>{_TOKEN_PATTERN.pattern})|(?P<tag>{_TAG_PATTERN})",
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)
_TEXT_INLINE_PATTERN = re.compile(
    rf"(?P<url>{_URL_PATTERN})|(?P<keyword>{_KEYWORD_PATTERN})",
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
        attributes = _opening_attributes(markup)
        kind: str | None = None
        if "data-wt-segment" in attributes:
            kind = "segment"
        elif name in _CODE_LIKE_TAGS:
            kind = "code"
        elif name in _EXCLUDED_TAGS or str(attributes.get("translate", "")).strip().lower() == "no":
            kind = "excluded"
        stack.append((name, tag_match.start(), kind))

    complete.sort(key=lambda span: (span[0], -span[1]))
    outermost: list[tuple[int, int, str]] = []
    for span in complete:
        if outermost and span[1] <= outermost[-1][1]:
            continue
        outermost.append(span)
    return outermost


def _opening_attributes(markup: str) -> dict[str, str | None]:
    parser = _OpeningTagAttributeParser()
    parser.feed(markup)
    parser.close()
    return parser.attributes


def _inline_spans(html: str, start: int, end: int) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    position = start
    for match in _STRUCTURAL_INLINE_PATTERN.finditer(html[start:end]):
        span_start = start + match.start()
        span_end = start + match.end()
        spans.extend(_visible_text_spans(html, position, span_start))
        spans.append((span_start, span_end, match.lastgroup or "tag"))
        position = span_end
    spans.extend(_visible_text_spans(html, position, end))
    return spans


def _visible_text_spans(html: str, start: int, end: int) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    position = start
    for command_start, command_end, kind in _command_spans(html, start, end):
        spans.extend(_text_pattern_spans(html, position, command_start))
        spans.append((command_start, command_end, kind))
        position = command_end
    spans.extend(_text_pattern_spans(html, position, end))
    return spans


def _text_pattern_spans(html: str, start: int, end: int) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in _TEXT_INLINE_PATTERN.finditer(html[start:end]):
        kind = match.lastgroup or "tag"
        span_start = start + match.start()
        span_end = start + match.end()
        if kind == "url":
            span_end = _trim_url_end(html, span_start, span_end)
        spans.append((span_start, span_end, kind))
    return spans


def _command_spans(html: str, start: int, end: int) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    consumed_until = start
    for executable in _COMMAND_EXECUTABLE_PATTERN.finditer(html[start:end]):
        command_start = start + executable.start()
        if command_start < consumed_until:
            continue
        tokens = _shell_tokens(html, command_start, end)
        consumed = _parse_command(html, tokens)
        if consumed is None:
            continue
        command_end = tokens[consumed - 1].end
        spans.append((command_start, command_end, "command"))
        consumed_until = command_end
    return spans


def _shell_tokens(html: str, start: int, end: int) -> list[_ShellToken]:
    tokens: list[_ShellToken] = []
    cursor = start
    while cursor < end:
        while cursor < end and html[cursor].isspace():
            cursor += 1
        match = _SHELL_TOKEN_PATTERN.match(html, cursor, end)
        if match is None:
            break
        raw = match.group()
        value = raw
        token_end = match.end()
        sentence_final = value.endswith(".") and not value.endswith("...")
        if sentence_final:
            value = value[:-1]
            token_end -= 1
        if not value:
            break
        tokens.append(
            _ShellToken(
                value=value,
                start=match.start(),
                end=token_end,
                sentence_final=sentence_final,
            )
        )
        cursor = match.end()
    return tokens


def _parse_command(html: str, tokens: list[_ShellToken]) -> int | None:
    if not tokens:
        return None
    executable = tokens[0].value
    normalized = "pip" if executable in {"pip", "pip3"} else executable
    if executable in {"python", "python3"}:
        return _parse_python_command(tokens)
    if normalized == "make":
        return _parse_make_command(html, tokens)
    if normalized == "git":
        return _parse_git_command(tokens)
    if normalized == "docker":
        return _parse_docker_command(tokens)
    if normalized == "npx":
        return _consume_command_arguments(
            tokens, 1, normalized, required_plain=1, plain_tail_limit=1
        )
    if normalized in {"curl", "wget"}:
        return _consume_command_arguments(tokens, 1, normalized, required_plain=1)
    if len(tokens) < 2:
        return None
    subcommand = tokens[1].value
    if subcommand not in _SUBCOMMANDS.get(normalized, set()):
        return None
    if normalized == "uv" and subcommand == "pip":
        return _parse_uv_pip_command(tokens)
    if normalized == "go" and subcommand == "mod":
        return _parse_go_mod_command(tokens)
    required_plain = _REQUIRED_PLAIN_OPERANDS.get((normalized, subcommand), 0)
    optional_plain = _OPTIONAL_PLAIN_OPERANDS.get((normalized, subcommand), 0)
    if normalized == "dotnet" and subcommand == "add":
        required_plain = 2
    return _consume_command_arguments(
        tokens,
        2,
        normalized,
        required_plain=required_plain,
        optional_plain=optional_plain,
    )


def _parse_git_command(tokens: list[_ShellToken]) -> int | None:
    cursor = 1
    while cursor < len(tokens) and _is_option(tokens[cursor].value):
        option = tokens[cursor].value
        cursor += 1
        option_name = option.split("=", 1)[0]
        if option_name in _GIT_GLOBAL_VALUED_OPTIONS and "=" not in option:
            if cursor >= len(tokens):
                return None
            cursor += 1
    if cursor >= len(tokens):
        return None
    subcommand = tokens[cursor].value
    if subcommand not in _SUBCOMMANDS["git"]:
        return None
    cursor += 1
    required_plain = _REQUIRED_PLAIN_OPERANDS.get(("git", subcommand), 0)
    if subcommand == "config":
        required_plain = 2
    return _consume_command_arguments(
        tokens,
        cursor,
        "git",
        required_plain=required_plain,
    )


def _parse_go_mod_command(tokens: list[_ShellToken]) -> int | None:
    if len(tokens) < 3 or tokens[2].value not in _GO_MOD_SUBCOMMANDS:
        return None
    return _consume_command_arguments(tokens, 3, "go", required_plain=0)


def _parse_docker_command(tokens: list[_ShellToken]) -> int | None:
    if len(tokens) < 2:
        return None
    subcommand = tokens[1].value
    if subcommand not in _SUBCOMMANDS["docker"]:
        return None
    if subcommand == "compose":
        cursor = _consume_leading_options(tokens, 2, "docker")
        if cursor is None or cursor >= len(tokens):
            return None
        if tokens[cursor].value not in _DOCKER_COMPOSE_SUBCOMMANDS:
            return None
        return _consume_command_arguments(
            tokens,
            cursor + 1,
            "docker",
            required_plain=0,
        )
    required_plain = _REQUIRED_PLAIN_OPERANDS.get(("docker", subcommand), 0)
    return _consume_command_arguments(
        tokens,
        2,
        "docker",
        required_plain=required_plain,
        plain_tail_limit=2 if subcommand in {"exec", "run"} else 0,
    )


def _consume_leading_options(
    tokens: list[_ShellToken], start: int, executable: str
) -> int | None:
    cursor = start
    valued_options = _VALUED_OPTIONS.get(executable, set())
    while cursor < len(tokens) and _is_option(tokens[cursor].value):
        option = tokens[cursor].value
        cursor += 1
        option_name = option.split("=", 1)[0]
        if option_name in valued_options and "=" not in option:
            if cursor >= len(tokens):
                return None
            cursor += 1
    return cursor


def _parse_python_command(tokens: list[_ShellToken]) -> int | None:
    if len(tokens) < 2:
        return None
    if tokens[1].value == "-m":
        return _consume_command_arguments(tokens, 2, "python", required_plain=1)
    if not tokens[1].value.endswith(".py"):
        return None
    return _consume_command_arguments(tokens, 1, "python", required_plain=1)


def _parse_uv_pip_command(tokens: list[_ShellToken]) -> int | None:
    if len(tokens) < 3:
        return None
    subcommand = tokens[2].value
    if subcommand not in _SUBCOMMANDS["pip"]:
        return None
    required_plain = _REQUIRED_PLAIN_OPERANDS.get(("pip", subcommand), 0)
    return _consume_command_arguments(
        tokens,
        3,
        "uv",
        required_plain=required_plain,
    )


def _parse_make_command(html: str, tokens: list[_ShellToken]) -> int | None:
    prefix = html[: tokens[0].start]
    cued = prefix.endswith("Run ")
    at_text_start = not prefix.strip()
    if not cued and not at_text_start:
        return None
    if len(tokens) == 1:
        return 1
    if not cued:
        first_argument = tokens[1].value
        if first_argument not in _MAKE_START_TARGETS and not _is_option(first_argument):
            return None

    consumed = 1
    valued_options = _VALUED_OPTIONS["make"]
    while consumed < len(tokens):
        value = tokens[consumed].value
        if _is_prose_boundary(value):
            break
        if _is_option(value):
            consumed += 1
            if value.split("=", 1)[0] in valued_options and "=" not in value:
                if consumed >= len(tokens):
                    break
                consumed += 1
            continue
        if _ASSIGNMENT_PATTERN.fullmatch(value):
            consumed += 1
            continue
        if value in _MAKE_START_TARGETS or _PATHLIKE_PATTERN.fullmatch(value):
            consumed += 1
            continue
        break
    return consumed


def _consume_command_arguments(
    tokens: list[_ShellToken],
    start: int,
    executable: str,
    *,
    required_plain: int,
    optional_plain: int = 0,
    plain_tail_limit: int = 0,
) -> int | None:
    consumed = start
    plain_count = 0
    plain_tail_remaining = plain_tail_limit
    valued_options = _VALUED_OPTIONS.get(executable, set())
    while consumed < len(tokens):
        value = tokens[consumed].value
        if plain_count >= required_plain and _is_prose_boundary(value):
            break
        if _is_option(value):
            consumed += 1
            option_name = value.split("=", 1)[0]
            if option_name in valued_options and "=" not in value:
                if consumed >= len(tokens):
                    break
                consumed += 1
            continue
        if plain_count < required_plain:
            plain_count += 1
            consumed += 1
            continue
        if optional_plain:
            token = tokens[consumed]
            if token.sentence_final and not (
                _is_quoted(value) or _PATHLIKE_PATTERN.fullmatch(value)
            ):
                break
            optional_plain -= 1
            consumed += 1
            continue
        if plain_tail_remaining:
            plain_tail_remaining -= 1
            consumed += 1
            continue
        if _is_quoted(value) or _PATHLIKE_PATTERN.fullmatch(value):
            consumed += 1
            continue
        break
    return consumed if plain_count == required_plain else None


def _is_prose_boundary(value: str) -> bool:
    if _is_quoted(value):
        return False
    lower = value.lower()
    return (
        lower in _PROSE_CONNECTORS
        or lower in _PROSE_CLAUSE_MARKERS
        or lower in _PROSE_ADJUNCT_MARKERS
        or lower in _COMMAND_META_NOUNS
        or lower in _POLITE_BOUNDARIES
        or (lower.isalpha() and lower.endswith("ly"))
    )


def _is_option(value: str) -> bool:
    return len(value) > 1 and value.startswith("-")


def _is_quoted(value: str) -> bool:
    return len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}


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

    model_text = _TOKEN_PATTERN.sub("", text)
    if _RAW_MARKUP_PATTERN.search(model_text):
        raise ProtectionError("translated text must not introduce markup")
    _validate_tag_boundaries(text, expected)
    return _TOKEN_PATTERN.sub(lambda match: expected[match.group()].value, text)


def _validate_tag_boundaries(text: str, expected: dict[str, ProtectedToken]) -> None:
    stack: list[str] = []
    for match in _TOKEN_PATTERN.finditer(text):
        protected = expected[match.group()]
        if protected.kind != "tag":
            continue
        markup = protected.value
        if markup.startswith("<!--"):
            continue
        if re.fullmatch(_TAG_PATTERN, markup, re.IGNORECASE | re.DOTALL) is None:
            raise ProtectionError(f"invalid tag boundary: {markup}")
        closing = _CLOSING_TAG_PATTERN.match(markup)
        if closing is not None:
            name = closing.group(1).lower()
            if not stack or stack[-1] != name:
                raise ProtectionError(f"invalid closing tag boundary: {markup}")
            stack.pop()
            continue
        opening = _OPENING_TAG_PATTERN.match(markup)
        if opening is None:
            raise ProtectionError(f"invalid tag boundary: {markup}")
        name = opening.group(1).lower()
        if name not in _VOID_TAGS and not markup.rstrip().endswith("/>"):
            stack.append(name)
    if stack:
        raise ProtectionError(f"unclosed tag boundary: <{stack[-1]}>")
