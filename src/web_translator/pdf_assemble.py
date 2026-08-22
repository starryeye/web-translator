"""Basic, fail-closed ReportLab assembly for reviewed PDF translations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from importlib.resources import as_file, files
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

import web_translator.pdf_acquire as pdf_acquire_module
from web_translator.models import Segment, Translation, read_segments
from web_translator.pdf_flowables import (
    PdfAssemblyError,
    PdfAssemblyLayout,
    PdfFlowableLayout,
    PdfPageSize,
    TrackedFlowable,
    write_pdf_layout,
)
from web_translator.pdf_models import PdfBlock, PdfContractError, PdfDocument, PdfSourceRecord
from web_translator.protection import ProtectionError, restore_tokens
from web_translator.terminology import TerminologyError, normalize_first_use


REGULAR_FONT_NAME = "WT-NotoSansKR"
BOLD_FONT_NAME = "WT-NotoSansKR-Bold"
BODY_FONT_SIZE = 11.0
MINIMUM_FONT_SIZE = 9.0
FONT_LICENSE_SHA256 = "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"
_REPARSE_POINT = 0x400
_IS_WINDOWS = os.name == "nt"
_DIRFD_PUBLICATION_SUPPORTED = all(
    operation in os.supports_dir_fd
    for operation in (os.link, os.open, os.stat, os.unlink)
)
_BASIC_KINDS = {"heading", "paragraph", "list-item"}
_IGNORED_KINDS = {"header", "footer", "page-number"}
_ALIGNMENTS = {
    "left": TA_LEFT,
    "center": TA_CENTER,
    "right": TA_RIGHT,
    "justify": TA_JUSTIFY,
}
_LIST_MARKER = re.compile(r"^\s*(?P<marker>[-+*•]|\d+(?:\.\d+)*[.)])\s+")
_LIST_INDENT_TOLERANCE = 3.0


@dataclass(slots=True)
class _DirectoryAnchor:
    path: Path
    label: str
    identity: tuple[int, int]
    descriptor: int | None
    path_anchor: object | None = None

    def current_path(self) -> Path:
        if self.descriptor is not None:
            resolver = pdf_acquire_module._PosixDirectoryPathAnchor(self.descriptor)
            return resolver.current_path()
        if self.path_anchor is None:
            raise PdfAssemblyError(
                f"safe {self.label} directory anchor is unavailable: {self.path}"
            )
        try:
            return self.path_anchor.current_path()  # type: ignore[attr-defined]
        except pdf_acquire_module.PdfAcquireError as error:
            raise PdfAssemblyError(str(error)) from error

    def verify_visible(self) -> None:
        try:
            result = self.path.lstat()
        except OSError as error:
            raise PdfAssemblyError(
                f"{self.label} directory changed identity: {self.path}: {error}"
            ) from error
        if (
            not stat.S_ISDIR(result.st_mode)
            or _is_reparse_stat(result)
            or (result.st_dev, result.st_ino) != self.identity
        ):
            raise PdfAssemblyError(
                f"{self.label} directory changed identity: {self.path}"
            )

    def close(self) -> None:
        if self.descriptor is not None:
            try:
                os.close(self.descriptor)
            except OSError:
                pass
            self.descriptor = None
        if self.path_anchor is not None:
            try:
                self.path_anchor.close()  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                pass
            self.path_anchor = None


@dataclass(slots=True)
class _PublishedFile:
    identity: tuple[int, int]
    windows_handle: int | None = None


def assemble_pdf(
    run_dir: Path,
    translations: Mapping[str, Translation],
    glossary: Mapping[str, str],
    output_dir: Path,
) -> Path:
    """Create only ``run_dir/staged-output/translated.pdf`` and strict layout evidence."""
    run_dir = Path(run_dir)
    output_dir = Path(output_dir)
    _validate_destinations(run_dir, output_dir)
    document = _read_pdf_document(run_dir / "document.json")
    source = _read_pdf_source(run_dir / "source.json")
    if source.sha256 != document.source_sha256:
        raise PdfAssemblyError("source.json SHA-256 does not match document.json")
    segments = _read_pdf_segments(run_dir / "segments.jsonl")
    ordered = _normalize_pdf_translations(
        document,
        segments,
        translations,
        glossary,
    )
    page_size = _select_page_size(document)

    temporary: Path | None = None
    run_anchor: _DirectoryAnchor | None = None
    temporary_anchor: _DirectoryAnchor | None = None
    temporary_staging_anchor: _DirectoryAnchor | None = None
    staging_anchor: _DirectoryAnchor | None = None
    owned_staging_identity: tuple[int, int] | None = None
    owned_pdf_identity: _PublishedFile | None = None
    owned_layout_identity: _PublishedFile | None = None
    staging = run_dir / "staged-output"
    try:
        run_anchor = _open_directory_anchor(run_dir, "run")
        temporary = Path(tempfile.mkdtemp(prefix=".pdf-assembling-", dir=run_dir))
        temporary_anchor = _open_directory_anchor(temporary, "temporary assembly")
        temporary_staging = temporary / "staged-output"
        temporary_staging.mkdir()
        temporary_staging_anchor = _open_directory_anchor(
            temporary_staging, "temporary PDF staging"
        )
        temporary_pdf = temporary_staging / "translated.pdf"
        records = _build_basic_document(
            ordered,
            source,
            temporary_pdf,
            page_size,
        )
        digest = _sha256_file(temporary_pdf)
        layout = PdfAssemblyLayout(
            schema_version="1.0",
            reserved_output_dir=str(output_dir),
            staged_pdf_sha256=digest,
            page_size=page_size,
            minimum_font_size=MINIMUM_FONT_SIZE,
            flowables=tuple(records),
        )
        # Validate every field before either run-visible artifact is published.
        PdfAssemblyLayout.from_dict(layout.to_dict())
        temporary_layout = temporary / "layout.json"
        write_pdf_layout(temporary_layout, layout)

        staging.mkdir()
        owned_staging_identity = _path_identity(staging)
        staging_anchor = _open_directory_anchor(
            staging,
            "staging",
            expected_identity=owned_staging_identity,
        )
        assert temporary_staging_anchor is not None
        owned_pdf_identity = _publish_new_file(
            temporary_staging_anchor,
            "translated.pdf",
            staging_anchor,
            "translated.pdf",
        )
        assert temporary_anchor is not None
        assert run_anchor is not None
        owned_layout_identity = _publish_new_file(
            temporary_anchor,
            "layout.json",
            run_anchor,
            "layout.json",
        )
        return staging / "translated.pdf"
    except PdfAssemblyError:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise PdfAssemblyError(f"cannot assemble staged PDF: {error}") from error
    finally:
        active_exception = __import__("sys").exc_info()[0] is not None
        if active_exception:
            if run_anchor is not None:
                _remove_owned_file(run_anchor, "layout.json", owned_layout_identity)
            if staging_anchor is not None:
                _remove_owned_file(
                    staging_anchor, "translated.pdf", owned_pdf_identity
                )
        if staging_anchor is not None:
            staging_anchor.close()
        if active_exception and run_anchor is not None:
            _remove_owned_directory(
                run_anchor, "staged-output", owned_staging_identity
            )
        _close_published_file(owned_layout_identity)
        _close_published_file(owned_pdf_identity)
        if temporary_staging_anchor is not None:
            temporary_staging_anchor.close()
        if temporary_anchor is not None:
            temporary_anchor.close()
        if run_anchor is not None:
            run_anchor.close()
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def _read_pdf_document(path: Path) -> PdfDocument:
    value = _read_json(path, "PDF document")
    try:
        return PdfDocument.from_dict(value)
    except (PdfContractError, TypeError, ValueError) as error:
        raise PdfAssemblyError(f"invalid PDF document {path}: {error}") from error


def _read_pdf_source(path: Path) -> PdfSourceRecord:
    value = _read_json(path, "PDF source record")
    try:
        return PdfSourceRecord.from_dict(value)
    except (PdfContractError, TypeError, ValueError) as error:
        raise PdfAssemblyError(f"invalid PDF source record {path}: {error}") from error


def _read_json(path: Path, context: str) -> Mapping[str, Any]:
    _require_regular_file(path, context)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PdfAssemblyError(f"cannot read {context} {path}: {error}") from error
    if not isinstance(value, Mapping):
        raise PdfAssemblyError(f"{context} must be a JSON object: {path}")
    return value


def _read_pdf_segments(path: Path) -> list[Segment]:
    _require_regular_file(path, "PDF segment manifest")
    try:
        return read_segments(path)
    except (OSError, UnicodeError, ValueError) as error:
        raise PdfAssemblyError(f"cannot read PDF segment manifest {path}: {error}") from error


def _normalize_pdf_translations(
    document: PdfDocument,
    segments: Sequence[Segment],
    translations: Mapping[str, Translation],
    glossary: Mapping[str, str],
) -> list[tuple[PdfBlock, Segment, str]]:
    if not isinstance(translations, Mapping):
        raise PdfAssemblyError("translations must be a mapping")
    segment_by_id: dict[str, Segment] = {}
    for segment in segments:
        if not isinstance(segment, Segment):
            raise PdfAssemblyError("segments must contain Segment values")
        if segment.id in segment_by_id:
            raise PdfAssemblyError(f"duplicate PDF segment ID: {segment.id}")
        segment_by_id[segment.id] = segment

    ordered_pairs: list[tuple[PdfBlock, Segment]] = []
    block_segment_ids: list[str] = []
    for block in document.blocks:
        if block.kind in _IGNORED_KINDS:
            if block.segment_id is not None:
                raise PdfAssemblyError(
                    f"ignored PDF block cannot have a translation segment: {block.id}"
                )
            continue
        if block.kind not in _BASIC_KINDS:
            raise PdfAssemblyError(
                f"unsupported PDF block kind before Task 8: {block.kind} ({block.id})"
            )
        if block.segment_id is None:
            raise PdfAssemblyError(f"visible PDF block is missing a segment: {block.id}")
        segment = segment_by_id.get(block.segment_id)
        if segment is None:
            raise PdfAssemblyError(
                f"PDF block references a missing segment: {block.segment_id}"
            )
        if not segment.target:
            raise PdfAssemblyError(f"PDF block segment is not a target: {segment.id}")
        if segment.locator != block.id:
            raise PdfAssemblyError(
                f"PDF segment locator does not match its block: {segment.id}"
            )
        if segment.semantic_type != block.kind:
            raise PdfAssemblyError(
                f"PDF segment semantic type does not match its block: {segment.id}"
            )
        ordered_pairs.append((block, segment))
        block_segment_ids.append(segment.id)

    target_ids = [segment.id for segment in segments if segment.target]
    if block_segment_ids != target_ids:
        raise PdfAssemblyError(
            "PDF document blocks and target segments must match exactly in document order"
        )

    translation_map: dict[str, Translation] = {}
    for key, record in translations.items():
        if not isinstance(key, str) or not isinstance(record, Translation):
            raise PdfAssemblyError("translation keys must map to Translation values")
        if key != record.segment_id:
            raise PdfAssemblyError(f"translation key does not match record ID: {key}")
        translation_map[key] = record
    if set(translation_map) != set(block_segment_ids):
        missing = sorted(set(block_segment_ids) - set(translation_map))
        foreign = sorted(set(translation_map) - set(block_segment_ids))
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if foreign:
            details.append(f"foreign: {', '.join(foreign)}")
        raise PdfAssemblyError(
            "translations must exactly cover PDF targets (" + "; ".join(details) + ")"
        )

    ordered_records = [translation_map[segment.id] for _block, segment in ordered_pairs]
    try:
        normalized = normalize_first_use(
            ordered_records,
            glossary,
            protected_by_segment={
                segment.id: segment.protected for _block, segment in ordered_pairs
            },
        )
    except TerminologyError as error:
        raise PdfAssemblyError(f"terminology normalization failed: {error}") from error

    restored: list[tuple[PdfBlock, Segment, str]] = []
    for (block, segment), record in zip(ordered_pairs, normalized, strict=True):
        try:
            text = restore_tokens(record.text, segment.protected)
        except ProtectionError as error:
            raise PdfAssemblyError(f"cannot restore {segment.id}: {error}") from error
        if not text.strip():
            raise PdfAssemblyError(f"translated PDF block is empty: {segment.id}")
        restored.append((block, segment, text))
    return restored


def _build_basic_document(
    ordered: Sequence[tuple[PdfBlock, Segment, str]],
    source: PdfSourceRecord,
    destination: Path,
    page_size: PdfPageSize,
) -> list[PdfFlowableLayout]:
    records: list[PdfFlowableLayout] = []
    left_margin = right_margin = 54.0
    top_margin = bottom_margin = 54.0
    frame = (
        left_margin,
        bottom_margin,
        page_size.width - left_margin - right_margin,
        page_size.height - top_margin - bottom_margin,
    )
    story: list[Any] = []
    heading_sizes = sorted(
        {
            round(block.style.font_size)
            for block, _segment, _text in ordered
            if block.kind == "heading"
        },
        reverse=True,
    )
    list_levels = _relative_list_levels(ordered)
    try:
        with ExitStack() as stack:
            _register_fonts(stack)
            for block, _segment, text in ordered:
                bullet_text: str | None = None
                rendered_text = text
                if block.kind == "list-item":
                    bullet_text, rendered_text = _normalized_list_parts(block, text)
                font_size, style = _style_for_block(
                    block,
                    heading_sizes=heading_sizes,
                    list_level=list_levels.get(block.id, 0),
                )
                paragraph = Paragraph(
                    escape(rendered_text),
                    style,
                    bulletText=escape(bullet_text) if bullet_text is not None else None,
                )
                story.append(
                    TrackedFlowable(
                        paragraph,
                        block_id=block.id,
                        kind=block.kind,
                        source_order=block.order,
                        split_part=0,
                        font_size=font_size,
                        frame=frame,
                        records=records,
                    )
                )
            story.append(Spacer(1.0, 16.0))
            attribution_style = ParagraphStyle(
                "WT-SourceAttribution",
                fontName=REGULAR_FONT_NAME,
                fontSize=MINIMUM_FONT_SIZE,
                leading=11.0,
                textColor="#444444",
                spaceBefore=6.0,
                spaceAfter=0.0,
            )
            source_label = source.final_source
            generated = datetime.now(UTC).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )
            story.append(
                Paragraph(
                    f'<font name="{BOLD_FONT_NAME}">Source:</font> '
                    f"{escape(source_label)}<br/>"
                    f'<font name="{BOLD_FONT_NAME}">Generated:</font> '
                    f"{escape(generated)}",
                    attribution_style,
                )
            )
            pdf = SimpleDocTemplate(
                str(destination),
                pagesize=(page_size.width, page_size.height),
                leftMargin=left_margin,
                rightMargin=right_margin,
                topMargin=top_margin,
                bottomMargin=bottom_margin,
                title="Reviewed Korean translation",
                author="web-translator",
                subject="Selectable Korean PDF translation",
            )
            pdf.build(story)
    except PdfAssemblyError:
        raise
    except Exception as error:
        raise PdfAssemblyError(f"ReportLab PDF assembly failed: {error}") from error
    if not destination.is_file() or destination.stat().st_size == 0:
        raise PdfAssemblyError("ReportLab did not create a nonempty staged PDF")
    if [item.source_order for item in records] != sorted(
        item.source_order for item in records
    ):
        raise PdfAssemblyError("tracked flowables were emitted out of document order")
    return records


def _style_for_block(
    block: PdfBlock,
    *,
    heading_sizes: Sequence[int],
    list_level: int,
) -> tuple[float, ParagraphStyle]:
    alignment = _ALIGNMENTS[block.style.alignment]
    if block.kind == "heading":
        level = _normalized_heading_level(block, heading_sizes)
        font_size = {1: 18.0, 2: 16.0, 3: 14.0, 4: 12.5, 5: 11.5, 6: 11.0}[level]
        return font_size, ParagraphStyle(
            f"WT-Heading-{level}",
            fontName=BOLD_FONT_NAME,
            fontSize=font_size,
            leading=font_size * 1.25,
            alignment=alignment,
            spaceBefore=8.0 if block.order else 0.0,
            spaceAfter=max(6.0, min(18.0, block.style.space_after)),
            keepWithNext=True,
        )
    left_indent = 0.0
    bullet_indent = 0.0
    if block.kind == "list-item":
        left_indent = (max(0, list_level) + 1) * 18.0
        bullet_indent = left_indent - BODY_FONT_SIZE * 0.6
    style = ParagraphStyle(
        f"WT-{block.kind}-{block.order}",
        fontName=REGULAR_FONT_NAME,
        bulletFontName=REGULAR_FONT_NAME,
        bulletFontSize=BODY_FONT_SIZE,
        fontSize=BODY_FONT_SIZE,
        leading=15.0,
        alignment=alignment,
        leftIndent=left_indent,
        firstLineIndent=0.0,
        bulletIndent=bullet_indent,
        bulletAnchor="end",
        spaceAfter=max(4.0, min(14.0, block.style.space_after)),
    )
    return BODY_FONT_SIZE, style


def _normalized_heading_level(
    block: PdfBlock, heading_sizes: Sequence[int]
) -> int:
    numbered = re.match(r"^(\d+(?:\.\d+)*)[.)]?\s+", block.source_text)
    if numbered is not None:
        return max(1, min(6, numbered.group(1).count(".") + 1))
    rounded_size = round(block.style.font_size)
    if rounded_size in heading_sizes:
        return max(1, min(6, heading_sizes.index(rounded_size) + 1))
    return 1


def _normalized_list_parts(block: PdfBlock, text: str) -> tuple[str, str]:
    source_match = _LIST_MARKER.match(block.source_text)
    marker = source_match.group("marker") if source_match is not None else "•"
    translated_match = _LIST_MARKER.match(text)
    body = text[translated_match.end() :] if translated_match is not None else text
    if not body.strip():
        raise PdfAssemblyError(
            f"translated list item is empty after marker normalization: {block.id}"
        )
    return marker, body


def _relative_list_levels(
    ordered: Sequence[tuple[PdfBlock, Segment, str]],
) -> dict[str, int]:
    levels: dict[str, int] = {}
    run: list[PdfBlock] = []

    def assign() -> None:
        if not run:
            return
        clusters: list[float] = []
        for indentation in sorted(block.style.indentation for block in run):
            if not clusters or indentation - clusters[-1] > _LIST_INDENT_TOLERANCE:
                clusters.append(indentation)
        for block in run:
            level = min(
                range(len(clusters)),
                key=lambda index: abs(block.style.indentation - clusters[index]),
            )
            levels[block.id] = level
        run.clear()

    for block, _segment, _text in ordered:
        if block.kind == "list-item":
            run.append(block)
        else:
            assign()
    assign()
    return levels


def _register_fonts(stack: ExitStack) -> None:
    root = files("web_translator").joinpath("font_assets")
    try:
        provenance = json.loads(root.joinpath("PROVENANCE.json").read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PdfAssemblyError(f"cannot read bundled font provenance: {error}") from error
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "license",
        "outputs",
        "schema_version",
        "source_sha256",
        "source_url",
        "unicode_ranges",
    }:
        raise PdfAssemblyError("bundled font provenance fields are invalid")
    outputs = provenance.get("outputs")
    if not isinstance(outputs, Mapping):
        raise PdfAssemblyError("bundled font provenance outputs are invalid")
    license_record = provenance.get("license")
    if not isinstance(license_record, Mapping) or set(license_record) != {
        "planned_url",
        "planned_url_status",
        "sha256",
        "url",
    }:
        raise PdfAssemblyError("bundled font provenance license is invalid")
    if license_record.get("sha256") != FONT_LICENSE_SHA256:
        raise PdfAssemblyError("bundled font license provenance hash is invalid")
    try:
        license_data = root.joinpath("OFL.txt").read_bytes()
    except OSError as error:
        raise PdfAssemblyError(f"cannot read bundled font license: {error}") from error
    if hashlib.sha256(license_data).hexdigest() != FONT_LICENSE_SHA256:
        raise PdfAssemblyError("bundled font license hash mismatch")
    for registered_name, filename in (
        (REGULAR_FONT_NAME, "NotoSansKR-Regular.ttf"),
        (BOLD_FONT_NAME, "NotoSansKR-Bold.ttf"),
    ):
        record = outputs.get(filename)
        if not isinstance(record, Mapping) or set(record) != {"axes", "sha256"}:
            raise PdfAssemblyError(f"bundled font provenance is missing {filename}")
        expected_hash = record.get("sha256")
        if not isinstance(expected_hash, str):
            raise PdfAssemblyError(f"bundled font hash is invalid for {filename}")
        resource = root.joinpath(filename)
        try:
            data = resource.read_bytes()
        except OSError as error:
            raise PdfAssemblyError(f"cannot read bundled font {filename}: {error}") from error
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise PdfAssemblyError(f"bundled font hash mismatch: {filename}")
        try:
            resource_path = stack.enter_context(as_file(resource))
            pdfmetrics.registerFont(TTFont(registered_name, str(resource_path), validate=1))
        except Exception as error:
            raise PdfAssemblyError(f"cannot register bundled font {filename}: {error}") from error


def _select_page_size(document: PdfDocument) -> PdfPageSize:
    ratios = sorted(min(page.width, page.height) / max(page.width, page.height) for page in document.pages)
    midpoint = len(ratios) // 2
    median = (
        ratios[midpoint]
        if len(ratios) % 2
        else (ratios[midpoint - 1] + ratios[midpoint]) / 2.0
    )
    a4_ratio = min(A4) / max(A4)
    letter_ratio = min(LETTER) / max(LETTER)
    name, values = ("A4", A4) if abs(median - a4_ratio) <= abs(median - letter_ratio) else ("LETTER", LETTER)
    width, height = sorted(float(value) for value in values)
    return PdfPageSize(name=name, width=width, height=height)  # type: ignore[arg-type]


def _validate_destinations(run_dir: Path, output_dir: Path) -> None:
    if not run_dir.is_dir() or _is_link_or_reparse(run_dir):
        raise PdfAssemblyError(f"run directory is missing or unsafe: {run_dir}")
    _reject_linked_ancestors(run_dir)
    _reject_linked_ancestors(output_dir)
    run_resolved = run_dir.resolve()
    output_resolved = output_dir.resolve(strict=False)
    if output_resolved == run_resolved or run_resolved in output_resolved.parents:
        raise PdfAssemblyError("reserved final output directory must be outside the run directory")
    for path in (run_dir / "staged-output", run_dir / "layout.json", output_dir):
        if path.exists() or path.is_symlink():
            raise PdfAssemblyError(f"assembly destination already exists: {path}")


def _require_regular_file(path: Path, context: str) -> None:
    if not path.is_file() or _is_link_or_reparse(path):
        raise PdfAssemblyError(f"{context} is missing or unsafe: {path}")


def _reject_linked_ancestors(path: Path) -> None:
    candidate = path if path.exists() else path.parent
    while True:
        if candidate.exists() and _is_link_or_reparse(candidate):
            raise PdfAssemblyError(f"path uses a linked ancestor: {candidate}")
        parent = candidate.parent
        if parent == candidate:
            return
        candidate = parent


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or _is_reparse_stat(metadata)


def _is_reparse_stat(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _path_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    return metadata.st_dev, metadata.st_ino


def _open_directory_anchor(
    path: Path,
    label: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> _DirectoryAnchor:
    try:
        visible = path.lstat()
    except OSError as error:
        raise PdfAssemblyError(f"cannot inspect {label} directory {path}: {error}") from error
    identity = (visible.st_dev, visible.st_ino)
    if (
        not stat.S_ISDIR(visible.st_mode)
        or _is_reparse_stat(visible)
        or (expected_identity is not None and identity != expected_identity)
    ):
        raise PdfAssemblyError(f"{label} directory changed identity: {path}")

    descriptor: int | None = None
    path_anchor: object | None = None
    try:
        if os.name != "nt" and _DIRFD_PUBLICATION_SUPPORTED:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _is_reparse_stat(opened)
                or (opened.st_dev, opened.st_ino) != identity
            ):
                raise PdfAssemblyError(f"{label} directory changed identity: {path}")
        else:
            path_anchor = pdf_acquire_module._open_fallback_run_anchor(path)
            current = path_anchor.current_path()  # type: ignore[attr-defined]
            opened = current.lstat()
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _is_reparse_stat(opened)
                or (opened.st_dev, opened.st_ino) != identity
            ):
                raise PdfAssemblyError(f"{label} directory changed identity: {path}")
        return _DirectoryAnchor(path, label, identity, descriptor, path_anchor)
    except PdfAssemblyError:
        if descriptor is not None:
            os.close(descriptor)
        if path_anchor is not None:
            path_anchor.close()  # type: ignore[attr-defined]
        raise
    except (pdf_acquire_module.PdfAcquireError, NotImplementedError, OSError) as error:
        if descriptor is not None:
            os.close(descriptor)
        if path_anchor is not None:
            path_anchor.close()  # type: ignore[attr-defined]
        raise PdfAssemblyError(
            f"safe {label} directory anchor unavailable: {path}: {error}"
        ) from error
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if path_anchor is not None:
            path_anchor.close()  # type: ignore[attr-defined]
        raise


def _anchored_entry_stat(anchor: _DirectoryAnchor, name: str) -> os.stat_result:
    if Path(name).name != name:
        raise PdfAssemblyError(f"unsafe anchored assembly name: {name}")
    try:
        if anchor.descriptor is not None:
            return os.stat(name, dir_fd=anchor.descriptor, follow_symlinks=False)
        return os.lstat(anchor.current_path() / name)
    except (NotImplementedError, OSError) as error:
        raise PdfAssemblyError(
            f"cannot inspect anchored assembly artifact {anchor.path / name}: {error}"
        ) from error


def _anchored_regular_file_stat(
    anchor: _DirectoryAnchor, name: str
) -> os.stat_result:
    result = _anchored_entry_stat(anchor, name)
    if not stat.S_ISREG(result.st_mode) or _is_reparse_stat(result):
        raise PdfAssemblyError(
            f"anchored assembly artifact is not a regular file: {anchor.path / name}"
        )
    return result


def _publish_new_file(
    source_directory: _DirectoryAnchor,
    source_name: str,
    destination_directory: _DirectoryAnchor,
    destination_name: str,
) -> _PublishedFile:
    source = _anchored_regular_file_stat(source_directory, source_name)
    source_identity = (source.st_dev, source.st_ino)
    destination = destination_directory.path / destination_name
    published = _PublishedFile(source_identity)
    visible = False
    try:
        # Treat the destination name as potentially owned before entering the
        # publication syscall so a post-syscall BaseException still cleans by
        # exact source inode/handle. A raced foreign inode never matches.
        visible = True
        if _IS_WINDOWS:
            published.windows_handle = _windows_move_anchored_file(
                source_directory,
                source_name,
                destination_directory,
                destination_name,
            )
        elif (
            source_directory.descriptor is not None
            and destination_directory.descriptor is not None
        ):
            os.link(
                source_name,
                destination_name,
                src_dir_fd=source_directory.descriptor,
                dst_dir_fd=destination_directory.descriptor,
                follow_symlinks=False,
            )
        else:
            raise PdfAssemblyError(
                f"safe anchored assembly publication unavailable: {destination}"
            )
        result = _anchored_regular_file_stat(
            destination_directory, destination_name
        )
        if (result.st_dev, result.st_ino) != source_identity:
            raise PdfAssemblyError(
                f"assembly destination changed identity: {destination}"
            )
        destination_directory.verify_visible()
        return published
    except FileExistsError as error:
        raise PdfAssemblyError(f"assembly destination already exists: {destination}") from error
    except PdfAssemblyError:
        raise
    except (AttributeError, NotImplementedError, OSError) as error:
        raise PdfAssemblyError(
            f"safe anchored assembly publication unavailable: {destination}: {error}"
        ) from error
    finally:
        if visible and __import__("sys").exc_info()[0] is not None:
            _remove_owned_file(destination_directory, destination_name, published)


def _windows_move_anchored_file(
    source_directory: _DirectoryAnchor,
    source_name: str,
    destination_directory: _DirectoryAnchor,
    destination_name: str,
) -> int:
    destination = destination_directory.path / destination_name
    if os.name != "nt":
        raise PdfAssemblyError("Windows anchored publication is unavailable")
    path_anchor = destination_directory.path_anchor
    destination_handle = getattr(path_anchor, "handle", None)
    if not isinstance(destination_handle, int):
        raise PdfAssemblyError(
            f"safe Windows destination handle unavailable: {destination_directory.path}"
        )
    source_path = source_directory.current_path() / source_name
    source_handle: int | None = None
    keep_handle = False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        raw_handle = create_file(
            str(source_path),
            0x00010000 | 0x00000080,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x00000080 | 0x00200000,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if raw_handle in (None, invalid_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        source_handle = int(raw_handle)

        payload = _windows_file_rename_information(
            destination_handle,
            destination_name,
        )
        buffer_size = len(payload)
        buffer = ctypes.create_string_buffer(payload, buffer_size)
        set_information = kernel32.SetFileInformationByHandle
        set_information.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        set_information.restype = wintypes.BOOL
        if not set_information(source_handle, 3, buffer, buffer_size):
            raise ctypes.WinError(ctypes.get_last_error())
        keep_handle = True
        return source_handle
    except OSError as error:
        if getattr(error, "winerror", None) in {80, 183}:
            raise PdfAssemblyError(
                f"assembly destination already exists: {destination}"
            ) from error
        raise PdfAssemblyError(
            f"safe Windows anchored publication unavailable: {destination}: {error}"
        ) from error
    except (AttributeError, NotImplementedError) as error:
        raise PdfAssemblyError(
            f"safe Windows anchored publication unavailable: {destination}: {error}"
        ) from error
    finally:
        if source_handle is not None and not keep_handle:
            pdf_acquire_module._close_windows_handle(source_handle)


def _windows_file_rename_information(
    root_handle: int,
    destination_name: str,
    *,
    pointer_size: int | None = None,
) -> bytes:
    if Path(destination_name).name != destination_name:
        raise PdfAssemblyError(
            f"unsafe Windows anchored assembly name: {destination_name}"
        )
    if pointer_size is None:
        import ctypes

        pointer_size = ctypes.sizeof(ctypes.c_void_p)
    if pointer_size not in {4, 8}:
        raise PdfAssemblyError(
            f"unsupported Windows pointer size for assembly: {pointer_size}"
        )
    encoded_name = destination_name.encode("utf-16-le")
    root_offset = 4 if pointer_size == 4 else 8
    length_offset = root_offset + pointer_size
    name_offset = length_offset + 4
    payload = bytearray(name_offset + len(encoded_name))
    struct.pack_into("<I", payload, 0, 0)
    struct.pack_into(
        "<I" if pointer_size == 4 else "<Q",
        payload,
        root_offset,
        root_handle,
    )
    struct.pack_into("<I", payload, length_offset, len(encoded_name))
    payload[name_offset:] = encoded_name
    return bytes(payload)


def _windows_delete_open_file(handle: int) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FileDispositionInfo(ctypes.Structure):
            _fields_ = (("delete_file", wintypes.BOOL),)

        information = FileDispositionInfo(True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        set_information = kernel32.SetFileInformationByHandle
        set_information.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        set_information.restype = wintypes.BOOL
        set_information(
            handle,
            4,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
    except (AttributeError, OSError):
        return


def _close_published_file(published: _PublishedFile | None) -> None:
    if published is None or published.windows_handle is None:
        return
    pdf_acquire_module._close_windows_handle(published.windows_handle)
    published.windows_handle = None


def _remove_owned_file(
    directory: _DirectoryAnchor,
    name: str,
    published: _PublishedFile | None,
) -> None:
    if published is None:
        return
    if published.windows_handle is not None:
        _windows_delete_open_file(published.windows_handle)
        _close_published_file(published)
        return
    try:
        result = _anchored_regular_file_stat(directory, name)
        if (result.st_dev, result.st_ino) != published.identity:
            return
        if directory.descriptor is not None:
            os.unlink(name, dir_fd=directory.descriptor)
        else:
            (directory.current_path() / name).unlink()
    except (PdfAssemblyError, NotImplementedError, OSError):
        return


def _remove_owned_directory(
    parent: _DirectoryAnchor,
    name: str,
    identity: tuple[int, int] | None,
) -> None:
    if identity is None:
        return
    try:
        result = _anchored_entry_stat(parent, name)
        if (
            not stat.S_ISDIR(result.st_mode)
            or _is_reparse_stat(result)
            or (result.st_dev, result.st_ino) != identity
        ):
            return
        if parent.descriptor is not None:
            os.rmdir(name, dir_fd=parent.descriptor)
        else:
            os.rmdir(parent.current_path() / name)
    except (PdfAssemblyError, NotImplementedError, OSError):
        return


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise PdfAssemblyError(f"cannot hash staged PDF {path}: {error}") from error
    return digest.hexdigest()


__all__ = ["PdfAssemblyError", "assemble_pdf"]
