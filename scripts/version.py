#!/usr/bin/env python3
"""Show, validate, and update Web Translator release versions."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$",
    re.ASCII,
)
PROJECT_VERSION = re.compile(r'^(version\s*=\s*")[^"]+("\s*)$')
PACKAGE_VERSION = re.compile(r'^(__version__\s*=\s*")[^"]+("\s*)$')


class VersionError(RuntimeError):
    """The repository's release version contract is invalid."""


@dataclass(frozen=True)
class VersionFiles:
    manifest: Path
    pyproject: Path
    package: Path

    @classmethod
    def from_root(cls, root: Path) -> "VersionFiles":
        return cls(
            manifest=root / ".codex-plugin" / "plugin.json",
            pyproject=root / "pyproject.toml",
            package=root / "src" / "web_translator" / "__init__.py",
        )


def _strict_semver(value: object) -> str:
    if not isinstance(value, str) or SEMVER.fullmatch(value) is None:
        raise VersionError(f"not a strict semantic version: {value!r}")
    return value


def _read_manifest(path: Path) -> tuple[dict[str, object], str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise VersionError(f"cannot read {path}: {error}") from error
    if not isinstance(document, dict):
        raise VersionError(f"{path} must contain a JSON object")
    return document, _strict_semver(document.get("version"))


def _read_assignment(path: Path, pattern: re.Pattern[str], section: str | None) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise VersionError(f"cannot read {path}: {error}") from error

    matches: list[str] = []
    active_section: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            active_section = stripped
        if section is not None and active_section != section:
            continue
        match = pattern.fullmatch(line)
        if match is not None:
            value = line[len(match.group(1)) : len(line) - len(match.group(2))]
            matches.append(_strict_semver(value))
    if len(matches) != 1:
        raise VersionError(
            f"{path} must contain exactly one release version assignment; found {len(matches)}"
        )
    return text, matches[0]


def _read_versions(files: VersionFiles) -> tuple[str, dict[str, object], str, str]:
    manifest, manifest_version = _read_manifest(files.manifest)
    pyproject_text, pyproject_version = _read_assignment(
        files.pyproject, PROJECT_VERSION, "[project]"
    )
    package_text, package_version = _read_assignment(
        files.package, PACKAGE_VERSION, None
    )
    versions = {
        str(files.manifest): manifest_version,
        str(files.pyproject): pyproject_version,
        str(files.package): package_version,
    }
    if len(set(versions.values())) != 1:
        detail = ", ".join(f"{path}={version}" for path, version in versions.items())
        raise VersionError(f"version mismatch: {detail}")
    return manifest_version, manifest, pyproject_text, package_text


def _replace_assignment(
    text: str, pattern: re.Pattern[str], version: str, section: str | None
) -> str:
    lines = text.splitlines(keepends=True)
    active_section: str | None = None
    replaced = 0
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        ending = line[len(content) :]
        stripped = content.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            active_section = stripped
        if section is not None and active_section != section:
            continue
        match = pattern.fullmatch(content)
        if match is not None:
            lines[index] = f"{match.group(1)}{version}{match.group(2)}{ending}"
            replaced += 1
    if replaced != 1:
        raise VersionError(f"expected one version assignment to replace; found {replaced}")
    return "".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _set_version(files: VersionFiles, target: str) -> tuple[str, str]:
    target = _strict_semver(target)
    current, manifest, pyproject_text, package_text = _read_versions(files)
    if target == current:
        return current, target

    manifest["version"] = target
    rendered_manifest = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    rendered_pyproject = _replace_assignment(
        pyproject_text, PROJECT_VERSION, target, "[project]"
    )
    rendered_package = _replace_assignment(
        package_text, PACKAGE_VERSION, target, None
    )

    _atomic_write(files.manifest, rendered_manifest)
    _atomic_write(files.pyproject, rendered_pyproject)
    _atomic_write(files.package, rendered_package)
    return current, target


def _bumped(version: str, component: str) -> str:
    match = SEMVER.fullmatch(version)
    assert match is not None
    major, minor, patch = (int(match.group(index)) for index in (1, 2, 3))
    if component == "major":
        return f"{major + 1}.0.0"
    if component == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="plugin repository root (defaults to the parent of scripts/)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("show", help="print the current plugin version")
    subparsers.add_parser("check", help="validate all version declarations")
    bump = subparsers.add_parser("bump", help="increment a stable SemVer component")
    bump.add_argument("component", choices=("major", "minor", "patch"))
    set_version = subparsers.add_parser("set", help="set an explicit strict SemVer")
    set_version.add_argument("version")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    files = VersionFiles.from_root(arguments.root.resolve())
    try:
        if arguments.command in {"show", "check"}:
            version, *_ = _read_versions(files)
            print(version)
            return 0
        if arguments.command == "bump":
            current, *_ = _read_versions(files)
            previous, target = _set_version(files, _bumped(current, arguments.component))
        else:
            previous, target = _set_version(files, arguments.version)
    except VersionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"{previous} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
