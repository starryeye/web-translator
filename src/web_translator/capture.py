"""Bounded, SSRF-safe capture of static HTML and its offline assets."""

from __future__ import annotations

import base64
import binascii
import hashlib
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote_to_bytes, urldefrag, urljoin

import httpx
import tinycss2
from bs4 import BeautifulSoup
from tinycss2.ast import AtRule, FunctionBlock, ParseError, StringToken, URLToken

from web_translator.assets import atomic_write, local_asset_name, sha256_bytes
from web_translator.network import (
    NetworkBudget,
    NetworkError,
    _assert_transport_compatibility,
    _read_limited,
    build_public_client,
    fetch_limited,
)
from web_translator.paths import validate_public_url


MAX_REDIRECTS = 5
MAX_HTML_BYTES = 10 * 1024 * 1024
MAX_ASSET_BYTES = 25 * 1024 * 1024
MAX_CSS_IMPORT_DEPTH = 5
MAX_DATA_CSS_BYTES = 256 * 1024
MAX_UNIQUE_REQUESTS = 256
MAX_TOTAL_REDIRECTS = 32
MAX_TOTAL_DOWNLOADED_BYTES = 128 * 1024 * 1024
MAX_TOTAL_EMITTED_BYTES = 128 * 1024 * 1024
MAX_CAPTURE_SECONDS = 120.0


class CaptureError(RuntimeError):
    """The source page or a critical offline dependency could not be captured."""


class _CaptureBudgetError(CaptureError):
    """A whole-run capture resource budget was exhausted."""


def _capture_network_error(error: NetworkError) -> CaptureError:
    message = str(error)
    if message.startswith("capture resource budget exceeded:"):
        return _CaptureBudgetError(message)
    return CaptureError(message)


class _CaptureBudget:
    def __init__(self, network_budget: NetworkBudget) -> None:
        self.network_budget = network_budget
        self.request_urls: set[str] = set()
        self.emitted_bytes = 0

    def check_deadline(self) -> None:
        try:
            self.network_budget.check_deadline()
        except NetworkError as error:
            raise _CaptureBudgetError(str(error)) from error

    def before_request(self, request: httpx.Request) -> None:
        self.check_deadline()
        url = str(request.url)
        if url not in self.request_urls:
            if len(self.request_urls) >= MAX_UNIQUE_REQUESTS:
                raise _CaptureBudgetError(
                    "capture resource budget exceeded: unique requests"
                )
            self.request_urls.add(url)

    def add_emitted(self, size: int) -> None:
        self.check_deadline()
        if self.emitted_bytes + size > MAX_TOTAL_EMITTED_BYTES:
            raise _CaptureBudgetError(
                "capture resource budget exceeded: emitted bytes"
            )
        self.emitted_bytes += size


@dataclass(frozen=True, slots=True)
class CaptureResult:
    requested_url: str
    final_url: str
    source_html: Path
    asset_map: dict[str, str]
    fingerprints: dict[str, str]
    missing_optional_assets: list[str]
    critical_assets: list[str]
    optional_assets: list[str]


def capture_page(
    url: str,
    run_dir: Path,
    transport: httpx.BaseTransport | None = None,
) -> CaptureResult:
    """Capture one public HTML page and rewrite its static dependencies offline."""
    try:
        requested = validate_public_url(url)
    except ValueError as error:
        raise CaptureError(str(error)) from error

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    source_path = run_dir / "source.html"
    if source_path.exists():
        raise CaptureError(f"capture target already exists: {source_path}")
    network_budget = NetworkBudget(
        max_bytes=MAX_TOTAL_DOWNLOADED_BYTES,
        max_redirects=MAX_REDIRECTS,
        max_total_redirects=MAX_TOTAL_REDIRECTS,
        deadline_seconds=MAX_CAPTURE_SECONDS,
        error_prefix="capture resource budget exceeded",
    )
    budget = _CaptureBudget(network_budget)
    try:
        client = build_public_client(budget=network_budget, transport=transport)
    except NetworkError as error:
        raise CaptureError(str(error)) from error
    client.event_hooks["request"].append(budget.before_request)
    with client:
        capture = _Capture(client, run_dir, budget, network_budget)
        response, html_bytes = capture.fetch(str(requested), MAX_HTML_BYTES, "HTML document")
        media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type != "text/html":
            raise CaptureError(f"HTML response must use text/html, got {media_type or 'no content type'}")

        final_url = str(response.url)
        encoding = response.encoding or "utf-8"
        try:
            source_text = html_bytes.decode(encoding)
        except (LookupError, UnicodeDecodeError) as error:
            raise CaptureError(f"HTML response could not be decoded as {encoding}") from error
        _validate_supported_html(source_text)
        rendered = capture.rewrite_html(source_text, final_url)
        rendered_bytes = rendered.encode("utf-8")
        budget.add_emitted(len(rendered_bytes))
        try:
            atomic_write(source_path, rendered_bytes)
        except FileExistsError as error:
            raise CaptureError(f"capture target already exists: {source_path}") from error
        capture.fingerprints["source.html"] = sha256_bytes(rendered_bytes)

    return CaptureResult(
        requested_url=str(requested),
        final_url=final_url,
        source_html=source_path,
        asset_map=dict(sorted(capture.asset_map.items())),
        fingerprints=dict(sorted(capture.fingerprints.items())),
        missing_optional_assets=sorted(capture.missing_optional_assets),
        critical_assets=sorted(capture.critical_assets),
        optional_assets=sorted(capture.optional_assets),
    )


class _Capture:
    def __init__(
        self,
        client: httpx.Client,
        run_dir: Path,
        budget: _CaptureBudget,
        network_budget: NetworkBudget,
    ) -> None:
        self.client = client
        self.run_dir = run_dir
        self.budget = budget
        self.network_budget = network_budget
        self.asset_map: dict[str, str] = {}
        self.fingerprints: dict[str, str] = {}
        self.missing_optional_assets: set[str] = set()
        self.critical_assets: set[str] = set()
        self.optional_assets: set[str] = set()
        self._complete_assets: set[str] = set()
        self._failed_assets: set[str] = set()

    def fetch(self, url: str, limit: int, label: str) -> tuple[httpx.Response, bytes]:
        try:
            return fetch_limited(self.client, url, limit, label)
        except NetworkError as error:
            raise _capture_network_error(error) from error
        except CaptureError:
            raise
        except (httpx.HTTPError, OSError) as error:
            raise CaptureError(f"failed to fetch {label}: {error}") from error

    def fetch_asset(self, url: str, visited: set[str]) -> tuple[httpx.Response | None, bytes, str | None]:
        """Fetch an asset while stopping a redirect before a completed cached target."""
        current = url
        try:
            for redirect_count in range(MAX_REDIRECTS + 1):
                visited.add(current)
                with self.client.stream(
                    "GET",
                    current,
                    follow_redirects=False,
                    timeout=self.network_budget.request_timeout(),
                ) as response:
                    if response.has_redirect_location:
                        if redirect_count == MAX_REDIRECTS:
                            raise CaptureError(f"asset redirect limit exceeded: {url}")
                        destination = urljoin(str(response.url), response.headers["location"])
                        try:
                            normalized_destination = str(validate_public_url(urldefrag(destination)[0]))
                        except ValueError as error:
                            raise CaptureError(f"unsafe asset redirect URL: {destination}") from error
                        visited.add(normalized_destination)
                        if (
                            normalized_destination in self._complete_assets
                            and normalized_destination in self.asset_map
                        ):
                            return None, b"", normalized_destination
                        if normalized_destination in self._failed_assets:
                            raise CaptureError(f"cached asset failure: {normalized_destination}")
                        current = normalized_destination
                        continue
                    response.raise_for_status()
                    content = _read_limited(
                        response,
                        MAX_ASSET_BYTES,
                        f"asset {url}",
                        self.network_budget,
                    )
                    return response, content, None
        except NetworkError as error:
            raise _capture_network_error(error) from error
        except CaptureError:
            raise
        except (httpx.HTTPError, OSError) as error:
            raise CaptureError(f"failed to fetch asset {url}: {error}") from error
        raise CaptureError(f"asset redirect limit exceeded: {url}")

    def rewrite_html(self, html: str, document_url: str) -> str:
        soup = BeautifulSoup(html, "lxml")
        base_tag = soup.find("base", href=True)
        base_url = urljoin(document_url, str(base_tag["href"])) if base_tag else document_url
        for tag in soup.find_all("base"):
            tag.decompose()

        for link in soup.find_all("link", href=True):
            rel = {str(value).lower() for value in link.get("rel", [])}
            if "stylesheet" in rel:
                link["href"] = self.capture_asset(
                    str(link["href"]),
                    critical=True,
                    css_depth=0,
                    base_url=base_url,
                )
            elif rel & {"icon", "apple-touch-icon", "mask-icon"}:
                absolute = self._absolute_reference(base_url, str(link["href"]))
                link["href"] = self.capture_asset(absolute, critical=False, expected_kind="image")

        for tag in soup.find_all(["img", "source"]):
            if tag.has_attr("src"):
                absolute = self._absolute_reference(base_url, str(tag["src"]))
                tag["src"] = self.capture_asset(absolute, critical=False, expected_kind="image")
            if tag.has_attr("srcset"):
                tag["srcset"] = self._rewrite_srcset(str(tag["srcset"]), base_url)

        for tag in soup.find_all(style=True):
            tag["style"] = self.rewrite_declarations(str(tag["style"]), base_url)
        for style in soup.find_all("style"):
            css = style.string if style.string is not None else style.get_text()
            style.string = self.rewrite_css(css, base_url, None, 0)

        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"])
            if href.startswith("#"):
                continue
            absolute = self._absolute_reference(base_url, href)
            target_page, fragment = urldefrag(absolute)
            current_page, _ = urldefrag(document_url)
            anchor["href"] = f"#{fragment}" if fragment and target_page == current_page else absolute

        return str(soup)

    def capture_asset(
        self,
        reference: str,
        *,
        critical: bool,
        css_depth: int | None = None,
        expected_kind: str = "asset",
        base_url: str | None = None,
    ) -> str:
        stripped = reference.strip()
        if critical and css_depth is not None and stripped.lower().startswith("data:"):
            return self._capture_data_stylesheet(stripped, base_url, css_depth)
        if critical and css_depth is not None and _is_non_network_reference(stripped):
            raise CaptureError(
                f"critical stylesheet uses unsupported non-network reference: {reference}"
            )
        if _is_non_network_reference(stripped):
            return reference
        if css_depth is not None and css_depth > MAX_CSS_IMPORT_DEPTH:
            raise CaptureError(f"critical stylesheet exceeds import depth {MAX_CSS_IMPORT_DEPTH}: {reference}")
        if base_url is not None:
            reference = self._absolute_reference(base_url, reference)
        try:
            fetch_url, fragment = urldefrag(reference)
            validated = validate_public_url(fetch_url)
            normalized = str(validated)
        except ValueError as error:
            if critical:
                raise CaptureError(f"critical stylesheet URL is unsafe: {reference}") from error
            self.missing_optional_assets.add(reference)
            return reference

        existing = self.asset_map.get(normalized)
        if existing is not None:
            self._classify_cached_asset(existing, critical)
            return _with_fragment(existing, fragment)
        if normalized in self._failed_assets:
            if critical:
                raise CaptureError(f"critical stylesheet could not be captured: {normalized}")
            return reference

        provisional = local_asset_name(validated, "text/css" if css_depth is not None else None).as_posix()
        self.asset_map[normalized] = provisional
        aliases = {normalized}
        visited = {normalized}
        try:
            response, content, cached_target = self.fetch_asset(normalized, visited)
            if cached_target is not None:
                local_path = self.asset_map[cached_target]
                self._classify_cached_asset(local_path, critical)
                for alias in visited:
                    self.asset_map[alias] = local_path
                    self._complete_assets.add(alias)
                return _with_fragment(local_path, fragment)
            assert response is not None
            content_type = response.headers.get("content-type")
            _validate_asset_content(content, content_type, css_depth is not None, expected_kind)
            local_path = local_asset_name(validated, content_type).as_posix()
            final_asset_url = str(validate_public_url(str(response.url)))
            aliases.add(final_asset_url)
            aliases.update(visited)
            for alias in aliases:
                self.asset_map[alias] = local_path
            if css_depth is not None:
                encoding = response.encoding or "utf-8"
                try:
                    css = content.decode(encoding)
                except (LookupError, UnicodeDecodeError) as error:
                    raise CaptureError(f"critical stylesheet could not be decoded: {normalized}") from error
                css = self.rewrite_css(css, final_asset_url, local_path, css_depth)
                content = css.encode("utf-8")
            destination = self.run_dir / Path(local_path)
            self.budget.add_emitted(len(content))
            atomic_write(destination, content)
            self.fingerprints[local_path] = sha256_bytes(content)
            self._complete_assets.update(aliases)
            self._classify_asset(local_path, critical)
            return _with_fragment(local_path, fragment)
        except (CaptureError, OSError) as error:
            for alias in aliases:
                self.asset_map.pop(alias, None)
            if isinstance(error, _CaptureBudgetError):
                raise
            if critical:
                raise CaptureError(f"critical stylesheet could not be captured: {normalized}: {error}") from error
            self._failed_assets.update(visited)
            self.missing_optional_assets.add(normalized)
            return reference

    def _capture_data_stylesheet(
        self, reference: str, base_url: str | None, css_depth: int
    ) -> str:
        if css_depth > MAX_CSS_IMPORT_DEPTH:
            raise CaptureError(
                f"critical stylesheet exceeds import depth {MAX_CSS_IMPORT_DEPTH}: {reference}"
            )
        existing = self.asset_map.get(reference)
        if existing is not None:
            self._classify_asset(existing, True)
            return existing
        content = _decode_data_stylesheet(reference)
        digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()[:16]
        local_path = f"assets/{digest}.css"
        self.asset_map[reference] = local_path
        try:
            try:
                css = content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise CaptureError(
                    "critical data stylesheet is not valid UTF-8"
                ) from error
            rewritten = self.rewrite_css(
                css,
                base_url or reference,
                local_path,
                css_depth,
            ).encode("utf-8")
            self.budget.add_emitted(len(rewritten))
            atomic_write(self.run_dir / Path(local_path), rewritten)
            self.fingerprints[local_path] = sha256_bytes(rewritten)
            self._complete_assets.add(reference)
            self._classify_asset(local_path, True)
            return local_path
        except (CaptureError, OSError):
            self.asset_map.pop(reference, None)
            raise

    def _classify_asset(self, local_path: str, critical: bool) -> None:
        if critical:
            self.optional_assets.discard(local_path)
            self.critical_assets.add(local_path)
        elif local_path not in self.critical_assets:
            self.optional_assets.add(local_path)

    def _classify_cached_asset(self, local_path: str, critical: bool) -> None:
        if critical and local_path in self.optional_assets:
            raise CaptureError("cannot reuse an optional asset as a critical stylesheet")
        self._classify_asset(local_path, critical)

    def rewrite_css(self, css: str, base_url: str, css_local_path: str | None, depth: int) -> str:
        rules = tinycss2.parse_stylesheet(css, skip_comments=False, skip_whitespace=False)
        discarded_error = any(
            isinstance(rule, ParseError)
            and _is_discardable_css_parse_error(rule, "stylesheet")
            for rule in rules
        )
        valid_rule = any(
            getattr(rule, "type", None) in {"at-rule", "qualified-rule"}
            for rule in rules
        )
        if css_local_path is not None and discarded_error and not valid_rule:
            raise CaptureError(
                "critical stylesheet contains no valid rules after CSS error recovery"
            )
        return "".join(
            self._render_css_rule(rule, base_url, css_local_path, depth)
            for rule in rules
        )

    def rewrite_declarations(self, css: str, base_url: str) -> str:
        rendered: list[str] = []
        for node in tinycss2.parse_declaration_list(css, skip_comments=False, skip_whitespace=False):
            if node.type == "declaration":
                value = self._render_component_values(node.value, base_url, None, 0)
                important = " !important" if node.important else ""
                rendered.append(f"{node.name}:{value}{important};")
            elif isinstance(node, ParseError):
                rendered.append(_serialize_css_parse_error(node, "declaration"))
            else:
                rendered.append(node.serialize())
        return "".join(rendered)

    def _render_css_rule(self, rule: object, base_url: str, css_local_path: str | None, depth: int) -> str:
        if isinstance(rule, ParseError):
            return _serialize_css_parse_error(rule, "stylesheet")
        if not hasattr(rule, "type"):
            return str(rule)
        if rule.type == "at-rule":
            assert isinstance(rule, AtRule)
            if rule.lower_at_keyword == "import":
                prelude = self._render_import_prelude(rule.prelude, base_url, css_local_path, depth)
            else:
                prelude = self._render_component_values(rule.prelude, base_url, css_local_path, depth)
            prefix = f"@{rule.at_keyword}{prelude}"
            if rule.content is None:
                return prefix + ";"
            content = self._render_component_values(rule.content, base_url, css_local_path, depth)
            return prefix + "{" + content + "}"
        if rule.type == "qualified-rule":
            prelude = self._render_component_values(rule.prelude, base_url, css_local_path, depth)
            content = self._render_component_values(rule.content, base_url, css_local_path, depth)
            return prelude + "{" + content + "}"
        return rule.serialize()

    def _render_import_prelude(
        self, tokens: list[object], base_url: str, css_local_path: str | None, depth: int
    ) -> str:
        rendered: list[str] = []
        replaced = False
        for token in tokens:
            import_url = _css_url_value(token) if not replaced else None
            if import_url is None:
                rendered.append(self._render_component(token, base_url, css_local_path, depth))
                continue
            local = self.capture_asset(
                import_url,
                critical=True,
                css_depth=depth + 1,
                base_url=base_url,
            )
            rendered.append(f'url("{_css_escape(self._css_relative(css_local_path, local))}")')
            replaced = True
        return "".join(rendered)

    def _render_component_values(
        self, tokens: list[object], base_url: str, css_local_path: str | None, depth: int
    ) -> str:
        return "".join(self._render_component(token, base_url, css_local_path, depth) for token in tokens)

    def _render_component(self, token: object, base_url: str, css_local_path: str | None, depth: int) -> str:
        if isinstance(token, ParseError):
            return _serialize_css_parse_error(token, "component")
        value = _css_url_value(token, allow_string=False)
        if value is not None:
            local_fragment = _same_document_fragment(value, base_url)
            if local_fragment is not None:
                return f'url("{_css_escape(local_fragment)}")'
            absolute = self._absolute_reference(base_url, value)
            local = self.capture_asset(absolute, critical=False)
            return f'url("{_css_escape(self._css_relative(css_local_path, local))}")'
        if isinstance(token, FunctionBlock):
            arguments = self._render_component_values(token.arguments, base_url, css_local_path, depth)
            return f"{token.name}({arguments})"
        if hasattr(token, "content") and token.type in {"() block", "[] block", "{} block"}:
            pairs = {"() block": ("(", ")"), "[] block": ("[", "]"), "{} block": ("{", "}")}
            opening, closing = pairs[token.type]
            return opening + self._render_component_values(token.content, base_url, css_local_path, depth) + closing
        return token.serialize()

    def _rewrite_srcset(self, value: str, base_url: str) -> str:
        candidates: list[str] = []
        for source, descriptor in _parse_srcset(value):
            absolute = self._absolute_reference(base_url, source)
            local = self.capture_asset(absolute, critical=False, expected_kind="image")
            candidates.append(" ".join(part for part in (local, descriptor) if part))
        return ", ".join(candidates)

    @staticmethod
    def _absolute_reference(base_url: str, reference: str) -> str:
        try:
            return urljoin(base_url, reference)
        except ValueError:
            return reference

    @staticmethod
    def _css_relative(css_local_path: str | None, target: str) -> str:
        if css_local_path is None or _is_non_local_path(target):
            return target
        path, separator, fragment = target.partition("#")
        relative = posixpath.relpath(path, start=str(PurePosixPath(css_local_path).parent))
        return relative + (separator + fragment if separator else "")


def _decode_data_stylesheet(reference: str) -> bytes:
    if len(reference.encode("utf-8")) > MAX_DATA_CSS_BYTES:
        raise CaptureError(
            f"critical data stylesheet exceeds the {MAX_DATA_CSS_BYTES}-byte size limit"
        )
    header, separator, payload = reference.partition(",")
    if not separator:
        raise CaptureError("critical stylesheet data URI is malformed")
    if not reference.startswith("data:"):
        raise CaptureError("critical stylesheet data URI must use canonical data: syntax")
    metadata = header[5:].split(";")
    if not metadata or metadata[0] != "text/css":
        raise CaptureError("critical stylesheet data URI must use text/css")
    charset = "utf-8"
    options = metadata[1:]
    is_base64 = bool(options and options[-1] == "base64")
    if is_base64:
        options = options[:-1]
    if options not in ([], ["charset=utf-8"], ["charset=us-ascii"]):
        raise CaptureError("critical stylesheet data URI has unsupported parameters")
    if options:
        charset = options[0].partition("=")[2]
    if re.search(r"%(?![0-9a-fA-F]{2})", payload):
        raise CaptureError("critical stylesheet data URI has malformed percent encoding")
    encoded = unquote_to_bytes(payload)
    if is_base64:
        try:
            encoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise CaptureError("critical stylesheet data URI has malformed base64") from error
    if len(encoded) > MAX_DATA_CSS_BYTES:
        raise CaptureError(
            f"critical data stylesheet exceeds the {MAX_DATA_CSS_BYTES}-byte size limit"
        )
    try:
        text = encoded.decode(charset)
    except (LookupError, UnicodeDecodeError) as error:
        raise CaptureError(
            f"critical stylesheet data URI cannot be decoded as {charset}"
        ) from error
    if not text:
        raise CaptureError("critical stylesheet data URI is empty")
    return text.encode("utf-8")


def _parse_srcset(value: str) -> list[tuple[str, str]]:
    """Follow the HTML candidate shape: URL token first, then descriptors."""
    candidates: list[tuple[str, str]] = []
    position = 0
    while position < len(value):
        while position < len(value) and (value[position].isspace() or value[position] == ","):
            position += 1
        if position >= len(value):
            break
        url_start = position
        while position < len(value) and not value[position].isspace():
            position += 1
        source = value[url_start:position]
        if source.endswith(","):
            source = source.rstrip(",")
            if source:
                candidates.append((source, ""))
            continue
        while position < len(value) and value[position].isspace():
            position += 1
        descriptor_start = position
        while position < len(value) and value[position] != ",":
            position += 1
        descriptor = value[descriptor_start:position].strip()
        if position < len(value):
            position += 1
        candidates.append((source, descriptor))
    return candidates


def _same_document_fragment(reference: str, base_url: str) -> str | None:
    if reference.startswith("#"):
        return reference
    absolute = urljoin(base_url, reference)
    target, fragment = urldefrag(absolute)
    current, _ = urldefrag(base_url)
    return f"#{fragment}" if fragment and target == current else None


def _validate_asset_content(
    content: bytes, content_type: str | None, stylesheet: bool, expected_kind: str
) -> None:
    if not content:
        raise CaptureError("asset response body is empty")
    media_type = (content_type or "").partition(";")[0].strip().lower()
    if stylesheet and media_type != "text/css":
        raise CaptureError(f"critical stylesheet returned {media_type or 'no content type'}")
    if expected_kind == "image" and not media_type.startswith("image/"):
        raise CaptureError(f"image asset returned {media_type or 'no content type'}")
    if not stylesheet and expected_kind == "asset":
        binary_application = media_type in {
            "application/octet-stream",
            "binary/octet-stream",
            "application/font-woff",
            "application/vnd.ms-fontobject",
        } or media_type.startswith("application/x-font-")
        if not (media_type.startswith(("image/", "font/")) or binary_application):
            raise CaptureError(f"asset returned unsupported {media_type or 'no content type'}")


def _validate_supported_html(html: str) -> None:
    soup = BeautifulSoup(html, "lxml")
    if soup.find("input", attrs={"type": lambda value: value and str(value).lower() == "password"}):
        raise CaptureError("unsupported authentication page")
    markup = str(soup).lower()
    if any(signal in markup for signal in ("g-recaptcha", "h-captcha", "hcaptcha", "recaptcha")):
        raise CaptureError("unsupported CAPTCHA page")
    visible_text = soup.get_text(" ", strip=True)
    if any(_is_authentication_form(form) for form in soup.find_all("form")):
        raise CaptureError("unsupported authentication page")
    challenge_headings = {
        re.sub(r"[^a-z ]+", "", node.get_text(" ", strip=True).lower()).strip()
        for node in soup.find_all(["title", "h1"])
    }
    has_challenge_heading = bool(
        challenge_headings & {"just a moment", "security check", "verify you are human"}
    )
    challenge_markers = (
        "checking your browser",
        "verify you are human",
        "cf-challenge",
        "challenge-form",
        "captcha",
    )
    if len(visible_text) <= 500 and has_challenge_heading and any(marker in markup for marker in challenge_markers):
        raise CaptureError("unsupported interstitial page")
    had_script = soup.find("script") is not None
    app_shell = soup.find(id=re.compile(r"^(?:app|root|__next|__nuxt)$", re.IGNORECASE))
    placeholder = re.sub(r"[.\s]+", " ", app_shell.get_text(" ", strip=True).lower()).strip() if app_shell else ""
    if had_script and app_shell is not None and placeholder in {"loading", "please wait", "initializing"}:
        raise CaptureError("unsupported JavaScript-only page")
    for node in soup.find_all(["script", "style", "noscript"]):
        node.decompose()
    if not soup.get_text(" ", strip=True) and had_script:
        raise CaptureError("unsupported JavaScript-only page")


def _is_authentication_form(form: object) -> bool:
    auth_attribute = re.compile(
        r"(?:^|[^a-z0-9])(?:login|log-in|signin|sign-in|auth|session)(?:[^a-z0-9]|$)",
        re.IGNORECASE,
    )
    for attribute in ("action", "id", "class"):
        value = form.get(attribute, "")
        serialized = " ".join(str(part) for part in value) if isinstance(value, list) else str(value)
        if auth_attribute.search(serialized):
            return True
    form_text = form.get_text(" ", strip=True)
    if re.search(r"\b(?:log|sign)[ -]?in\b", form_text, re.IGNORECASE):
        return True
    for control in form.find_all(["input", "button"]):
        for attribute in ("name", "id", "value", "aria-label"):
            if auth_attribute.search(str(control.get(attribute, ""))):
                return True
    return False


def _css_url_value(token: object, *, allow_string: bool = True) -> str | None:
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


def _is_non_network_reference(reference: str) -> bool:
    lowered = reference.strip().lower()
    return lowered.startswith(("#", "data:", "blob:"))


def _is_non_local_path(reference: str) -> bool:
    return "://" in reference or _is_non_network_reference(reference)


def _with_fragment(path: str, fragment: str) -> str:
    return path + (f"#{fragment}" if fragment else "")


def _css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\a ")


def _serialize_css_parse_error(error: ParseError, context: str) -> str:
    """Apply CSS error recovery only where tinycss2 identifies an invalid unit."""
    if _is_discardable_css_parse_error(error, context):
        return ""
    try:
        return error.serialize()
    except TypeError as serialization_error:
        raise CaptureError(
            "unsupported CSS parse error "
            f"in {context} at {error.source_line}:{error.source_column}: "
            f"{error.kind}: {error.message}"
        ) from serialization_error


def _is_discardable_css_parse_error(error: ParseError, context: str) -> bool:
    return (
        context == "stylesheet"
        and error.kind == "invalid"
        and error.message == "EOF reached before {} block for a qualified rule."
    ) or (
        context == "declaration"
        and error.kind == "invalid"
        and (
            error.message.startswith("Expected <ident> for declaration name, got ")
            or error.message.startswith("Expected ':' after declaration name, got ")
            or error.message == "Declaration contains {} block"
        )
    )
