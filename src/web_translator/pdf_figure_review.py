"""Exact source-character inventory and explicit PDF artwork review contracts."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
from typing import Any
import uuid

import pdfplumber
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from web_translator.pdf_acquire import MAX_PDF_BYTES
from web_translator.pdf_extract import (
    PdfExtractionError, PdfInspection, _inspect_page, _normalized_rotation,
    _validated_page_tree_count, reject_unsupported_pdf,
)
from web_translator.pdf_media import PdfMediaError, figure_owns_character
from web_translator.pdf_models import PdfContractError, PdfDocument, PdfSourceRecord
from web_translator.pdf_unit_bindings import PdfUnitBindingError, require_assignable_pdf

FIGURE_TEXT_INPUT_NAME = "figure-text-input.json"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BLOCK = re.compile(r"pdf:page-(\d{4}):block-\d{4}\Z")


class PdfFigureReviewError(ValueError):
    """Figure review evidence is malformed, stale, unsafe or incomplete."""


def _fields(value: Any, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise PdfFigureReviewError(f"{label} fields must be exact")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PdfFigureReviewError("figure review SHA-256 is invalid")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PdfFigureReviewError(f"{label} must be nonempty text")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PdfFigureReviewError(f"{label} must be an array")
    return value


def _rectangle(value: Any, *, glyph: bool = False) -> list[float]:
    # Combining/ToUnicode glyphs can have zero advance; retain exact source boxes.
    # Artwork/native block boxes must still have positive area.
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)
            or value[0] > value[2] or value[1] > value[3]
            or (not glyph and (value[0] == value[2] or value[1] == value[3]))):
        raise PdfFigureReviewError("figure review rectangle must be finite and ordered")
    return list(value)


def _block_id(value: Any) -> int:
    match = _BLOCK.fullmatch(value) if isinstance(value, str) else None
    if match is None or int(match.group(1)) < 1:
        raise PdfFigureReviewError("figure review requires a stable figure block ID")
    return int(match.group(1))


def _indexes(value: Any, *, nonempty: bool = True) -> list[int]:
    values = _array(value, "character indexes")
    if ((nonempty and not values) or any(type(v) is not int or v < 0 for v in values)
            or values != sorted(set(values))):
        raise PdfFigureReviewError("character indexes must be sorted unique nonnegative integers")
    return list(values)


def parse_pdf_figure_text_input(value: Any) -> dict[str, Any]:
    root = _fields(value, {"schema_version", "source_sha256", "document_sha256", "figures"}, "figure input")
    if root["schema_version"] != "1.0":
        raise PdfFigureReviewError("figure input schema must be 1.0")
    figures = []
    seen_ids: set[str] = set()
    seen_indexes: set[tuple[int, int]] = set()
    for item in _array(root["figures"], "figures"):
        figure = _fields(item, {"block_id", "page_number", "bbox", "media_sha256", "characters"}, "figure")
        page = _block_id(figure["block_id"])
        if type(figure["page_number"]) is not int or figure["page_number"] != page:
            raise PdfFigureReviewError("figure ID and page number do not correspond")
        if figure["block_id"] in seen_ids:
            raise PdfFigureReviewError("duplicate figure block ID")
        seen_ids.add(figure["block_id"])
        bbox = _rectangle(figure["bbox"])
        characters = []
        previous = -1
        for item in _array(figure["characters"], "characters"):
            char = _fields(item, {"index", "text", "bbox"}, "character")
            index = char["index"]
            if type(index) is not int or index < 0 or index <= previous or (page, index) in seen_indexes:
                raise PdfFigureReviewError("character indexes must be ordered and uniquely owned")
            seen_indexes.add((page, index))
            previous = index
            char_box = _rectangle(char["bbox"], glyph=True)
            try:
                if not figure_owns_character(dict(zip(("x0", "top", "x1", "bottom"), char_box)), tuple(bbox), page_number=page):
                    raise PdfFigureReviewError("inventory character is outside its figure")
            except PdfMediaError as error:
                raise PdfFigureReviewError(str(error)) from error
            characters.append({"index": index, "text": _text(char["text"], "character text"), "bbox": char_box})
        figures.append({"block_id": figure["block_id"], "page_number": page, "bbox": bbox,
                        "media_sha256": _digest(figure["media_sha256"]), "characters": characters})
    return {"schema_version": "1.0", "source_sha256": _digest(root["source_sha256"]),
            "document_sha256": _digest(root["document_sha256"]), "figures": figures}


def canonical_figure_text_input_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (json.dumps(parse_pdf_figure_text_input(value), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (PdfMediaError, UnicodeError, TypeError, ValueError) as error:
        raise PdfFigureReviewError(str(error)) from error


def parse_pdf_figure_text_review(value: Any) -> dict[str, Any]:
    root = _fields(value, {"schema_version", "inventory_sha256", "figures"}, "figure review")
    if root["schema_version"] != "1.0":
        raise PdfFigureReviewError("figure review schema must be 1.0")
    if not isinstance(root["figures"], Mapping):
        raise PdfFigureReviewError("figure review figures must be a map")
    figures = {}
    for block_id, item in root["figures"].items():
        _block_id(block_id)
        figure = _fields(item, {"verdict", "evidence", "labels"}, "reviewed figure")
        if figure["verdict"] not in ("pass", "required-fix"):
            raise PdfFigureReviewError("figure verdict must be pass or required-fix")
        labels = []
        owned: set[int] = set()
        for item in _array(figure["labels"], "labels"):
            label = _fields(item, {"character_indexes", "text", "reason"}, "figure label")
            indexes = _indexes(label["character_indexes"])
            if owned.intersection(indexes):
                raise PdfFigureReviewError("figure labels have duplicate character ownership")
            owned.update(indexes)
            labels.append({"character_indexes": indexes, "text": _text(label["text"], "label text"),
                           "reason": _text(label["reason"], "label reason")})
        figures[block_id] = {"verdict": figure["verdict"], "evidence": _text(figure["evidence"], "figure evidence"), "labels": labels}
    return {"schema_version": "1.0", "inventory_sha256": _digest(root["inventory_sha256"]), "figures": figures}


def validate_pdf_figure_text_review(value: Any, *, inventory_bytes: bytes, require_pass: bool = True) -> dict[str, Any]:
    try:
        inventory = parse_pdf_figure_text_input(json.loads(inventory_bytes))
        if canonical_figure_text_input_bytes(inventory) != inventory_bytes:
            raise PdfFigureReviewError("figure inventory file must contain exact canonical bytes")
        review = parse_pdf_figure_text_review(value)
        if review["inventory_sha256"] != hashlib.sha256(inventory_bytes).hexdigest():
            raise PdfFigureReviewError("figure review inventory digest does not match")
        expected = {figure["block_id"]: figure for figure in inventory["figures"]}
        if review["figures"].keys() != expected.keys():
            raise PdfFigureReviewError("figure review must exactly cover inventory figures")
        for block_id, figure in review["figures"].items():
            if require_pass and figure["verdict"] != "pass":
                raise PdfFigureReviewError(f"figure {block_id} requires a fix before publication")
            chars = {c["index"]: c["text"] for c in expected[block_id]["characters"]}
            covered: set[int] = set()
            for label in figure["labels"]:
                indexes = label["character_indexes"]
                if any(i not in chars for i in indexes):
                    raise PdfFigureReviewError(f"figure {block_id} label has foreign character indexes")
                if label["text"] != "".join(chars[i] for i in indexes):
                    raise PdfFigureReviewError(f"figure {block_id} label text is not exact source text")
                covered.update(indexes)
            if figure["verdict"] == "pass" and covered != chars.keys():
                raise PdfFigureReviewError(f"figure {block_id} label coverage is incomplete")
        return review
    except (UnicodeError, json.JSONDecodeError, PdfMediaError) as error:
        raise PdfFigureReviewError(str(error)) from error


def build_pdf_figure_text_input(document: PdfDocument, *, document_bytes: bytes,
                                source_pdf_bytes: bytes, media_payloads: Mapping[str, bytes]) -> dict[str, Any]:
    """Inventory all non-whitespace glyphs; make no semantic label decisions."""
    try:
        if PdfDocument.from_dict(json.loads(document_bytes)) != document:
            raise PdfFigureReviewError("document bytes do not match the supplied document")
        require_assignable_pdf(document)
        if len(source_pdf_bytes) > MAX_PDF_BYTES:
            raise PdfFigureReviewError("source PDF exceeds the 50 MiB size limit")
        if not source_pdf_bytes.rstrip(b" \t\r\n\f\x00").endswith(b"%%EOF"):
            raise PdfFigureReviewError("source PDF is missing its final EOF")
        source_hash = hashlib.sha256(source_pdf_bytes).hexdigest()
        if source_hash != document.source_sha256:
            raise PdfFigureReviewError("source PDF SHA-256 does not match document")
        figures = [b for b in document.blocks if b.kind == "figure"]
        if media_payloads.keys() != {b.id for b in figures} or any(not isinstance(p, bytes) or not p for p in media_payloads.values()):
            raise PdfFigureReviewError("figure media payloads must exactly cover source figures")
        reader = PdfReader(io.BytesIO(source_pdf_bytes), strict=True)
        if reader.is_encrypted:
            raise PdfFigureReviewError("encrypted source PDF is unsupported")
        tree_page_count = _validated_page_tree_count(reader)
        if tree_page_count != document.page_count or not 1 <= tree_page_count <= 500:
            raise PdfFigureReviewError("source/document page count mismatch or page limit exceeded")
        source_pages = list(reader.pages)
        if len(source_pages) != tree_page_count:
            raise PdfFigureReviewError("source page tree count disagrees with flattened pages")
        rotations = [_normalized_rotation(page.get("/Rotate", 0)) for page in source_pages]
        result = {"schema_version": "1.0", "source_sha256": source_hash,
                  "document_sha256": hashlib.sha256(document_bytes).hexdigest(), "figures": []}
        by_id = {}
        for figure in figures:
            record = {"block_id": figure.id, "page_number": figure.page_number, "bbox": list(figure.bbox),
                      "media_sha256": hashlib.sha256(media_payloads[figure.id]).hexdigest(), "characters": []}
            result["figures"].append(record)
            by_id[figure.id] = record
        with pdfplumber.open(io.BytesIO(source_pdf_bytes)) as pdf:
            if len(pdf.pages) != tree_page_count:
                raise PdfFigureReviewError("source PDF readers disagree on page count")
            evidence = [_inspect_page(n, page, rotations[n - 1]) for n, page in enumerate(pdf.pages, 1)]
            inspection = PdfInspection(document.page_count, sum(p.selectable_characters for p in evidence),
                                       [p.number for p in evidence if p.scan_candidate], evidence)
            reject_unsupported_pdf(inspection)
            if (inspection.selectable_characters != document.selectable_characters
                    or inspection.scan_candidate_pages != document.scan_candidate_pages):
                raise PdfFigureReviewError("source/document selectable page evidence mismatch")
            for page, declared, observed in zip(pdf.pages, document.pages, evidence, strict=True):
                if (declared.width, declared.height, declared.rotation) != (observed.width, observed.height, observed.rotation):
                    raise PdfFigureReviewError("source/document page geometry mismatch")
                local_figures = [b for b in figures if b.page_number == declared.number]
                body = [b for b in document.blocks if b.page_number == declared.number and b.segment_id is not None]
                for figure in local_figures:
                    _rectangle(figure.bbox)
                    if not (0 <= figure.bbox[0] < figure.bbox[2] <= observed.width
                            and 0 <= figure.bbox[1] < figure.bbox[3] <= observed.height):
                        raise PdfFigureReviewError("figure bounds are outside source page")
                for index, char in enumerate(page.chars):
                    owners = [b for b in local_figures if figure_owns_character(char, b.bbox, page_number=declared.number)]
                    if len(owners) > 1:
                        raise PdfFigureReviewError("source glyph has duplicate figure ownership")
                    if not owners:
                        continue
                    bbox = _rectangle([char["x0"], char["top"], char["x1"], char["bottom"]], glyph=True)
                    if any(figure_owns_character(char, b.bbox, page_number=declared.number) for b in body):
                        raise PdfFigureReviewError("figure glyph overlaps a translatable block")
                    text = char["text"]
                    if not isinstance(text, str):
                        raise PdfFigureReviewError("source character text is invalid")
                    if text.strip():
                        by_id[owners[0].id]["characters"].append({"index": index, "text": text, "bbox": bbox})
        return parse_pdf_figure_text_input(result)
    except PdfFigureReviewError:
        raise
    except (PdfContractError, PdfUnitBindingError, PdfMediaError, PdfExtractionError, PyPdfError,
            UnicodeError, json.JSONDecodeError, ValueError, KeyError, TypeError, OSError) as error:
        raise PdfFigureReviewError(str(error)) from error


@dataclass(slots=True)
class PdfFigureInputSnapshot:
    """Held exact inputs; valid only while their owning context is alive."""

    run_anchor: Any
    document: PdfDocument
    source: PdfSourceRecord
    document_bytes: bytes
    source_pdf_bytes: bytes
    media_payloads: dict[str, bytes]
    inventory_bytes: bytes | None
    root_files: dict[str, Any]
    root_payloads: dict[str, bytes]
    media_anchor: Any | None
    media_files: dict[str, Any]
    media_file_payloads: dict[str, bytes]
    media_names: list[str]

    def verify(self) -> None:
        import web_translator.pdf_assemble as anchored

        try:
            self.run_anchor.verify_visible()
            anchored._verify_anchored_evidence(self.run_anchor, self.root_files)
            for name, payload in self.root_payloads.items():
                if anchored._read_opened_bytes(self.root_files[name], self.run_anchor.path / name, name) != payload:
                    raise PdfFigureReviewError(f"PDF figure input changed content: {name}")
            if self.media_anchor is not None:
                self.media_anchor.verify_visible()
                if anchored._anchored_directory_names(self.media_anchor) != self.media_names:
                    raise PdfFigureReviewError("PDF figure media directory changed child set")
                anchored._verify_anchored_evidence(self.media_anchor, self.media_files)
                for name, payload in self.media_file_payloads.items():
                    if anchored._read_opened_bytes(self.media_files[name], self.media_anchor.path / name, name) != payload:
                        raise PdfFigureReviewError(f"PDF figure input changed content: media/{name}")
        except anchored.PdfAssemblyError as error:
            raise PdfFigureReviewError(str(error)) from error


@contextmanager
def hold_pdf_figure_inputs(run: Path | Any) -> Iterator[PdfFigureInputSnapshot]:
    """Hold source/document/artwork (and any existing inventory) through consumption."""
    import web_translator.pdf_assemble as anchored

    owns_run = isinstance(run, (str, os.PathLike))
    run_anchor = None
    media_anchor = None
    root_files: dict[str, Any] = {}
    media_files: dict[str, Any] = {}
    try:
        run_anchor = anchored._open_directory_anchor(Path(run), "PDF run") if owns_run else run
        names = anchored._anchored_directory_names(run_anchor)
        required = ["document.json", "source.json", "source.pdf"]
        if FIGURE_TEXT_INPUT_NAME in names:
            required.append(FIGURE_TEXT_INPUT_NAME)
        for name in required:
            root_files[name] = anchored._open_anchored_input_file(run_anchor, name, f"PDF figure input {name}")
        if os.fstat(root_files["source.pdf"].stream.fileno()).st_size > MAX_PDF_BYTES:
            raise PdfFigureReviewError("source PDF exceeds the 50 MiB size limit")
        payloads = {name: anchored._read_opened_bytes(opened, run_anchor.path / name, name) for name, opened in root_files.items()}
        document = PdfDocument.from_dict(json.loads(payloads["document.json"]))
        source = PdfSourceRecord.from_dict(json.loads(payloads["source.json"]))
        require_assignable_pdf(document)
        if (source.content_type != "application/pdf" or source.byte_length != len(payloads["source.pdf"])
                or source.sha256 != document.source_sha256
                or source.sha256 != hashlib.sha256(payloads["source.pdf"]).hexdigest()):
            raise PdfFigureReviewError("source.json length/SHA-256 does not match held PDF source/document")
        figures = [b for b in document.blocks if b.kind == "figure"]
        media_payloads = {}
        media_file_payloads = {}
        media_names = []
        if figures:
            media_anchor = anchored._open_existing_child_directory(run_anchor, "media", "PDF figure media")
            media_names = anchored._anchored_directory_names(media_anchor)
            for block in figures:
                name = anchored._media_name(block)
                if name not in media_files:
                    media_files[name] = anchored._open_anchored_input_file(media_anchor, name, "PDF figure media")
                    media_file_payloads[name] = anchored._read_opened_bytes(media_files[name], media_anchor.path / name, "PDF figure media")
                media_payloads[block.id] = media_file_payloads[name]
        # Validate actual source page/selectability/ownership evidence at context entry.
        build_pdf_figure_text_input(document, document_bytes=payloads["document.json"],
                                    source_pdf_bytes=payloads["source.pdf"], media_payloads=media_payloads)
        snapshot = PdfFigureInputSnapshot(run_anchor, document, source, payloads["document.json"],
            payloads["source.pdf"], media_payloads, payloads.get(FIGURE_TEXT_INPUT_NAME), root_files,
            payloads, media_anchor, media_files, media_file_payloads, media_names)
        snapshot.verify()
        yield snapshot
    except PdfFigureReviewError:
        raise
    except (anchored.PdfAssemblyError, PdfContractError, PdfUnitBindingError, UnicodeError,
            json.JSONDecodeError, KeyError, TypeError, OSError) as error:
        raise PdfFigureReviewError(str(error)) from error
    finally:
        for opened in media_files.values():
            anchored._close_opened_file(opened)
        if media_anchor is not None:
            media_anchor.close()
        for opened in root_files.values():
            anchored._close_opened_file(opened)
        if owns_run and run_anchor is not None:
            run_anchor.close()


def _snapshot_inventory_bytes(snapshot: PdfFigureInputSnapshot) -> bytes:
    return canonical_figure_text_input_bytes(build_pdf_figure_text_input(snapshot.document,
        document_bytes=snapshot.document_bytes, source_pdf_bytes=snapshot.source_pdf_bytes,
        media_payloads=snapshot.media_payloads))


def validate_current_pdf_figure_review(snapshot: PdfFigureInputSnapshot, *, inventory_bytes: bytes,
                                       review_value: Any) -> dict[str, Any]:
    """Bind a passing review to recomputed exact held current source evidence."""
    if _snapshot_inventory_bytes(snapshot) != inventory_bytes:
        raise PdfFigureReviewError("figure inventory does not match current source/document/media; regenerate figure review input")
    if snapshot.inventory_bytes is not None and snapshot.inventory_bytes != inventory_bytes:
        raise PdfFigureReviewError("figure inventory does not match the held inventory file")
    review = validate_pdf_figure_text_review(review_value, inventory_bytes=inventory_bytes)
    snapshot.verify()
    return review


def write_pdf_figure_review_input(run_dir: Path) -> Path:
    """Publish new canonical inventory atomically, or reuse identical held evidence."""
    import web_translator.pdf_assemble as anchored

    with hold_pdf_figure_inputs(run_dir) as snapshot:
        payload = _snapshot_inventory_bytes(snapshot)
        destination = snapshot.run_anchor.path / FIGURE_TEXT_INPUT_NAME
        if snapshot.inventory_bytes is not None:
            if snapshot.inventory_bytes != payload:
                raise PdfFigureReviewError("existing figure inventory differs from current canonical source evidence; use a fresh run")
            snapshot.verify()
            return destination
        temporary_name = f".figure-text-input-{uuid.uuid4().hex}.json"
        opened = None
        published = None
        completed = False
        try:
            opened = anchored._create_anchored_binary_file(snapshot.run_anchor, temporary_name)
            opened.stream.write(payload)
            digest = anchored._finalize_opened_file(opened, "PDF figure review input")
            if digest != hashlib.sha256(payload).hexdigest():
                raise PdfFigureReviewError("figure inventory changed content while writing")
            snapshot.verify()
            anchored._verify_anchored_input_identity(snapshot.run_anchor, temporary_name, opened.identity)
            published = anchored._publish_new_file(snapshot.run_anchor, temporary_name, snapshot.run_anchor, FIGURE_TEXT_INPUT_NAME)
            snapshot.verify()
            anchored._verify_anchored_input_identity(snapshot.run_anchor, FIGURE_TEXT_INPUT_NAME, opened.identity)
            if anchored._read_opened_bytes(opened, destination, "PDF figure review input") != payload:
                raise PdfFigureReviewError("published figure inventory changed content")
            completed = True
            return destination
        except (anchored.PdfAssemblyError, OSError) as error:
            raise PdfFigureReviewError(str(error)) from error
        finally:
            if not completed:
                anchored._remove_owned_file(snapshot.run_anchor, FIGURE_TEXT_INPUT_NAME, published)
            if opened is not None:
                anchored._close_opened_file(opened)
                anchored._remove_owned_file(snapshot.run_anchor, temporary_name, anchored._PublishedFile(opened.identity))
            anchored._close_published_file(published)
