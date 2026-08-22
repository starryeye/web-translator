#!/usr/bin/env python3
"""Reproducibly vendor the two static Korean fonts used by PDF assembly."""

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

UNICODE_RANGES = (
    ("ASCII", 0x0000, 0x007F),
    ("Latin-1", 0x0080, 0x00FF),
    ("Hangul Jamo", 0x1100, 0x11FF),
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


def _build_static_font(source_path: Path, destination: Path, weight: int) -> None:
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
            f"cannot instantiate and subset weight {weight}: {error}"
        ) from error


def vendor_fonts(
    output_dir: Path,
    *,
    source_file: Path | None = None,
    license_file: Path | None = None,
) -> dict[str, object]:
    """Create a new font asset directory from the exact pinned inputs."""
    output_dir = Path(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise FontVendoringError(f"output directory already exists: {output_dir}")
    if (source_file is None) != (license_file is None):
        raise FontVendoringError(
            "--source-file and --license-file must be supplied together"
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
    try:
        license_text = license_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FontVendoringError("font license must be UTF-8") from error
    if "SIL OPEN FONT LICENSE Version 1.1" not in license_text:
        raise FontVendoringError("font license is not the pinned SIL OFL 1.1 text")

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
        outputs: dict[str, dict[str, object]] = {}
        for name, weight in OUTPUTS:
            destination = temporary / name
            _build_static_font(source_path, destination, weight)
            outputs[name] = {
                "axes": {"wght": weight},
                "sha256": _sha256(destination.read_bytes()),
            }
        source_path.unlink()
        (temporary / "OFL.txt").write_text(license_text, encoding="utf-8", newline="\n")
        provenance: dict[str, object] = {
            "license": {
                "planned_url": PLANNED_FONT_LICENSE_URL,
                "planned_url_status": PLANNED_FONT_LICENSE_URL_STATUS,
                "url": FONT_LICENSE_URL,
            },
            "outputs": outputs,
            "schema_version": "1.0",
            "source_sha256": FONT_SOURCE_SHA256,
            "source_url": FONT_SOURCE_URL,
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
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        provenance = vendor_fonts(
            arguments.output_dir,
            source_file=arguments.source_file,
            license_file=arguments.license_file,
        )
    except FontVendoringError as error:
        print(f"vendor_pdf_fonts.py: {error}", file=sys.stderr)
        return 1
    print(json.dumps(provenance["outputs"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
