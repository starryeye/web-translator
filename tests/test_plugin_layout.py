import json
from importlib.resources import files
from pathlib import Path

from web_translator import __version__
from web_translator.cli import main


ROOT = Path(__file__).parents[1]


def test_manifest_discovers_web_translator_skill() -> None:
    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text("utf-8"))
    assert manifest["name"] == "web-translator"
    assert manifest["version"] == __version__
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["displayName"] == "Web Translator"
    assert (ROOT / "skills/web-translator").is_dir()


def test_cli_help_returns_success(capsys) -> None:
    assert main(["--help"]) == 0
    assert "capture" in capsys.readouterr().out


def test_pdf_fonts_are_exposed_as_package_resources() -> None:
    assets = files("web_translator").joinpath("font_assets")

    assert assets.joinpath("NotoSansKR-Regular.ttf").is_file()
    assert assets.joinpath("NotoSansKR-Bold.ttf").is_file()
    assert assets.joinpath("OFL.txt").is_file()
    assert assets.joinpath("PROVENANCE.json").is_file()
