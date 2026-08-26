import json
from importlib.resources import files
from pathlib import Path

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


def test_windows_poppler_install_pins_binary_bearing_chocolatey_package() -> None:
    workflow = (ROOT / ".github/workflows/pdf-cross-platform.yml").read_text("utf-8")

    # Chocolatey's latest Poppler package is source-only, so it cannot supply
    # the pdfinfo.exe and pdftoppm.exe binaries required by the Windows job.
    assert "choco install poppler --version=22.11.0.20240421 -y" in workflow
    assert "choco install poppler -y" not in workflow


def test_windows_poppler_diagnostic_braces_variable_before_colon() -> None:
    workflow = (ROOT / ".github/workflows/pdf-cross-platform.yml").read_text("utf-8")

    assert 'Write-Host "Poppler files under ${popplerRoot}:`n' in workflow
    assert 'Write-Host "Poppler files under $popplerRoot:`n' not in workflow
