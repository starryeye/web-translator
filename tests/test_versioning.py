from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_SCRIPT = ROOT / "scripts" / "version.py"


def _version_root(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    for relative in (
        Path(".codex-plugin/plugin.json"),
        Path("pyproject.toml"),
        Path("src/web_translator/__init__.py"),
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    return root


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERSION_SCRIPT), "--root", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def _versions(root: Path) -> tuple[str, str, str]:
    manifest = json.loads(
        (root / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
    )
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    package = (root / "src/web_translator/__init__.py").read_text(encoding="utf-8")
    project_version = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in pyproject.splitlines()
        if line.startswith("version = ")
    )
    package_version = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in package.splitlines()
        if line.startswith("__version__ = ")
    )
    return manifest["version"], project_version, package_version


def _next_patch(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


class VersioningTests(unittest.TestCase):
    def test_check_and_show_require_all_version_sources_to_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = _version_root(Path(temporary_directory))
            expected = _versions(root)[0]

            checked = _run(root, "check")
            shown = _run(root, "show")

            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(checked.stdout.strip(), expected)
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(shown.stdout.strip(), expected)

            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                pyproject.read_text(encoding="utf-8").replace(
                    f'version = "{expected}"', 'version = "9.9.9"', 1
                ),
                encoding="utf-8",
            )

            mismatch = _run(root, "check")

            self.assertEqual(mismatch.returncode, 1)
            self.assertIn("version mismatch", mismatch.stderr)

    def test_bump_patch_updates_every_version_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = _version_root(Path(temporary_directory))
            current = _versions(root)[0]
            target = _next_patch(current)

            result = _run(root, "bump", "patch")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), f"{current} -> {target}")
            self.assertEqual(_versions(root), (target, target, target))

    def test_set_accepts_strict_semver_and_rejects_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = _version_root(Path(temporary_directory))

            accepted = _run(root, "set", "1.2.3-rc.1+build.5")
            rejected = _run(root, "set", "v2")

            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(
                _versions(root),
                (
                    "1.2.3-rc.1+build.5",
                    "1.2.3-rc.1+build.5",
                    "1.2.3-rc.1+build.5",
                ),
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("strict semantic version", rejected.stderr)
            self.assertEqual(
                _versions(root),
                (
                    "1.2.3-rc.1+build.5",
                    "1.2.3-rc.1+build.5",
                    "1.2.3-rc.1+build.5",
                ),
            )


if __name__ == "__main__":
    unittest.main()
