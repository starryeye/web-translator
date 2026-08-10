"""Deterministic names and safe writes for captured web assets."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

import httpx


_CONTENT_TYPE_SUFFIXES = {
    "text/css": ".css",
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/x-icon": ".ico",
    "font/otf": ".otf",
    "font/ttf": ".ttf",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "application/font-woff": ".woff",
    "application/vnd.ms-fontobject": ".eot",
}
_SAFE_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,10}\Z")


def safe_suffix(path: str, content_type: str | None) -> str:
    """Choose a short, inert suffix from a URL path or response media type."""
    media_type = (content_type or "").partition(";")[0].strip().lower()
    if media_type in _CONTENT_TYPE_SUFFIXES:
        return _CONTENT_TYPE_SUFFIXES[media_type]

    suffix = PurePosixPath(path).suffix.lower()
    return suffix if _SAFE_SUFFIX.fullmatch(suffix) else ".bin"


def local_asset_name(url: httpx.URL, content_type: str | None) -> Path:
    """Return the stable relative path used to store one absolute asset URL."""
    digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:16]
    suffix = safe_suffix(url.path, content_type)
    return Path("assets") / f"{digest}{suffix}"


def atomic_write(path: Path, content: bytes) -> None:
    """Replace *path* atomically after fully writing and flushing a sibling file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary_name = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
