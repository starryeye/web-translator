"""Command-line interface for Web Translator."""

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Web Translator command-line interface."""
    parser = argparse.ArgumentParser(prog="web-translator")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("capture", help="Capture a public web page for translation.")
    try:
        parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    return 0


def console_main() -> None:
    """Expose :func:`main` as a console-script entry point."""
    raise SystemExit(main())
