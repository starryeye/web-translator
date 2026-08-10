"""Safe reconstruction of reviewed translations into the captured DOM."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import os
from pathlib import Path
import re
import shutil
import tempfile

from bs4 import BeautifulSoup, NavigableString, Tag

from web_translator.models import Segment, Translation
from web_translator.paths import validate_public_url
from web_translator.protection import ProtectionError, restore_tokens
from web_translator.terminology import TerminologyError, normalize_first_use


_CSP = (
    "default-src 'none'; "
    "script-src 'none'; "
    "connect-src 'none'; "
    "object-src 'none'; "
    "frame-src 'none'; "
    "worker-src 'none'; "
    "child-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "media-src 'self' data:"
)
_EXECUTABLE_TAGS = {
    "applet",
    "base",
    "embed",
    "iframe",
    "link",
    "meta",
    "object",
    "script",
    "style",
}
_URL_ATTRIBUTES = {"action", "formaction", "href", "src", "xlink:href"}
_REPARSE_POINT = 0x400


class AssemblyError(RuntimeError):
    """The reviewed page cannot be assembled without violating invariants."""


def assemble_page(
    source_html: Path,
    segments: Mapping[str, Segment],
    translations: Mapping[str, Translation],
    glossary: Mapping[str, str],
    output_dir: Path,
    source_url: str,
) -> Path:
    """Build an offline HTML bundle without mutating the captured source."""
    source_html = Path(source_html)
    output_dir = Path(output_dir)
    _reject_unsafe_output_ancestors(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise AssemblyError(f"output directory already exists: {output_dir}")
    if not source_html.is_file() or _is_link_or_reparse(source_html):
        raise AssemblyError(f"source HTML is missing or unsafe: {source_html}")
    try:
        final_source_url = str(validate_public_url(source_url))
    except (TypeError, ValueError) as error:
        raise AssemblyError("source URL must be a public HTTP(S) URL") from error

    source_text = _read_source(source_html)
    soup = BeautifulSoup(source_text, "lxml")
    if soup.html is None or soup.head is None or soup.body is None:
        raise AssemblyError("source HTML must contain html, head, and body elements")
    _reject_source_meta_policies(soup)

    segment_map = _validate_segments(segments)
    translation_map = _validate_translations(translations, segment_map)
    marked = _validate_markers(soup, segment_map)
    ordered_ids = [str(element["data-wt-segment"]) for element in marked]
    try:
        normalized = normalize_first_use(
            [translation_map[segment_id] for segment_id in ordered_ids],
            glossary,
            protected_by_segment={
                segment_id: segment_map[segment_id].protected
                for segment_id in ordered_ids
            },
        )
    except TerminologyError as error:
        raise AssemblyError(f"terminology normalization failed: {error}") from error

    for segment_id, record in zip(ordered_ids, normalized, strict=True):
        current_matches = soup.find_all(attrs={"data-wt-segment": segment_id})
        if len(current_matches) != 1:
            raise AssemblyError(
                f"DOM marker {segment_id} was not preserved during parent replacement"
            )
        element = current_matches[0]
        segment = segment_map[segment_id]
        try:
            restored = restore_tokens(record.text, segment.protected)
        except ProtectionError as error:
            raise AssemblyError(f"cannot restore {segment_id}: {error}") from error
        fragment = BeautifulSoup(restored, "html.parser")
        _reject_executable_fragment(fragment, segment_id)
        if _shape(element) != _shape(fragment):
            raise AssemblyError(f"translated fragment shape changed for {segment_id}")
        replacement = list(fragment.contents)
        element.clear()
        for child in replacement:
            element.append(child.extract())
        del element["data-wt-segment"]

    if soup.select_one("[data-wt-segment]") is not None:
        raise AssemblyError("not all DOM segment markers were consumed")

    _add_csp(soup)
    _add_attribution(soup, final_source_url)
    return _publish_bundle(source_html.parent, soup, output_dir)


def _read_source(source_html: Path) -> str:
    try:
        text = source_html.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise AssemblyError(f"source HTML cannot be read as UTF-8: {error}") from error
    lowered = text.lower()
    if "\x00" in text or "<html" not in lowered or "<body" not in lowered:
        raise AssemblyError("source HTML is malformed")
    return text


def _validate_segments(segments: Mapping[str, Segment]) -> dict[str, Segment]:
    if not isinstance(segments, Mapping):
        raise AssemblyError("segments must be a mapping")
    result: dict[str, Segment] = {}
    for key, value in segments.items():
        if not isinstance(key, str) or not isinstance(value, Segment):
            raise AssemblyError("segment keys must map to Segment values")
        if key != value.id:
            raise AssemblyError(f"segment key does not match record ID: {key}")
        if not value.target:
            raise AssemblyError(f"non-target segment cannot be assembled: {key}")
        result[key] = value
    return result


def _validate_translations(
    translations: Mapping[str, Translation], segments: Mapping[str, Segment]
) -> dict[str, Translation]:
    if not isinstance(translations, Mapping):
        raise AssemblyError("translations must be a mapping")
    if any(not isinstance(key, str) for key in translations):
        raise AssemblyError("translation keys must be strings")
    translation_keys = set(translations)
    segment_keys = set(segments)
    missing = sorted(segment_keys - translation_keys)
    if missing:
        raise AssemblyError(f"missing translation IDs: {', '.join(missing)}")
    foreign = sorted(translation_keys - segment_keys)
    if foreign:
        raise AssemblyError(f"foreign translation IDs: {', '.join(foreign)}")
    result: dict[str, Translation] = {}
    for key, value in translations.items():
        if not isinstance(key, str) or not isinstance(value, Translation):
            raise AssemblyError("translation keys must map to Translation values")
        if key != value.segment_id:
            raise AssemblyError(f"translation key does not match record ID: {key}")
        result[key] = value
    return result


def _validate_markers(soup: BeautifulSoup, segments: Mapping[str, Segment]) -> list[Tag]:
    marked = [node for node in soup.select("[data-wt-segment]") if isinstance(node, Tag)]
    marker_ids = [str(node.get("data-wt-segment", "")) for node in marked]
    duplicates = sorted(identifier for identifier, count in Counter(marker_ids).items() if count > 1)
    if duplicates:
        raise AssemblyError(f"duplicate DOM marker IDs: {', '.join(duplicates)}")
    missing = sorted(set(segments) - set(marker_ids))
    foreign = sorted(set(marker_ids) - set(segments))
    if missing:
        raise AssemblyError(f"missing DOM marker IDs: {', '.join(missing)}")
    if foreign:
        raise AssemblyError(f"foreign DOM marker IDs: {', '.join(foreign)}")
    return marked


def _shape(node: Tag | BeautifulSoup) -> tuple[object, ...]:
    return tuple(_tag_signature(child) for child in node.children if isinstance(child, Tag))


def _tag_signature(tag: Tag) -> tuple[object, ...]:
    attributes = tuple(
        sorted(
            (name.lower(), tuple(value) if isinstance(value, list) else str(value))
            for name, value in tag.attrs.items()
        )
    )
    return (
        tag.name.lower(),
        attributes,
        tuple(_tag_signature(child) for child in tag.children if isinstance(child, Tag)),
    )


def _reject_executable_fragment(fragment: BeautifulSoup, segment_id: str) -> None:
    for tag in fragment.find_all(True):
        name = tag.name.lower()
        if name in _EXECUTABLE_TAGS or name in {"html", "head", "body"}:
            raise AssemblyError(f"executable tag in translated fragment {segment_id}: {name}")
        for attribute, value in tag.attrs.items():
            lowered_attribute = attribute.lower()
            values = value if isinstance(value, list) else [value]
            if lowered_attribute.startswith("on") or lowered_attribute == "srcdoc":
                raise AssemblyError(
                    f"executable attribute in translated fragment {segment_id}: {attribute}"
                )
            if lowered_attribute in _URL_ATTRIBUTES and any(
                _is_executable_url(str(item)) for item in values
            ):
                raise AssemblyError(
                    f"executable URL in translated fragment {segment_id}: {attribute}"
                )


def _is_executable_url(value: str) -> bool:
    normalized = re.sub(r"[\x00-\x20\x7f]+", "", value).lower()
    return normalized.startswith(("javascript:", "vbscript:", "data:text/html"))


def _reject_source_meta_policies(soup: BeautifulSoup) -> None:
    for meta in soup.find_all("meta"):
        directive = str(meta.get("http-equiv", "")).strip().lower()
        if directive == "refresh":
            raise AssemblyError("source HTML contains an unsafe meta refresh")
        if directive == "content-security-policy":
            raise AssemblyError(
                "source HTML contains a Content-Security-Policy that may block offline assets"
            )


def _add_csp(soup: BeautifulSoup) -> None:
    assert soup.head is not None
    meta = soup.new_tag("meta")
    meta["http-equiv"] = "Content-Security-Policy"
    meta["content"] = _CSP
    meta["data-wt-csp"] = "offline"
    soup.head.insert(0, meta)


def _add_attribution(soup: BeautifulSoup, source_url: str) -> None:
    assert soup.body is not None
    if soup.select_one("[data-wt-attribution]") is not None:
        raise AssemblyError("source HTML already contains a plugin attribution marker")
    attribution = soup.new_tag("footer")
    attribution["data-wt-attribution"] = "source"
    attribution.append(NavigableString("AI 번역본 · "))
    link = soup.new_tag("a", href=source_url)
    link["rel"] = "noopener noreferrer"
    link.string = "원문 보기"
    attribution.append(link)
    soup.body.append(attribution)


def _publish_bundle(source_root: Path, soup: BeautifulSoup, output_dir: Path) -> Path:
    output_parent = output_dir.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.assembling-", dir=output_parent))
    try:
        _copy_assets(source_root / "assets", temporary / "assets", temporary)
        index = temporary / "index.html"
        index.write_text(str(soup), encoding="utf-8", newline="\n")
        try:
            temporary.rename(output_dir)
        except FileExistsError as error:
            raise AssemblyError(f"output directory already exists: {output_dir}") from error
    except AssemblyError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except OSError as error:
        shutil.rmtree(temporary, ignore_errors=True)
        raise AssemblyError(f"cannot publish offline bundle: {error}") from error
    return output_dir / "index.html"


def _copy_assets(source: Path, destination: Path, bundle_root: Path) -> None:
    if _is_link_or_reparse(source):
        raise AssemblyError(f"asset root is a link or reparse point: {source}")
    if not source.exists():
        return
    if not source.is_dir():
        raise AssemblyError(f"asset root is a link or reparse point: {source}")
    destination.mkdir()
    bundle_resolved = bundle_root.resolve(strict=True)
    for root, directory_names, file_names in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        if _is_link_or_reparse(root_path):
            raise AssemblyError(f"asset directory is a link or reparse point: {root_path}")
        relative = root_path.relative_to(source)
        target_root = destination / relative
        target_root.mkdir(parents=True, exist_ok=True)
        if not target_root.resolve(strict=True).is_relative_to(bundle_resolved):
            raise AssemblyError(f"asset destination escaped output bundle: {target_root}")
        for directory_name in directory_names:
            child = root_path / directory_name
            if _is_link_or_reparse(child):
                raise AssemblyError(f"asset directory is a link or reparse point: {child}")
        for file_name in file_names:
            source_file = root_path / file_name
            if _is_link_or_reparse(source_file) or not source_file.is_file():
                raise AssemblyError(f"asset file is a link or reparse point: {source_file}")
            target_file = target_root / file_name
            if not target_file.parent.resolve(strict=True).is_relative_to(bundle_resolved):
                raise AssemblyError(f"asset destination escaped output bundle: {target_file}")
            shutil.copy2(source_file, target_file)


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _REPARSE_POINT)


def _reject_unsafe_output_ancestors(output_dir: Path) -> None:
    candidate = output_dir.parent
    while not candidate.exists() and not candidate.is_symlink():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    while True:
        if _is_link_or_reparse(candidate):
            raise AssemblyError(
                f"output path contains a link or reparse point: {candidate}"
            )
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
