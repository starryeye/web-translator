#!/usr/bin/env python3
"""Reproducibly vendor static Korean fonts with extended-Latin coverage."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.request import Request, urlopen

from fontTools import subset
from fontTools.misc.roundTools import otRound
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont


FONT_SOURCE_URL = (
    "https://raw.githubusercontent.com/notofonts/noto-cjk/"
    "f8d157532fbfaeda587e826d4cd5b21a49186f7c/"
    "Sans/Variable/TTF/NotoSansCJKkr-VF.ttf"
)
FONT_SOURCE_SHA256 = "7715af52f5fe77153ce5678546258993982d2da61abea8d25fb89eb5aaec5ca6"

# The implementation plan originally named the repository-root URL below. That
# path returns HTTP 404 at the pinned commit; the font's license is under Sans/.
PLANNED_FONT_LICENSE_URL = (
    "https://raw.githubusercontent.com/notofonts/noto-cjk/"
    "f8d157532fbfaeda587e826d4cd5b21a49186f7c/LICENSE"
)
PLANNED_FONT_LICENSE_URL_STATUS = 404
FONT_LICENSE_URL = (
    "https://raw.githubusercontent.com/notofonts/noto-cjk/"
    "f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/LICENSE"
)
FONT_LICENSE_SHA256 = "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"
SUPPLEMENTAL_SOURCE_URL = (
    "https://raw.githubusercontent.com/google/fonts/"
    "2984c575fdce412ee02b2baaba67672b9a9434d8/ofl/notosans/"
    "NotoSans[wdth,wght].ttf"
)
SUPPLEMENTAL_SOURCE_SHA256 = "bfb7bb691513f12e734dc346c03a03f784912432d7e3fa8e56efcf906fe86b3d"
SUPPLEMENTAL_LICENSE_URL = (
    "https://raw.githubusercontent.com/google/fonts/"
    "2984c575fdce412ee02b2baaba67672b9a9434d8/ofl/notosans/OFL.txt"
)
SUPPLEMENTAL_LICENSE_SHA256 = "cee9892f9f0cc8fe882c9e9537ee6a89621d86ee7ceaf70b02e2b2b1c25c061a"
SUPPLEMENTAL_LICENSE_FILENAME = "NotoSans-OFL.txt"

UNICODE_RANGES = (
    ("ASCII", 0x0000, 0x007F),
    ("Latin-1", 0x0080, 0x00FF),
    ("Latin Extended-A and B", 0x0100, 0x024F),
    ("Combining Diacritical Marks", 0x0300, 0x036F),
    ("Hangul Jamo", 0x1100, 0x11FF),
    ("Latin Extended Additional", 0x1E00, 0x1EFF),
    ("General Punctuation", 0x2000, 0x206F),
    ("Currency Symbols", 0x20A0, 0x20CF),
    ("Arrows", 0x2190, 0x21FF),
    ("CJK Symbols and Punctuation", 0x3000, 0x303F),
    ("Hangul Compatibility Jamo", 0x3130, 0x318F),
    ("Hangul Syllables", 0xAC00, 0xD7A3),
)
OUTPUTS = (
    ("NotoSansKR-Regular.ttf", 400),
    ("NotoSansKR-Bold.ttf", 700),
)


class FontVendoringError(RuntimeError):
    """Pinned font inputs cannot produce the required package assets."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _download(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "web-translator-font-vendor/1.0"})
    try:
        with urlopen(request, timeout=60) as response:
            return response.read()
    except OSError as error:
        raise FontVendoringError(f"cannot download pinned font resource {url}: {error}") from error


def _unicode_values() -> set[int]:
    return {
        codepoint
        for _name, start, end in UNICODE_RANGES
        for codepoint in range(start, end + 1)
    }


def _augment_missing_glyphs(target: TTFont, supplemental: TTFont) -> None:
    """Copy decomposed outlines only for required Unicode absent from CJK."""
    target_cmap = target.getBestCmap() or {}
    supplemental_cmap = supplemental.getBestCmap() or {}
    glyph_set = supplemental.getGlyphSet()
    scale = target["head"].unitsPerEm / supplemental["head"].unitsPerEm
    # glyf assignment appends to its shared order; retain an independent order
    # so each missing glyph is added exactly once on every fontTools version.
    glyph_order = list(target.getGlyphOrder())
    for codepoint in sorted(_unicode_values() - target_cmap.keys()):
        source_name = supplemental_cmap.get(codepoint)
        if source_name is None:
            continue
        name = f"wtSupplementU{codepoint:04X}"
        if name in target["glyf"]:
            raise FontVendoringError(f"supplemental glyph name collision: {name}")
        recording = DecomposingRecordingPen(glyph_set)
        glyph_set[source_name].draw(recording)
        pen = TTGlyphPen(None)
        recording.replay(TransformPen(pen, (scale, 0, 0, scale, 0, 0)))
        target["glyf"][name] = pen.glyph()
        target["hmtx"].metrics[name] = tuple(
            otRound(value * scale) for value in supplemental["hmtx"].metrics[source_name]
        )
        if "vmtx" in target:
            target["vmtx"].metrics[name] = target["vmtx"].metrics[".notdef"]
        glyph_order.append(name)
        for table in target["cmap"].tables:
            if table.isUnicode() and hasattr(table, "cmap"):
                table.cmap[codepoint] = name
    target.setGlyphOrder(glyph_order)


def _build_static_font(
    source_path: Path, supplemental_path: Path, destination: Path, weight: int
) -> None:
    try:
        font = TTFont(source_path, recalcTimestamp=False)
        if "fvar" not in font:
            raise FontVendoringError("pinned font source is not a variable font")
        instantiated = instantiateVariableFont(
            font,
            {"wght": weight},
            inplace=False,
            optimize=True,
            updateFontNames=True,
        )
        supplemental = TTFont(supplemental_path, recalcTimestamp=False)
        if "fvar" not in supplemental:
            raise FontVendoringError("pinned supplemental source is not a variable font")
        supplemental_static = instantiateVariableFont(
            supplemental, {"wght": weight, "wdth": 100}, inplace=False,
            optimize=True, updateFontNames=True,
        )
        _augment_missing_glyphs(instantiated, supplemental_static)
        options = subset.Options()
        options.canonical_order = True
        options.layout_features = ["*"]
        options.name_IDs = ["*"]
        options.name_languages = ["*"]
        options.name_legacy = True
        options.notdef_glyph = True
        options.notdef_outline = True
        options.recalc_average_width = True
        options.recalc_max_context = True
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(unicodes=_unicode_values())
        subsetter.subset(instantiated)
        instantiated.recalcTimestamp = False
        instantiated.save(destination, reorderTables=True)
    except FontVendoringError:
        raise
    except Exception as error:
        raise FontVendoringError(
            f"cannot instantiate and subset weight {weight}: {type(error).__name__}: {error}"
        ) from error


def vendor_fonts(
    output_dir: Path,
    *,
    source_file: Path | None = None,
    license_file: Path | None = None,
    supplemental_source_file: Path | None = None,
    supplemental_license_file: Path | None = None,
) -> dict[str, object]:
    """Create a new font asset directory from the exact pinned inputs."""
    output_dir = Path(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise FontVendoringError(f"output directory already exists: {output_dir}")
    if (source_file is None) != (license_file is None):
        raise FontVendoringError(
            "--source-file and --license-file must be supplied together"
        )
    if (supplemental_source_file is None) != (supplemental_license_file is None):
        raise FontVendoringError(
            "--supplemental-source-file and --supplemental-license-file must be supplied together"
        )
    if source_file is None:
        source_bytes = _download(FONT_SOURCE_URL)
        license_bytes = _download(FONT_LICENSE_URL)
    else:
        try:
            source_bytes = Path(source_file).read_bytes()
            license_bytes = Path(license_file).read_bytes()  # type: ignore[arg-type]
        except OSError as error:
            raise FontVendoringError(f"cannot read local vendoring input: {error}") from error

    actual_source_hash = _sha256(source_bytes)
    if actual_source_hash != FONT_SOURCE_SHA256:
        raise FontVendoringError(
            "source SHA-256 mismatch: "
            f"expected {FONT_SOURCE_SHA256}, found {actual_source_hash}"
        )
    actual_license_hash = _sha256(license_bytes)
    if actual_license_hash != FONT_LICENSE_SHA256:
        raise FontVendoringError(
            "license SHA-256 mismatch: "
            f"expected {FONT_LICENSE_SHA256}, found {actual_license_hash}"
        )
    try:
        license_text = license_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FontVendoringError("font license must be UTF-8") from error
    if "SIL OPEN FONT LICENSE Version 1.1" not in license_text:
        raise FontVendoringError("font license is not the pinned SIL OFL 1.1 text")

    if supplemental_source_file is None:
        supplemental_bytes = _download(SUPPLEMENTAL_SOURCE_URL)
        supplemental_license_bytes = _download(SUPPLEMENTAL_LICENSE_URL)
    else:
        try:
            supplemental_bytes = Path(supplemental_source_file).read_bytes()
            supplemental_license_bytes = Path(supplemental_license_file).read_bytes()  # type: ignore[arg-type]
        except OSError as error:
            raise FontVendoringError(f"cannot read local supplemental input: {error}") from error
    for label, data, expected in (
        ("source", supplemental_bytes, SUPPLEMENTAL_SOURCE_SHA256),
        ("license", supplemental_license_bytes, SUPPLEMENTAL_LICENSE_SHA256),
    ):
        actual = _sha256(data)
        if actual != expected:
            raise FontVendoringError(
                f"supplemental {label} SHA-256 mismatch: expected {expected}, found {actual}"
            )
    try:
        supplemental_license_text = supplemental_license_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FontVendoringError("supplemental font license must be UTF-8") from error
    if "SIL OPEN FONT LICENSE Version 1.1" not in supplemental_license_text:
        raise FontVendoringError("supplemental font license is not the pinned SIL OFL 1.1 text")

    try:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".pdf-font-assets-", dir=output_dir.parent)
        )
    except OSError as error:
        raise FontVendoringError(f"cannot prepare font asset staging: {error}") from error

    published = False
    try:
        source_path = temporary / "source.ttf"
        source_path.write_bytes(source_bytes)
        supplemental_path = temporary / "supplemental.ttf"
        supplemental_path.write_bytes(supplemental_bytes)
        outputs: dict[str, dict[str, object]] = {}
        for name, weight in OUTPUTS:
            destination = temporary / name
            _build_static_font(source_path, supplemental_path, destination, weight)
            outputs[name] = {
                "axes": {"wght": weight},
                "sha256": _sha256(destination.read_bytes()),
            }
        source_path.unlink()
        supplemental_path.unlink()
        (temporary / "OFL.txt").write_bytes(license_bytes)
        (temporary / SUPPLEMENTAL_LICENSE_FILENAME).write_bytes(supplemental_license_bytes)
        provenance: dict[str, object] = {
            "license": {
                "planned_url": PLANNED_FONT_LICENSE_URL,
                "planned_url_status": PLANNED_FONT_LICENSE_URL_STATUS,
                "sha256": FONT_LICENSE_SHA256,
                "url": FONT_LICENSE_URL,
            },
            "outputs": outputs,
            "schema_version": "1.1",
            "source_sha256": FONT_SOURCE_SHA256,
            "source_url": FONT_SOURCE_URL,
            "supplemental_source": {
                "axes": {"wdth": 100},
                "license": {
                    "filename": SUPPLEMENTAL_LICENSE_FILENAME,
                    "sha256": SUPPLEMENTAL_LICENSE_SHA256,
                    "url": SUPPLEMENTAL_LICENSE_URL,
                },
                "source_sha256": SUPPLEMENTAL_SOURCE_SHA256,
                "source_url": SUPPLEMENTAL_SOURCE_URL,
                "strategy": "missing-required-glyphs-only",
            },
            "unicode_ranges": [
                {"name": name, "start": f"U+{start:04X}", "end": f"U+{end:04X}"}
                for name, start, end in UNICODE_RANGES
            ],
        }
        (temporary / "PROVENANCE.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.rename(output_dir)
        published = True
        return provenance
    except BaseException:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parents[1] / "src/web_translator/font_assets",
    )
    parser.add_argument("--source-file", type=Path)
    parser.add_argument("--license-file", type=Path)
    parser.add_argument("--supplemental-source-file", type=Path)
    parser.add_argument("--supplemental-license-file", type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        provenance = vendor_fonts(
            arguments.output_dir,
            source_file=arguments.source_file,
            license_file=arguments.license_file,
            supplemental_source_file=arguments.supplemental_source_file,
            supplemental_license_file=arguments.supplemental_license_file,
        )
    except FontVendoringError as error:
        print(f"vendor_pdf_fonts.py: {error}", file=sys.stderr)
        return 1
    print(json.dumps(provenance["outputs"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
