"""Strict layout evidence and tracked ReportLab flowables for PDF assembly."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Literal, get_args

from reportlab.platypus import Flowable

from web_translator.pdf_models import PdfContractError, PdfLinkEvidence, PdfSemanticRole


_BLOCK_ID = re.compile(
    r"pdf:page-\d{4}:(?:block-\d{4}|table-\d{4}:row-\d{4}:cell-\d{4})\Z"
)
_KINDS = {
    "heading",
    "paragraph",
    "list-item",
    "table-cell",
    "figure",
    "caption",
    "footnote",
    "header",
    "footer",
    "page-number",
}
_PAGE_NAMES = {"A4", "LETTER", "SOURCE"}
_IS_WINDOWS = os.name == "nt"


class PdfAssemblyError(RuntimeError):
    """A PDF cannot be assembled without violating its strict contract."""


@dataclass(frozen=True, slots=True)
class PdfPageSize:
    name: Literal["A4", "LETTER", "SOURCE"]
    width: float
    height: float

    def to_dict(self) -> dict[str, object]:
        return {"height": self.height, "name": self.name, "width": self.width}

    @classmethod
    def from_dict(cls, value: object) -> PdfPageSize:
        data = _exact_mapping(value, "page_size", {"height", "name", "width"})
        name = _string(data, "name", "page_size")
        if name not in _PAGE_NAMES:
            raise PdfAssemblyError("page_size.name must be A4, LETTER or SOURCE")
        return cls(
            name=name,  # type: ignore[arg-type]
            width=_positive_number(data, "width", "page_size"),
            height=_positive_number(data, "height", "page_size"),
        )


@dataclass(frozen=True, slots=True)
class PdfFlowableLayout:
    block_id: str
    kind: Literal[
        "heading",
        "paragraph",
        "list-item",
        "table-cell",
        "figure",
        "caption",
        "footnote",
        "header",
        "footer",
        "page-number",
    ]
    source_order: int
    split_part: int
    page_number: int
    bounds: tuple[float, float, float, float]
    frame: tuple[float, float, float, float]
    font_size: float
    semantic_role: PdfSemanticRole = "body"
    footnote_owner_id: str | None = None
    footnote_ownership: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            **({"footnote_owner_id": self.footnote_owner_id, "footnote_ownership": self.footnote_ownership}
               if self.footnote_owner_id is not None else {}),
            "block_id": self.block_id,
            "bounds": list(self.bounds),
            "font_size": self.font_size,
            "frame": list(self.frame),
            "kind": self.kind,
            "page_number": self.page_number,
            "source_order": self.source_order,
            "split_part": self.split_part,
            "semantic_role": self.semantic_role,
        }

    @classmethod
    def from_dict(
        cls, value: object, index: int, *, schema_version: str = "1.1",
    ) -> PdfFlowableLayout:
        context = f"flowables[{index}]"
        ownership_fields = {"footnote_owner_id", "footnote_ownership"}
        ownership_present = ownership_fields.intersection(value) if isinstance(value, Mapping) else set()
        data = _exact_mapping(
            value,
            context,
            {
                "block_id",
                "bounds",
                "font_size",
                "frame",
                "kind",
                "page_number",
                "source_order",
                "split_part",
            } | ({"semantic_role"} if schema_version == "1.1" else set()) | ownership_present,
        )
        semantic_role = (
            _string(data, "semantic_role", context) if schema_version == "1.1" else "body"
        )
        if semantic_role not in get_args(PdfSemanticRole):
            raise PdfAssemblyError(f"{context}.semantic_role is not supported")
        block_id = _string(data, "block_id", context)
        if _BLOCK_ID.fullmatch(block_id) is None:
            raise PdfAssemblyError(f"{context}.block_id is not a stable PDF block ID")
        kind = _string(data, "kind", context)
        if kind not in _KINDS:
            raise PdfAssemblyError(f"{context}.kind is not a supported flowable kind")
        if ownership_present:
            if ownership_present != ownership_fields or kind != "footnote":
                raise PdfAssemblyError(f"{context} ownership requires a footnote and complete fields")
            if _BLOCK_ID.fullmatch(_string(data, "footnote_owner_id", context)) is None:
                raise PdfAssemblyError(f"{context}.footnote_owner_id is invalid")
            if data["footnote_ownership"] not in {"protected-footnote-marker", "source-link-marker-span", "unique-source-marker", "block-only-legacy"}:
                raise PdfAssemblyError(f"{context}.footnote_ownership is invalid")
        bounds = _box(data, "bounds", context)
        frame = _box(data, "frame", context)
        _validate_inside_frame(bounds, frame, context)
        return cls(
            block_id=block_id,
            kind=kind,  # type: ignore[arg-type]
            source_order=_nonnegative_integer(data, "source_order", context),
            split_part=_nonnegative_integer(data, "split_part", context),
            page_number=_positive_integer(data, "page_number", context),
            bounds=bounds,
            frame=frame,
            font_size=_positive_number(data, "font_size", context),
            semantic_role=semantic_role,  # type: ignore[arg-type]
            footnote_owner_id=data.get("footnote_owner_id"),
            footnote_ownership=data.get("footnote_ownership"),
        )


@dataclass(frozen=True, slots=True)
class PdfTocResolution:
    block_id: str
    source_reference: str | None
    target_block_id: str | None
    output_page: int | None
    evidence: str
    warning: str | None = None

    @classmethod
    def from_dict(cls, value: object) -> PdfTocResolution:
        data = _exact_mapping(value, "toc_entry", set(cls.__dataclass_fields__))
        for key in ("block_id", "evidence"):
            if not _string(data, key, "toc_entry"):
                raise PdfAssemblyError(f"toc_entry.{key} must be nonempty")
        reference = data["source_reference"]
        if reference is not None and (not isinstance(reference, str) or re.fullmatch(r"\d+|[ivxlcdm]+", reference, re.IGNORECASE) is None):
            raise PdfAssemblyError("toc_entry.source_reference must be null or a printed page token")
        if _BLOCK_ID.fullmatch(data["block_id"]) is None:
            raise PdfAssemblyError("toc_entry.block_id is invalid")
        target = data["target_block_id"]
        if target is not None:
            if not isinstance(target, str) or _BLOCK_ID.fullmatch(target) is None:
                raise PdfAssemblyError("toc_entry.target_block_id is invalid")
            _positive_integer(data, "output_page", "toc_entry")
            if data["warning"] is not None:
                raise PdfAssemblyError("resolved TOC entry cannot carry a warning")
        elif data["output_page"] is not None or data["warning"] != "toc-target-outside-input":
            raise PdfAssemblyError("unresolved TOC entry requires outside-input evidence")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class PdfFootnoteContinuation:
    block_id: str
    owner_block_id: str
    split_part: int
    page_number: int
    label: str = "각주 계속"

    @classmethod
    def from_dict(cls, value: object) -> PdfFootnoteContinuation:
        data = _exact_mapping(value, "footnote_continuation", set(cls.__dataclass_fields__))
        for key in ("block_id", "owner_block_id"):
            if _BLOCK_ID.fullmatch(_string(data, key, "footnote_continuation")) is None:
                raise PdfAssemblyError(f"footnote_continuation.{key} is invalid")
        for key in ("split_part", "page_number"):
            _positive_integer(data, key, "footnote_continuation")
        if data["label"] != "각주 계속":
            raise PdfAssemblyError("footnote continuation label is invalid")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class PdfAssemblyLayout:
    schema_version: str
    reserved_output_dir: str
    staged_pdf_sha256: str
    page_size: PdfPageSize
    minimum_font_size: float
    flowables: tuple[PdfFlowableLayout, ...]
    links: tuple[PdfLinkEvidence, ...]
    toc_entries: tuple[PdfTocResolution, ...] = ()
    footnote_continuations: tuple[PdfFootnoteContinuation, ...] = ()
    anchor_pages: tuple[tuple[str, int], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            **({
                "toc_entries": [asdict(item) for item in self.toc_entries],
                "footnote_continuations": [asdict(item) for item in self.footnote_continuations],
                "anchor_pages": dict(self.anchor_pages),
            } if self.schema_version == "1.1" else {}),
            "flowables": [
                {
                    key: value for key, value in item.to_dict().items()
                    if self.schema_version != "1.0" or key != "semantic_role"
                }
                for item in self.flowables
            ],
            "links": [item.to_dict() for item in self.links],
            "minimum_font_size": self.minimum_font_size,
            "page_size": self.page_size.to_dict(),
            "reserved_output_dir": self.reserved_output_dir,
            "schema_version": self.schema_version,
            "staged_pdf_sha256": self.staged_pdf_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> PdfAssemblyLayout:
        optional = {"toc_entries", "footnote_continuations", "anchor_pages"}
        present = optional.intersection(value) if isinstance(value, Mapping) else set()
        data = _exact_mapping(
            value,
            "layout",
            {
                "flowables",
                "links",
                "minimum_font_size",
                "page_size",
                "reserved_output_dir",
                "schema_version",
                "staged_pdf_sha256",
            } | present,
            root_message="layout fields must be exactly",
        )
        schema_version = _string(data, "schema_version", "layout")
        if schema_version not in {"1.0", "1.1"}:
            raise PdfAssemblyError("layout.schema_version must be '1.0' or '1.1'")
        digest = _string(data, "staged_pdf_sha256", "layout")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise PdfAssemblyError("layout.staged_pdf_sha256 must be lowercase SHA-256")
        raw_flowables = data.get("flowables")
        if not isinstance(raw_flowables, list):
            raise PdfAssemblyError("layout.flowables must be an array")
        flowables = tuple(
            PdfFlowableLayout.from_dict(item, index, schema_version=schema_version)
            for index, item in enumerate(raw_flowables)
        )
        raw_links = data.get("links")
        if not isinstance(raw_links, list):
            raise PdfAssemblyError("layout.links must be an array")
        try:
            links = tuple(
                PdfLinkEvidence.from_dict(
                    item
                )
                for index, item in enumerate(raw_links)
                if isinstance(item, Mapping)
            )
            if len(links) != len(raw_links):
                raise PdfAssemblyError("layout link evidence entries must be objects")
        except (PdfContractError, TypeError, ValueError) as error:
            raise PdfAssemblyError(f"layout link evidence is invalid: {error}") from error
        pairs = [(item.block_id, item.split_part) for item in flowables]
        if len(pairs) != len(set(pairs)):
            raise PdfAssemblyError(
                "layout flowable block and split-part pairs must be unique"
            )
        if list(flowables) != sorted(
            flowables,
            key=lambda item: (item.source_order, item.split_part),
        ):
            raise PdfAssemblyError("layout.flowables must be in source and split-part order")
        for index, left in enumerate(flowables):
            for right in flowables[index + 1 :]:
                if left.page_number != right.page_number or left.frame != right.frame:
                    continue
                if _intersection_area(left.bounds, right.bounds) <= 1e-6:
                    continue
                raise PdfAssemblyError(
                    "layout contains overlapping peer flowables: "
                    f"{left.block_id} and {right.block_id}"
                )
        minimum_font_size = _positive_number(
            data, "minimum_font_size", "layout"
        )
        if minimum_font_size < 9.0:
            raise PdfAssemblyError("layout minimum_font_size must be at least 9")
        if any(item.font_size < minimum_font_size for item in flowables):
            raise PdfAssemblyError(
                "layout flowable font size is below minimum_font_size"
            )
        reserved_output_dir = _string(data, "reserved_output_dir", "layout")
        if not reserved_output_dir:
            raise PdfAssemblyError("layout reserved_output_dir must be nonempty")
        for key in ("toc_entries", "footnote_continuations"):
            if not isinstance(data.get(key, []), list):
                raise PdfAssemblyError(f"layout.{key} must be an array")
        toc_entries = tuple(PdfTocResolution.from_dict(item) for item in data.get("toc_entries", []))
        continuations = tuple(PdfFootnoteContinuation.from_dict(item) for item in data.get("footnote_continuations", []))
        anchors = data.get("anchor_pages", {})
        if not isinstance(anchors, Mapping) or any(
            not isinstance(key, str) or type(page) is not int or page <= 0
            for key, page in anchors.items()
        ):
            raise PdfAssemblyError("layout.anchor_pages must map names to positive pages")
        return cls(
            schema_version=schema_version,
            reserved_output_dir=reserved_output_dir,
            staged_pdf_sha256=digest,
            page_size=PdfPageSize.from_dict(data.get("page_size")),
            minimum_font_size=minimum_font_size,
            flowables=flowables,
            links=links,
            toc_entries=toc_entries,
            footnote_continuations=continuations,
            anchor_pages=tuple(sorted(anchors.items())),
        )


class TrackedFlowable(Flowable):
    """Wrap one basic flowable and record its emitted page/frame bounds."""

    def __init__(
        self,
        content: Flowable,
        *,
        block_id: str,
        kind: str,
        source_order: int,
        split_part: int,
        font_size: float,
        frame: tuple[float, float, float, float],
        records: list[PdfFlowableLayout],
        part_counters: dict[str, int] | None = None,
        anchor_name: str | None = None,
        on_draw: Callable[[Any, int, Flowable], None] | None = None,
        semantic_role: PdfSemanticRole = "body",
        footnote_owner_id: str | None = None,
        footnote_ownership: str | None = None,
    ) -> None:
        super().__init__()
        self._content = content
        self._block_id = block_id
        self._kind = kind
        self._source_order = source_order
        self._split_part = split_part
        self._font_size = font_size
        self._frame_bounds = frame
        self._records = records
        self._part_counters = part_counters
        self._anchor_name = anchor_name
        self._on_draw = on_draw
        self._semantic_role = semantic_role
        self._footnote_owner_id = footnote_owner_id
        self._footnote_ownership = footnote_ownership
        self.hAlign = getattr(content, "hAlign", "LEFT")
        self.width = 0.0
        self.height = 0.0

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        width, height = self._content.wrap(available_width, available_height)
        self.width = float(width)
        self.height = float(height)
        self._deferred = self._minimum_page() > int(self.canv.getPageNumber()) if hasattr(self, "canv") else False
        if self._deferred and height <= available_height:
            self.height = available_height + 1
        return self.width, self.height

    def _minimum_page(self, content: Flowable | None = None) -> int:
        callback = getattr(self._on_draw, "minimum_page_for", None)
        return callback(content or self._content) if callback is not None else 0

    def split(self, available_width: float, available_height: float) -> list[Flowable]:
        parts = self._content.split(available_width, available_height)
        if getattr(self, "_deferred", False):
            # Leave the marker-bearing line for the next page while allowing preceding
            # owner lines to fill this one. Reuse ReportLab's line-boundary split.
            leading = float(getattr(getattr(self._content, "style", None), "leading", 0))
            if leading <= 0:
                return []
            limit = min(available_height, self._content.height)
            while parts and self._minimum_page(parts[0]) > int(self.canv.getPageNumber()):
                limit -= leading
                if limit <= 0:
                    return []
                parts = self._content.split(available_width, limit)
        return [
            TrackedFlowable(
                part,
                block_id=self._block_id,
                kind=self._kind,
                source_order=self._source_order,
                split_part=self._split_part + index,
                font_size=self._font_size,
                frame=self._frame_bounds,
                records=self._records,
                part_counters=self._part_counters,
                anchor_name=self._anchor_name,
                on_draw=self._on_draw,
                semantic_role=self._semantic_role,
                footnote_owner_id=self._footnote_owner_id,
                footnote_ownership=self._footnote_ownership,
            )
            for index, part in enumerate(parts)
        ]

    def getSpaceBefore(self) -> float:
        return float(self._content.getSpaceBefore())

    def getSpaceAfter(self) -> float:
        return float(self._content.getSpaceAfter())

    def getKeepWithNext(self) -> bool:
        return bool(self._content.getKeepWithNext())

    def drawOn(self, canvas: Any, x: float, y: float, _sW: float = 0) -> None:
        x = self._hAlignAdjust(x, _sW)
        corners = [
            canvas.absolutePosition(point_x, point_y)
            for point_x, point_y in (
                (x, y),
                (x + self.width, y),
                (x, y + self.height),
                (x + self.width, y + self.height),
            )
        ]
        x_values = [float(point[0]) for point in corners]
        y_values = [float(point[1]) for point in corners]
        bounds = (
            min(x_values),
            min(y_values),
            max(x_values) - min(x_values),
            max(y_values) - min(y_values),
        )
        document = getattr(canvas, "_doctemplate", None)
        frame = (document.body_frame(self._frame_bounds)
            if self._footnote_owner_id is None and hasattr(document, "body_frame") else self._frame_bounds)
        _validate_inside_frame(bounds, frame, self._block_id)
        page_number = int(canvas.getPageNumber())
        if self._anchor_name is not None and not any(
            record.block_id == self._block_id for record in self._records
        ):
            canvas.bookmarkHorizontalAbsolute(
                self._anchor_name,
                bounds[1] + bounds[3],
                left=bounds[0],
            )
            document = getattr(canvas, "_doctemplate", None)
            if hasattr(document, "anchor_pages"):
                document.anchor_pages.setdefault(self._anchor_name, page_number)
        if self._on_draw is not None:
            self._on_draw(canvas, page_number, self._content)
        self._content.drawOn(canvas, x, y)
        if self._part_counters is None:
            split_part = self._split_part
        else:
            split_part = self._part_counters.get(self._block_id, 0)
            self._part_counters[self._block_id] = split_part + 1
        self._records.append(
            PdfFlowableLayout(
                block_id=self._block_id,
                kind=self._kind,  # type: ignore[arg-type]
                source_order=self._source_order,
                split_part=split_part,
                page_number=page_number,
                bounds=bounds,
                frame=frame,
                font_size=self._font_size,
                semantic_role=self._semantic_role,
                footnote_owner_id=self._footnote_owner_id,
                footnote_ownership=self._footnote_ownership,
            )
        )

    def draw(self) -> None:
        # Platypus calls drawOn above. This method exists only for Flowable's API.
        return None


def read_pdf_layout(path: Path) -> PdfAssemblyLayout:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PdfAssemblyError(f"cannot read PDF layout {path}: {error}") from error
    return PdfAssemblyLayout.from_dict(value)


def write_pdf_layout(path: Path, layout: PdfAssemblyLayout) -> None:
    """Validate and atomically write one new strict layout record."""
    path = Path(path)
    validated = PdfAssemblyLayout.from_dict(layout.to_dict())
    if path.exists() or path.is_symlink():
        raise PdfAssemblyError(f"layout destination already exists: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    validated.to_dict(), stream, ensure_ascii=False, indent=2, sort_keys=True
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError as error:
                raise PdfAssemblyError(
                    f"layout destination already exists: {path}"
                ) from error
            except (AttributeError, NotImplementedError, OSError) as link_error:
                if not _IS_WINDOWS:
                    raise PdfAssemblyError(
                        f"safe layout publication unavailable: {path}: {link_error}"
                    ) from link_error
                try:
                    # Windows rename is atomic and refuses an existing target.
                    os.rename(temporary, path)
                except FileExistsError as error:
                    raise PdfAssemblyError(
                        f"layout destination already exists: {path}"
                    ) from error
                except OSError as error:
                    if path.exists() or path.is_symlink():
                        raise PdfAssemblyError(
                            f"layout destination already exists: {path}"
                        ) from error
                    raise PdfAssemblyError(
                        f"cannot publish PDF layout {path}: {error}"
                    ) from error
        finally:
            temporary.unlink(missing_ok=True)
    except PdfAssemblyError:
        raise
    except OSError as error:
        raise PdfAssemblyError(f"cannot write PDF layout {path}: {error}") from error


def _exact_mapping(
    value: object,
    context: str,
    fields: set[str],
    *,
    root_message: str | None = None,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PdfAssemblyError(f"{context} must be an object")
    if set(value) != fields:
        expected = ", ".join(sorted(fields))
        prefix = root_message or f"{context} fields must be exactly"
        raise PdfAssemblyError(f"{prefix}: {expected}")
    return value


def _string(data: Mapping[str, Any], field: str, context: str) -> str:
    value = data.get(field)
    if not isinstance(value, str):
        raise PdfAssemblyError(f"{context}.{field} must be a string")
    return value


def _number(data: Mapping[str, Any], field: str, context: str) -> float:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PdfAssemblyError(f"{context}.{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise PdfAssemblyError(f"{context}.{field} must be a finite number")
    return result


def _positive_number(data: Mapping[str, Any], field: str, context: str) -> float:
    value = _number(data, field, context)
    if value <= 0:
        raise PdfAssemblyError(f"{context}.{field} must be positive")
    return value


def _integer(data: Mapping[str, Any], field: str, context: str) -> int:
    value = data.get(field)
    if type(value) is not int:
        raise PdfAssemblyError(f"{context}.{field} must be an integer")
    return value


def _positive_integer(data: Mapping[str, Any], field: str, context: str) -> int:
    value = _integer(data, field, context)
    if value <= 0:
        raise PdfAssemblyError(f"{context}.{field} must be positive")
    return value


def _nonnegative_integer(data: Mapping[str, Any], field: str, context: str) -> int:
    value = _integer(data, field, context)
    if value < 0:
        raise PdfAssemblyError(f"{context}.{field} must be nonnegative")
    return value


def _box(
    data: Mapping[str, Any], field: str, context: str
) -> tuple[float, float, float, float]:
    value = data.get(field)
    if not isinstance(value, list) or len(value) != 4:
        raise PdfAssemblyError(f"{context}.{field} must contain four numbers")
    parsed: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise PdfAssemblyError(f"{context}.{field} must contain four finite numbers")
        number = float(item)
        if not math.isfinite(number):
            raise PdfAssemblyError(f"{context}.{field} must contain four finite numbers")
        parsed.append(number)
    if parsed[2] <= 0 or parsed[3] <= 0:
        raise PdfAssemblyError(f"{context}.{field} width and height must be positive")
    return parsed[0], parsed[1], parsed[2], parsed[3]


def _validate_inside_frame(
    bounds: tuple[float, float, float, float],
    frame: tuple[float, float, float, float],
    context: str,
) -> None:
    x, y, width, height = bounds
    frame_x, frame_y, frame_width, frame_height = frame
    tolerance = 1e-6
    if (
        x < frame_x - tolerance
        or y < frame_y - tolerance
        or x + width > frame_x + frame_width + tolerance
        or y + height > frame_y + frame_height + tolerance
    ):
        raise PdfAssemblyError(f"{context} bounds fall outside the document frame")


def _intersection_area(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    left_x, left_y, left_width, left_height = left
    right_x, right_y, right_width, right_height = right
    width = min(left_x + left_width, right_x + right_width) - max(left_x, right_x)
    height = min(left_y + left_height, right_y + right_height) - max(left_y, right_y)
    return max(0.0, width) * max(0.0, height)
