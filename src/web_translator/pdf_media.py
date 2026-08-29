"""Deterministic Poppler rendering and source-rendered PDF figure crops."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time

from PIL import Image
from PIL import ImageDraw, ImageFont
from pypdf import PdfReader
from pypdf.errors import PyPdfError


_POPPLER_TIMEOUT_SECONDS = 60
_GRAPHIC_JOIN_TOLERANCE = 6.0
_CONTACT_PAGES_PER_SHEET = 12
_CONTACT_COLUMNS = 4
_CONTACT_THUMBNAIL = (360, 480)
_CONTACT_LABEL_HEIGHT = 28
_CONTACT_GAP = 16
_RENDERED_PAGE_NAME = re.compile(r"page-(\d+)\.png\Z")
MAX_RENDERED_PIXELS_PER_PAGE = 36_000_000
MAX_RENDERED_PIXELS_TOTAL = 360_000_000
MAX_RENDERED_PNG_BYTES_PER_PAGE = 64 * 1024 * 1024
MAX_RENDERED_PNG_BYTES_TOTAL = 1024 * 1024 * 1024


class PdfMediaError(RuntimeError):
    """PDF graphical evidence cannot be rendered or cropped safely."""


@dataclass(frozen=True, slots=True)
class PopplerTools:
    pdfinfo: Path
    pdftoppm: Path


@dataclass(frozen=True, slots=True)
class FigureRegion:
    """One graphical region in shared pdfplumber top-origin coordinates."""

    page_number: int
    bbox: tuple[float, float, float, float]
    page_width: float
    page_height: float
    crop_bbox: tuple[float, float, float, float] | None = None


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
    source_pdf: Path,
    destination: Path,
    *,
    dpi: int = 144,
    name_width: int | None = None,
    existing_destination_identity: tuple[int, int] | None = None,
) -> list[Path]:
    """Render every source page to a deterministic PNG prefix."""
    if type(dpi) is not int or dpi <= 0:
        raise PdfMediaError("PDF render DPI must be a positive integer")
    expected_pixels = _expected_render_pixel_counts(Path(source_pdf), dpi)
    tools = find_poppler()
    destination = Path(destination)
    destination_identity = _prepare_render_destination(
        destination,
        existing_destination_identity,
    )
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
    _verify_render_destination(destination, destination_identity)
    pages = _rendered_page_files(destination)
    if not pages:
        raise PdfMediaError("render PDF pages produced no PNG output")
    _validate_rendered_png_set(pages, expected_page_count=len(expected_pixels))
    if name_width is not None:
        if type(name_width) is not int or name_width <= 0:
            raise PdfMediaError("PDF render name width must be a positive integer")
        renamed: list[Path] = []
        for page_number, page in enumerate(pages, start=1):
            destination_name = destination / f"page-{page_number:0{name_width}d}.png"
            if destination_name != page:
                try:
                    os.replace(page, destination_name)
                except OSError as error:
                    raise PdfMediaError(
                        f"cannot normalize rendered PDF page name: {error}"
                    ) from error
            renamed.append(destination_name)
        pages = renamed
    _verify_render_destination(destination, destination_identity)
    final_pages = _rendered_page_files(destination)
    if final_pages != pages:
        raise PdfMediaError(
            "rendered PDF page set changed during name normalization"
        )
    _validate_rendered_png_set(final_pages, expected_page_count=len(expected_pixels))
    return final_pages


def _expected_render_pixel_counts(source_pdf: Path, dpi: int) -> list[int]:
    try:
        reader = PdfReader(source_pdf, strict=True)
        pixels = []
        for page in reader.pages:
            user_unit = float(page.user_unit)
            width = abs(float(page.cropbox.width)) * user_unit
            height = abs(float(page.cropbox.height)) * user_unit
            if (
                not math.isfinite(user_unit)
                or user_unit <= 0
                or not math.isfinite(width)
                or not math.isfinite(height)
                or width <= 0
                or height <= 0
            ):
                raise PdfMediaError("PDF render geometry must be finite and positive")
            pixel_width = math.ceil(width * dpi / 72.0)
            pixel_height = math.ceil(height * dpi / 72.0)
            pixels.append(pixel_width * pixel_height)
    except PdfMediaError:
        raise
    except (OSError, PyPdfError, TypeError, ValueError) as error:
        raise PdfMediaError(f"cannot inspect PDF render geometry: {error}") from error
    if not pixels:
        raise PdfMediaError("PDF render geometry contains no pages")
    _validate_render_budget_counts(pixels, [0] * len(pixels))
    return pixels


def _validate_render_budget_counts(
    page_pixels: Sequence[int],
    encoded_bytes: Sequence[int],
) -> None:
    if len(page_pixels) != len(encoded_bytes):
        raise PdfMediaError("render budget evidence page counts disagree")
    if any(value > MAX_RENDERED_PIXELS_PER_PAGE for value in page_pixels):
        raise PdfMediaError(
            "PDF render exceeds 36,000,000 pixels per page"
        )
    if sum(page_pixels) > MAX_RENDERED_PIXELS_TOTAL:
        raise PdfMediaError(
            "PDF render exceeds 360,000,000 total rendered pixels"
        )
    if any(value > MAX_RENDERED_PNG_BYTES_PER_PAGE for value in encoded_bytes):
        raise PdfMediaError(
            "PDF render exceeds 64 MiB encoded bytes per page"
        )
    if sum(encoded_bytes) > MAX_RENDERED_PNG_BYTES_TOTAL:
        raise PdfMediaError("PDF render exceeds 1 GiB encoded bytes total")


def _validate_rendered_png_set(
    pages: Sequence[Path],
    *,
    expected_page_count: int,
) -> None:
    if len(pages) != expected_page_count:
        raise PdfMediaError(
            "rendered PNG page count does not match source PDF geometry"
        )
    pixels: list[int] = []
    encoded_bytes: list[int] = []
    for page in pages:
        try:
            encoded_bytes.append(page.stat().st_size)
            with page.open("rb") as stream:
                header = stream.read(24)
        except OSError as error:
            raise PdfMediaError(f"cannot inspect rendered PNG budget: {error}") from error
        if (
            len(header) != 24
            or header[:8] != b"\x89PNG\r\n\x1a\n"
            or header[12:16] != b"IHDR"
        ):
            raise PdfMediaError(f"rendered page is not a valid PNG: {page}")
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        if width <= 0 or height <= 0:
            raise PdfMediaError("rendered PNG dimensions must be positive")
        pixels.append(width * height)
    _validate_render_budget_counts(pixels, encoded_bytes)


def _prepare_render_destination(
    destination: Path,
    expected_identity: tuple[int, int] | None,
) -> tuple[int, int]:
    if expected_identity is None:
        try:
            destination.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise PdfMediaError(
                f"cannot create PDF render destination {destination}: {error}"
            ) from error
    elif (
        not isinstance(expected_identity, tuple)
        or len(expected_identity) != 2
        or any(type(value) is not int or value < 0 for value in expected_identity)
    ):
        raise PdfMediaError("existing PDF render destination identity is invalid")
    identity = _verify_render_destination(destination, expected_identity)
    if expected_identity is not None:
        try:
            if next(destination.iterdir(), None) is not None:
                raise PdfMediaError(
                    f"existing PDF render destination is not empty: {destination}"
                )
        except PdfMediaError:
            raise
        except OSError as error:
            raise PdfMediaError(
                f"cannot inspect existing PDF render destination {destination}: {error}"
            ) from error
        _verify_render_destination(destination, identity)
    return identity


def _verify_render_destination(
    destination: Path,
    expected_identity: tuple[int, int] | None,
) -> tuple[int, int]:
    try:
        metadata = destination.lstat()
    except OSError as error:
        raise PdfMediaError(
            f"cannot inspect safe existing PDF render destination {destination}: {error}"
        ) from error
    identity = (metadata.st_dev, metadata.st_ino)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or _is_reparse_metadata(metadata)
        or (expected_identity is not None and identity != expected_identity)
    ):
        raise PdfMediaError(
            f"safe existing PDF render destination changed identity: {destination}"
        )
    return identity


def _rendered_page_files(destination: Path) -> list[Path]:
    try:
        entries = list(destination.iterdir())
        pages: list[tuple[int, Path]] = []
        page_numbers: set[int] = set()
        for path in entries:
            match = _RENDERED_PAGE_NAME.fullmatch(path.name)
            metadata = path.lstat()
            if (
                match is None
                or not stat.S_ISREG(metadata.st_mode)
                or _is_reparse_metadata(metadata)
            ):
                raise PdfMediaError(
                    "render PDF pages must output only page PNG files"
                )
            page_number = int(match.group(1))
            if page_number <= 0 or page_number in page_numbers:
                raise PdfMediaError("render PDF pages produced duplicate page numbers")
            page_numbers.add(page_number)
            pages.append((page_number, path))
        return [path for _number, path in sorted(pages)]
    except PdfMediaError:
        raise
    except OSError as error:
        raise PdfMediaError(f"cannot inspect rendered PDF pages: {error}") from error


def _is_reparse_metadata(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def build_contact_sheets(
    rendered_pages: Sequence[Path],
    destination: Path,
) -> dict[str, list[int]]:
    """Build deterministic numbered contact sheets covering at most 12 pages."""
    pages = [Path(path) for path in rendered_pages]
    if not pages:
        raise PdfMediaError("contact sheets require at least one rendered page")
    expected_names = [f"page-{number:03d}.png" for number in range(1, len(pages) + 1)]
    if [path.name for path in pages] != expected_names:
        raise PdfMediaError("rendered pages must be exact sequential page-NNN.png files")
    _validate_rendered_png_set(pages, expected_page_count=len(pages))
    destination = Path(destination)
    if destination.exists():
        try:
            if destination.resolve(strict=True) != pages[0].parent.resolve(strict=True):
                raise PdfMediaError(
                    f"contact-sheet destination already exists: {destination}"
                )
        except OSError as error:
            raise PdfMediaError(f"cannot inspect contact-sheet destination: {error}") from error
    else:
        try:
            destination.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise PdfMediaError(
                f"cannot create contact-sheet destination {destination}: {error}"
            ) from error

    mapping: dict[str, list[int]] = {}
    rows = 3
    cell_width = _CONTACT_THUMBNAIL[0] + _CONTACT_GAP * 2
    cell_height = _CONTACT_THUMBNAIL[1] + _CONTACT_LABEL_HEIGHT + _CONTACT_GAP * 2
    font = ImageFont.load_default()
    for sheet_index, offset in enumerate(
        range(0, len(pages), _CONTACT_PAGES_PER_SHEET), start=1
    ):
        selected = pages[offset : offset + _CONTACT_PAGES_PER_SHEET]
        sheet = Image.new(
            "RGB",
            (_CONTACT_COLUMNS * cell_width, rows * cell_height),
            "white",
        )
        draw = ImageDraw.Draw(sheet)
        covered: list[int] = []
        try:
            for cell_index, page_path in enumerate(selected):
                page_number = offset + cell_index + 1
                covered.append(page_number)
                with Image.open(page_path) as page:
                    page.load()
                    thumbnail = page.convert("RGB")
                    thumbnail.thumbnail(_CONTACT_THUMBNAIL)
                column = cell_index % _CONTACT_COLUMNS
                row = cell_index // _CONTACT_COLUMNS
                cell_x = column * cell_width
                cell_y = row * cell_height
                x = cell_x + (cell_width - thumbnail.width) // 2
                y = cell_y + _CONTACT_LABEL_HEIGHT + _CONTACT_GAP
                draw.text(
                    (cell_x + _CONTACT_GAP, cell_y + _CONTACT_GAP // 2),
                    f"Page {page_number}",
                    fill="black",
                    font=font,
                )
                draw.rectangle(
                    (x - 1, y - 1, x + thumbnail.width, y + thumbnail.height),
                    outline="#666666",
                )
                sheet.paste(thumbnail, (x, y))
            name = f"contact-sheet-{sheet_index:03d}.png"
            sheet.save(destination / name, format="PNG", optimize=False)
            mapping[name] = covered
        finally:
            sheet.close()
    return mapping


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

    remove_empty_parent = not destination.parent.exists()
    render_root: Path | None = None
    crop_root: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        render_root = Path(
            tempfile.mkdtemp(prefix=".pdf-render-", dir=str(destination.parent))
        )
        crop_root = Path(
            tempfile.mkdtemp(prefix=".pdf-crops-", dir=str(destination.parent))
        )
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
        if render_root is not None:
            shutil.rmtree(render_root, ignore_errors=True)
        if crop_root is not None and crop_root.exists():
            shutil.rmtree(crop_root, ignore_errors=True)
        if remove_empty_parent:
            try:
                destination.parent.rmdir()
            except OSError:
                pass


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
        if (bbox := _object_bbox(item))
        is not None
        and not _excluded(bbox, excluded_bboxes)
    ]
    vector = [
        bbox
        for collection in ("lines", "rects", "curves")
        for item in getattr(page, collection, ())
        if (bbox := _object_bbox(item))
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
    semantic = sorted(
        {
            tuple(round(value, 6) for value in clipped)
            for bbox in merged
            if (
                clipped := _clip_bbox(
                    bbox,
                    (
                        page_x0,
                        page_top,
                        page_x0 + page_width,
                        page_top + page_height,
                    ),
                )
            )
            is not None
        },
        key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]),
    )
    return [
        FigureRegion(
            page_number,
            bbox,
            page_width,
            page_height,
            tuple(
                round(value, 6)
                for value in (
                    bbox[0] - page_x0,
                    bbox[1] - page_top,
                    bbox[2] - page_x0,
                    bbox[3] - page_top,
                )
            ),
        )
        for bbox in semantic
    ]


def _run_poppler(command: list[str], action: str) -> None:
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + _POPPLER_TIMEOUT_SECONDS
        output_directory = Path(command[-1]).parent
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.communicate()
                raise PdfMediaError(
                    f"cannot {action}: exceeded {_POPPLER_TIMEOUT_SECONDS} second timeout"
                )
            try:
                _stdout, stderr = process.communicate(timeout=min(0.05, remaining))
                break
            except subprocess.TimeoutExpired:
                sizes = _current_render_output_sizes(output_directory)
                if (
                    any(size > MAX_RENDERED_PNG_BYTES_PER_PAGE for size in sizes)
                    or sum(sizes) > MAX_RENDERED_PNG_BYTES_TOTAL
                ):
                    process.kill()
                    process.communicate()
                    raise PdfMediaError(
                        f"cannot {action}: encoded PNG output exceeded render budget"
                    )
    except PdfMediaError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        raise PdfMediaError(f"cannot {action}: {error}") from error
    if process.returncode != 0:
        detail = stderr.strip() or "no error detail"
        raise PdfMediaError(
            f"cannot {action}: Poppler exited {process.returncode}: {detail}"
        )


def _current_render_output_sizes(directory: Path) -> list[int]:
    try:
        return [
            entry.stat().st_size
            for entry in directory.iterdir()
            if entry.is_file() and not entry.is_symlink()
        ]
    except OSError as error:
        raise PdfMediaError(f"cannot monitor Poppler output growth: {error}") from error


def _crop_rendered_region(
    rendered_page: Path,
    region: FigureRegion,
    destination: Path,
    index: int,
) -> None:
    _validate_rendered_png_set([rendered_page], expected_page_count=1)
    crop_bbox = region.crop_bbox or region.bbox
    x0, top, x1, bottom = crop_bbox
    if (
        region.page_number <= 0
        or not _valid_bbox(crop_bbox)
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
        min(x0, x1),
        min(top, bottom),
        max(x0, x1),
        max(top, bottom),
    )
    if not all(math.isfinite(value) for value in bbox):
        return None
    return bbox


def _clip_bbox(
    bbox: tuple[float, float, float, float],
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    clipped = (
        max(bbox[0], bounds[0]),
        max(bbox[1], bounds[1]),
        min(bbox[2], bounds[2]),
        min(bbox[3], bounds[3]),
    )
    return clipped if _valid_bbox(clipped) else None


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
