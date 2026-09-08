"""Basic, fail-closed ReportLab assembly for reviewed PDF translations."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
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
from urllib.parse import urlsplit
from xml.sax.saxutils import escape, quoteattr

from PIL import Image as PillowImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    ActionFlowable,
    BaseDocTemplate,
    Frame,
    Flowable,
    Image,
    IndexingFlowable,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

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
    PdfFootnoteContinuation,
    PdfPageSize,
    PdfTocResolution,
    TrackedFlowable,
)
from web_translator.pdf_layout import split_list_marker
from web_translator.pdf_models import (
    PdfBlock,
    PdfContractError,
    PdfDocument,
    PdfLinkEvidence,
    PdfSourceRecord,
    font_size_bucket,
)
from web_translator.protection import ProtectionError, restore_tokens
from web_translator.terminology import TerminologyError, normalize_terminology


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
_TEXT_KINDS = {
    "heading",
    "paragraph",
    "list-item",
    "table-cell",
    "caption",
    "footnote",
}
_IGNORED_KINDS = {"header", "footer", "page-number"}
_ALIGNMENTS = {
    "left": TA_LEFT,
    "center": TA_CENTER,
    "right": TA_RIGHT,
    "justify": TA_JUSTIFY,
}
_LIST_INDENT_TOLERANCE = 3.0
_CANONICAL_BULLET_MARKERS = {"•", "‣", "◦", "⁃", "∙"}
_RICH_KINDS = {"table-cell", "figure", "caption", "footnote"}
_TABLE_COLUMN_MINIMUM = 54.0
_TABLE_CELL_PADDING = 5.0
_FIGURE_RENDER_DPI = 144.0
_URI_CHARACTERS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*\Z")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_HTTP_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_OPENER_ROLES = {"part-label", "part-title", "chapter-label", "chapter-title"}
_EPIGRAPH_ROLES = {"dedication", "epigraph", "epigraph-attribution"}
_CALLOUT_ROLES = {"callout-title", "callout-body"}


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


def _anchored_directory_names(directory: _DirectoryAnchor) -> list[str]:
    """Enumerate the exact held directory without reopening its visible path."""
    try:
        if directory.descriptor is not None:
            return sorted(os.listdir(directory.descriptor))
        if _IS_WINDOWS:
            return sorted(_windows_directory_names(_windows_anchor_handle(directory)))
        raise PdfAssemblyError(
            f"safe anchored directory enumeration is unavailable: {directory.path}"
        )
    except PdfAssemblyError:
        raise
    except (AttributeError, NotImplementedError, OSError, UnicodeError) as error:
        raise PdfAssemblyError(
            f"cannot enumerate anchored directory {directory.path}: {error}"
        ) from error


def assemble_pdf(
    run_dir: Path,
    translations: Mapping[str, Translation],
    glossary: Mapping[str, str],
    output_dir: Path,
    *,
    semantic_snapshot: Any | None = None,
) -> Path:
    """Create only ``run_dir/staged-output/translated.pdf`` and strict layout evidence."""
    run_dir = Path(run_dir)
    output_dir = Path(output_dir)
    temporary_name: str | None = None
    run_anchor: _DirectoryAnchor | None = None
    evidence_files: dict[str, _OpenedFile] = {}
    media_anchor: _DirectoryAnchor | None = None
    media_files: dict[str, _OpenedFile] = {}
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
        evidence_names = [
            ("document.json", "PDF document"),
            ("source.json", "PDF source record"),
        ]
        if semantic_snapshot is None:
            evidence_names.append(("segments.jsonl", "PDF segment manifest"))
        for name, context in evidence_names:
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
        if semantic_snapshot is None:
            segments = _read_pdf_segments(
                evidence_files["segments.jsonl"],
                run_dir / "segments.jsonl",
            )
            consumed_translations = translations
            consumed_glossary = glossary
        else:
            (
                segments,
                consumed_translations,
                consumed_glossary,
            ) = _semantic_snapshot_assembly_values(
                semantic_snapshot,
                run_anchor,
            )
            if dict(translations) != consumed_translations:
                raise PdfAssemblyError(
                    "held PDF translations disagree with assembly arguments"
                )
            if dict(glossary) != consumed_glossary:
                raise PdfAssemblyError(
                    "held PDF glossary disagrees with assembly arguments"
                )
        ordered = _normalize_pdf_translations(
            document,
            segments,
            consumed_translations,
            consumed_glossary,
        )
        toc_resolutions = _resolve_toc_targets(document)
        document = replace(
            document,
            links=list(_translated_link_evidence(document, ordered)),
        )
        _validate_rich_relationships(document)
        media_payloads: dict[str, bytes] = {}
        figure_blocks = [block for block in document.blocks if block.kind == "figure"]
        if figure_blocks:
            media_anchor = _open_existing_child_directory(
                run_anchor,
                "media",
                "PDF media",
            )
            for block in figure_blocks:
                assert block.media_path is not None
                media_name = _media_name(block)
                opened = _open_anchored_input_file(
                    media_anchor,
                    media_name,
                    f"figure media for {block.id}",
                )
                media_files[media_name] = opened
                media_payloads[block.id] = _read_opened_bytes(
                    opened,
                    run_dir / block.media_path,
                    f"figure media for {block.id}",
                )
            _verify_anchored_evidence(media_anchor, media_files)
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
        uses_rich_layout = bool(document.links) or any(
            block.kind in _RICH_KINDS
            or block.kind in _IGNORED_KINDS
            or block.semantic_role != "body"
            for block in document.blocks
        )
        publication_evidence: dict[str, Any] = {}
        if uses_rich_layout:
            records = _build_rich_document(
                document,
                ordered,
                source,
                temporary_pdf.stream,
                page_size,
                media_payloads,
                toc_resolutions=toc_resolutions,
                publication_evidence=publication_evidence,
            )
        else:
            records = _build_basic_document(
                document,
                ordered,
                source,
                temporary_pdf.stream,
                page_size,
            )
        if media_anchor is not None:
            _verify_anchored_evidence(media_anchor, media_files)
        records.sort(key=lambda item: (item.source_order, item.split_part))
        digest = _finalize_opened_file(temporary_pdf, "staged PDF")
        temporary_pdf.stream.close()
        temporary_staging_anchor.verify_visible()
        layout = PdfAssemblyLayout(
            schema_version="1.1",
            reserved_output_dir=str(output_dir),
            staged_pdf_sha256=digest,
            page_size=page_size,
            minimum_font_size=MINIMUM_FONT_SIZE,
            flowables=tuple(records),
            links=tuple(document.links),
            toc_entries=tuple(publication_evidence.get("toc_entries", ())),
            footnote_continuations=tuple(publication_evidence.get("footnote_continuations", ())),
            anchor_pages=tuple(sorted(publication_evidence.get("anchor_pages", {}).items())),
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
        if semantic_snapshot is not None:
            _verify_semantic_snapshot(semantic_snapshot, run_anchor)
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
        for opened in media_files.values():
            _close_opened_file(opened)
        if media_anchor is not None:
            media_anchor.close()
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


def _semantic_snapshot_assembly_values(
    snapshot: Any,
    run_anchor: _DirectoryAnchor,
) -> tuple[list[Segment], dict[str, Translation], dict[str, str]]:
    """Parse assembly inputs only from the already-held reviewed byte snapshot."""
    _verify_semantic_snapshot(snapshot, run_anchor)
    try:
        payloads = snapshot.payloads
        segments_payload = payloads["segments.jsonl"]
        glossary_payload = payloads["glossary.json"]
        if not isinstance(segments_payload, bytes) or not isinstance(
            glossary_payload, bytes
        ):
            raise TypeError("held semantic payloads must be bytes")
        segments = read_segments_stream(
            io.StringIO(segments_payload.decode("utf-8"))
        )
        glossary_value = json.loads(glossary_payload.decode("utf-8"))
        if not isinstance(glossary_value, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in glossary_value.items()
        ):
            raise PdfAssemblyError("held PDF glossary must map strings to strings")
        translations: dict[str, Translation] = {}
        for relative, payload in sorted(payloads.items()):
            if not relative.startswith("translations/"):
                continue
            if not isinstance(payload, bytes):
                raise TypeError("held semantic payloads must be bytes")
            for line_number, line in enumerate(
                payload.decode("utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                record = Translation.from_dict(json.loads(line))
                if record.segment_id in translations:
                    raise PdfAssemblyError(
                        f"duplicate held PDF translation ID: {record.segment_id}"
                    )
                translations[record.segment_id] = record
        return segments, translations, dict(glossary_value)
    except PdfAssemblyError:
        raise
    except (
        AttributeError,
        KeyError,
        UnicodeError,
        json.JSONDecodeError,
        SegmentContractError,
        TypeError,
        ValueError,
    ) as error:
        raise PdfAssemblyError(f"invalid held PDF semantic inputs: {error}") from error


def _verify_semantic_snapshot(
    snapshot: Any,
    run_anchor: _DirectoryAnchor,
) -> None:
    try:
        snapshot_anchor = snapshot.run_anchor
        if snapshot_anchor.identity != run_anchor.identity:
            raise PdfAssemblyError(
                "held PDF semantic snapshot belongs to a different run directory"
            )
        snapshot.verify()
    except PdfAssemblyError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as error:
        raise PdfAssemblyError(f"invalid held PDF semantic inputs: {error}") from error


def _read_opened_utf8(
    opened: _OpenedFile,
    path: Path,
    context: str,
) -> str:
    try:
        payload = _read_opened_bytes(opened, path, context)
        return payload.decode("utf-8")
    except (OSError, UnicodeError, TypeError) as error:
        raise PdfAssemblyError(f"cannot read {context} {path}: {error}") from error


def _read_opened_bytes(
    opened: _OpenedFile,
    path: Path,
    context: str,
) -> bytes:
    try:
        opened.stream.seek(0)
        payload = opened.stream.read()
        if not isinstance(payload, bytes):
            raise TypeError("anchored evidence stream did not return bytes")
        if not payload:
            raise PdfAssemblyError(f"{context} is empty: {path}")
        return payload
    except PdfAssemblyError:
        raise
    except (OSError, TypeError) as error:
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
        if block.kind == "figure":
            if block.segment_id is not None:
                raise PdfAssemblyError(
                    f"figure blocks cannot have translation segments: {block.id}"
                )
            continue
        if block.kind == "table-cell" and not block.source_text.strip():
            if block.segment_id is not None:
                raise PdfAssemblyError(
                    f"empty table cells cannot have translation segments: {block.id}"
                )
            continue
        if block.kind not in _TEXT_KINDS:
            raise PdfAssemblyError(
                f"unsupported PDF block kind: {block.kind} ({block.id})"
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
        normalized = normalize_terminology(
            ordered_records,
            glossary,
            policy="korean-first",
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


def _validate_rich_relationships(document: PdfDocument) -> None:
    by_id = {block.id: block for block in document.blocks}
    table_blocks = {
        block.id: block for block in document.blocks if block.kind == "table-cell"
    }
    cells_by_block = {cell.block_id: cell for cell in document.table_cells}
    if set(table_blocks) != set(cells_by_block):
        raise PdfAssemblyError(
            "table cells and table-cell blocks must match exactly"
        )
    occupied_by_table: dict[str, set[tuple[int, int]]] = {}
    for block_id, block in table_blocks.items():
        cell = cells_by_block[block_id]
        if (
            block.table_id,
            block.row,
            block.column,
            block.row_span,
            block.column_span,
        ) != (
            cell.table_id,
            cell.row,
            cell.column,
            cell.row_span,
            cell.column_span,
        ):
            raise PdfAssemblyError(f"table metadata mismatch for {block_id}")
        occupied = occupied_by_table.setdefault(cell.table_id, set())
        for row in range(cell.row, cell.row + cell.row_span):
            for column in range(cell.column, cell.column + cell.column_span):
                coordinate = (row, column)
                if coordinate in occupied:
                    raise PdfAssemblyError(
                        f"overlapping table spans in {cell.table_id}"
                    )
                occupied.add(coordinate)

    figures = [block for block in document.blocks if block.kind == "figure"]
    captions = [block for block in document.blocks if block.kind == "caption"]
    media_names = [_media_name(figure) for figure in figures]
    if len(media_names) != len(set(media_names)):
        raise PdfAssemblyError("figure media paths must be unique")
    claimed_captions: set[str] = set()
    for figure in figures:
        if figure.caption_id is not None:
            caption = by_id.get(figure.caption_id)
            if (
                caption is None
                or caption.kind != "caption"
                or caption.caption_id != figure.id
                or caption.id in claimed_captions
            ):
                raise PdfAssemblyError(
                    f"ambiguous figure-caption relationship for {figure.id}"
                )
            claimed_captions.add(caption.id)
        page = document.pages[figure.page_number - 1]
        x0, top, x1, bottom = figure.bbox
        if (
            x0 < 0
            or top < 0
            or x1 > page.width
            or bottom > page.height
            or x1 <= x0
            or bottom <= top
        ):
            raise PdfAssemblyError(
                f"figure bounds fall outside source page: {figure.id}"
            )
    if claimed_captions != {caption.id for caption in captions}:
        raise PdfAssemblyError("every caption must have one reciprocal figure")

    emitted_ids = {
        block.id
        for block in document.blocks
        if block.kind not in _IGNORED_KINDS
    }
    footnote_ids = {
        block.id for block in document.blocks if block.kind == "footnote"
    }
    footnote_owners = {identifier: [] for identifier in footnote_ids}
    for block in document.blocks:
        if block.uri is not None and block.destination is not None:
            raise PdfAssemblyError(
                f"PDF block cannot have both URI and internal destination: {block.id}"
            )
        if block.uri is not None:
            _safe_uri(block.uri, block.id)
        if block.destination is not None:
            if block.destination not in emitted_ids:
                raise PdfAssemblyError(
                    f"unresolved internal destination for {block.id}: "
                    f"{block.destination}"
                )
            if block.destination in footnote_owners:
                footnote_owners[block.destination].append(block.id)
    for footnote_id, owners in footnote_owners.items():
        if len(owners) != 1:
            raise PdfAssemblyError(
                f"footnote must have exactly one marker owner: {footnote_id}"
            )


def _media_name(block: PdfBlock) -> str:
    if block.kind != "figure" or block.media_path is None:
        raise PdfAssemblyError(f"figure is missing media: {block.id}")
    media_path = Path(block.media_path)
    if (
        media_path.parts[:1] != ("media",)
        or len(media_path.parts) != 2
        or media_path.name != media_path.parts[1]
        or media_path.suffix.lower() != ".png"
    ):
        raise PdfAssemblyError(f"unsafe figure media path: {block.media_path}")
    return media_path.name


def _safe_uri(uri: str, block_id: str) -> str:
    if (
        _URI_CHARACTERS.fullmatch(uri) is None
        or _INVALID_PERCENT_ESCAPE.search(uri) is not None
    ):
        raise PdfAssemblyError(f"unsafe external URI for {block_id}")
    try:
        parsed = urlsplit(uri)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise PdfAssemblyError(f"unsafe external URI for {block_id}") from error
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "mailto"}:
        raise PdfAssemblyError(f"unsafe external URI for {block_id}")
    if scheme in {"http", "https"}:
        at_count = parsed.netloc.count("@")
        authority = parsed.netloc.rsplit("@", 1)[-1]
        if (
            not parsed.netloc
            or not hostname
            or at_count > 1
            or (at_count == 1 and not parsed.username)
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise PdfAssemblyError(f"unsafe external URI for {block_id}")

        if authority.startswith("["):
            closing_bracket = authority.find("]")
            port_suffix = authority[closing_bracket + 1 :]
            if closing_bracket < 0 or (port_suffix and not port_suffix.startswith(":")):
                raise PdfAssemblyError(f"unsafe external URI for {block_id}")
            explicit_port = port_suffix[1:] if port_suffix else None
        else:
            if authority.count(":") > 1:
                raise PdfAssemblyError(f"unsafe external URI for {block_id}")
            _host, separator, port_text = authority.rpartition(":")
            explicit_port = port_text if separator else None
        if explicit_port is not None and (
            not explicit_port or not explicit_port.isdecimal()
        ):
            raise PdfAssemblyError(f"unsafe external URI for {block_id}")

        if ":" not in hostname:
            dns_hostname = hostname[:-1] if hostname.endswith(".") else hostname
            if (
                not dns_hostname
                or len(dns_hostname) > 253
                or any(
                    _HTTP_HOST_LABEL.fullmatch(label) is None
                    for label in dns_hostname.split(".")
                )
            ):
                raise PdfAssemblyError(f"unsafe external URI for {block_id}")
    if scheme == "mailto" and (parsed.netloc or not parsed.path):
        raise PdfAssemblyError(f"unsafe external URI for {block_id}")
    return uri


def _anchor_name(block_id: str) -> str:
    return "wt-" + re.sub(r"[^A-Za-z0-9_.-]", "-", block_id)


def _consume_role_group(
    blocks: Sequence[PdfBlock], start: int, roles: set[str],
) -> list[PdfBlock]:
    group: list[PdfBlock] = []
    first = blocks[start]
    for block in blocks[start:]:
        if block.page_number != first.page_number or block.semantic_role not in roles:
            break
        # A new label/title starts a distinct callout or opener, even on one page.
        if group and (
            block.semantic_role == "callout-title"
            or block.semantic_role.endswith("-label")
        ):
            break
        group.append(block)
    return group


def _role_paragraph(
    block: PdfBlock, translated: Mapping[str, str],
    links: Mapping[str, Sequence[PdfLinkEvidence]],
    frame: tuple[float, float, float, float], records: list[PdfFlowableLayout],
    counters: dict[str, int], style: ParagraphStyle,
    callbacks: Mapping[str, Callable[[Any, int, Flowable], None]],
) -> TrackedFlowable:
    return TrackedFlowable(
        Paragraph(_linked_markup(block, translated[block.id], links.get(block.id, ())), style),
        block_id=block.id, kind=block.kind, source_order=block.order, split_part=0,
        font_size=style.fontSize, frame=frame, records=records, part_counters=counters,
        anchor_name=_anchor_name(block.id), semantic_role=block.semantic_role,
        on_draw=callbacks.get(block.id),
    )


def _append_opener_group(
    story: list[Any], blocks: Sequence[PdfBlock], translated: Mapping[str, str],
    links: Mapping[str, Sequence[PdfLinkEvidence]],
    frame: tuple[float, float, float, float], records: list[PdfFlowableLayout],
    part_counters: dict[str, int], *, callbacks: Mapping[str, Callable[[Any, int, Flowable], None]],
) -> None:
    contents: list[Any] = [Spacer(1, frame[3] * 0.16)]
    for block in blocks:
        label = block.semantic_role.endswith("-label")
        style = ParagraphStyle(
            f"WT-{block.semantic_role}", fontName=BOLD_FONT_NAME,
            fontSize=14 if label else 24, leading=20 if label else 34,
            alignment=_ALIGNMENTS[block.style.alignment], spaceAfter=14 if label else 24,
        )
        contents.append(_role_paragraph(
            block, translated, links, frame, records, part_counters, style, callbacks,
        ))
    if story and not isinstance(story[-1], PageBreak):
        story.append(PageBreak())
    story.append(KeepTogether(contents))


def _append_epigraph_group(
    story: list[Any], blocks: Sequence[PdfBlock], translated: Mapping[str, str],
    links: Mapping[str, Sequence[PdfLinkEvidence]],
    frame: tuple[float, float, float, float], records: list[PdfFlowableLayout],
    part_counters: dict[str, int], *, callbacks: Mapping[str, Callable[[Any, int, Flowable], None]],
) -> None:
    contents: list[Any] = [Spacer(1, frame[3] * 0.25)]
    for block in blocks:
        attribution = block.semantic_role == "epigraph-attribution"
        style = ParagraphStyle(
            f"WT-{block.semantic_role}", fontName=REGULAR_FONT_NAME,
            fontSize=10 if attribution else 12, leading=16 if attribution else 20,
            alignment=_ALIGNMENTS[block.style.alignment],
            leftIndent=24, rightIndent=24,
            spaceBefore=18 if block.semantic_role == "epigraph" else 0,
            spaceAfter=8,
        )
        contents.append(_role_paragraph(
            block, translated, links, frame, records, part_counters, style, callbacks,
        ))
    if story and not isinstance(story[-1], PageBreak):
        story.append(PageBreak())
    story.append(KeepTogether(contents))


def _append_callout_group(
    story: list[Any], blocks: Sequence[PdfBlock], translated: Mapping[str, str],
    links: Mapping[str, Sequence[PdfLinkEvidence]],
    frame: tuple[float, float, float, float], records: list[PdfFlowableLayout],
    part_counters: dict[str, int], *, media_payloads: Mapping[str, bytes],
    callbacks: Mapping[str, Callable[[Any, int, Flowable], None]],
) -> None:
    icon = blocks[0] if blocks[0].kind == "figure" else None
    paragraphs: list[TrackedFlowable] = []
    for block in blocks:
        if block is icon:
            continue
        title = block.semantic_role == "callout-title"
        style = ParagraphStyle(
            f"WT-{block.semantic_role}", fontName=BOLD_FONT_NAME if title else REGULAR_FONT_NAME,
            fontSize=12 if title else BODY_FONT_SIZE, leading=18 if title else 16,
            spaceAfter=6, keepWithNext=title,
        )
        paragraphs.append(_role_paragraph(
            block, translated, links, frame, records, part_counters, style, callbacks,
        ))
    if icon is None:
        data, widths = [[paragraphs]], [frame[2]]
    else:
        icon_width = min(icon.bbox[2] - icon.bbox[0] + 16, frame[2] * 0.25)
        image = _figure_flowable(
            icon, media_payloads[icon.id], frame, records, part_counters,
            max_width=icon_width - 16,
        )
        data, widths = [[image, paragraphs]], [icon_width, frame[2] - icon_width]
    table = Table(
        data, colWidths=widths, hAlign="LEFT", splitByRow=1, splitInRow=1,
        spaceBefore=8, spaceAfter=10,
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F5F7")),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#8B969F")),
        ("LINEABOVE", (0, "splitfirst"), (-1, "splitfirst"),
         0.6, colors.HexColor("#8B969F"), 1, (2, 2)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(KeepTogether([table]))


def _reference_style(block: PdfBlock) -> ParagraphStyle:
    return ParagraphStyle(
        f"WT-Reference-{block.order}", fontName=REGULAR_FONT_NAME,
        fontSize=BODY_FONT_SIZE, leading=16, leftIndent=18,
        firstLineIndent=0 if block.continuation_of else -18,
        spaceBefore=0 if block.continuation_of else 3, spaceAfter=4,
    )


def _embed_font_faces(canvas: Any, _doc: Any) -> None:
    # Keep the resource contract even when no visible block uses one face.
    for name in (REGULAR_FONT_NAME, BOLD_FONT_NAME):
        pdfmetrics.getFont(name).getSubsetInternalName(0, canvas._doc)


def _build_basic_document(
    document: PdfDocument,
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
            font_size_bucket(block.style.font_size)
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
                    _linked_markup(
                        block,
                        rendered_text,
                        _links_by_block(document).get(block.id, ()),
                    ),
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
                        semantic_role=block.semantic_role,
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
                subject=_provenance_metadata(source),
            )
            pdf.build(story, onFirstPage=_embed_font_faces)
    except PdfAssemblyError:
        raise
    except Exception as error:
        raise PdfAssemblyError(f"ReportLab PDF assembly failed: {error}") from error
    if [item.source_order for item in records] != sorted(
        item.source_order for item in records
    ):
        raise PdfAssemblyError("tracked flowables were emitted out of document order")
    return records


_TOC_REFERENCE = re.compile(r"^(.*?)\s+(\d+|[ivxlcdm]+)\s*$", re.IGNORECASE)


def _toc_parts(text: str) -> tuple[str, str]:
    match = _TOC_REFERENCE.fullmatch(text)
    if match is None:
        raise PdfAssemblyError(f"TOC entry has no unambiguous page column: {text!r}")
    return match[1].rstrip(" .·…"), match[2]


def _toc_heading_key(text: str) -> str:
    text = re.sub(r"^(?:(?:chapter|part)\s+)?(?:\d+|[IVX]+)[. :]+", "", text, flags=re.IGNORECASE)
    return " ".join(re.findall(r"\w+", text.casefold()))


def _resolve_toc_targets(document: PdfDocument) -> dict[str, PdfTocResolution]:
    """Resolve using source annotations/titles and printed furniture, never PDF indices."""
    entries = [b for b in document.blocks if b.semantic_role in {"toc-part", "toc-chapter", "toc-entry"}]
    if not entries:
        return {}
    targets = [b for b in document.blocks if b.kind == "heading" and not b.semantic_role.startswith("toc-")]
    target_titles = {block.id: block.source_text for block in targets}
    # A wrapped chapter title may occupy several adjacent semantic blocks.
    for index, block in enumerate(document.blocks):
        if block.semantic_role not in {"chapter-title", "part-title"}:
            continue
        group = _consume_role_group(document.blocks, index, {block.semantic_role})
        if len(group) > 1:
            target_titles[block.id] = " ".join(item.source_text for item in group)
    printed: dict[int, int] = {}
    for block in document.blocks:
        if block.kind in {"footer", "page-number"}:
            match = re.search(r"^\s*(\d+)(?:\s*\||\s*$)|\|\s*(\d+)\s*$", block.source_text)
            if match:
                number = int(match[1] or match[2])
                if block.page_number in printed and printed[block.page_number] != number:
                    raise PdfAssemblyError(f"ambiguous printed source page: {block.page_number}")
                printed[block.page_number] = number
    terminal_reference = printed.get(document.page_count)
    if terminal_reference is not None and terminal_reference != max(printed.values()):
        terminal_reference = None
    result = {}
    for index, entry in enumerate(entries):
        if entry.semantic_role == "toc-part" and _TOC_REFERENCE.fullmatch(entry.source_text) is None:
            title, reference = entry.source_text, None
        else:
            title, reference = _toc_parts(entry.source_text)
        annotated = {link.destination for link in document.links if link.source_block_id == entry.id and link.reconstructed and link.destination}
        if entry.destination:
            annotated.add(entry.destination)
        if len(annotated) > 1:
            raise PdfAssemblyError(f"ambiguous TOC annotation destinations: {entry.id}")
        matches = [b for b in targets if _toc_heading_key(title) in {
            _toc_heading_key(target_titles[b.id]), _toc_heading_key(b.source_text),
        }]
        if reference is not None and reference.isdecimal():
            matches = [b for b in matches if b.page_number not in printed or printed[b.page_number] == int(reference)]
            if terminal_reference is not None and int(reference) > terminal_reference:
                matches = []
        following_reference = None
        if reference is None:
            following = next((b for b in entries[index + 1:] if b.semantic_role in {"toc-part", "toc-chapter"}), None)
            if following is not None and following.semantic_role == "toc-chapter":
                following_reference = _toc_parts(following.source_text)[1]
        if annotated:
            target = next(iter(annotated))
            if target not in {b.id for b in document.blocks}:
                raise PdfAssemblyError(f"TOC target missing from input: {entry.id}")
            evidence = "source-internal-destination"
        elif len(matches) == 1:
            target = matches[0].id
            evidence = "unique-source-heading-title"
        elif len(matches) > 1:
            raise PdfAssemblyError(f"ambiguous TOC heading target: {entry.id}")
        elif reference is not None and reference.isdecimal() and terminal_reference is not None and int(reference) > terminal_reference:
            target = None
            evidence = f"source-reference-{reference}-beyond-observed-printed-page-{terminal_reference}-at-input-end"
        elif following_reference and following_reference.isdecimal() and terminal_reference is not None and int(following_reference) > terminal_reference:
            target = None
            evidence = f"absent-source-part-title;following-chapter-reference-{following_reference}-beyond-observed-printed-page-{terminal_reference}-at-input-end"
        else:
            raise PdfAssemblyError(f"TOC target cannot be resolved or proven outside input: {entry.id}")
        result[entry.id] = PdfTocResolution(entry.id, reference, target, None, evidence,
            "toc-target-outside-input" if target is None else None)
    return result


class ResolvedTocEntry(TrackedFlowable, IndexingFlowable):
    """A translated label with a separate, linked output-page column."""

    def __init__(self, *, text: str, style: ParagraphStyle, resolution: PdfTocResolution,
                 anchor_pages: dict[str, int], **kwargs: Any) -> None:
        self.label, reference = _toc_parts(text) if resolution.source_reference is not None else (text, None)
        if reference != resolution.source_reference:
            raise PdfAssemblyError(f"translated TOC source reference changed: {resolution.block_id}")
        self.resolution = resolution
        self.anchor_pages = anchor_pages
        self.style = style
        self.style.endDots = "." if reference is not None else None
        self._rendered_page: int | None = None
        super().__init__(Paragraph("", style), **kwargs)

    def beforeBuild(self) -> None:
        self._rendered_page = self.anchor_pages.get(_anchor_name(self.resolution.target_block_id)) if self.resolution.target_block_id else None

    def isSatisfied(self) -> bool:
        return self.resolution.target_block_id is None or (
            self._rendered_page is not None and self._rendered_page == self.anchor_pages.get(_anchor_name(self.resolution.target_block_id))
        )

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        page = str(self._rendered_page) if self._rendered_page else self.resolution.source_reference
        label = escape(self.label)
        if self.resolution.target_block_id:
            href = quoteattr("#" + _anchor_name(self.resolution.target_block_id))
            label = f"<link href={href}>{label}</link>"
            page = f"<link href={href}>{page}</link>"
        if self.resolution.source_reference is None:
            self._content = Paragraph(label, self.style)
            return super().wrap(available_width, available_height)
        right = ParagraphStyle(self.style.name + "-page", parent=self.style, alignment=TA_RIGHT, leftIndent=0, endDots=None)
        self._content = Table([[Paragraph(label, self.style), Paragraph(page, right)]],
            colWidths=[available_width - 36, 36], style=TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]))
        return super().wrap(available_width, available_height)

    def split(self, available_width: float, available_height: float) -> list[Any]:
        return []


class PublicationDocTemplate(BaseDocTemplate):
    """Bounded indexing with nested draw anchors and final-pass-only evidence."""

    def __init__(self, *args: Any, reset_pass: Callable[[], None], anchor_pages: dict[str, int],
                 last_note_page: Callable[[], int], owner_page_floors: dict[str, int],
                 body_frame: Callable[[tuple[float, float, float, float]], tuple[float, float, float, float]],
                 **kwargs: Any) -> None:
        self.reset_pass = reset_pass
        self.anchor_pages = anchor_pages
        self.last_note_page = last_note_page
        self.owner_page_floors = owner_page_floors
        self.body_frame = body_frame
        super().__init__(*args, **kwargs)

    def beforeDocument(self) -> None:
        self.reset_pass()

    def handle_noteTail(self) -> None:
        while self.page < self.last_note_page():
            self.handle_pageBreak()
            self.clean_hanging()
            # A continuation-only page is intentional content, not an empty page.
            self._curPageFlowableCount += 1

    def handle_ownerFloor(self, owners: Sequence[str]) -> None:
        minimum = max((self.owner_page_floors.get(owner, 0) for owner in owners), default=0)
        while self.page < minimum:
            self.handle_pageBreak()
            self.clean_hanging()


class _PublicationIndex(IndexingFlowable):
    def __init__(self, update: Callable[[], bool]) -> None:
        super().__init__()
        self.update = update
        self.satisfied = False

    def afterBuild(self) -> None:
        self.satisfied = self.update()

    def isSatisfied(self) -> bool:
        return self.satisfied

    def draw(self) -> None:
        pass


def _paragraph_fragment_text(content: Flowable) -> str:
    """Read emitted lines: split Paragraph.getPlainText() can legitimately be empty."""
    if not isinstance(content, Paragraph):
        return ""
    lines = getattr(getattr(content, "blPara", None), "lines", [])
    return " ".join(
        " ".join(line[1]) if isinstance(line, tuple)
        else "".join(getattr(word, "text", "") for word in line.words)
        for line in lines
    )


class _PageNoteCallback:
    """Schedule on the evidenced marker fragment, or explicitly block-only legacy evidence."""

    def __init__(self, owner_id: str, marker: re.Pattern[str] | None,
                 schedule: Callable[[int], None], floors: dict[str, int], ownership: str) -> None:
        self.owner_id = owner_id
        self.marker = marker
        self.schedule = schedule
        self.floors = floors
        self.ownership = ownership

    def matches(self, content: Flowable) -> bool:
        return self.marker is None or self.marker.search(_paragraph_fragment_text(content)) is not None

    def minimum_page_for(self, content: Flowable) -> int:
        return self.floors.get(self.owner_id, 0) if self.marker is not None and self.matches(content) else 0

    def __call__(self, _canvas: Any, page_number: int, content: Flowable) -> None:
        if self.matches(content):
            self.schedule(page_number)


def _build_rich_document(
    document: PdfDocument,
    ordered: Sequence[tuple[PdfBlock, Segment, str]],
    source: PdfSourceRecord,
    destination: BinaryIO,
    page_size: PdfPageSize,
    media_payloads: Mapping[str, bytes],
    *,
    toc_resolutions: Mapping[str, PdfTocResolution] | None = None,
    publication_evidence: dict[str, Any] | None = None,
) -> list[PdfFlowableLayout]:
    records: list[PdfFlowableLayout] = []
    part_counters: dict[str, int] = {}
    translated = {block.id: text for block, _segment, text in ordered}
    segments_by_block = {block.id: segment for block, segment, _text in ordered}
    block_by_id = {block.id: block for block in document.blocks}
    links_by_block = _links_by_block(document)
    left_margin = right_margin = 54.0
    top_margin = 54.0
    bottom_margin = 42.0
    portrait_size = (page_size.width, page_size.height)
    landscape_size = landscape(portrait_size)

    def body_frame_for(size: tuple[float, float]) -> tuple[float, float, float, float]:
        width, height = size
        return (
            left_margin,
            bottom_margin,
            width - left_margin - right_margin,
            height - top_margin - bottom_margin,
        )

    portrait_frame = body_frame_for(portrait_size)
    landscape_frame = body_frame_for(landscape_size)
    page_local_notes, section_notes, owner_to_note = _classify_footnotes(document)
    scheduled_note_pages: dict[str, int] = {}
    drawn_page_notes: set[tuple[int, str]] = set()
    anchor_pages: dict[str, int] = {}
    note_plan: dict[int, list[tuple[str, int, Paragraph, float]]] = {}
    note_only_pages: set[int] = set()
    page_sizes: dict[int, tuple[float, float]] = {}
    continuations: list[PdfFootnoteContinuation] = []
    owner_page_floors: dict[str, int] = {}
    previous_owner_pages: dict[str, int] = {}
    note_owners = {note: owner for owner, note in owner_to_note.items()}

    def reserve_for(page: int) -> float:
        parts = note_plan.get(page, [])
        return (8.0 + 4.0 + sum(height + (13.0 if part else 0) + 2.0
            for _note, part, _paragraph, height in parts)) if parts else 0.0

    def reset_pass() -> None:
        records.clear()
        part_counters.clear()
        scheduled_note_pages.clear()
        drawn_page_notes.clear()
        anchor_pages.clear()
        page_sizes.clear()
        continuations.clear()

    def update_note_plan() -> bool:
        new_plan: dict[int, list[tuple[str, int, Paragraph, float]]] = {}
        body_last_page = max((record.page_number for record in records
            if record.kind not in _IGNORED_KINDS | {"footnote"}), default=0)
        owners_stable = True
        for note_id in sorted(page_local_notes, key=lambda identifier: block_by_id[identifier].order):
            if note_id not in scheduled_note_pages:
                raise PdfAssemblyError(f"footnote owner was not emitted: {note_id}")
            block = block_by_id[note_id]
            paragraph = Paragraph(_linked_markup(block, translated[note_id], links_by_block.get(note_id, ())), footnote_style)
            page = scheduled_note_pages[note_id]
            previous = previous_owner_pages.get(note_id, page)
            owner = note_owners[note_id]
            if page < previous:
                owner_page_floors[owner] = max(owner_page_floors.get(owner, 0), previous)
            previous_owner_pages[note_id] = page
            if page < owner_page_floors.get(owner, 0):
                owners_stable = False
                page = owner_page_floors[owner]
            part = 0
            while paragraph is not None:
                size = page_sizes.get(page, portrait_size)
                width = size[0] - left_margin - right_margin
                body_height = size[1] - top_margin - bottom_margin
                capacity = (body_height if page > body_last_page else body_height / 2) - 12.0
                occupied = sum(h + (13.0 if p else 0) + 2 for _n, p, _f, h in new_plan.get(page, []))
                available = capacity - occupied - (13.0 if part else 0) - 2
                _, height = paragraph.wrap(width, available)
                if height <= available:
                    first, remainder = paragraph, None
                else:
                    pieces = paragraph.split(width, available)
                    if not pieces:
                        if part == 0:
                            raise PdfAssemblyError(f"footnote cannot start with owner on page {page}: {note_id}")
                        page += 1
                        continue
                    first, remainder = pieces[0], pieces[1] if len(pieces) > 1 else None
                    _, height = first.wrap(width, available)
                new_plan.setdefault(page, []).append((note_id, part, first, height))
                paragraph = remainder
                part += 1
                page += 1
        def signature(plan: dict[int, list[tuple[str, int, Paragraph, float]]]) -> list[Any]:
            return [(page, [(note, part, height) for note, part, _p, height in parts]) for page, parts in sorted(plan.items())]
        new_note_only_pages = {page for page in new_plan if page > body_last_page}
        satisfied = (owners_stable and signature(new_plan) == signature(note_plan)
            and new_note_only_pages == note_only_pages)
        if not satisfied:
            note_plan.clear()
            note_plan.update(new_plan)
            note_only_pages.clear()
            note_only_pages.update(new_note_only_pages)
        return satisfied

    running_header = _running_block(document, "header")
    running_footer = _running_block(document, "footer")
    running_page_number = _running_block(
        document, "page-number"
    ) or _varying_composite_page_number_source(document)
    header_embeds_page_number = _embedded_running_page_number(document, "header")
    footer_embeds_page_number = _embedded_running_page_number(document, "footer")
    running_style = ParagraphStyle(
        "WT-Running",
        fontName=REGULAR_FONT_NAME,
        fontSize=MINIMUM_FONT_SIZE,
        leading=11.0,
        textColor="#555555",
    )
    footnote_style = ParagraphStyle(
        "WT-PageFootnote",
        fontName=REGULAR_FONT_NAME,
        fontSize=MINIMUM_FONT_SIZE,
        leading=11.0,
        textColor="#333333",
        spaceAfter=2.0,
    )

    def schedule_page_note(note_id: str) -> Any:
        owner_id = note_owners[note_id]
        protected = [token.value for token in segments_by_block[owner_id].protected if token.kind == "footnote-marker"]
        source_links = [link for link in document.links if link.source_block_id == owner_id
            and link.destination == note_id and link.source_span is not None
            and (link.reconstructed or link.reason == "translated-visible-label-not-unambiguous")]
        authoritative_markers = set(protected)
        ownership = "protected-footnote-marker" if protected else "block-only-legacy"
        if not protected and source_links:
            authoritative_markers = {block_by_id[owner_id].source_text[link.source_span[0]:link.source_span[1]] for link in source_links}
            ownership = "source-link-marker-span"
        if len(authoritative_markers) > 1:
            raise PdfAssemblyError(f"ambiguous source footnote marker evidence: {owner_id}")
        marker_match = re.match(r"^\s*(\d+|[*†‡])(?:\s|[.)])", block_by_id[note_id].source_text)
        value = next(iter(authoritative_markers), None)
        if value is None and marker_match:
            value = marker_match[1]
            ownership = "unique-source-marker"
        marker = None
        if value is not None:
            # Korean particles may directly follow a restored numeric marker ("1이").
            # Numeric boundaries still distinguish marker1 from page11 or identifier12.
            boundary = r"\d" if value.isdecimal() and authoritative_markers else r"\w"
            candidate = re.compile(r"(?<!" + boundary + ")" + re.escape(value) + r"(?!" + boundary + ")")
            source_matches = list(candidate.finditer(block_by_id[owner_id].source_text))
            if len(source_matches) == 1:
                translated_matches = list(candidate.finditer(translated[owner_id]))
                if len(translated_matches) != 1:
                    raise PdfAssemblyError(f"footnote marker is not unique in translated owner: {owner_id}")
                marker = candidate
            elif authoritative_markers:
                raise PdfAssemblyError(f"footnote marker is not unique in source owner: {owner_id}")
        if marker is None:
            ownership = "block-only-legacy"

        def schedule(page_number: int) -> None:
            if note_id in scheduled_note_pages:
                return
            scheduled_note_pages[note_id] = page_number

        return _PageNoteCallback(owner_id, marker, schedule, owner_page_floors, ownership)

    page_note_callbacks: dict[str, _PageNoteCallback] = {
        owner_id: schedule_page_note(note_id)
        for owner_id, note_id in owner_to_note.items()
        if note_id in page_local_notes
    }

    def block_first_owners(blocks: Sequence[PdfBlock]) -> list[str]:
        return [b.id for b in blocks if b.id in page_note_callbacks and page_note_callbacks[b.id].marker is None]

    def draw_running(canvas: Any, _doc: BaseDocTemplate, *, orientation: str) -> None:
        _embed_font_faces(canvas, _doc)
        width, height = (
            portrait_size if orientation == "portrait" else landscape_size
        )
        page_number = int(canvas.getPageNumber())
        page_sizes[page_number] = (width, height)
        base_frame = portrait_frame if orientation == "portrait" else landscape_frame
        reserve = reserve_for(page_number)
        frame = _doc.pageTemplate.frames[0]
        frame._y1 = base_frame[1] + reserve
        frame._height = base_frame[3] - reserve
        frame._geom()
        if running_header is not None:
            header_text = running_header.source_text
            if header_embeds_page_number:
                header_text = f"{header_text} | {page_number}"
            frame = (left_margin, height - 37.0, width - 108.0, 13.0)
            flowable = TrackedFlowable(
                Paragraph(escape(header_text), running_style),
                block_id=running_header.id,
                kind="header",
                source_order=running_header.order,
                split_part=0,
                font_size=MINIMUM_FONT_SIZE,
                frame=frame,
                records=records,
                part_counters=part_counters,
            )
            flowable.wrapOn(canvas, frame[2], frame[3])
            flowable.drawOn(canvas, frame[0], frame[1])
        if running_footer is not None:
            footer_text = running_footer.source_text
            if footer_embeds_page_number:
                footer_text = f"{footer_text} | {page_number}"
            frame = (left_margin, 20.0, width - 180.0, 13.0)
            flowable = TrackedFlowable(
                Paragraph(escape(footer_text), running_style),
                block_id=running_footer.id,
                kind="footer",
                source_order=running_footer.order,
                split_part=0,
                font_size=MINIMUM_FONT_SIZE,
                frame=frame,
                records=records,
                part_counters=part_counters,
            )
            flowable.wrapOn(canvas, frame[2], frame[3])
            flowable.drawOn(canvas, frame[0], frame[1])
        if running_page_number is not None and not (
            header_embeds_page_number or footer_embeds_page_number
        ):
            frame = (width - right_margin - 54.0, 20.0, 54.0, 13.0)
            page_style = ParagraphStyle(
                "WT-PageNumber",
                parent=running_style,
                alignment=TA_RIGHT,
            )
            flowable = TrackedFlowable(
                Paragraph(str(page_number), page_style),
                block_id=running_page_number.id,
                kind="page-number",
                source_order=running_page_number.order,
                split_part=0,
                font_size=MINIMUM_FONT_SIZE,
                frame=frame,
                records=records,
                part_counters=part_counters,
            )
            flowable.wrapOn(canvas, frame[2], frame[3])
            flowable.drawOn(canvas, frame[0], frame[1])

    def draw_page_notes(
        canvas: Any,
        _doc: BaseDocTemplate,
        *,
        orientation: str,
    ) -> None:
        page_number = int(canvas.getPageNumber())
        planned_parts = note_plan.get(page_number, [])
        if not planned_parts:
            return
        width, height = portrait_size if orientation == "portrait" else landscape_size
        note_height = (height - top_margin - bottom_margin
            if page_number in note_only_pages else reserve_for(page_number) - 8)
        frame = (left_margin, bottom_margin, width - left_margin - right_margin, note_height)
        cursor = frame[1] + frame[3]
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#888888"))
        canvas.setLineWidth(0.5)
        canvas.line(frame[0], cursor, frame[0] + frame[2], cursor)
        canvas.restoreState()
        cursor -= 4.0
        for note_id, split_part, paragraph, height in planned_parts:
            if (page_number, note_id) in drawn_page_notes:
                continue
            block = block_by_id[note_id]
            if split_part:
                label = Paragraph("각주 계속", footnote_style)
                label.wrapOn(canvas, frame[2], 13)
                cursor -= 13
                label.drawOn(canvas, frame[0], cursor)
                continuations.append(PdfFootnoteContinuation(note_id, note_owners[note_id], split_part, page_number))
            flowable = TrackedFlowable(
                paragraph,
                block_id=block.id,
                kind="footnote",
                source_order=block.order,
                split_part=split_part,
                font_size=MINIMUM_FONT_SIZE,
                frame=frame,
                records=records,
                part_counters=part_counters,
                anchor_name=_anchor_name(block.id),
                footnote_owner_id=note_owners[note_id],
                footnote_ownership=page_note_callbacks[note_owners[note_id]].ownership,
            )
            _width, height = flowable.wrapOn(canvas, frame[2], cursor - frame[1])
            cursor -= height
            if cursor < frame[1] - 1e-6:
                raise PdfAssemblyError(
                    f"page-local footnotes exceed their frame on page {page_number}"
                )
            flowable.drawOn(canvas, frame[0], cursor)
            cursor -= 2
            drawn_page_notes.add((page_number, note_id))

    portrait_template = PageTemplate(
        id="portrait",
        pagesize=portrait_size,
        frames=[
            Frame(
                *portrait_frame,
                id="portrait-body",
                leftPadding=0,
                rightPadding=0,
                topPadding=0,
                bottomPadding=0,
                showBoundary=0,
            )
        ],
        onPage=lambda canvas, doc: draw_running(
            canvas, doc, orientation="portrait"
        ),
        onPageEnd=lambda canvas, doc: draw_page_notes(
            canvas, doc, orientation="portrait"
        ),
    )
    landscape_template = PageTemplate(
        id="landscape",
        pagesize=landscape_size,
        frames=[
            Frame(
                *landscape_frame,
                id="landscape-body",
                leftPadding=0,
                rightPadding=0,
                topPadding=0,
                bottomPadding=0,
                showBoundary=0,
            )
        ],
        onPage=lambda canvas, doc: draw_running(
            canvas, doc, orientation="landscape"
        ),
        onPageEnd=lambda canvas, doc: draw_page_notes(
            canvas, doc, orientation="landscape"
        ),
    )

    heading_sizes = sorted(
        {
            font_size_bucket(block.style.font_size)
            for block, _segment, _text in ordered
            if block.kind == "heading"
        },
        reverse=True,
    )
    list_levels = _relative_list_levels(ordered)
    tables = _table_blocks(document)
    table_header_rows = _table_header_rows(document)
    table_orientation = {
        table_id: _table_widths(
            table_id,
            table_blocks,
            translated,
            portrait_frame[2],
            landscape_frame[2],
        )
        for table_id, table_blocks in tables.items()
    }
    first_emitted = next(
        (
            block
            for block in document.blocks
            if block.kind not in _IGNORED_KINDS
            and block.id not in page_local_notes
        ),
        None,
    )
    initial_landscape = bool(
        first_emitted is not None
        and first_emitted.table_id is not None
        and table_orientation[first_emitted.table_id][1] == "landscape"
    )
    story: list[Any] = []
    emitted_tables: set[str] = set()
    emitted_figures: set[str] = set()
    emitted_section_notes: set[str] = set()
    emitted_block_ids: set[str] = set()
    composition_page: int | None = None

    def append_text(block: PdfBlock, frame: tuple[float, float, float, float]) -> None:
        if block_first_owners([block]):
            story.append(ActionFlowable(("ownerFloor", [block.id])))
        if block.id not in translated:
            raise PdfAssemblyError(f"translated text is missing for {block.id}")
        text = translated[block.id]
        bullet_text: str | None = None
        rendered_text = text
        if block.kind == "list-item":
            bullet_text, rendered_text = _normalized_list_parts(block, text)
        if block.kind in {"caption", "footnote"}:
            font_size = MINIMUM_FONT_SIZE
            style = ParagraphStyle(
                f"WT-{block.kind}-{block.order}",
                fontName=REGULAR_FONT_NAME,
                fontSize=font_size,
                leading=11.0,
                textColor="#333333",
                spaceBefore=2.0,
                spaceAfter=5.0,
            )
        else:
            font_size, style = _style_for_block(
                block,
                heading_sizes=heading_sizes,
                list_level=list_levels.get(block.id, 0),
            )
        if toc_resolutions and block.id in toc_resolutions:
            entry_indent = min((item.style.indentation for item in document.blocks if item.semantic_role == "toc-entry"), default=0)
            indent = {"toc-part": 0.0, "toc-chapter": 12.0, "toc-entry": 24.0}[block.semantic_role]
            if block.semantic_role == "toc-entry":
                indent += max(0.0, min(48.0, block.style.indentation - entry_indent))
            style = ParagraphStyle(
                f"WT-{block.semantic_role}-{block.order}", parent=style,
                fontName=REGULAR_FONT_NAME if block.semantic_role == "toc-entry" else BOLD_FONT_NAME,
                fontSize=BODY_FONT_SIZE, leading=15.0, leftIndent=indent,
                alignment=TA_LEFT, keepWithNext=block.semantic_role == "toc-part",
            )
            font_size = BODY_FONT_SIZE
            story.append(ResolvedTocEntry(
                text=text, style=style, resolution=toc_resolutions[block.id],
                anchor_pages=anchor_pages, block_id=block.id, kind=block.kind,
                source_order=block.order, split_part=0, font_size=font_size,
                frame=frame, records=records, part_counters=part_counters,
                anchor_name=_anchor_name(block.id), semantic_role=block.semantic_role,
            ))
            return
        story.append(
            TrackedFlowable(
                Paragraph(
                    _linked_markup(block, rendered_text, links_by_block.get(block.id, ())),
                    style,
                    bulletText=(
                        escape(bullet_text) if bullet_text is not None else None
                    ),
                ),
                block_id=block.id,
                kind=block.kind,
                source_order=block.order,
                split_part=0,
                font_size=font_size,
                frame=frame,
                records=records,
                part_counters=part_counters,
                anchor_name=_anchor_name(block.id),
                on_draw=page_note_callbacks.get(block.id),
                semantic_role=block.semantic_role,
            )
        )

    def append_pending_section_notes(before_order: int | None) -> None:
        pending = sorted(
            (
                (block_by_id[owner_id].order, note_id)
                for owner_id, note_id in owner_to_note.items()
                if note_id in section_notes and note_id not in emitted_section_notes
            ),
            key=lambda item: (item[0], block_by_id[item[1]].order),
        )
        for owner_order, note_id in pending:
            if before_order is not None and owner_order >= before_order:
                continue
            append_text(block_by_id[note_id], portrait_frame)
            emitted_section_notes.add(note_id)

    def append_figure_pair(figure: PdfBlock, caption: PdfBlock | None) -> None:
        image = _figure_flowable(
            figure,
            media_payloads[figure.id],
            portrait_frame,
            records,
            part_counters,
        )
        if caption is None:
            story.append(image)
            emitted_figures.add(figure.id)
            return
        caption_style = ParagraphStyle(
            f"WT-caption-{caption.order}",
            fontName=REGULAR_FONT_NAME,
            fontSize=MINIMUM_FONT_SIZE,
            leading=11.0,
            textColor="#333333",
            spaceBefore=2.0,
            spaceAfter=6.0,
        )
        caption_flowable = TrackedFlowable(
            Paragraph(
                _linked_markup(
                    caption,
                    translated[caption.id],
                    links_by_block.get(caption.id, ()),
                ),
                caption_style,
            ),
            block_id=caption.id,
            kind="caption",
            source_order=caption.order,
            split_part=0,
            font_size=MINIMUM_FONT_SIZE,
            frame=portrait_frame,
            records=records,
            part_counters=part_counters,
            anchor_name=_anchor_name(caption.id),
        )
        contents = (
            [caption_flowable, image]
            if caption.order < figure.order
            else [image, caption_flowable]
        )
        story.append(KeepTogether(contents))
        emitted_figures.add(figure.id)

    try:
        with ExitStack() as stack:
            _register_fonts(stack)
            for index, block in enumerate(document.blocks):
                if block.kind in _IGNORED_KINDS or block.id in page_local_notes:
                    continue
                if block.id in emitted_block_ids:
                    continue
                if block.kind == "heading":
                    append_pending_section_notes(block.order)
                if composition_page is not None and block.page_number != composition_page:
                    if story and not isinstance(story[-1], PageBreak):
                        story.append(PageBreak())
                    composition_page = None
                if block.semantic_role in _OPENER_ROLES | _EPIGRAPH_ROLES:
                    opener = block.semantic_role in _OPENER_ROLES
                    group = _consume_role_group(
                        document.blocks, index, _OPENER_ROLES if opener else _EPIGRAPH_ROLES,
                    )
                    append_group = _append_opener_group if opener else _append_epigraph_group
                    append_group(
                        story, group, translated, links_by_block, portrait_frame,
                        records, part_counters, callbacks=page_note_callbacks,
                    )
                    if owners := block_first_owners(group):
                        story.insert(len(story) - 1, ActionFlowable(("ownerFloor", owners)))
                    emitted_block_ids.update(item.id for item in group)
                    composition_page = block.page_number
                    continue
                next_block = (
                    document.blocks[index + 1] if index + 1 < len(document.blocks) else None
                )
                callout_icon = (
                    block.kind == "figure" and block.caption_id is None
                    and next_block is not None and next_block.semantic_role in _CALLOUT_ROLES
                    and block.page_number == next_block.page_number
                    and block.bbox[2] <= next_block.bbox[0]
                    and block.bbox[1] < next_block.bbox[3] and block.bbox[3] > next_block.bbox[1]
                )
                if block.semantic_role in _CALLOUT_ROLES or callout_icon:
                    group = _consume_role_group(
                        document.blocks, index + int(callout_icon), _CALLOUT_ROLES,
                    )
                    if callout_icon:
                        group.insert(0, block)
                    if owners := block_first_owners(group):
                        story.append(ActionFlowable(("ownerFloor", owners)))
                    _append_callout_group(
                        story, group, translated, links_by_block, portrait_frame,
                        records, part_counters, media_payloads=media_payloads,
                        callbacks=page_note_callbacks,
                    )
                    emitted_block_ids.update(item.id for item in group)
                    continue
                if block.kind == "table-cell":
                    assert block.table_id is not None
                    if block.table_id in emitted_tables:
                        continue
                    widths, orientation = table_orientation[block.table_id]
                    table_frame = (
                        portrait_frame
                        if orientation == "portrait"
                        else landscape_frame
                    )
                    if orientation == "landscape" and not (
                        initial_landscape and not story
                    ):
                        story.extend([NextPageTemplate("landscape"), PageBreak()])
                    story.append(
                        _native_table(
                            block.table_id,
                            tables[block.table_id],
                            translated,
                            widths,
                            table_frame,
                            records,
                            part_counters,
                            table_header_rows[block.table_id],
                            page_note_callbacks,
                            links_by_block,
                        )
                    )
                    emitted_tables.add(block.table_id)
                    if orientation == "landscape":
                        story.extend([NextPageTemplate("portrait"), PageBreak()])
                    continue
                if block.kind in {"figure", "caption"}:
                    if block.kind == "figure":
                        figure = block
                        caption = (
                            block_by_id[block.caption_id]
                            if block.caption_id is not None
                            else None
                        )
                    else:
                        assert block.caption_id is not None
                        figure = block_by_id[block.caption_id]
                        caption = block
                    if figure.id not in emitted_figures:
                        append_figure_pair(figure, caption)
                    continue
                if block.kind == "footnote" and block.id in section_notes:
                    continue
                append_text(block, portrait_frame)
            append_pending_section_notes(None)
            story.extend([_PublicationIndex(update_note_plan), ActionFlowable(("noteTail",))])
            pdf = PublicationDocTemplate(
                destination,
                reset_pass=reset_pass,
                anchor_pages=anchor_pages,
                last_note_page=lambda: max(note_plan, default=0),
                owner_page_floors=owner_page_floors,
                body_frame=lambda frame: (
                    (frame[0], frame[1] + reserve_for(pdf.page), frame[2], frame[3] - reserve_for(pdf.page))
                    if frame in {portrait_frame, landscape_frame} else frame
                ),
                pagesize=(
                    landscape_size if initial_landscape else portrait_size
                ),
                leftMargin=left_margin,
                rightMargin=right_margin,
                topMargin=top_margin,
                bottomMargin=bottom_margin,
                title="Reviewed Korean translation",
                author="web-translator",
                subject=_provenance_metadata(source),
            )
            templates = (
                [landscape_template, portrait_template]
                if initial_landscape
                else [portrait_template, landscape_template]
            )
            pdf.addPageTemplates(templates)
            pdf.multiBuild(story, maxPasses=8)
    except PdfAssemblyError:
        raise
    except Exception as error:
        raise PdfAssemblyError(f"ReportLab PDF assembly failed: {error}") from error
    if set(page_local_notes) != {
        note_id for _page, note_id in drawn_page_notes
    }:
        raise PdfAssemblyError("not every page-local footnote was emitted")
    if publication_evidence is not None:
        publication_evidence.update(
            anchor_pages=dict(anchor_pages),
            toc_entries=[replace(item, output_page=anchor_pages[_anchor_name(item.target_block_id)] if item.target_block_id else None)
                for item in (toc_resolutions or {}).values()],
            footnote_continuations=continuations,
        )
    return records


def _linked_markup(
    block: PdfBlock,
    text: str,
    links: Sequence[PdfLinkEvidence] = (),
) -> str:
    reconstructed = [link for link in links if link.reconstructed]
    if reconstructed:
        spans = _translated_link_spans(block, text, reconstructed)
        rendered_parts: list[str] = []
        cursor = 0
        for start, end, link in spans:
            rendered_parts.append(escape(text[cursor:start]))
            label = escape(text[start:end])
            if link.uri is not None:
                href = _safe_uri(link.uri, link.id)
            else:
                assert link.destination is not None
                href = "#" + _anchor_name(link.destination)
            rendered_parts.append(
                f"<link href={quoteattr(href)}>{label}</link>"
            )
            cursor = end
        rendered_parts.append(escape(text[cursor:]))
        return "".join(rendered_parts)
    rendered = escape(text)
    if block.uri is not None:
        return f"<link href={quoteattr(_safe_uri(block.uri, block.id))}>{rendered}</link>"
    if block.destination is not None:
        return (
            f"<link href={quoteattr('#' + _anchor_name(block.destination))}>"
            f"{rendered}</link>"
        )
    return rendered


def _links_by_block(
    document: PdfDocument,
) -> dict[str, tuple[PdfLinkEvidence, ...]]:
    grouped: dict[str, list[PdfLinkEvidence]] = {}
    for link in document.links:
        if link.source_block_id is not None:
            grouped.setdefault(link.source_block_id, []).append(link)
    return {
        block_id: tuple(sorted(items, key=lambda item: item.source_span or (0, 0)))
        for block_id, items in grouped.items()
    }


def _translated_link_spans(
    block: PdfBlock,
    text: str,
    links: Sequence[PdfLinkEvidence],
) -> list[tuple[int, int, PdfLinkEvidence]]:
    spans: list[tuple[int, int, PdfLinkEvidence]] = []
    for link in links:
        exact = text.find(link.visible_label)
        if exact >= 0 and text.find(link.visible_label, exact + 1) < 0:
            start, end = exact, exact + len(link.visible_label)
        else:
            raise PdfAssemblyError(
                f"link {link.id} has no unambiguous exact translated label "
                f"for {block.id}"
            )
        if end <= start or (spans and start < spans[-1][1]):
            raise PdfAssemblyError(
                f"ambiguous translated link spans for {block.id}"
            )
        spans.append((start, end, link))
    return spans


def _translated_link_evidence(
    document: PdfDocument,
    translated: Mapping[str, str] | Sequence[tuple[PdfBlock, Segment, str]],
) -> tuple[PdfLinkEvidence, ...]:
    """Downgrade links whose visible label has no unique exact target mapping."""
    by_block = (
        dict(translated)
        if isinstance(translated, Mapping)
        else {block.id: text for block, _segment, text in translated}
    )
    effective: list[PdfLinkEvidence] = []
    for link in document.links:
        if not link.reconstructed or link.source_block_id is None:
            effective.append(link)
            continue
        text = by_block.get(link.source_block_id, "")
        first = text.find(link.visible_label)
        if first >= 0 and text.find(link.visible_label, first + 1) < 0:
            effective.append(link)
            continue
        effective.append(
            replace(
                link,
                reconstructed=False,
                reason="translated-visible-label-not-unambiguous",
            )
        )
    return tuple(effective)


def _classify_footnotes(
    document: PdfDocument,
) -> tuple[set[str], set[str], dict[str, str]]:
    footnotes = [block for block in document.blocks if block.kind == "footnote"]
    owners = {
        block.destination: block
        for block in document.blocks
        if block.destination in {footnote.id for footnote in footnotes}
    }
    page_local = {
        footnote.id
        for footnote in footnotes
        if owners[footnote.id].page_number == footnote.page_number
    }
    section = {footnote.id for footnote in footnotes} - page_local
    return page_local, section, {
        owner.id: note_id for note_id, owner in owners.items()
    }


def _running_block(document: PdfDocument, kind: str) -> PdfBlock | None:
    blocks = [block for block in document.blocks if block.kind == kind]
    if not blocks:
        return None
    if kind != "page-number":
        label = _validated_composite_running_label(blocks, kind)
        if label is _VARYING_COMPOSITE_RUNNING_LABELS:
            return None
        if isinstance(label, str):
            return replace(blocks[0], source_text=label)
        if len({block.source_text for block in blocks}) != 1:
            raise PdfAssemblyError(f"ambiguous repeated {kind} evidence")
    return blocks[0]


_COMPOSITE_RUNNING_PAGE_PATTERN = re.compile(r"(?:\d+|[ivxlcdm]+)\Z", re.I)
_VARYING_COMPOSITE_RUNNING_LABELS = object()


def _embedded_running_page_number(document: PdfDocument, kind: str) -> bool:
    blocks = [block for block in document.blocks if block.kind == kind]
    return bool(blocks) and isinstance(
        _validated_composite_running_label(blocks, kind), str
    )


def _varying_composite_page_number_source(document: PdfDocument) -> PdfBlock | None:
    """Return evidence for drawing page numbers when varying labels are omitted."""
    for kind in ("header", "footer"):
        blocks = [block for block in document.blocks if block.kind == kind]
        if blocks and (
            _validated_composite_running_label(blocks, kind)
            is _VARYING_COMPOSITE_RUNNING_LABELS
        ):
            return blocks[0]
    return None


def _validated_composite_running_label(
    blocks: Sequence[PdfBlock],
    kind: str,
) -> str | object | None:
    parsed = [_composite_running_band(block.source_text) for block in blocks]
    if not any(item is not None for item in parsed):
        return None
    if len(blocks) < 2 or any(item is None for item in parsed):
        if len({block.source_text for block in blocks}) == 1:
            return None
        raise PdfAssemblyError(f"ambiguous repeated {kind} evidence")
    evidence = [item for item in parsed if item is not None]
    grouped: dict[str, list[tuple[PdfBlock, tuple[str, int]]]] = {}
    for block, item in zip(blocks, evidence, strict=True):
        grouped.setdefault(item[0].casefold(), []).append((block, item))
    for group in grouped.values():
        if len(group) < 2:
            raise PdfAssemblyError(f"ambiguous repeated {kind} evidence")
        ordered = sorted(group, key=lambda item: item[0].page_number)
        if any(
            right_evidence[1] - left_evidence[1]
            != right_block.page_number - left_block.page_number
            for (left_block, left_evidence), (right_block, right_evidence)
            in zip(ordered, ordered[1:])
        ):
            raise PdfAssemblyError(f"ambiguous repeated {kind} evidence")
    if len(grouped) > 1:
        return _VARYING_COMPOSITE_RUNNING_LABELS
    return evidence[0][0]


def _composite_running_band(text: str) -> tuple[str, int] | None:
    parts = [part.strip() for part in text.split("|")]
    if len(parts) != 2 or any(not part for part in parts):
        return None
    matches = [
        _COMPOSITE_RUNNING_PAGE_PATTERN.fullmatch(part) is not None for part in parts
    ]
    if sum(matches) != 1:
        return None
    token_index = matches.index(True)
    value = _running_page_value(parts[token_index])
    if value is None:
        return None
    return parts[1 - token_index], value


def _running_page_value(token: str) -> int | None:
    if token.isdecimal():
        value = int(token)
        return value if value > 0 else None
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = 0
    previous = 0
    for character in reversed(token.casefold()):
        value = values.get(character)
        if value is None:
            return None
        total += -value if value < previous else value
        previous = max(previous, value)
    return (
        total
        if 0 < total <= 3999 and _running_roman_token(total) == token.casefold()
        else None
    )


def _running_roman_token(value: int) -> str:
    parts: list[str] = []
    for amount, token in (
        (1000, "m"),
        (900, "cm"),
        (500, "d"),
        (400, "cd"),
        (100, "c"),
        (90, "xc"),
        (50, "l"),
        (40, "xl"),
        (10, "x"),
        (9, "ix"),
        (5, "v"),
        (4, "iv"),
        (1, "i"),
    ):
        while value >= amount:
            parts.append(token)
            value -= amount
    return "".join(parts)


def _table_blocks(document: PdfDocument) -> dict[str, list[PdfBlock]]:
    tables: dict[str, list[PdfBlock]] = {}
    for block in document.blocks:
        if block.kind == "table-cell":
            assert block.table_id is not None
            tables.setdefault(block.table_id, []).append(block)
    return tables


def _table_header_rows(document: PdfDocument) -> dict[str, set[int]]:
    rows_by_table: dict[str, dict[int, list[bool]]] = {}
    for cell in document.table_cells:
        rows_by_table.setdefault(cell.table_id, {}).setdefault(cell.row, []).append(
            cell.is_header
        )
    result: dict[str, set[int]] = {}
    for table_id, rows in rows_by_table.items():
        mixed = [row for row, values in rows.items() if any(values) and not all(values)]
        if mixed:
            raise PdfAssemblyError(
                f"table {table_id} has mixed header status in row {mixed[0]}"
            )
        header_rows = {row for row, values in rows.items() if values and all(values)}
        if header_rows and header_rows != set(range(max(header_rows) + 1)):
            raise PdfAssemblyError(f"table {table_id} has non-prefix header rows")
        result[table_id] = header_rows
    return result


def _table_widths(
    table_id: str,
    blocks: Sequence[PdfBlock],
    translated: Mapping[str, str],
    portrait_width: float,
    landscape_width: float,
) -> tuple[list[float], str]:
    column_count = max(
        (block.column or 0) + block.column_span for block in blocks
    )
    widths = [_TABLE_COLUMN_MINIMUM] * column_count
    for block in blocks:
        if not block.source_text.strip():
            continue
        text = translated[block.id]
        tokens = text.split() or [text]
        # The exact face is registered only inside the build ExitStack.  A
        # conservative normalized-glyph estimate keeps sizing independent of
        # prior global ReportLab registrations.
        longest = max(len(token) * MINIMUM_FONT_SIZE * 0.62 for token in tokens)
        required = min(108.0, max(_TABLE_COLUMN_MINIMUM, longest + 10.0))
        assert block.column is not None
        current = sum(widths[block.column : block.column + block.column_span])
        if required > current:
            addition = (required - current) / block.column_span
            for column in range(block.column, block.column + block.column_span):
                widths[column] += addition
    total = sum(widths)
    if total <= portrait_width + 1e-6:
        return widths, "portrait"
    if total <= landscape_width + 1e-6:
        return widths, "landscape"
    raise PdfAssemblyError(f"table {table_id} unreadable at 9-point")


def _native_table(
    table_id: str,
    blocks: Sequence[PdfBlock],
    translated: Mapping[str, str],
    widths: Sequence[float],
    frame: tuple[float, float, float, float],
    records: list[PdfFlowableLayout],
    part_counters: dict[str, int],
    header_rows: set[int],
    on_draw_by_block: Mapping[str, Callable[[Any, int, Flowable], None]],
    links_by_block: Mapping[str, Sequence[PdfLinkEvidence]],
) -> Table:
    row_count = max((block.row or 0) + block.row_span for block in blocks)
    column_count = len(widths)
    data: list[list[Any]] = [["" for _ in range(column_count)] for _ in range(row_count)]
    commands: list[tuple[Any, ...]] = []
    repeat_rows = len(header_rows)
    cell_style = ParagraphStyle(
        f"WT-table-{table_id}",
        fontName=REGULAR_FONT_NAME,
        fontSize=MINIMUM_FONT_SIZE,
        leading=11.0,
        spaceAfter=0.0,
    )
    header_style = ParagraphStyle(
        f"WT-table-header-{table_id}",
        parent=cell_style,
        fontName=BOLD_FONT_NAME,
    )
    for block in blocks:
        assert block.row is not None and block.column is not None
        style = header_style if block.row in header_rows else cell_style
        if block.source_text.strip():
            paragraph = Paragraph(
                _linked_markup(
                    block,
                    translated[block.id],
                    links_by_block.get(block.id, ()),
                ),
                style,
            )
        else:
            paragraph = Spacer(1.0, MINIMUM_FONT_SIZE)
        data[block.row][block.column] = TrackedFlowable(
            paragraph,
            block_id=block.id,
            kind="table-cell",
            source_order=block.order,
            split_part=0,
            font_size=MINIMUM_FONT_SIZE,
            frame=frame,
            records=records,
            part_counters=part_counters,
            anchor_name=_anchor_name(block.id),
            on_draw=on_draw_by_block.get(block.id),
        )
        if block.row_span > 1 or block.column_span > 1:
            commands.append(
                (
                    "SPAN",
                    (block.column, block.row),
                    (
                        block.column + block.column_span - 1,
                        block.row + block.row_span - 1,
                    ),
                )
            )
    commands.extend(
        [
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#777777")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), _TABLE_CELL_PADDING),
            ("RIGHTPADDING", (0, 0), (-1, -1), _TABLE_CELL_PADDING),
            ("TOPPADDING", (0, 0), (-1, -1), 4.0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4.0),
        ]
    )
    if repeat_rows:
        commands.append(
            ("BACKGROUND", (0, 0), (-1, repeat_rows - 1), colors.HexColor("#E8EEF5"))
        )
    table = Table(
        data,
        colWidths=list(widths),
        repeatRows=repeat_rows,
        splitByRow=1,
        hAlign="LEFT",
    )
    table.setStyle(TableStyle(commands))
    return table


def _figure_flowable(
    block: PdfBlock,
    payload: bytes,
    frame: tuple[float, float, float, float],
    records: list[PdfFlowableLayout],
    part_counters: dict[str, int],
    *,
    max_width: float | None = None,
) -> TrackedFlowable:
    try:
        with PillowImage.open(io.BytesIO(payload)) as image:
            if image.format != "PNG":
                raise PdfAssemblyError(f"figure media is not PNG: {block.id}")
            pixel_width, pixel_height = image.size
            image.verify()
    except PdfAssemblyError:
        raise
    except Exception as error:
        raise PdfAssemblyError(f"invalid figure media for {block.id}: {error}") from error
    expected_width = (block.bbox[2] - block.bbox[0]) * _FIGURE_RENDER_DPI / 72.0
    expected_height = (block.bbox[3] - block.bbox[1]) * _FIGURE_RENDER_DPI / 72.0
    if (
        abs(pixel_width - expected_width) > 2.0
        or abs(pixel_height - expected_height) > 2.0
    ):
        raise PdfAssemblyError(f"figure media dimensions do not match source bounds: {block.id}")
    natural_width = pixel_width * 72.0 / _FIGURE_RENDER_DPI
    natural_height = pixel_height * 72.0 / _FIGURE_RENDER_DPI
    available_width = frame[2] if max_width is None else min(frame[2], max_width)
    scale = min(1.0, available_width / natural_width, frame[3] / natural_height)
    width = natural_width * scale
    height = natural_height * scale
    image = Image(io.BytesIO(payload), width=width, height=height)
    image.hAlign = "LEFT"
    return TrackedFlowable(
        image,
        block_id=block.id,
        kind="figure",
        source_order=block.order,
        split_part=0,
        font_size=MINIMUM_FONT_SIZE,
        frame=frame,
        records=records,
        part_counters=part_counters,
        anchor_name=_anchor_name(block.id),
    )


def _provenance_metadata(source: PdfSourceRecord) -> str:
    generated = datetime.now(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    return f"Selectable Korean PDF translation; Source: {source.final_source}; Generated: {generated}"


def _style_for_block(
    block: PdfBlock,
    *,
    heading_sizes: Sequence[int],
    list_level: int,
) -> tuple[float, ParagraphStyle]:
    if block.semantic_role == "reference-entry":
        return BODY_FONT_SIZE, _reference_style(block)
    if block.kind == "heading":
        alignment = _ALIGNMENTS[block.style.alignment]
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
        alignment=_ALIGNMENTS["left"],
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
    rounded_size = font_size_bucket(block.style.font_size)
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
    pages = [page for page in document.pages if page.width <= page.height] or document.pages
    counts = Counter((page.width, page.height) for page in pages)
    width, height = counts.most_common(1)[0][0]
    return PdfPageSize(name="SOURCE", width=width, height=height)


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


def _open_existing_child_directory(
    parent: _DirectoryAnchor,
    name: str,
    label: str,
) -> _DirectoryAnchor:
    if Path(name).name != name:
        raise PdfAssemblyError(f"unsafe anchored evidence directory name: {name}")
    parent.verify_visible()
    descriptor: int | None = None
    path_anchor: object | None = None
    windows_handle: int | None = None
    try:
        if parent.descriptor is not None:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent.descriptor,
            )
            result = os.fstat(descriptor)
            if not stat.S_ISDIR(result.st_mode) or _is_reparse_stat(result):
                raise PdfAssemblyError(
                    f"anchored {label} is not a safe directory: {parent.path / name}"
                )
            identity = (result.st_dev, result.st_ino)
        elif _IS_WINDOWS:
            windows_handle = _windows_open_relative_directory(
                _windows_anchor_handle(parent),
                name,
            )
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
                f"safe anchored evidence directory open unavailable: "
                f"{parent.path / name}"
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
    except PdfAssemblyError:
        raise
    except (AttributeError, NotImplementedError, OSError) as error:
        raise PdfAssemblyError(
            f"cannot open anchored {label} directory {parent.path / name}: {error}"
        ) from error
    finally:
        if sys.exc_info()[0] is not None:
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
            if windows_handle is not None:
                pdf_acquire_module._close_windows_handle(windows_handle)


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


def _windows_directory_names(directory_handle: int) -> list[str]:
    if not _IS_WINDOWS:
        raise PdfAssemblyError("Windows anchored directory enumeration is unavailable")
    try:
        import ctypes
        from ctypes import wintypes

        class IoStatusBlock(ctypes.Structure):
            _fields_ = (
                ("status_or_pointer", ctypes.c_void_p),
                ("information", ctypes.c_size_t),
            )

        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        query_directory = ntdll.NtQueryDirectoryFile
        query_directory.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.LPVOID,
            ctypes.POINTER(IoStatusBlock),
            wintypes.LPVOID,
            wintypes.ULONG,
            ctypes.c_int32,
            wintypes.BOOLEAN,
            wintypes.LPVOID,
            wintypes.BOOLEAN,
        )
        query_directory.restype = ctypes.c_int32
        to_dos_error = ntdll.RtlNtStatusToDosError
        to_dos_error.argtypes = (ctypes.c_int32,)
        to_dos_error.restype = wintypes.ULONG

        names: list[str] = []
        restart_scan = True
        while True:
            buffer = ctypes.create_string_buffer(64 * 1024)
            status_block = IoStatusBlock()
            status = int(
                query_directory(
                    wintypes.HANDLE(directory_handle),
                    wintypes.HANDLE(),
                    None,
                    None,
                    ctypes.byref(status_block),
                    buffer,
                    len(buffer),
                    12,
                    False,
                    None,
                    restart_scan,
                )
            )
            if ctypes.c_uint32(status).value == 0x80000006:
                break
            if status < 0:
                raise ctypes.WinError(int(to_dos_error(status)))
            used = int(status_block.information)
            offset = 0
            while offset < used:
                if used - offset < 12:
                    raise PdfAssemblyError(
                        "Windows directory enumeration returned a truncated record"
                    )
                next_offset = int.from_bytes(buffer.raw[offset : offset + 4], "little")
                name_length = int.from_bytes(
                    buffer.raw[offset + 8 : offset + 12], "little"
                )
                name_start = offset + 12
                name_end = name_start + name_length
                if name_length % 2 or name_end > used:
                    raise PdfAssemblyError(
                        "Windows directory enumeration returned an invalid name"
                    )
                name = buffer.raw[name_start:name_end].decode("utf-16-le")
                if name not in {".", ".."}:
                    names.append(name)
                if next_offset == 0:
                    break
                if next_offset < 12 or offset + next_offset > used:
                    raise PdfAssemblyError(
                        "Windows directory enumeration returned an invalid offset"
                    )
                offset += next_offset
            restart_scan = False
        return names
    except PdfAssemblyError:
        raise
    except (AttributeError, NotImplementedError, OSError, UnicodeError) as error:
        raise PdfAssemblyError(
            f"cannot enumerate Windows anchored directory: {error}"
        ) from error


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


def _windows_open_relative_directory(root_handle: int, name: str) -> int:
    return _windows_nt_create_relative(
        root_handle,
        name,
        desired_access=0x00000001 | 0x00000080 | 0x00100000,
        create_disposition=1,
        create_options=0x00000001 | 0x00000020 | 0x00200000,
        file_attributes=0,
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
    succeeded = False
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
        succeeded = True
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
        if not succeeded:
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
        # Use the native information class that consumes a held RootDirectory
        # handle.  The Win32 class-3 wrapper rejects this relative form with
        # ERROR_INVALID_PARAMETER on current Windows runners even though the
        # underlying FileRenameInformation contract supports it.
        class IoStatusBlock(ctypes.Structure):
            _fields_ = (
                ("status_or_pointer", ctypes.c_void_p),
                ("information", ctypes.c_size_t),
            )

        status_block = IoStatusBlock()
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        set_information = ntdll.NtSetInformationFile
        set_information.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(IoStatusBlock),
            wintypes.LPVOID,
            ctypes.c_uint32,
            ctypes.c_int32,
        )
        set_information.restype = ctypes.c_int32
        status = int(
            set_information(
                source_handle,
                ctypes.byref(status_block),
                buffer,
                buffer_size,
                10,
            )
        )
        if status < 0:
            to_dos_error = ntdll.RtlNtStatusToDosError
            to_dos_error.argtypes = (ctypes.c_int32,)
            to_dos_error.restype = wintypes.ULONG
            raise ctypes.WinError(int(to_dos_error(status)))
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
        if not set_information(
            handle,
            4,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    except (AttributeError, OSError) as error:
        raise PdfAssemblyError(
            f"cannot mark exact Windows file for deletion: {error}"
        ) from error


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
