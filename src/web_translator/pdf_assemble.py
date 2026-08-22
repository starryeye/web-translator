"""Basic, fail-closed ReportLab assembly for reviewed PDF translations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
import hashlib
from importlib.resources import as_file, files
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

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
_REPARSE_POINT = 0x400
_IS_WINDOWS = os.name == "nt"
_BASIC_KINDS = {"heading", "paragraph", "list-item"}
_IGNORED_KINDS = {"header", "footer", "page-number"}
_ALIGNMENTS = {
    "left": TA_LEFT,
    "center": TA_CENTER,
    "right": TA_RIGHT,
    "justify": TA_JUSTIFY,
}


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
    owned_staging_identity: tuple[int, int] | None = None
    owned_pdf_identity: tuple[int, int] | None = None
    owned_layout_identity: tuple[int, int] | None = None
    staging = run_dir / "staged-output"
    layout_path = run_dir / "layout.json"
    try:
        temporary = Path(tempfile.mkdtemp(prefix=".pdf-assembling-", dir=run_dir))
        temporary_staging = temporary / "staged-output"
        temporary_staging.mkdir()
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
        owned_pdf_identity = _publish_new_file(
            temporary_pdf, staging / "translated.pdf"
        )
        owned_layout_identity = _publish_new_file(temporary_layout, layout_path)
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
            _remove_owned_file(layout_path, owned_layout_identity)
            _remove_owned_staging(
                staging, owned_staging_identity, owned_pdf_identity
            )
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
    try:
        with ExitStack() as stack:
            _register_fonts(stack)
            for block, segment, text in ordered:
                font_size, style = _style_for_block(block, segment)
                paragraph = Paragraph(
                    escape(text),
                    style,
                    bulletText=getattr(style, "bulletText", None),
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
                    f"Source: {escape(source_label)}<br/>Generated: {escape(generated)}",
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
    block: PdfBlock, segment: Segment
) -> tuple[float, ParagraphStyle]:
    alignment = _ALIGNMENTS[block.style.alignment]
    if block.kind == "heading":
        level = max(1, min(6, len(segment.heading_path) or 1))
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
    first_indent = 0.0
    bullet_text: str | None = None
    if block.kind == "list-item":
        left_indent = 18.0 + max(0.0, min(54.0, block.style.indentation))
        first_indent = -10.0
        bullet_text = "•"
    style = ParagraphStyle(
        f"WT-{block.kind}-{block.order}",
        fontName=REGULAR_FONT_NAME,
        bulletFontName=REGULAR_FONT_NAME,
        bulletFontSize=BODY_FONT_SIZE,
        fontSize=BODY_FONT_SIZE,
        leading=15.0,
        alignment=alignment,
        leftIndent=left_indent,
        firstLineIndent=first_indent,
        spaceAfter=max(4.0, min(14.0, block.style.space_after)),
    )
    if bullet_text is not None:
        # Paragraph accepts bullet text at construction time; keep it on the style
        # as a private, assembly-only marker consumed by the caller below.
        style.bulletText = bullet_text
    return BODY_FONT_SIZE, style


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
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _path_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    return metadata.st_dev, metadata.st_ino


def _publish_new_file(source: Path, destination: Path) -> tuple[int, int]:
    source_identity = _path_identity(source)
    try:
        os.link(source, destination)
        return source_identity
    except FileExistsError as error:
        raise PdfAssemblyError(f"assembly destination already exists: {destination}") from error
    except (AttributeError, NotImplementedError, OSError) as link_error:
        if not _IS_WINDOWS:
            raise PdfAssemblyError(
                f"safe assembly publication unavailable: {destination}: {link_error}"
            ) from link_error
    try:
        # Windows rename is atomic and refuses an existing destination. It is
        # the no-overwrite fallback when hard links are unavailable there.
        os.rename(source, destination)
        return source_identity
    except FileExistsError as error:
        raise PdfAssemblyError(f"assembly destination already exists: {destination}") from error
    except OSError as error:
        if destination.exists() or destination.is_symlink():
            raise PdfAssemblyError(
                f"assembly destination already exists: {destination}"
            ) from error
        raise PdfAssemblyError(f"cannot publish assembly artifact: {error}") from error


def _remove_owned_file(path: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        if _path_identity(path) == identity and not _is_link_or_reparse(path):
            path.unlink()
    except OSError:
        return


def _remove_owned_staging(
    path: Path,
    identity: tuple[int, int] | None,
    translated_identity: tuple[int, int] | None,
) -> None:
    if identity is None:
        return
    try:
        if _path_identity(path) != identity or _is_link_or_reparse(path):
            return
        translated = path / "translated.pdf"
        if (
            translated_identity is not None
            and translated.is_file()
            and not _is_link_or_reparse(translated)
            and _path_identity(translated) == translated_identity
        ):
            translated.unlink()
        path.rmdir()
    except OSError:
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
