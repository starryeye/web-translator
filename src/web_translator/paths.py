"""Public URL validation and collision-safe run-directory allocation."""

from __future__ import annotations

import ipaddress
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from web_translator.models import RunPaths


def validate_public_url(url: str) -> httpx.URL:
    """Return a public HTTP(S) URL or reject unsafe URL targets."""
    try:
        parsed = httpx.URL(url)
    except (TypeError, ValueError, httpx.InvalidURL) as error:
        raise ValueError("URL must be a valid public HTTP(S) URL") from error

    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ValueError("URL must use HTTP(S) and include a host")
    if "@" in urlsplit(url).netloc:
        raise ValueError("URL userinfo is not allowed")

    host = parsed.host.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("localhost URLs are not allowed")

    address = _parse_ip_address(host)
    if address is None:
        return parsed

    if not address.is_global or address.is_multicast:
        raise ValueError("non-public IP URLs are not allowed")
    return parsed


def create_run_paths(workspace: Path, url: str, now: datetime) -> RunPaths:
    """Allocate a unique work directory and an unused output directory path."""
    parsed = validate_public_url(url)
    workspace = Path(workspace)
    timestamp = _utc_timestamp(now)
    base_run_id = _run_id(parsed, timestamp)
    runs_dir = workspace / ".web-translator" / "runs"
    outputs_dir = workspace / "translated-pages"

    suffix = 1
    while True:
        run_id = base_run_id if suffix == 1 else f"{base_run_id}-{suffix}"
        work_dir = runs_dir / run_id
        output_dir = outputs_dir / run_id
        if output_dir.exists():
            suffix += 1
            continue
        try:
            work_dir.mkdir(parents=True)
        except FileExistsError:
            suffix += 1
            continue
        return RunPaths(run_id=run_id, work_dir=work_dir, output_dir=output_dir)


def _utc_timestamp(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC).strftime("%Y%m%d-%H%M%S")


def _run_id(url: httpx.URL, timestamp: str) -> str:
    host = _slugify(url.host or "")
    path = _slugify(unquote(url.path))
    return "-".join(part for part in (host, path, timestamp) if part)


def _slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip(".-_")


def _parse_ip_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return _parse_legacy_ipv4(host)


def _parse_legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse numeric IPv4 spellings accepted by socket implementations."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None

    numbers: list[int] = []
    for part in parts:
        if not part:
            return None
        base = 16 if part.lower().startswith("0x") else 8 if len(part) > 1 and part.startswith("0") else 10
        digits = part[2:] if base == 16 else part
        if not digits or not re.fullmatch(r"[0-9a-fA-F]+" if base == 16 else r"[0-7]+" if base == 8 else r"[0-9]+", digits):
            return None
        numbers.append(int(digits, base))

    if any(number > 255 for number in numbers[:-1]):
        return None
    last_bits = 32 - 8 * (len(numbers) - 1)
    if numbers[-1] >= 1 << last_bits:
        return None

    value = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        value |= number << (24 - 8 * index)
    return ipaddress.IPv4Address(value)
