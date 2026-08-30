import json
from importlib.resources import files
import os
from pathlib import Path
import subprocess

import pytest

from web_translator import __version__
from web_translator.cli import main


ROOT = Path(__file__).parents[1]


def test_manifest_discovers_both_translator_skills() -> None:
    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text("utf-8"))
    assert manifest["name"] == "web-translator"
    assert manifest["version"] == __version__
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["displayName"] == "Web Translator"
    assert (ROOT / "skills/web-translator").is_dir()
    assert (ROOT / "skills/pdf-translator").is_dir()
    assert {
        path.name for path in (ROOT / "skills").iterdir() if path.is_dir()
    } == {"web-translator", "pdf-translator"}

    searchable_metadata = " ".join(
        (
            manifest["description"],
            manifest["interface"]["shortDescription"],
            manifest["interface"]["longDescription"],
            *manifest["interface"]["defaultPrompt"],
        )
    ).lower()
    assert "html" in searchable_metadata
    assert "pdf" in searchable_metadata
    assert "public" in searchable_metadata
    assert "local" in searchable_metadata


def test_cli_help_returns_success(capsys) -> None:
    assert main(["--help"]) == 0
    assert "capture" in capsys.readouterr().out


def test_pdf_fonts_are_exposed_as_package_resources() -> None:
    assets = files("web_translator").joinpath("font_assets")

    assert assets.joinpath("NotoSansKR-Regular.ttf").is_file()
    assert assets.joinpath("NotoSansKR-Bold.ttf").is_file()
    assert assets.joinpath("OFL.txt").is_file()
    assert assets.joinpath("PROVENANCE.json").is_file()


def test_font_license_is_marked_binary_to_preserve_pinned_bytes_on_windows() -> None:
    assert "src/web_translator/font_assets/OFL.txt -text" in (
        ROOT / ".gitattributes"
    ).read_text("utf-8")


def test_windows_poppler_install_pins_dependency_complete_archive() -> None:
    workflow = (ROOT / ".github/workflows/pdf-cross-platform.yml").read_text("utf-8")

    assert "python scripts/install_pinned_poppler.py --destination-root $extractRoot" in workflow
    assert "Invoke-WebRequest" not in workflow
    assert "Expand-Archive" not in workflow
    assert "Get-FileHash" not in workflow
    assert "choco install poppler --version=22.11.0.20240421 -y" not in workflow
    assert "choco install poppler -y" not in workflow


def test_windows_poppler_install_gates_both_executables_before_path_export() -> None:
    workflow = (ROOT / ".github/workflows/pdf-cross-platform.yml").read_text("utf-8")

    pdfinfo_gate = workflow.index("& $pdfinfo -v")
    pdftoppm_gate = workflow.index("& $pdftoppm -v")
    path_export = workflow.index("$env:GITHUB_PATH")

    assert pdfinfo_gate < pdftoppm_gate < path_export


def test_windows_workflow_runs_live_nonempty_directory_publication_contracts() -> None:
    workflow = (ROOT / ".github/workflows/pdf-cross-platform.yml").read_text("utf-8")

    for node in (
        "tests/test_pdf_qa.py::test_windows_publishes_nonempty_qa_directory_after_releasing_descendant_handles",
        "tests/test_pdf_qa.py::test_windows_nonempty_qa_directory_publication_never_clobbers_destination",
        "tests/test_pdf_pipeline.py::test_windows_finalize_durably_publishes_exact_reviewed_output",
    ):
        assert node in workflow


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Poppler executables")
@pytest.mark.parametrize("command", ["pdfinfo", "pdftoppm"])
def test_windows_poppler_commands_load_with_all_runtime_dependencies(command: str) -> None:
    """Executable discovery alone must not pass when a dependent DLL is absent."""
    completed = subprocess.run(
        [command, "-v"],
        shell=False,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
