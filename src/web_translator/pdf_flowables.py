"""Strict layout evidence and tracked ReportLab flowables for PDF assembly."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Literal

from reportlab.platypus import Flowable


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
_PAGE_NAMES = {"A4", "LETTER"}
_IS_WINDOWS = os.name == "nt"


class PdfAssemblyError(RuntimeError):
    """A PDF cannot be assembled without violating its strict contract."""


@dataclass(frozen=True, slots=True)
class PdfPageSize:
    name: Literal["A4", "LETTER"]
    width: float
    height: float

    def to_dict(self) -> dict[str, object]:
        return {"height": self.height, "name": self.name, "width": self.width}

    @classmethod
    def from_dict(cls, value: object) -> PdfPageSize:
        data = _exact_mapping(value, "page_size", {"height", "name", "width"})
        name = _string(data, "name", "page_size")
        if name not in _PAGE_NAMES:
            raise PdfAssemblyError("page_size.name must be A4 or LETTER")
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

    def to_dict(self) -> dict[str, object]:
        return {
            "block_id": self.block_id,
            "bounds": list(self.bounds),
            "font_size": self.font_size,
            "frame": list(self.frame),
            "kind": self.kind,
            "page_number": self.page_number,
            "source_order": self.source_order,
            "split_part": self.split_part,
        }

    @classmethod
    def from_dict(cls, value: object, index: int) -> PdfFlowableLayout:
        context = f"flowables[{index}]"
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
            },
        )
        block_id = _string(data, "block_id", context)
        if _BLOCK_ID.fullmatch(block_id) is None:
            raise PdfAssemblyError(f"{context}.block_id is not a stable PDF block ID")
        kind = _string(data, "kind", context)
        if kind not in _KINDS:
            raise PdfAssemblyError(f"{context}.kind is not a supported flowable kind")
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
        )


@dataclass(frozen=True, slots=True)
class PdfAssemblyLayout:
    schema_version: str
    reserved_output_dir: str
    staged_pdf_sha256: str
    page_size: PdfPageSize
    minimum_font_size: float
    flowables: tuple[PdfFlowableLayout, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "flowables": [item.to_dict() for item in self.flowables],
            "minimum_font_size": self.minimum_font_size,
            "page_size": self.page_size.to_dict(),
            "reserved_output_dir": self.reserved_output_dir,
            "schema_version": self.schema_version,
            "staged_pdf_sha256": self.staged_pdf_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> PdfAssemblyLayout:
        data = _exact_mapping(
            value,
            "layout",
            {
                "flowables",
                "minimum_font_size",
                "page_size",
                "reserved_output_dir",
                "schema_version",
                "staged_pdf_sha256",
            },
            root_message="layout fields must be exactly",
        )
        schema_version = _string(data, "schema_version", "layout")
        if schema_version != "1.0":
            raise PdfAssemblyError("layout.schema_version must be '1.0'")
        digest = _string(data, "staged_pdf_sha256", "layout")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise PdfAssemblyError("layout.staged_pdf_sha256 must be lowercase SHA-256")
        raw_flowables = data.get("flowables")
        if not isinstance(raw_flowables, list):
            raise PdfAssemblyError("layout.flowables must be an array")
        flowables = tuple(
            PdfFlowableLayout.from_dict(item, index)
            for index, item in enumerate(raw_flowables)
        )
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
        return cls(
            schema_version=schema_version,
            reserved_output_dir=reserved_output_dir,
            staged_pdf_sha256=digest,
            page_size=PdfPageSize.from_dict(data.get("page_size")),
            minimum_font_size=minimum_font_size,
            flowables=flowables,
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
        on_draw: Callable[[Any, int], None] | None = None,
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
        self.width = 0.0
        self.height = 0.0

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        width, height = self._content.wrap(available_width, available_height)
        self.width = float(width)
        self.height = float(height)
        return self.width, self.height

    def split(self, available_width: float, available_height: float) -> list[Flowable]:
        parts = self._content.split(available_width, available_height)
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
            )
            for index, part in enumerate(parts)
        ]

    def getSpaceBefore(self) -> float:
        return float(self._content.getSpaceBefore())

    def getSpaceAfter(self) -> float:
        return float(self._content.getSpaceAfter())

    def drawOn(self, canvas: Any, x: float, y: float, _sW: float = 0) -> None:
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
        _validate_inside_frame(bounds, self._frame_bounds, self._block_id)
        page_number = int(canvas.getPageNumber())
        if self._anchor_name is not None and not any(
            record.block_id == self._block_id for record in self._records
        ):
            canvas.bookmarkHorizontalAbsolute(
                self._anchor_name,
                bounds[1] + bounds[3],
                left=bounds[0],
            )
        if self._on_draw is not None:
            self._on_draw(canvas, page_number)
        self._content.drawOn(canvas, x, y, _sW)
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
                frame=self._frame_bounds,
                font_size=self._font_size,
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
