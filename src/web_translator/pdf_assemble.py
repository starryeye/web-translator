"""Basic, fail-closed ReportLab assembly for reviewed PDF translations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
from importlib.resources import as_file, files
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import sys
from typing import Any, BinaryIO
from xml.sax.saxutils import escape

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

import web_translator.pdf_acquire as pdf_acquire_module
from web_translator.models import (
    Segment,
    SegmentContractError,
    Translation,
    read_segments_stream,
)
from web_translator.pdf_flowables import (
    PdfAssemblyError,
    PdfAssemblyLayout,
    PdfFlowableLayout,
    PdfPageSize,
    TrackedFlowable,
)
from web_translator.pdf_layout import split_list_marker
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
    for operation in (os.link, os.mkdir, os.open, os.rmdir, os.stat, os.unlink)
)
_BASIC_KINDS = {"heading", "paragraph", "list-item"}
_IGNORED_KINDS = {"header", "footer", "page-number"}
_ALIGNMENTS = {
    "left": TA_LEFT,
    "center": TA_CENTER,
    "right": TA_RIGHT,
    "justify": TA_JUSTIFY,
}
_LIST_INDENT_TOLERANCE = 3.0
_CANONICAL_BULLET_MARKERS = {"•", "‣", "◦", "⁃", "∙"}


@dataclass(slots=True)
class _DirectoryAnchor:
    path: Path
    label: str
    identity: tuple[int, int]
    descriptor: int | None
    path_anchor: object | None = None
    owned_files: dict[str, tuple[int, int]] = field(default_factory=dict)

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


@dataclass(slots=True)
class _OpenedFile:
    stream: BinaryIO
    identity: tuple[int, int]


def assemble_pdf(
    run_dir: Path,
    translations: Mapping[str, Translation],
    glossary: Mapping[str, str],
    output_dir: Path,
) -> Path:
    """Create only ``run_dir/staged-output/translated.pdf`` and strict layout evidence."""
    run_dir = Path(run_dir)
    output_dir = Path(output_dir)
    temporary_name: str | None = None
    run_anchor: _DirectoryAnchor | None = None
    evidence_files: dict[str, _OpenedFile] = {}
    temporary_anchor: _DirectoryAnchor | None = None
    temporary_staging_anchor: _DirectoryAnchor | None = None
    staging_anchor: _DirectoryAnchor | None = None
    temporary_pdf: _OpenedFile | None = None
    temporary_layout: _OpenedFile | None = None
    owned_staging_identity: tuple[int, int] | None = None
    owned_pdf_identity: _PublishedFile | None = None
    owned_layout_identity: _PublishedFile | None = None
    staging = run_dir / "staged-output"
    try:
        run_anchor = _open_directory_anchor(run_dir, "run")
        _validate_destinations(run_anchor, output_dir)
        for name, context in (
            ("document.json", "PDF document"),
            ("source.json", "PDF source record"),
            ("segments.jsonl", "PDF segment manifest"),
        ):
            evidence_files[name] = _open_anchored_input_file(
                run_anchor,
                name,
                context,
            )
        _verify_anchored_evidence(run_anchor, evidence_files)
        document = _read_pdf_document(
            evidence_files["document.json"],
            run_dir / "document.json",
        )
        source = _read_pdf_source(
            evidence_files["source.json"],
            run_dir / "source.json",
        )
        if source.sha256 != document.source_sha256:
            raise PdfAssemblyError("source.json SHA-256 does not match document.json")
        segments = _read_pdf_segments(
            evidence_files["segments.jsonl"],
            run_dir / "segments.jsonl",
        )
        ordered = _normalize_pdf_translations(
            document,
            segments,
            translations,
            glossary,
        )
        page_size = _select_page_size(document)
        _verify_anchored_evidence(run_anchor, evidence_files)
        temporary_name, temporary_anchor = _create_unique_child_directory(
            run_anchor,
            ".pdf-assembling-",
            "temporary assembly",
        )
        temporary_staging_anchor = _create_child_directory(
            temporary_anchor,
            "staged-output",
            "temporary PDF staging",
        )
        temporary_pdf = _create_anchored_binary_file(
            temporary_staging_anchor,
            "translated.pdf",
        )
        records = _build_basic_document(
            ordered,
            source,
            temporary_pdf.stream,
            page_size,
        )
        digest = _finalize_opened_file(temporary_pdf, "staged PDF")
        temporary_pdf.stream.close()
        temporary_staging_anchor.verify_visible()
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
        temporary_layout = _create_anchored_binary_file(
            temporary_anchor,
            "layout.json",
        )
        _write_layout_stream(temporary_layout.stream, layout)
        _finalize_opened_file(temporary_layout, "layout evidence")
        temporary_layout.stream.close()
        temporary_anchor.verify_visible()

        _verify_anchored_evidence(run_anchor, evidence_files)
        staging_anchor = _create_child_directory(
            run_anchor,
            "staged-output",
            "staging",
        )
        owned_staging_identity = staging_anchor.identity
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
        run_anchor.verify_visible()
        staging_anchor.verify_visible()
        return staging / "translated.pdf"
    except PdfAssemblyError:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise PdfAssemblyError(f"cannot assemble staged PDF: {error}") from error
    finally:
        active_exception = sys.exc_info()[0] is not None
        _close_opened_file(temporary_layout)
        _close_opened_file(temporary_pdf)
        if active_exception:
            if run_anchor is not None:
                _remove_owned_file(run_anchor, "layout.json", owned_layout_identity)
            if staging_anchor is not None:
                _remove_owned_file(
                    staging_anchor, "translated.pdf", owned_pdf_identity
                )
        _close_published_file(owned_layout_identity)
        _close_published_file(owned_pdf_identity)
        if temporary_staging_anchor is not None:
            identity = temporary_staging_anchor.owned_files.get("translated.pdf")
            if identity is not None:
                _remove_owned_file(
                    temporary_staging_anchor,
                    "translated.pdf",
                    _PublishedFile(identity),
                )
        if temporary_anchor is not None and temporary_staging_anchor is not None:
            _remove_owned_directory(
                temporary_anchor,
                "staged-output",
                temporary_staging_anchor.identity,
                child=temporary_staging_anchor,
            )
        if temporary_staging_anchor is not None:
            temporary_staging_anchor.close()
        if temporary_anchor is not None:
            identity = temporary_anchor.owned_files.get("layout.json")
            if identity is not None:
                _remove_owned_file(
                    temporary_anchor,
                    "layout.json",
                    _PublishedFile(identity),
                )
        if (
            run_anchor is not None
            and temporary_anchor is not None
            and temporary_name is not None
        ):
            _remove_owned_directory(
                run_anchor,
                temporary_name,
                temporary_anchor.identity,
                child=temporary_anchor,
            )
        if temporary_anchor is not None:
            temporary_anchor.close()
        if active_exception and run_anchor is not None and staging_anchor is not None:
            _remove_owned_directory(
                run_anchor,
                "staged-output",
                owned_staging_identity,
                child=staging_anchor,
            )
        if staging_anchor is not None:
            staging_anchor.close()
        for opened in evidence_files.values():
            _close_opened_file(opened)
        if run_anchor is not None:
            run_anchor.close()


def _read_pdf_document(opened: _OpenedFile, path: Path) -> PdfDocument:
    value = _read_json(opened, path, "PDF document")
    try:
        return PdfDocument.from_dict(value)
    except (PdfContractError, TypeError, ValueError) as error:
        raise PdfAssemblyError(f"invalid PDF document {path}: {error}") from error


def _read_pdf_source(opened: _OpenedFile, path: Path) -> PdfSourceRecord:
    value = _read_json(opened, path, "PDF source record")
    try:
        return PdfSourceRecord.from_dict(value)
    except (PdfContractError, TypeError, ValueError) as error:
        raise PdfAssemblyError(f"invalid PDF source record {path}: {error}") from error


def _read_json(
    opened: _OpenedFile,
    path: Path,
    context: str,
) -> Mapping[str, Any]:
    try:
        value = json.loads(_read_opened_utf8(opened, path, context))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PdfAssemblyError(f"cannot read {context} {path}: {error}") from error
    if not isinstance(value, Mapping):
        raise PdfAssemblyError(f"{context} must be a JSON object: {path}")
    return value


def _read_pdf_segments(opened: _OpenedFile, path: Path) -> list[Segment]:
    try:
        text = _read_opened_utf8(opened, path, "PDF segment manifest")
        return read_segments_stream(io.StringIO(text))
    except (OSError, UnicodeError, SegmentContractError, ValueError) as error:
        raise PdfAssemblyError(f"cannot read PDF segment manifest {path}: {error}") from error


def _read_opened_utf8(
    opened: _OpenedFile,
    path: Path,
    context: str,
) -> str:
    try:
        opened.stream.seek(0)
        payload = opened.stream.read()
        if not isinstance(payload, bytes):
            raise TypeError("anchored evidence stream did not return bytes")
        return payload.decode("utf-8")
    except (OSError, UnicodeError, TypeError) as error:
        raise PdfAssemblyError(f"cannot read {context} {path}: {error}") from error


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
    destination: BinaryIO,
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
                destination,
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
    source_parts = split_list_marker(block.source_text)
    marker = source_parts[0] if source_parts is not None else "•"
    if marker in _CANONICAL_BULLET_MARKERS:
        marker = "•"
    translated_parts = split_list_marker(text)
    body = translated_parts[1] if translated_parts is not None else text
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


def _validate_destinations(
    run_anchor: _DirectoryAnchor,
    output_dir: Path,
) -> None:
    run_anchor.verify_visible()
    _reject_linked_ancestors(run_anchor.path)
    _reject_linked_ancestors(output_dir)
    try:
        run_resolved = run_anchor.current_path().resolve(strict=True)
    except OSError as error:
        raise PdfAssemblyError(
            f"cannot resolve anchored run directory {run_anchor.path}: {error}"
        ) from error
    output_resolved = output_dir.resolve(strict=False)
    if output_resolved == run_resolved or run_resolved in output_resolved.parents:
        raise PdfAssemblyError("reserved final output directory must be outside the run directory")
    for name in ("staged-output", "layout.json"):
        _require_anchored_name_absent(run_anchor, name)
    if output_dir.exists() or output_dir.is_symlink():
        raise PdfAssemblyError(
            f"assembly destination already exists: {output_dir}"
        )


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


def _require_anchored_name_absent(
    directory: _DirectoryAnchor,
    name: str,
) -> None:
    if Path(name).name != name:
        raise PdfAssemblyError(f"unsafe anchored assembly name: {name}")
    handle: int | None = None
    try:
        if directory.descriptor is not None:
            os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
        elif _IS_WINDOWS:
            handle = _windows_open_relative_entry(
                _windows_anchor_handle(directory),
                name,
            )
        else:
            raise PdfAssemblyError(
                f"safe anchored destination check unavailable: "
                f"{directory.path / name}"
            )
    except FileNotFoundError:
        return
    except PdfAssemblyError:
        raise
    except (AttributeError, NotImplementedError, OSError) as error:
        raise PdfAssemblyError(
            f"cannot inspect anchored assembly destination "
            f"{directory.path / name}: {error}"
        ) from error
    finally:
        if handle is not None:
            pdf_acquire_module._close_windows_handle(handle)
    raise PdfAssemblyError(
        f"assembly destination already exists: {directory.path / name}"
    )


def _open_anchored_input_file(
    directory: _DirectoryAnchor,
    name: str,
    context: str,
) -> _OpenedFile:
    if Path(name).name != name:
        raise PdfAssemblyError(f"unsafe anchored evidence name: {name}")
    directory.verify_visible()
    descriptor: int | None = None
    windows_handle: int | None = None
    stream: BinaryIO | None = None
    try:
        if directory.descriptor is not None:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory.descriptor,
            )
            result = os.fstat(descriptor)
            if not stat.S_ISREG(result.st_mode) or _is_reparse_stat(result):
                raise PdfAssemblyError(
                    f"{context} is not an anchored regular file: "
                    f"{directory.path / name}"
                )
            identity = (result.st_dev, result.st_ino)
            stream = os.fdopen(descriptor, "rb")
            descriptor = None
        elif _IS_WINDOWS:
            windows_handle = _windows_open_relative_read_file(
                _windows_anchor_handle(directory),
                name,
            )
            identity = _windows_file_identity(
                windows_handle,
                require_regular=True,
            )
            import msvcrt

            descriptor = msvcrt.open_osfhandle(
                windows_handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
            windows_handle = None
            stream = os.fdopen(descriptor, "rb")
            descriptor = None
        else:
            raise PdfAssemblyError(
                f"safe anchored input open unavailable: {directory.path / name}"
            )
        opened = _OpenedFile(stream, identity)
        directory.verify_visible()
        _verify_anchored_input_identity(directory, name, identity)
        return opened
    except PdfAssemblyError:
        raise
    except (AttributeError, NotImplementedError, OSError) as error:
        raise PdfAssemblyError(
            f"cannot open {context} {directory.path / name}: {error}"
        ) from error
    finally:
        if sys.exc_info()[0] is not None:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if windows_handle is not None:
                pdf_acquire_module._close_windows_handle(windows_handle)


def _verify_anchored_evidence(
    directory: _DirectoryAnchor,
    evidence_files: Mapping[str, _OpenedFile],
) -> None:
    directory.verify_visible()
    for name, opened in evidence_files.items():
        _verify_anchored_input_identity(directory, name, opened.identity)


def _verify_anchored_input_identity(
    directory: _DirectoryAnchor,
    name: str,
    expected: tuple[int, int],
) -> None:
    handle: int | None = None
    try:
        if directory.descriptor is not None:
            result = _anchored_regular_file_stat(directory, name)
            identity = (result.st_dev, result.st_ino)
        elif _IS_WINDOWS:
            handle = _windows_open_relative_read_file(
                _windows_anchor_handle(directory),
                name,
            )
            identity = _windows_file_identity(handle, require_regular=True)
        else:
            raise PdfAssemblyError(
                f"safe anchored input verification unavailable: "
                f"{directory.path / name}"
            )
    finally:
        if handle is not None:
            pdf_acquire_module._close_windows_handle(handle)
    if identity != expected:
        raise PdfAssemblyError(
            f"anchored input changed identity: {directory.path / name}"
        )


def _create_unique_child_directory(
    parent: _DirectoryAnchor,
    prefix: str,
    label: str,
) -> tuple[str, _DirectoryAnchor]:
    for _ in range(100):
        name = f"{prefix}{secrets.token_hex(8)}"
        try:
            return name, _create_child_directory(parent, name, label)
        except FileExistsError:
            continue
    raise PdfAssemblyError(
        f"cannot reserve unique {label} directory in {parent.path}"
    )


def _create_child_directory(
    parent: _DirectoryAnchor,
    name: str,
    label: str,
) -> _DirectoryAnchor:
    if Path(name).name != name:
        raise PdfAssemblyError(f"unsafe anchored assembly directory name: {name}")
    parent.verify_visible()
    descriptor: int | None = None
    path_anchor: object | None = None
    windows_handle: int | None = None
    identity: tuple[int, int] | None = None
    created = False
    creation_attempted = False
    try:
        if parent.descriptor is not None:
            try:
                creation_attempted = True
                os.mkdir(name, 0o700, dir_fd=parent.descriptor)
            except FileExistsError:
                raise
            created = True
            result = os.stat(
                name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(result.st_mode) or _is_reparse_stat(result):
                raise PdfAssemblyError(
                    f"anchored {label} is not a safe directory: {parent.path / name}"
                )
            identity = (result.st_dev, result.st_ino)
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent.descriptor,
            )
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != identity:
                raise PdfAssemblyError(
                    f"{label} directory changed identity while opening"
                )
        elif _IS_WINDOWS:
            parent_handle = _windows_anchor_handle(parent)
            windows_handle = _windows_create_relative_directory(parent_handle, name)
            created = True
            path_anchor = pdf_acquire_module._WindowsDirectoryPathAnchor(
                windows_handle
            )
            windows_handle = None
            current = path_anchor.current_path()  # type: ignore[attr-defined]
            result = current.lstat()
            if not stat.S_ISDIR(result.st_mode) or _is_reparse_stat(result):
                raise PdfAssemblyError(
                    f"anchored {label} is not a safe directory: {parent.path / name}"
                )
            identity = (result.st_dev, result.st_ino)
        else:
            raise PdfAssemblyError(
                f"safe anchored child creation unavailable: {parent.path / name}"
            )
        child = _DirectoryAnchor(
            parent.path / name,
            label,
            identity,
            descriptor,
            path_anchor,
        )
        parent.verify_visible()
        child.verify_visible()
        return child
    except FileExistsError:
        raise
    except PdfAssemblyError:
        raise
    except (AttributeError, NotImplementedError, OSError) as error:
        raise PdfAssemblyError(
            f"cannot create anchored {label} directory {parent.path / name}: {error}"
        ) from error
    finally:
        if sys.exc_info()[0] is not None:
            active_error = sys.exc_info()[1]
            if (
                identity is None
                and creation_attempted
                and not isinstance(active_error, OSError)
                and parent.descriptor is not None
            ):
                try:
                    possible = os.stat(
                        name,
                        dir_fd=parent.descriptor,
                        follow_symlinks=False,
                    )
                    if stat.S_ISDIR(possible.st_mode) and not _is_reparse_stat(
                        possible
                    ):
                        identity = (possible.st_dev, possible.st_ino)
                except OSError:
                    pass
            cleanup_anchor: _DirectoryAnchor | None = None
            if identity is not None:
                cleanup_anchor = _DirectoryAnchor(
                    parent.path / name,
                    label,
                    identity,
                    descriptor,
                    path_anchor,
                )
                _remove_owned_directory(
                    parent,
                    name,
                    identity,
                    child=cleanup_anchor,
                )
            elif created and path_anchor is not None:
                _windows_delete_open_file(_windows_path_anchor_handle(path_anchor))
            if windows_handle is not None:
                _windows_delete_open_file(windows_handle)
                pdf_acquire_module._close_windows_handle(windows_handle)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if path_anchor is not None:
                try:
                    path_anchor.close()  # type: ignore[attr-defined]
                except (AttributeError, OSError):
                    pass


def _create_anchored_binary_file(
    directory: _DirectoryAnchor,
    name: str,
) -> _OpenedFile:
    if Path(name).name != name:
        raise PdfAssemblyError(f"unsafe anchored assembly name: {name}")
    directory.verify_visible()
    descriptor: int | None = None
    windows_handle: int | None = None
    stream: BinaryIO | None = None
    identity: tuple[int, int] | None = None
    creation_attempted = False
    try:
        if directory.descriptor is not None:
            creation_attempted = True
            descriptor = os.open(
                name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory.descriptor,
            )
            result = os.fstat(descriptor)
            if not stat.S_ISREG(result.st_mode) or _is_reparse_stat(result):
                raise PdfAssemblyError(
                    f"anchored assembly artifact is not a regular file: "
                    f"{directory.path / name}"
                )
            identity = (result.st_dev, result.st_ino)
            stream = os.fdopen(descriptor, "w+b")
            descriptor = None
        elif _IS_WINDOWS:
            windows_handle = _windows_create_relative_file(
                _windows_anchor_handle(directory),
                name,
            )
            identity = _windows_file_identity(windows_handle, require_regular=True)
            import msvcrt

            descriptor = msvcrt.open_osfhandle(
                windows_handle,
                os.O_RDWR | os.O_BINARY,
            )
            windows_handle = None
            stream = os.fdopen(descriptor, "w+b")
            descriptor = None
        else:
            raise PdfAssemblyError(
                f"safe anchored file creation unavailable: {directory.path / name}"
            )
        directory.owned_files[name] = identity
        directory.verify_visible()
        return _OpenedFile(stream, identity)
    except FileExistsError as error:
        raise PdfAssemblyError(
            f"assembly destination already exists: {directory.path / name}"
        ) from error
    except PdfAssemblyError:
        raise
    except (AttributeError, NotImplementedError, OSError) as error:
        raise PdfAssemblyError(
            f"cannot create anchored assembly file {directory.path / name}: {error}"
        ) from error
    finally:
        if sys.exc_info()[0] is not None:
            active_error = sys.exc_info()[1]
            if (
                identity is None
                and creation_attempted
                and not isinstance(active_error, OSError)
                and directory.descriptor is not None
            ):
                try:
                    possible = os.stat(
                        name,
                        dir_fd=directory.descriptor,
                        follow_symlinks=False,
                    )
                    if stat.S_ISREG(possible.st_mode) and not _is_reparse_stat(
                        possible
                    ):
                        identity = (possible.st_dev, possible.st_ino)
                except OSError:
                    pass
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if windows_handle is not None:
                _windows_delete_open_file(windows_handle)
                pdf_acquire_module._close_windows_handle(windows_handle)
            if identity is not None:
                _remove_owned_file(
                    directory,
                    name,
                    _PublishedFile(identity),
                )


def _write_layout_stream(stream: BinaryIO, layout: PdfAssemblyLayout) -> None:
    validated = PdfAssemblyLayout.from_dict(layout.to_dict())
    serialized = (
        json.dumps(
            validated.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    stream.write(serialized)


def _finalize_opened_file(opened: _OpenedFile, label: str) -> str:
    try:
        opened.stream.flush()
        os.fsync(opened.stream.fileno())
        if _IS_WINDOWS:
            import msvcrt

            handle = msvcrt.get_osfhandle(opened.stream.fileno())
            current_identity = _windows_file_identity(
                handle,
                require_regular=True,
            )
        else:
            result = os.fstat(opened.stream.fileno())
            if not stat.S_ISREG(result.st_mode) or _is_reparse_stat(result):
                raise PdfAssemblyError(f"{label} handle is not a regular file")
            current_identity = (result.st_dev, result.st_ino)
        if current_identity != opened.identity:
            raise PdfAssemblyError(f"{label} changed identity while writing")
        opened.stream.seek(0)
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: opened.stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
        if size == 0:
            raise PdfAssemblyError(f"{label} is empty")
        return digest.hexdigest()
    except PdfAssemblyError:
        raise
    except (AttributeError, NotImplementedError, OSError) as error:
        raise PdfAssemblyError(f"cannot finalize {label}: {error}") from error


def _close_opened_file(opened: _OpenedFile | None) -> None:
    if opened is None or opened.stream.closed:
        return
    try:
        opened.stream.close()
    except OSError:
        return


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
    destination = destination_directory.path / destination_name
    if _IS_WINDOWS:
        source_identity = source_directory.owned_files.get(source_name)
        if source_identity is None:
            raise PdfAssemblyError(
                f"anchored Windows source identity is unavailable: "
                f"{source_directory.path / source_name}"
            )
    else:
        source = _anchored_regular_file_stat(source_directory, source_name)
        source_identity = (source.st_dev, source.st_ino)
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
        if _IS_WINDOWS:
            verification_handle = _windows_open_relative_file(
                _windows_anchor_handle(destination_directory),
                destination_name,
            )
            try:
                if (
                    _windows_file_identity(
                        verification_handle,
                        require_regular=True,
                    )
                    != source_identity
                ):
                    raise PdfAssemblyError(
                        f"assembly destination changed identity: {destination}"
                    )
            finally:
                pdf_acquire_module._close_windows_handle(verification_handle)
        else:
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
        if visible and sys.exc_info()[0] is not None:
            _remove_owned_file(destination_directory, destination_name, published)


def _windows_move_anchored_file(
    source_directory: _DirectoryAnchor,
    source_name: str,
    destination_directory: _DirectoryAnchor,
    destination_name: str,
) -> int:
    destination = destination_directory.path / destination_name
    if not _IS_WINDOWS:
        raise PdfAssemblyError("Windows anchored publication is unavailable")
    source_root_handle = _windows_anchor_handle(source_directory)
    destination_handle = _windows_anchor_handle(destination_directory)
    source_handle: int | None = None
    keep_handle = False
    try:
        source_handle = _windows_open_relative_file(
            source_root_handle,
            source_name,
        )
        expected_identity = source_directory.owned_files.get(source_name)
        if (
            expected_identity is not None
            and _windows_file_identity(source_handle, require_regular=True)
            != expected_identity
        ):
            raise PdfAssemblyError(
                f"anchored assembly source changed identity: "
                f"{source_directory.path / source_name}"
            )
        _windows_rename_open_file(
            source_handle,
            destination_handle,
            destination_name,
        )
        keep_handle = True
        return source_handle
    except FileExistsError as error:
        raise PdfAssemblyError(
            f"assembly destination already exists: {destination}"
        ) from error
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


def _windows_anchor_handle(anchor: _DirectoryAnchor) -> int:
    path_anchor = anchor.path_anchor
    handle = getattr(path_anchor, "handle", None)
    if not isinstance(handle, int):
        raise PdfAssemblyError(
            f"safe Windows directory handle unavailable: {anchor.path}"
        )
    return handle


def _windows_path_anchor_handle(path_anchor: object) -> int:
    handle = getattr(path_anchor, "handle", None)
    if not isinstance(handle, int):
        raise PdfAssemblyError("safe Windows directory handle is unavailable")
    return handle


def _windows_create_relative_directory(root_handle: int, name: str) -> int:
    return _windows_nt_create_relative(
        root_handle,
        name,
        desired_access=0x001301FF,
        create_disposition=2,
        create_options=0x00000001 | 0x00000020 | 0x00200000,
        file_attributes=0x00000010,
    )


def _windows_create_relative_file(root_handle: int, name: str) -> int:
    return _windows_nt_create_relative(
        root_handle,
        name,
        desired_access=0x001F01FF,
        create_disposition=2,
        create_options=0x00000020 | 0x00000040 | 0x00200000,
        file_attributes=0x00000080,
    )


def _windows_open_relative_file(root_handle: int, name: str) -> int:
    return _windows_nt_create_relative(
        root_handle,
        name,
        desired_access=0x00010000 | 0x00000080 | 0x00100000,
        create_disposition=1,
        create_options=0x00000020 | 0x00000040 | 0x00200000,
        file_attributes=0,
    )


def _windows_open_relative_read_file(root_handle: int, name: str) -> int:
    return _windows_nt_create_relative(
        root_handle,
        name,
        desired_access=0x00000001 | 0x00000080 | 0x00100000,
        create_disposition=1,
        create_options=0x00000020 | 0x00000040 | 0x00200000,
        file_attributes=0,
    )


def _windows_open_relative_entry(root_handle: int, name: str) -> int:
    return _windows_nt_create_relative(
        root_handle,
        name,
        desired_access=0x00000080 | 0x00100000,
        create_disposition=1,
        create_options=0x00000020 | 0x00200000,
        file_attributes=0,
    )


def _windows_nt_create_relative(
    root_handle: int,
    name: str,
    *,
    desired_access: int,
    create_disposition: int,
    create_options: int,
    file_attributes: int,
) -> int:
    if Path(name).name != name:
        raise PdfAssemblyError(f"unsafe Windows anchored assembly name: {name}")
    if not _IS_WINDOWS:
        raise PdfAssemblyError("Windows anchored relative open is unavailable")
    handle_value: int | None = None
    output_handle: object | None = None
    try:
        import ctypes
        from ctypes import wintypes

        class UnicodeString(ctypes.Structure):
            _fields_ = (
                ("length", wintypes.USHORT),
                ("maximum_length", wintypes.USHORT),
                ("buffer", wintypes.LPWSTR),
            )

        class ObjectAttributes(ctypes.Structure):
            _fields_ = (
                ("length", wintypes.ULONG),
                ("root_directory", wintypes.HANDLE),
                ("object_name", ctypes.POINTER(UnicodeString)),
                ("attributes", wintypes.ULONG),
                ("security_descriptor", wintypes.LPVOID),
                ("security_quality_of_service", wintypes.LPVOID),
            )

        class IoStatusBlock(ctypes.Structure):
            _fields_ = (
                ("status", wintypes.LPVOID),
                ("information", ctypes.c_size_t),
            )

        name_buffer = ctypes.create_unicode_buffer(name)
        encoded_length = len(name.encode("utf-16-le"))
        unicode_name = UnicodeString(
            encoded_length,
            encoded_length + 2,
            ctypes.cast(name_buffer, wintypes.LPWSTR),
        )
        attributes = ObjectAttributes(
            ctypes.sizeof(ObjectAttributes),
            wintypes.HANDLE(root_handle),
            ctypes.pointer(unicode_name),
            0x00000040,
            None,
            None,
        )
        status_block = IoStatusBlock()
        output_handle = wintypes.HANDLE()
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        create_file = ntdll.NtCreateFile
        create_file.argtypes = (
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.ULONG,
            ctypes.POINTER(ObjectAttributes),
            ctypes.POINTER(IoStatusBlock),
            ctypes.POINTER(ctypes.c_longlong),
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.LPVOID,
            wintypes.ULONG,
        )
        create_file.restype = ctypes.c_long
        status = int(
            create_file(
                ctypes.byref(output_handle),
                desired_access,
                ctypes.byref(attributes),
                ctypes.byref(status_block),
                None,
                file_attributes,
                0x00000001 | 0x00000002 | 0x00000004,
                create_disposition,
                create_options,
                None,
                0,
            )
        )
        if status < 0:
            rtl_error = ntdll.RtlNtStatusToDosError
            rtl_error.argtypes = (ctypes.c_long,)
            rtl_error.restype = wintypes.ULONG
            error_code = int(rtl_error(status))
            if error_code in {2, 3}:
                raise FileNotFoundError(
                    error_code,
                    "Windows anchored entry is missing",
                    name,
                )
            if error_code in {80, 183}:
                raise FileExistsError(error_code, "Windows destination exists", name)
            raise ctypes.WinError(error_code)
        if output_handle.value is None:
            raise PdfAssemblyError("Windows anchored relative open returned no handle")
        handle_value = int(output_handle.value)
        return handle_value
    except PdfAssemblyError:
        raise
    except FileNotFoundError:
        raise
    except FileExistsError:
        raise
    except (AttributeError, NotImplementedError, OSError) as error:
        raise PdfAssemblyError(
            f"safe Windows anchored relative open unavailable for {name}: {error}"
        ) from error
    finally:
        if sys.exc_info()[0] is not None:
            candidate = handle_value
            if candidate is None and output_handle is not None:
                raw_candidate = getattr(output_handle, "value", None)
                if isinstance(raw_candidate, int):
                    candidate = raw_candidate
            if candidate is not None:
                pdf_acquire_module._close_windows_handle(candidate)


def _windows_file_identity(
    handle: int,
    *,
    require_regular: bool,
) -> tuple[int, int]:
    if not _IS_WINDOWS:
        raise PdfAssemblyError("Windows file identity is unavailable")
    try:
        import ctypes
        from ctypes import wintypes

        class FileInformation(ctypes.Structure):
            _fields_ = (
                ("file_attributes", wintypes.DWORD),
                ("creation_time", wintypes.FILETIME),
                ("last_access_time", wintypes.FILETIME),
                ("last_write_time", wintypes.FILETIME),
                ("volume_serial_number", wintypes.DWORD),
                ("file_size_high", wintypes.DWORD),
                ("file_size_low", wintypes.DWORD),
                ("number_of_links", wintypes.DWORD),
                ("file_index_high", wintypes.DWORD),
                ("file_index_low", wintypes.DWORD),
            )

        information = FileInformation()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_information = kernel32.GetFileInformationByHandle
        get_information.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(FileInformation),
        )
        get_information.restype = wintypes.BOOL
        if not get_information(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        if require_regular and (
            information.file_attributes & (0x00000010 | _REPARSE_POINT)
        ):
            raise PdfAssemblyError(
                "Windows anchored assembly artifact is not a regular file"
            )
        file_index = (
            int(information.file_index_high) << 32
        ) | int(information.file_index_low)
        return int(information.volume_serial_number), file_index
    except PdfAssemblyError:
        raise
    except (AttributeError, NotImplementedError, OSError) as error:
        raise PdfAssemblyError(
            f"cannot inspect Windows assembly handle identity: {error}"
        ) from error


def _windows_rename_open_file(
    source_handle: int,
    destination_handle: int,
    destination_name: str,
) -> None:
    try:
        import ctypes
        from ctypes import wintypes

        payload = _windows_file_rename_information(
            destination_handle,
            destination_name,
        )
        buffer_size = len(payload)
        buffer = ctypes.create_string_buffer(payload, buffer_size)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
    except OSError as error:
        if getattr(error, "winerror", None) in {80, 183}:
            raise FileExistsError(
                183,
                "Windows destination exists",
                destination_name,
            ) from error
        raise PdfAssemblyError(
            f"safe Windows anchored rename unavailable for {destination_name}: {error}"
        ) from error
    except (AttributeError, NotImplementedError) as error:
        raise PdfAssemblyError(
            f"safe Windows anchored rename unavailable for {destination_name}: {error}"
        ) from error


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
    if not _IS_WINDOWS:
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
    if _IS_WINDOWS:
        handle: int | None = None
        try:
            handle = _windows_open_relative_file(
                _windows_anchor_handle(directory),
                name,
            )
            if (
                _windows_file_identity(handle, require_regular=True)
                != published.identity
            ):
                return
            _windows_delete_open_file(handle)
        except (PdfAssemblyError, NotImplementedError, OSError):
            return
        finally:
            if handle is not None:
                pdf_acquire_module._close_windows_handle(handle)
        return
    try:
        result = _anchored_regular_file_stat(directory, name)
        if (result.st_dev, result.st_ino) != published.identity:
            return
        if directory.descriptor is not None:
            os.unlink(name, dir_fd=directory.descriptor)
    except (PdfAssemblyError, NotImplementedError, OSError):
        return


def _remove_owned_directory(
    parent: _DirectoryAnchor,
    name: str,
    identity: tuple[int, int] | None,
    *,
    child: _DirectoryAnchor | None = None,
) -> None:
    if identity is None:
        return
    if _IS_WINDOWS and child is not None and child.path_anchor is not None:
        try:
            _windows_delete_open_file(_windows_anchor_handle(child))
        except PdfAssemblyError:
            pass
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
    except (PdfAssemblyError, NotImplementedError, OSError):
        return


__all__ = ["PdfAssemblyError", "assemble_pdf"]
