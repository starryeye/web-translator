"""Public URL validation and collision-safe run-directory allocation."""

from __future__ import annotations

import ipaddress
import os
import re
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from collections.abc import Iterator

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
    timestamp = _utc_timestamp(now)
    base_run_id = _run_id(parsed, timestamp)
    return _allocate_run_paths(Path(workspace), base_run_id, Path("translated-pages"))


def create_pdf_run_paths(workspace: Path, source_label: str, now: datetime) -> RunPaths:
    """Allocate one private run directory and a reserved PDF output path."""
    source_name = Path(source_label.replace("\\", "/")).name
    stem = Path(source_name).stem or "document"
    base_run_id = "-".join(
        part for part in (_slugify(stem), _utc_timestamp(now)) if part
    )
    return _allocate_run_paths(
        Path(workspace), base_run_id, Path("translated-pdfs")
    )


def lexical_workspace() -> Path:
    """Return the shell's lexical cwd only after rejecting link/reparse evidence."""
    raw = os.environ.get("PWD") if os.name != "nt" else None
    workspace = Path(raw) if raw else Path.cwd()
    if not workspace.is_absolute():
        raise ValueError("workspace cwd must be an absolute lexical path")
    import web_translator.pdf_assemble as anchored

    try:
        handle = anchored._open_directory_anchor(workspace, "workspace")
        try:
            current = os.stat(".")
            if handle.identity != (current.st_dev, current.st_ino):
                raise ValueError("lexical workspace does not identify the current directory")
        finally:
            handle.close()
    except anchored.PdfAssemblyError as error:
        raise ValueError(f"workspace cwd is a link or reparse point: {error}") from error
    return workspace


@dataclass(slots=True)
class HeldAllocatedRunPaths:
    workspace: object
    control: object
    runs: object
    run: object
    outputs: object
    output_name: str

    def verify(self) -> None:
        import web_translator.pdf_assemble as anchored

        try:
            self.workspace.verify_visible()
            self.control.verify_visible()
            self.runs.verify_visible()
            self.run.verify_visible()
            self.outputs.verify_visible()
        except anchored.PdfAssemblyError as error:
            raise ValueError(f"workspace or allocated roots changed identity: {error}") from error


@contextmanager
def hold_allocated_run_paths(
    workspace: Path,
    work_dir: Path,
    output_dir: Path,
    *,
    output_root: str,
) -> Iterator[HeldAllocatedRunPaths]:
    """Prove and retain one exact allocator run/output relationship."""
    workspace = Path(os.path.abspath(os.fspath(workspace)))
    work_dir = Path(os.path.abspath(os.fspath(work_dir)))
    output_dir = Path(os.path.abspath(os.fspath(output_dir)))
    expected_run_root = workspace / ".web-translator" / "runs"
    expected_output_root = workspace / output_root
    if (
        work_dir.parent != expected_run_root
        or output_dir.parent != expected_output_root
    ):
        raise ValueError("allocated paths are not exact children of intended roots")
    if work_dir.name != output_dir.name:
        raise ValueError("allocated work and output paths must use the same run ID")
    import web_translator.pdf_assemble as anchored

    workspace_anchor = control_anchor = runs_anchor = run_anchor = outputs_anchor = None
    yield_started = False
    try:
        workspace_anchor = anchored._open_directory_anchor(workspace, "workspace")
        control_anchor = anchored._open_existing_child_directory(
            workspace_anchor, ".web-translator", "run control"
        )
        runs_anchor = anchored._open_existing_child_directory(
            control_anchor, "runs", "run root"
        )
        run_anchor = anchored._open_existing_child_directory(
            runs_anchor, work_dir.name, "run"
        )
        outputs_anchor = anchored._open_existing_child_directory(
            workspace_anchor, output_root, "output root"
        )
        contract = HeldAllocatedRunPaths(
            workspace_anchor,
            control_anchor,
            runs_anchor,
            run_anchor,
            outputs_anchor,
            output_dir.name,
        )
        contract.verify()
        yield_started = True
        yield contract
    except ValueError:
        if yield_started:
            raise
        raise
    except anchored.PdfAssemblyError as error:
        if yield_started:
            raise
        raise ValueError(f"cannot hold exact allocated run paths: {error}") from error
    finally:
        for anchor in (
            outputs_anchor,
            run_anchor,
            runs_anchor,
            control_anchor,
            workspace_anchor,
        ):
            if anchor is not None:
                anchor.close()


def _allocate_run_paths(workspace: Path, base_run_id: str, output_root: Path) -> RunPaths:
    # ``abspath`` is deliberately lexical: unlike ``resolve`` it does not erase
    # symlink evidence before each ancestor has been opened without following it.
    workspace = Path(os.path.abspath(os.fspath(workspace)))
    if output_root.parent != Path(".") or output_root.name != os.fspath(output_root):
        raise ValueError("run output root must be one direct workspace child")

    # The PDF assembly anchor implementation is the package's common held-directory
    # primitive on POSIX and Windows.  Import lazily so URL-only helpers stay light and
    # to avoid the paths -> acquisition -> paths import cycle.
    import web_translator.pdf_assemble as anchored

    created_run: anchored._DirectoryAnchor | None = None
    runs_anchor: anchored._DirectoryAnchor | None = None
    try:
        with ExitStack() as stack:
            workspace_anchor = _open_or_create_anchored_path(
                workspace,
                anchored,
                stack,
            )
            control_anchor = _open_or_create_anchored_child(
                workspace_anchor,
                ".web-translator",
                "run control",
                anchored,
            )
            stack.callback(control_anchor.close)
            runs_anchor = _open_or_create_anchored_child(
                control_anchor,
                "runs",
                "run root",
                anchored,
            )
            stack.callback(runs_anchor.close)
            outputs_anchor = _open_or_create_anchored_child(
                workspace_anchor,
                output_root.name,
                "output root",
                anchored,
            )
            stack.callback(outputs_anchor.close)

            for suffix in range(1, 10_001):
                run_id = base_run_id if suffix == 1 else f"{base_run_id}-{suffix}"
                try:
                    anchored._require_anchored_name_absent(outputs_anchor, run_id)
                except anchored.PdfAssemblyError:
                    continue
                try:
                    created_run = anchored._create_child_directory(
                        runs_anchor,
                        run_id,
                        "run",
                    )
                except FileExistsError:
                    continue

                try:
                    # Close the allocation race: the output name must still be absent
                    # after the exact run child has been created.
                    anchored._require_anchored_name_absent(outputs_anchor, run_id)
                    workspace_anchor.verify_visible()
                    control_anchor.verify_visible()
                    runs_anchor.verify_visible()
                    outputs_anchor.verify_visible()
                    created_run.verify_visible()
                    return RunPaths(
                        run_id=run_id,
                        work_dir=workspace
                        / ".web-translator"
                        / "runs"
                        / run_id,
                        output_dir=workspace / output_root.name / run_id,
                    )
                except anchored.PdfAssemblyError:
                    anchored._remove_owned_directory(
                        runs_anchor,
                        run_id,
                        created_run.identity,
                        child=created_run,
                    )
                    created_run.close()
                    created_run = None
                    # If all held roots remain visible, this was only an output-name
                    # collision.  A moved/replaced ancestor is a hard failure.
                    workspace_anchor.verify_visible()
                    control_anchor.verify_visible()
                    runs_anchor.verify_visible()
                    outputs_anchor.verify_visible()
                    continue
                finally:
                    if created_run is not None:
                        created_run.close()
                        created_run = None
            raise ValueError("cannot reserve a unique run and output name")
    except ValueError:
        raise
    except anchored.PdfAssemblyError as error:
        if created_run is not None and runs_anchor is not None:
            anchored._remove_owned_directory(
                runs_anchor,
                created_run.path.name,
                created_run.identity,
                child=created_run,
            )
            created_run.close()
        raise ValueError(f"cannot safely allocate run paths: {error}") from error


def _open_or_create_anchored_path(
    path: Path,
    anchored: object,
    stack: ExitStack,
) -> object:
    """Open/create every lexical component while retaining all parent anchors."""
    parts = path.parts
    if not parts or not path.is_absolute():
        raise ValueError("workspace must have an absolute lexical path")
    current = Path(parts[0])
    try:
        parent = anchored._open_directory_anchor(current, "workspace root")
    except anchored.PdfAssemblyError as error:
        raise ValueError(f"workspace root is not a safe directory: {error}") from error
    stack.callback(parent.close)
    for name in parts[1:]:
        child = _open_or_create_anchored_child(
            parent,
            name,
            "workspace",
            anchored,
        )
        stack.callback(child.close)
        parent = child
        current /= name
    parent.verify_visible()
    return parent


def _open_or_create_anchored_child(
    parent: object,
    name: str,
    label: str,
    anchored: object,
) -> object:
    absent = True
    try:
        anchored._require_anchored_name_absent(parent, name)
    except anchored.PdfAssemblyError:
        absent = False
    if not absent:
        try:
            return anchored._open_existing_child_directory(parent, name, label)
        except anchored.PdfAssemblyError as error:
            raise ValueError(
                f"{label} is a link, reparse point, or unsafe directory: "
                f"{parent.path / name}: {error}"
            ) from error
    try:
        return anchored._create_child_directory(parent, name, label)
    except (FileExistsError, anchored.PdfAssemblyError) as error:
        raise ValueError(
            f"{label} changed identity while it was created: {parent.path / name}: {error}"
        ) from error


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
