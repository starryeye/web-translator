"""Deterministic structural, offline, and browser QA for translated pages."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Route, WebSocketRoute, sync_playwright
import tinycss2
from tinycss2.ast import AtRule, FunctionBlock, StringToken, URLToken

from web_translator.models import Finding, ProtectedToken, QAInputs, QAResult


_TOKEN_PATTERN = re.compile(r"⟦WT:\d{6}⟧")
_VIEWPORTS = (("desktop-1440x900", 1440, 900), ("narrow-390x844", 390, 844))
_BROWSER_METRICS = """({
  horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  brokenImages: [...document.images].filter(img => !img.complete || img.naturalWidth === 0).map(img => img.src),
  clippedText: [...document.querySelectorAll('main *')].filter(el => el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).overflowX === 'hidden').length
})"""
_URL_ATTRIBUTES = {
    "a": ("href",),
    "audio": ("src",),
    "embed": ("src",),
    "iframe": ("src",),
    "img": ("src", "srcset"),
    "link": ("href",),
    "object": ("data",),
    "script": ("src",),
    "source": ("src", "srcset"),
    "video": ("src", "poster"),
}
_OPTIONAL_TAGS = {"audio", "img", "source", "video"}
_REPARSE_POINT = 0x400
_END_GUARD = "\x00END"


def run_qa(inputs: QAInputs) -> QAResult:
    """Run stable preflight checks, followed by isolated Chromium QA."""
    if not isinstance(inputs, QAInputs):
        raise TypeError("inputs must be QAInputs")

    required: list[Finding] = []
    warnings: list[Finding] = []
    screenshots: list[Path] = []
    browser_metrics: dict[str, dict[str, object]] = {}

    _check_coverage(inputs, required)
    source_text = _read_utf8(inputs.source_html, "source-html", required)
    output_text = _read_utf8(inputs.output_html, "output-html", required)
    if source_text is not None and output_text is not None:
        source_soup = BeautifulSoup(source_text, "lxml")
        output_soup = BeautifulSoup(output_text, "lxml")
        _check_tokens(inputs, source_soup, output_soup, output_text, required)
        _check_structure(source_soup, output_soup, required)
        _check_anchors(output_soup, inputs.output_html.name, required)
        _check_external_dependencies(inputs, output_soup, required, warnings)

    _check_assets(inputs, required, warnings)
    _check_master_review(inputs, required)
    screenshot_root = _validated_screenshot_dir(inputs, required)

    # Browser evidence is meaningful only after deterministic prerequisites pass.
    if not required and screenshot_root is not None:
        browser_required, browser_warnings, screenshots, browser_metrics = _run_browser_checks(
            inputs.output_html, screenshot_root
        )
        required.extend(browser_required)
        warnings.extend(browser_warnings)

    required = _coalesce_findings(required)
    warnings = _coalesce_findings(warnings)

    return QAResult(
        passed=not required and not inputs.master_review.unresolved_required,
        required_findings=required,
        warnings=warnings,
        screenshots=screenshots,
        source_url=inputs.source_url,
        browser_metrics=browser_metrics,
        capture_metadata=dict(inputs.capture_metadata),
    )


def _coalesce_findings(findings: list[Finding]) -> list[Finding]:
    result: list[Finding] = []
    positions: dict[tuple[str, str], int] = {}
    for finding in findings:
        key = (finding.severity, finding.code)
        position = positions.get(key)
        if position is None:
            positions[key] = len(result)
            result.append(finding)
            continue
        previous = result[position]
        evidence = dict(previous.evidence)
        for name, value in finding.evidence.items():
            old_value = evidence.get(name)
            if isinstance(old_value, list) and isinstance(value, list):
                evidence[name] = sorted(set(old_value) | set(value))
            elif old_value is None:
                evidence[name] = value
            elif old_value != value:
                evidence[name] = [old_value, value]
        result[position] = Finding(
            previous.code, previous.severity, previous.message, evidence
        )
    return result


def _required(code: str, message: str, evidence: dict[str, object]) -> Finding:
    return Finding(code, "required", message, evidence)


def _warning(code: str, message: str, evidence: dict[str, object]) -> Finding:
    return Finding(code, "warning", message, evidence)


def _check_coverage(inputs: QAInputs, findings: list[Finding]) -> None:
    missing = sorted(inputs.source_segment_ids - inputs.translated_segment_ids)
    foreign = sorted(inputs.translated_segment_ids - inputs.source_segment_ids)
    if missing or foreign:
        findings.append(
            _required(
                "translation-coverage",
                "Translated segment IDs do not exactly cover source segment IDs.",
                {"foreign": foreign, "missing": missing},
            )
        )


def _read_utf8(path: Path, label: str, findings: list[Finding]) -> str | None:
    try:
        if _is_link_or_reparse(path) or not path.is_file():
            raise OSError("file is missing or unsafe")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        findings.append(
            _required(
                f"{label}-unreadable",
                f"{label} must be a safe UTF-8 file.",
                {"path": str(path), "reason": str(error)},
            )
        )
        return None


def _check_tokens(
    inputs: QAInputs,
    source_soup: BeautifulSoup,
    output_soup: BeautifulSoup,
    output_text: str,
    findings: list[Finding],
) -> None:
    changed: set[str] = set()
    for segment_id in sorted(inputs.protected_tokens):
        tokens = inputs.protected_tokens[segment_id]
        text = inputs.translated_texts.get(segment_id)
        if text is None or not _has_exact_tokens(text, tokens):
            changed.add(segment_id)
            continue
        source_element = source_soup.find(attrs={"data-wt-segment": segment_id})
        output_element = _corresponding_output_element(
            source_soup.body, output_soup.body, source_element
        )
        if not isinstance(source_element, Tag) or output_element is None:
            changed.add(segment_id)
            continue
        source_fragment = source_element.decode_contents()
        output_fragment = output_element.decode_contents()
        source_expectations: Counter[tuple[str, str]] = Counter()
        output_expectations: Counter[tuple[str, str, str | None]] = Counter()
        for token in tokens:
            if token.kind == "tag":
                continue
            normalized = _normalized_fragment(token.value)
            if not normalized:
                changed.add(segment_id)
                break
            source_expectations[(normalized, token.kind)] += 1
            guard = _following_token_guard(text, token.token, tokens)
            output_expectations[(normalized, token.kind, guard)] += 1
        if segment_id in changed:
            continue
        if any(
            _exact_fragment_count(source_fragment, value, kind) != count
            for (value, kind), count in source_expectations.items()
        ) or any(
            _guarded_fragment_count(output_fragment, value, kind, guard) != count
            for (value, kind, guard), count in output_expectations.items()
        ):
            changed.add(segment_id)
    leaked = sorted(set(_TOKEN_PATTERN.findall(output_text)))
    if changed or leaked:
        findings.append(
            _required(
                "protected-token-integrity",
                "Protected placeholders changed or leaked into the assembled page.",
                {"changed_segments": sorted(changed), "leaked_placeholders": leaked},
            )
        )


def _has_exact_tokens(text: str, tokens: Iterable[ProtectedToken]) -> bool:
    expected: list[str] = []
    for token in tokens:
        if not isinstance(token, ProtectedToken) or _TOKEN_PATTERN.fullmatch(token.token) is None:
            return False
        expected.append(token.token)
    if len(expected) != len(set(expected)):
        return False
    actual = _TOKEN_PATTERN.findall(text)
    return sorted(actual) == sorted(expected) and all(text.count(token) == 1 for token in expected)


def _normalized_fragment(value: str) -> str:
    if "<" not in value and "&" not in value:
        return value
    return BeautifulSoup(value, "html.parser").decode_contents()


def _exact_fragment_count(fragment: str, value: str, kind: str) -> int:
    lexical = kind in {"command", "identifier", "keyword", "url"}
    prefix = (
        r"(?<![A-Za-z0-9_])"
        if lexical or (value[0].isascii() and value[0].isalnum())
        else ""
    )
    if kind == "url":
        suffix = r"(?![A-Za-z0-9_~:/?#\[\]@!$&'()*+=%_-]|[.,;](?=[A-Za-z0-9]))"
    else:
        suffix = (
            r"(?![A-Za-z0-9_])"
            if lexical or (value[-1].isascii() and value[-1].isalnum())
            else ""
        )
    return len(re.findall(prefix + re.escape(value) + suffix, fragment))


def _guarded_fragment_count(
    fragment: str, value: str, kind: str, guard: str | None
) -> int:
    lexical = kind in {"command", "identifier", "keyword", "url"}
    prefix = (
        r"(?<![A-Za-z0-9_])"
        if lexical or (value[0].isascii() and value[0].isalnum())
        else ""
    )
    if guard == _END_GUARD:
        suffix = r"(?=$)"
    elif guard:
        suffix = rf"(?={re.escape(guard)})"
    elif kind == "url":
        suffix = r"(?![A-Za-z0-9_~:/?#\[\]@!$&'()*+,;=%-])"
    else:
        suffix = r"(?![A-Za-z0-9_])" if lexical else ""
    return len(re.findall(prefix + re.escape(value) + suffix, fragment))


def _following_token_guard(
    text: str, placeholder: str, tokens: Iterable[ProtectedToken]
) -> str | None:
    start = text.index(placeholder) + len(placeholder)
    after = text[start:]
    following: list[tuple[int, ProtectedToken]] = []
    for token in tokens:
        position = after.find(token.token)
        if position >= 0:
            following.append((position, token))
    if following:
        position, next_token = min(following, key=lambda item: item[0])
        literal = _normalized_fragment(after[:position])
        if literal:
            return literal[0]
        normalized_next = _normalized_fragment(next_token.value)
        return normalized_next[0] if normalized_next else None
    literal = _normalized_fragment(after)
    return literal[0] if literal else _END_GUARD


def _corresponding_output_element(
    source_body: Tag | None, output_body: Tag | None, source_element: Tag | None
) -> Tag | None:
    if source_body is None or output_body is None or source_element is None:
        return None
    indices: list[int] = []
    current = source_element
    while current is not source_body:
        parent = current.parent
        if not isinstance(parent, Tag):
            return None
        siblings = [child for child in parent.children if isinstance(child, Tag)]
        indices.append(siblings.index(current))
        current = parent
    candidate = output_body
    for index in reversed(indices):
        children = [
            child
            for child in candidate.children
            if isinstance(child, Tag) and not child.has_attr("data-wt-attribution")
        ]
        if index >= len(children):
            return None
        candidate = children[index]
    return candidate


def _check_structure(
    source: BeautifulSoup, output: BeautifulSoup, findings: list[Finding]
) -> None:
    if source.body is None or output.body is None:
        findings.append(
            _required(
                "structural-signature",
                "Source and output must both contain a body element.",
                {"output_body": output.body is not None, "source_body": source.body is not None},
            )
        )
        return
    source_signature = _tag_signature(source.body, ignore_attribution_children=False)
    output_signature = _tag_signature(output.body, ignore_attribution_children=True)
    if source_signature != output_signature:
        findings.append(
            _required(
                "structural-signature",
                "Captured content tag hierarchy or structural attributes changed.",
                {
                    "output_signature": repr(output_signature),
                    "source_signature": repr(source_signature),
                },
            )
        )


def _tag_signature(
    tag: Tag, *, ignore_attribution_children: bool = False
) -> tuple[object, ...]:
    children = []
    for child in tag.children:
        if not isinstance(child, Tag):
            continue
        if ignore_attribution_children and child.has_attr("data-wt-attribution"):
            continue
        children.append(_tag_signature(child))
    attributes = tuple(
        sorted(
            (name.lower(), tuple(value) if isinstance(value, list) else str(value))
            for name, value in tag.attrs.items()
            if name.lower() != "data-wt-segment"
        )
    )
    return (tag.name.lower(), attributes, tuple(children))


def _check_anchors(
    soup: BeautifulSoup, output_name: str, findings: list[Finding]
) -> None:
    identifiers = {
        str(tag.get("id")) for tag in soup.find_all(attrs={"id": True}) if tag.get("id")
    }
    identifiers.update(
        str(tag.get("name"))
        for tag in soup.find_all("a", attrs={"name": True})
        if tag.get("name")
    )
    unresolved: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        parsed = _safe_urlsplit(href)
        if parsed is None or parsed.scheme or parsed.netloc or not parsed.fragment:
            continue
        normalized_path = parsed.path.replace("\\", "/")
        if normalized_path not in {"", ".", "./", output_name, f"./{output_name}"}:
            continue
        fragment = unquote(parsed.fragment)
        if fragment not in identifiers:
            unresolved.add(fragment)
    if unresolved:
        findings.append(
            _required(
                "internal-anchor-unresolved",
                "One or more same-page fragment links have no target.",
                {"fragments": sorted(unresolved)},
            )
        )


def _check_external_dependencies(
    inputs: QAInputs,
    soup: BeautifulSoup,
    required: list[Finding],
    warnings: list[Finding],
) -> None:
    critical_urls: set[str] = set()
    optional_urls: set[str] = set()
    invalid_critical_urls: set[str] = set()
    invalid_optional_urls: set[str] = set()
    base_href: str | None = None
    base_tag = soup.find("base", href=True)
    if isinstance(base_tag, Tag):
        candidate = str(base_tag["href"])
        if _is_external_reference(candidate):
            base_href = candidate
    for tag in soup.find_all(True):
        name = tag.name.lower()
        if name == "link":
            classification = _link_dependency_class(tag)
        elif name in _OPTIONAL_TAGS:
            classification = "optional"
        elif name == "a":
            classification = None
        else:
            classification = "critical"
        if classification is None:
            continue
        for attribute in _URL_ATTRIBUTES.get(name, ()):
            raw_value = tag.get(attribute)
            if raw_value is None:
                continue
            values = _srcset_urls(str(raw_value)) if attribute == "srcset" else [str(raw_value)]
            for value in values:
                resolved, invalid = _resolved_dependency_reference(value, base_href)
                if invalid:
                    if classification == "critical":
                        invalid_critical_urls.add(value)
                    else:
                        invalid_optional_urls.add(value)
                    continue
                if resolved is None:
                    continue
                if classification == "optional":
                    optional_urls.add(resolved)
                else:
                    critical_urls.add(resolved)
    for style in soup.find_all("style"):
        imported, resources, invalid_imported, invalid_resources = _css_external_urls(
            style.get_text(), base_href=base_href
        )
        critical_urls.update(imported)
        optional_urls.update(resources)
        invalid_critical_urls.update(invalid_imported)
        invalid_optional_urls.update(invalid_resources)
    for tag in soup.find_all(style=True):
        _, resources, _, invalid_resources = _css_external_urls(
            str(tag["style"]), declarations=True, base_href=base_href
        )
        optional_urls.update(resources)
        invalid_optional_urls.update(invalid_resources)
    asset_root = inputs.output_html.parent.resolve(strict=False)
    for candidate in inputs.critical_assets:
        resolved = _asset_path(asset_root, candidate)
        if resolved is None or resolved.suffix.lower() != ".css" or not resolved.is_file():
            continue
        try:
            css = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        imported, resources, invalid_imported, invalid_resources = _css_external_urls(css)
        critical_urls.update(imported)
        optional_urls.update(resources)
        invalid_critical_urls.update(invalid_imported)
        invalid_optional_urls.update(invalid_resources)
    if critical_urls:
        required.append(
            _required(
                "external-critical-dependency",
                "Critical layout or content still requires an external network URL.",
                {"urls": sorted(critical_urls)},
            )
        )
    if invalid_critical_urls:
        required.append(
            _required(
                "invalid-critical-dependency-url",
                "A critical dependency contains a malformed URL.",
                {"urls": sorted(invalid_critical_urls)},
            )
        )
    if optional_urls:
        warnings.append(
            _warning(
                "external-optional-dependency",
                "Optional media still uses an external fallback URL.",
                {"urls": sorted(optional_urls)},
            )
        )
    if invalid_optional_urls:
        warnings.append(
            _warning(
                "invalid-optional-dependency-url",
                "An optional dependency contains a malformed URL.",
                {"urls": sorted(invalid_optional_urls)},
            )
        )


def _srcset_urls(value: str) -> list[str]:
    return [candidate.strip().split()[0] for candidate in value.split(",") if candidate.strip()]


def _css_external_urls(
    css: str, *, declarations: bool = False, base_href: str | None = None
) -> tuple[set[str], set[str], set[str], set[str]]:
    imported: set[str] = set()
    resources: set[str] = set()
    invalid_imported: set[str] = set()
    invalid_resources: set[str] = set()
    nodes = (
        tinycss2.parse_declaration_list(css, skip_comments=True, skip_whitespace=True)
        if declarations
        else tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    )
    for node in nodes:
        if isinstance(node, AtRule) and node.lower_at_keyword == "import":
            for token in node.prelude:
                value = _css_url_value(token, allow_string=True)
                if value is None:
                    continue
                resolved, invalid = _resolved_dependency_reference(value, base_href)
                if invalid:
                    invalid_imported.add(value)
                elif resolved is not None:
                    imported.add(resolved)
                break
            if node.content:
                urls, invalid = _external_component_urls(node.content, base_href)
                resources.update(urls)
                invalid_resources.update(invalid)
            continue
        for attribute in ("prelude", "value", "content"):
            tokens = getattr(node, attribute, None)
            if tokens:
                urls, invalid = _external_component_urls(tokens, base_href)
                resources.update(urls)
                invalid_resources.update(invalid)
    return imported, resources, invalid_imported, invalid_resources


def _external_component_urls(
    tokens: Iterable[object], base_href: str | None
) -> tuple[set[str], set[str]]:
    urls: set[str] = set()
    invalid_urls: set[str] = set()
    for token in tokens:
        value = _css_url_value(token, allow_string=False)
        if value is not None:
            resolved, invalid = _resolved_dependency_reference(value, base_href)
            if invalid:
                invalid_urls.add(value)
            elif resolved is not None:
                urls.add(resolved)
        if isinstance(token, FunctionBlock):
            nested_urls, nested_invalid = _external_component_urls(token.arguments, base_href)
            urls.update(nested_urls)
            invalid_urls.update(nested_invalid)
        content = getattr(token, "content", None)
        if content:
            nested_urls, nested_invalid = _external_component_urls(content, base_href)
            urls.update(nested_urls)
            invalid_urls.update(nested_invalid)
    return urls, invalid_urls


def _css_url_value(token: object, *, allow_string: bool) -> str | None:
    if isinstance(token, URLToken):
        return token.value
    if isinstance(token, FunctionBlock) and token.lower_name == "url":
        value = tinycss2.serialize(token.arguments).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    if allow_string and isinstance(token, StringToken):
        return token.value
    return None


def _safe_urlsplit(value: str) -> Any | None:
    try:
        return urlsplit(value)
    except ValueError:
        return None


def _is_external_reference(value: str) -> bool:
    parsed = _safe_urlsplit(value)
    if parsed is None:
        return False
    return parsed.scheme.lower() in {"http", "https"} or (
        not parsed.scheme and bool(parsed.netloc)
    )


def _resolved_dependency_reference(
    value: str, base_href: str | None
) -> tuple[str | None, bool]:
    parsed = _safe_urlsplit(value)
    if parsed is None:
        return None, True
    if parsed.scheme.lower() in {"http", "https"} or (
        not parsed.scheme and parsed.netloc
    ):
        return value, False
    if parsed.scheme or parsed.netloc or value.startswith("#") or base_href is None:
        return None, False
    try:
        resolved = urljoin(base_href, value)
    except ValueError:
        return None, True
    return (resolved, False) if _is_external_reference(resolved) else (None, False)


def _link_dependency_class(tag: Tag) -> str | None:
    rel_value = tag.get("rel", [])
    rels = {
        str(value).lower()
        for value in (rel_value if isinstance(rel_value, list) else str(rel_value).split())
    }
    if "stylesheet" in rels or "modulepreload" in rels:
        return "critical"
    if "icon" in rels:
        return "optional"
    if "preload" in rels:
        return "optional" if str(tag.get("as", "")).lower() in {"font", "image", "audio", "video"} else "critical"
    return None


def _check_assets(
    inputs: QAInputs, required: list[Finding], warnings: list[Finding]
) -> None:
    root = inputs.output_html.parent.resolve(strict=False)
    missing_critical: list[str] = []
    unsafe_critical: list[str] = []
    missing_optional: list[str] = []
    unsafe_optional: list[str] = []
    for candidate in inputs.critical_assets:
        resolved = _asset_path(root, candidate)
        if resolved is None:
            unsafe_critical.append(str(candidate))
        elif not resolved.is_file() or _is_link_or_reparse(resolved):
            missing_critical.append(str(candidate))
    for candidate in inputs.optional_assets:
        resolved = _asset_path(root, candidate)
        if resolved is None:
            unsafe_optional.append(str(candidate))
        elif not resolved.is_file() or _is_link_or_reparse(resolved):
            missing_optional.append(str(candidate))
    if missing_critical:
        required.append(
            _required(
                "critical-asset-missing",
                "One or more critical offline assets are unavailable.",
                {"paths": sorted(missing_critical)},
            )
        )
    if unsafe_critical:
        required.append(
            _required(
                "critical-asset-unsafe",
                "A critical asset path escapes the offline bundle.",
                {"paths": sorted(unsafe_critical)},
            )
        )
    if missing_optional:
        warnings.append(
            _warning(
                "optional-asset-missing",
                "One or more optional image or font assets are unavailable.",
                {"paths": sorted(missing_optional)},
            )
        )
    if unsafe_optional:
        warnings.append(
            _warning(
                "optional-asset-unsafe",
                "An optional asset path escapes the offline bundle.",
                {"paths": sorted(unsafe_optional)},
            )
        )


def _asset_path(root: Path, candidate: Path) -> Path | None:
    try:
        path = candidate if candidate.is_absolute() else root / candidate
        resolved = path.resolve(strict=False)
    except OSError:
        return None
    return resolved if resolved.is_relative_to(root) else None


def _check_master_review(inputs: QAInputs, findings: list[Finding]) -> None:
    unresolved = sorted(set(inputs.master_review.unresolved_required))
    if unresolved:
        findings.append(
            _required(
                "master-review-unresolved",
                "The master semantic review still has required fixes.",
                {"items": unresolved},
            )
        )


def _validated_screenshot_dir(inputs: QAInputs, findings: list[Finding]) -> Path | None:
    work_root = inputs.source_html.parent.resolve(strict=False)
    try:
        target = inputs.screenshot_dir.resolve(strict=False)
    except OSError as error:
        target = None
        reason = str(error)
    else:
        reason = "path escapes the run work directory or uses a link/reparse point"
    if (
        target is None
        or not target.is_relative_to(work_root)
        or _has_link_or_reparse_ancestor(target, work_root)
    ):
        findings.append(
            _required(
                "screenshot-directory-unsafe",
                "Browser screenshots must remain under the run work directory.",
                {"path": str(inputs.screenshot_dir), "reason": reason},
            )
        )
        return None
    return target


def _run_browser_checks(
    output_html: Path, screenshot_dir: Path
) -> tuple[list[Finding], list[Finding], list[Path], dict[str, dict[str, object]]]:
    required: list[Finding] = []
    warnings: list[Finding] = []
    screenshots: list[Path] = []
    metrics: dict[str, dict[str, object]] = {}
    blocked: dict[str, set[str]] = {}
    server: _LoopbackServer | None = None
    try:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        if not screenshot_dir.is_dir() or _is_link_or_reparse(screenshot_dir):
            raise OSError("screenshot destination is not a safe directory")
        server = _LoopbackServer(output_html.parent)
        server.start()
        allowed_origin = _origin(server.url)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for label, width, height in _VIEWPORTS:
                    context = browser.new_context(
                        viewport={"width": width, "height": height},
                        service_workers="block",
                    )

                    def websocket_guard(route: WebSocketRoute) -> None:
                        blocked.setdefault(route.url, set()).add("websocket")
                        # A routed socket does not connect unless connect_to_server()
                        # is called. Leaving it unconnected blocks network I/O without
                        # re-entering Chromium's close handshake from this callback.

                    context.route_web_socket("**/*", websocket_guard)

                    def guard(route: Route) -> None:
                        parsed = urlsplit(route.request.url)
                        if parsed.scheme in {"http", "https"} and _origin(route.request.url) != allowed_origin:
                            blocked.setdefault(route.request.url, set()).add(
                                route.request.resource_type
                            )
                            route.abort()
                        else:
                            route.continue_()

                    context.route("**/*", guard)
                    page = context.new_page()
                    try:
                        response = page.goto(server.url, wait_until="networkidle", timeout=15_000)
                        if response is None or not response.ok:
                            status = None if response is None else response.status
                            raise RuntimeError(f"loopback page returned status {status}")
                        observed = page.evaluate(_BROWSER_METRICS)
                        normalized = {
                            "brokenImages": sorted(str(item) for item in observed["brokenImages"]),
                            "clippedText": int(observed["clippedText"]),
                            "horizontalOverflow": bool(observed["horizontalOverflow"]),
                        }
                        metrics[label] = normalized
                        screenshot = screenshot_dir / f"{label}.png"
                        if screenshot.exists() and _is_link_or_reparse(screenshot):
                            raise OSError(f"screenshot target is unsafe: {screenshot}")
                        page.screenshot(path=str(screenshot), full_page=True)
                        screenshots.append(screenshot)
                    finally:
                        context.close()
            finally:
                browser.close()
    except (OSError, PlaywrightError, RuntimeError, KeyError, TypeError, ValueError) as error:
        required.append(
            _required(
                "browser-qa-failed",
                "Chromium could not complete isolated visual QA.",
                {"reason": str(error)},
            )
        )
    finally:
        if server is not None:
            try:
                server.close()
            except (OSError, RuntimeError) as error:
                required.append(
                    _required(
                        "browser-cleanup-failed",
                        "The isolated QA server did not shut down cleanly.",
                        {"reason": str(error)},
                    )
                )

    overflow = sorted(label for label, value in metrics.items() if value["horizontalOverflow"])
    clipped = {label: value["clippedText"] for label, value in metrics.items() if value["clippedText"]}
    broken = {
        label: value["brokenImages"]
        for label, value in metrics.items()
        if value["brokenImages"]
    }
    if overflow:
        required.append(
            _required(
                "viewport-horizontal-overflow",
                "The page has unintended horizontal overflow.",
                {"viewports": overflow},
            )
        )
    if clipped:
        required.append(
            _required(
                "viewport-clipped-text",
                "Required text is clipped at one or more viewports.",
                {"viewports": clipped},
            )
        )
    if broken:
        warnings.append(
            _warning(
                "viewport-broken-images",
                "One or more optional images did not render offline.",
                {"viewports": broken},
            )
        )

    blocked_critical = sorted(
        url for url, resource_types in blocked.items()
        if any(item not in {"image", "font", "media"} for item in resource_types)
    )
    blocked_optional = sorted(
        url for url, resource_types in blocked.items()
        if resource_types and all(item in {"image", "font", "media"} for item in resource_types)
    )
    if blocked_critical:
        required.append(
            _required(
                "external-critical-dependency",
                "Browser rendering attempted a critical external network request.",
                {"urls": blocked_critical},
            )
        )
    if blocked_optional:
        warnings.append(
            _warning(
                "external-optional-dependency",
                "Browser rendering attempted an optional external network request.",
                {"urls": blocked_optional},
            )
        )
    return required, warnings, screenshots, metrics


def _origin(value: str) -> tuple[str, str, int | None] | None:
    parsed = _safe_urlsplit(value)
    if parsed is None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


class _QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: object, directory: str, **kwargs: object) -> None:
        self._safe_root = Path(directory).resolve(strict=True)
        self._blocked_path = False
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format: str, *args: object) -> None:
        return

    def translate_path(self, path: str) -> str:
        translated = Path(super().translate_path(path))
        self._blocked_path = (
            not translated.resolve(strict=False).is_relative_to(self._safe_root)
            or _has_link_or_reparse_ancestor(translated, self._safe_root)
        )
        return str(translated)

    def send_head(self) -> Any:
        self.translate_path(self.path)
        if self._blocked_path:
            self.send_error(404, "Unsafe path")
            return None
        return super().send_head()


class _LoopbackServer:
    def __init__(self, root: Path) -> None:
        supplied = root.absolute()
        if _has_link_or_reparse_ancestor_to_root(supplied):
            raise OSError("browser server root uses a link or reparse ancestor")
        resolved = supplied.resolve(strict=True)
        if not resolved.is_dir() or _is_link_or_reparse(resolved):
            raise OSError("browser server root is missing or unsafe")
        handler = lambda *args, **kwargs: _QuietHandler(  # noqa: E731
            *args, directory=str(resolved), **kwargs
        )
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="web-translator-qa-http",
            daemon=True,
        )
        self.url = f"http://127.0.0.1:{self._server.server_port}/index.html"
        parsed = urlsplit(self.url)
        if parsed.hostname != "127.0.0.1":
            self._server.server_close()
            raise OSError("QA HTTP server did not bind to IPv4 loopback")

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("QA HTTP server thread is still alive after shutdown")


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _REPARSE_POINT)


def _has_link_or_reparse_ancestor(path: Path, boundary: Path) -> bool:
    current = path
    while True:
        if current.exists() and _is_link_or_reparse(current):
            return True
        if current == boundary:
            return False
        parent = current.parent
        if parent == current or not current.is_relative_to(boundary):
            return True
        current = parent


def _has_link_or_reparse_ancestor_to_root(path: Path) -> bool:
    current = path
    while True:
        if (current.exists() or current.is_symlink()) and _is_link_or_reparse(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent
