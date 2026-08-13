import json
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
