"""Deterministic Poppler rendering and source-rendered PDF figure crops."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image


_POPPLER_TIMEOUT_SECONDS = 60
_GRAPHIC_JOIN_TOLERANCE = 6.0


class PdfMediaError(RuntimeError):
    """PDF graphical evidence cannot be rendered or cropped safely."""


@dataclass(frozen=True, slots=True)
class PopplerTools:
    pdfinfo: Path
    pdftoppm: Path


@dataclass(frozen=True, slots=True)
class FigureRegion:
    """One page-local graphical region in pdfplumber top-origin coordinates."""

    page_number: int
    bbox: tuple[float, float, float, float]
    page_width: float
    page_height: float


def find_poppler() -> PopplerTools:
    """Locate both Poppler commands or raise one actionable error."""
    pdfinfo = shutil.which("pdfinfo")
    pdftoppm = shutil.which("pdftoppm")
    if pdfinfo is None or pdftoppm is None:
        missing = [
            name
            for name, value in (("pdfinfo", pdfinfo), ("pdftoppm", pdftoppm))
            if value is None
        ]
        raise PdfMediaError(
            "Poppler commands pdfinfo and pdftoppm are required; missing "
            f"{', '.join(missing)}; install Poppler and add both commands to PATH."
        )
    try:
        return PopplerTools(
            pdfinfo=Path(pdfinfo).resolve(strict=True),
            pdftoppm=Path(pdftoppm).resolve(strict=True),
        )
    except OSError as error:
        raise PdfMediaError(
            f"cannot resolve Poppler commands pdfinfo and pdftoppm: {error}"
        ) from error


def render_pdf_pages(
    source_pdf: Path, destination: Path, *, dpi: int = 144
) -> list[Path]:
    """Render every source page to a deterministic PNG prefix."""
    if type(dpi) is not int or dpi <= 0:
        raise PdfMediaError("PDF render DPI must be a positive integer")
    tools = find_poppler()
    destination = Path(destination)
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        raise PdfMediaError(
            f"cannot create PDF render destination {destination}: {error}"
        ) from error
    prefix = destination / "page"
    command = [
        str(tools.pdftoppm),
        "-png",
        "-r",
        str(dpi),
        str(Path(source_pdf)),
        str(prefix),
    ]
    _run_poppler(command, "render PDF pages")
    pages = sorted(destination.glob("page-*.png"))
    if not pages:
        raise PdfMediaError("render PDF pages produced no PNG output")
    return pages


def crop_figure_regions(
    source_pdf: Path,
    regions: Sequence[FigureRegion],
    destination: Path,
    *,
    dpi: int = 144,
) -> list[Path]:
    """Crop graphical regions from fixed-DPI source-page renders."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise PdfMediaError(f"PDF media destination already exists: {destination}")
    if not regions:
        try:
            destination.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise PdfMediaError(
                f"cannot create PDF media destination {destination}: {error}"
            ) from error
        return []

    destination.parent.mkdir(parents=True, exist_ok=True)
    render_root = Path(
        tempfile.mkdtemp(prefix=".pdf-render-", dir=str(destination.parent))
    )
    crop_root = Path(
        tempfile.mkdtemp(prefix=".pdf-crops-", dir=str(destination.parent))
    )
    try:
        rendered = render_pdf_pages(Path(source_pdf), render_root / "pages", dpi=dpi)
        crops: list[Path] = []
        for index, region in enumerate(regions, start=1):
            page_index = region.page_number - 1
            if page_index < 0 or page_index >= len(rendered):
                raise PdfMediaError(
                    f"cannot crop figure region {index}: page {region.page_number} "
                    "was not rendered"
                )
            crop_path = crop_root / f"figure-{index:04d}.png"
            _crop_rendered_region(rendered[page_index], region, crop_path, index)
            crops.append(crop_path)
        os.replace(crop_root, destination)
        return [destination / path.name for path in crops]
    except PdfMediaError:
        raise
    except (OSError, ValueError) as error:
        raise PdfMediaError(f"cannot crop figure regions: {error}") from error
    finally:
        shutil.rmtree(render_root, ignore_errors=True)
        if crop_root.exists():
            shutil.rmtree(crop_root, ignore_errors=True)


def detect_figure_regions(
    page: object,
    *,
    page_number: int,
    excluded_bboxes: Sequence[tuple[float, float, float, float]] = (),
) -> list[FigureRegion]:
    """Group raster objects and connected vector objects into graphical regions."""
    page_width = _positive_number(getattr(page, "width", None), "page width")
    page_height = _positive_number(getattr(page, "height", None), "page height")
    page_x0, page_top = _page_origin(page)
    raster = [
        bbox
        for item in getattr(page, "images", ())
        if (
            bbox := _object_bbox(
                item,
                page_x0=page_x0,
                page_top=page_top,
                page_width=page_width,
                page_height=page_height,
            )
        )
        is not None
        and not _excluded(bbox, excluded_bboxes)
    ]
    vector = [
        bbox
        for collection in ("lines", "rects", "curves")
        for item in getattr(page, collection, ())
        if (
            bbox := _object_bbox(
                item,
                page_x0=page_x0,
                page_top=page_top,
                page_width=page_width,
                page_height=page_height,
            )
        )
        is not None
        and not _excluded(bbox, excluded_bboxes)
    ]
    vector_groups = [
        group
        for group in _connected_groups(vector)
        if (
            len(group) >= 2 and _usable_vector_bbox(_union_bbox(group))
        )
        or any(
            _bbox_distance(vector_bbox, raster_bbox) <= _GRAPHIC_JOIN_TOLERANCE
            for vector_bbox in group
            for raster_bbox in raster
        )
    ]
    candidates = [*raster, *(_union_bbox(group) for group in vector_groups)]
    merged = [_union_bbox(group) for group in _connected_groups(candidates)]
    normalized = sorted(
        {
            tuple(round(value, 6) for value in bbox)
            for bbox in merged
            if _valid_bbox(bbox)
        },
        key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]),
    )
    return [
        FigureRegion(page_number, bbox, page_width, page_height)
        for bbox in normalized
    ]


def _run_poppler(command: list[str], action: str) -> None:
    try:
        completed = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            timeout=_POPPLER_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PdfMediaError(f"cannot {action}: {error}") from error
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "no error detail"
        raise PdfMediaError(
            f"cannot {action}: Poppler exited {completed.returncode}: {stderr}"
        )


def _crop_rendered_region(
    rendered_page: Path,
    region: FigureRegion,
    destination: Path,
    index: int,
) -> None:
    x0, top, x1, bottom = region.bbox
    if (
        region.page_number <= 0
        or not _valid_bbox(region.bbox)
        or not math.isfinite(region.page_width)
        or not math.isfinite(region.page_height)
        or region.page_width <= 0
        or region.page_height <= 0
        or x0 < 0
        or top < 0
        or x1 > region.page_width
        or bottom > region.page_height
    ):
        raise PdfMediaError(f"cannot crop figure region {index}: invalid PDF bounds")
    try:
        with Image.open(rendered_page) as image:
            image.load()
            scale_x = image.width / region.page_width
            scale_y = image.height / region.page_height
            pixel_box = (
                math.floor(x0 * scale_x),
                math.floor(top * scale_y),
                math.ceil(x1 * scale_x),
                math.ceil(bottom * scale_y),
            )
            if (
                pixel_box[0] < 0
                or pixel_box[1] < 0
                or pixel_box[2] > image.width
                or pixel_box[3] > image.height
                or pixel_box[2] <= pixel_box[0]
                or pixel_box[3] <= pixel_box[1]
            ):
                raise PdfMediaError(
                    f"cannot crop figure region {index}: invalid rendered bounds"
                )
            image.crop(pixel_box).save(destination, format="PNG")
    except PdfMediaError:
        raise
    except (OSError, ValueError) as error:
        raise PdfMediaError(f"cannot crop figure region {index}: {error}") from error


def _object_bbox(
    item: object,
    *,
    page_x0: float,
    page_top: float,
    page_width: float,
    page_height: float,
) -> tuple[float, float, float, float] | None:
    if not isinstance(item, Mapping):
        return None
    try:
        x0 = float(item["x0"])
        x1 = float(item["x1"])
        top = float(item["top"])
        bottom = float(item["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    bbox = (
        max(0.0, min(page_width, min(x0, x1) - page_x0)),
        max(0.0, min(page_height, min(top, bottom) - page_top)),
        max(0.0, min(page_width, max(x0, x1) - page_x0)),
        max(0.0, min(page_height, max(top, bottom) - page_top)),
    )
    if not all(math.isfinite(value) for value in bbox):
        return None
    return bbox


def _page_origin(page: object) -> tuple[float, float]:
    bbox = getattr(page, "bbox", None)
    if (
        not isinstance(bbox, Sequence)
        or isinstance(bbox, (str, bytes))
        or len(bbox) != 4
    ):
        return (0.0, 0.0)
    try:
        x0 = float(bbox[0])
        top = float(bbox[1])
    except (TypeError, ValueError):
        return (0.0, 0.0)
    if not math.isfinite(x0) or not math.isfinite(top):
        return (0.0, 0.0)
    return (x0, top)


def _connected_groups(
    bboxes: Sequence[tuple[float, float, float, float]],
) -> list[list[tuple[float, float, float, float]]]:
    remaining = list(bboxes)
    groups: list[list[tuple[float, float, float, float]]] = []
    while remaining:
        group = [remaining.pop(0)]
        changed = True
        while changed:
            changed = False
            group_bbox = _union_bbox(group)
            for candidate in list(remaining):
                if _bbox_distance(group_bbox, candidate) <= _GRAPHIC_JOIN_TOLERANCE:
                    group.append(candidate)
                    remaining.remove(candidate)
                    changed = True
        groups.append(group)
    return groups


def _bbox_distance(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    horizontal = max(left[0] - right[2], right[0] - left[2], 0.0)
    vertical = max(left[1] - right[3], right[1] - left[3], 0.0)
    return math.hypot(horizontal, vertical)


def _union_bbox(
    bboxes: Sequence[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    return (
        min(bbox[0] for bbox in bboxes),
        min(bbox[1] for bbox in bboxes),
        max(bbox[2] for bbox in bboxes),
        max(bbox[3] for bbox in bboxes),
    )


def _excluded(
    bbox: tuple[float, float, float, float],
    excluded: Sequence[tuple[float, float, float, float]],
) -> bool:
    center = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
    return any(
        region[0] - 1e-6 <= center[0] <= region[2] + 1e-6
        and region[1] - 1e-6 <= center[1] <= region[3] + 1e-6
        for region in excluded
    )


def _valid_bbox(bbox: tuple[float, float, float, float]) -> bool:
    return (
        all(math.isfinite(value) for value in bbox)
        and bbox[2] > bbox[0]
        and bbox[3] > bbox[1]
    )


def _usable_vector_bbox(bbox: tuple[float, float, float, float]) -> bool:
    return bbox[2] - bbox[0] >= 12.0 and bbox[3] - bbox[1] >= 12.0


def _positive_number(value: object, context: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise PdfMediaError(f"{context} must be finite and positive") from error
    if not math.isfinite(number) or number <= 0:
        raise PdfMediaError(f"{context} must be finite and positive")
    return number
