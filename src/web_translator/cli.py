"""Deterministic command-line orchestration for translation runs."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
from typing import Any

from web_translator.assemble import AssemblyError, assemble_page
from web_translator.assets import atomic_write
from web_translator.capture import CaptureError, capture_page
from web_translator.extract import extract_segments
from web_translator.models import (
    MasterReview,
    QAInputs,
    Segment,
    read_segments,
)
from web_translator.paths import validate_public_url
from web_translator.qa import run_qa
from web_translator.report import write_manifest, write_review_report
from web_translator.translations import TranslationContractError, merge_translations
from web_translator.zones import Zone, build_zones


EXIT_INVALID_ARGUMENTS = 2
EXIT_CAPTURE_FAILURE = 3
EXIT_CONTRACT_FAILURE = 4
EXIT_ASSEMBLY_FAILURE = 5
EXIT_QA_FAILURE = 6
_REPARSE_POINT = 0x400
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class CLIContractError(ValueError):
    """A documented run file is missing, malformed, or inconsistent."""


class InvalidArgumentsError(ValueError):
    """A command argument passed parsing but violates its public contract."""


class QAFailure(RuntimeError):
    """QA completed and wrote evidence, but required checks failed."""


Handler = Callable[[argparse.Namespace], None]


class _CommandArgumentParser(argparse.ArgumentParser):
    """Keep subcommand help human-readable without polluting status stdout."""

    def print_help(self, file: Any = None) -> None:
        super().print_help(file=sys.stderr if file is None else file)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one explicit pipeline command and return its stable exit code."""
    arguments = list(argv) if argv is not None else sys.argv[1:]
    command_hint = arguments[0] if arguments and not arguments[0].startswith("-") else "cli"
    parser = _build_parser()
    try:
        namespace = parser.parse_args(arguments)
    except SystemExit as error:
        code = int(error.code)
        if code:
            _emit_status(command_hint, "error", EXIT_INVALID_ARGUMENTS)
            return EXIT_INVALID_ARGUMENTS
        if command_hint != "cli":
            _emit_status(command_hint, "ok", 0)
        return 0

    command = str(namespace.command)
    handler: Handler = namespace.handler
    try:
        handler(namespace)
    except InvalidArgumentsError as error:
        return _fail(command, EXIT_INVALID_ARGUMENTS, error)
    except CaptureError as error:
        return _fail(command, EXIT_CAPTURE_FAILURE, error)
    except (CLIContractError, TranslationContractError) as error:
        return _fail(command, EXIT_CONTRACT_FAILURE, error)
    except AssemblyError as error:
        return _fail(command, EXIT_ASSEMBLY_FAILURE, error)
    except QAFailure as error:
        return _fail(command, EXIT_QA_FAILURE, error)

    _emit_status(command, "ok", 0)
    return 0


def console_main() -> None:
    """Expose :func:`main` as a console-script entry point."""
    raise SystemExit(main())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="web-translator")
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=_CommandArgumentParser
    )

    capture = subparsers.add_parser("capture", help="Capture a public web page.")
    capture.add_argument("url")
    _add_run_dir(capture)
    capture.set_defaults(handler=_capture_command)

    extract = subparsers.add_parser("extract", help="Extract translation segments.")
    _add_run_dir(extract)
    extract.set_defaults(handler=_extract_command)

    plan = subparsers.add_parser("plan-zones", help="Create translation zones.")
    _add_run_dir(plan)
    plan.add_argument("--max-chars", type=int, default=12_000)
    plan.set_defaults(handler=_plan_zones_command)

    validate = subparsers.add_parser(
        "validate-translations", help="Validate reviewed zone results."
    )
    _add_run_dir(validate)
    validate.set_defaults(handler=_validate_translations_command)

    assemble = subparsers.add_parser("assemble", help="Assemble the offline page.")
    _add_run_dir(assemble)
    _add_output_dir(assemble)
    assemble.set_defaults(handler=_assemble_command)

    qa = subparsers.add_parser("qa", help="Run deterministic and browser QA.")
    _add_run_dir(qa)
    _add_output_dir(qa)
    qa.set_defaults(handler=_qa_command)
    return parser


def _add_run_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", required=True, type=Path)


def _add_output_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", required=True, type=Path)


def _capture_command(args: argparse.Namespace) -> None:
    try:
        url = str(validate_public_url(args.url))
    except (TypeError, ValueError) as error:
        raise InvalidArgumentsError(str(error)) from error
    try:
        _validate_run_root(args.run_dir)
        if args.run_dir.exists() and any(args.run_dir.iterdir()):
            raise CLIContractError(
                f"capture run directory must be empty: {args.run_dir}"
            )
        _reject_if_link(args.run_dir / "source.html")
        _reject_if_link(args.run_dir / "capture.json")
    except (CLIContractError, OSError) as error:
        raise CaptureError(str(error)) from error
    result = capture_page(url, args.run_dir)
    asset_paths = sorted(set(result.asset_map.values()))
    critical_assets = [path for path in asset_paths if Path(path).suffix.lower() == ".css"]
    optional_assets = [path for path in asset_paths if path not in critical_assets]
    payload = {
        "asset_map": result.asset_map,
        "critical_assets": critical_assets,
        "final_url": result.final_url,
        "fingerprints": result.fingerprints,
        "missing_optional_assets": result.missing_optional_assets,
        "optional_assets": optional_assets,
        "requested_url": result.requested_url,
    }
    try:
        _write_json_atomic(args.run_dir / "capture.json", payload)
    except OSError as error:
        raise CaptureError(f"cannot write capture metadata: {error}") from error


def _extract_command(args: argparse.Namespace) -> None:
    try:
        _validate_run_root(args.run_dir)
        source = args.run_dir / "source.html"
        segments = args.run_dir / "segments.jsonl"
        capture_path = args.run_dir / "capture.json"
        _require_safe_file(source)
        if segments.exists():
            _require_safe_file(segments)
            original_segments: bytes | None = segments.read_bytes()
        else:
            _reject_if_link(segments)
            original_segments = None
        capture = _read_capture(args.run_dir)
        original_source = source.read_bytes()
        original_capture = capture_path.read_bytes()
        with tempfile.TemporaryDirectory(
            prefix=".extracting-", dir=args.run_dir
        ) as temporary_name:
            temporary = Path(temporary_name)
            temporary_source = temporary / "source.html"
            temporary_segments = temporary / "segments.jsonl"
            temporary_source.write_bytes(source.read_bytes())
            extract_segments(temporary_source, temporary_segments)
            capture["fingerprints"]["source.html"] = _sha256_file(
                temporary_source
            )
            try:
                os.replace(temporary_source, source)
                os.replace(temporary_segments, segments)
                _replace_json_atomic(capture_path, capture)
            except BaseException as publish_error:
                try:
                    _replace_bytes_atomic(source, original_source)
                    if original_segments is None:
                        segments.unlink(missing_ok=True)
                    else:
                        _replace_bytes_atomic(segments, original_segments)
                    _replace_bytes_atomic(capture_path, original_capture)
                except OSError as rollback_error:
                    raise CLIContractError(
                        "extraction publication and rollback both failed: "
                        f"publish={publish_error}; rollback={rollback_error}"
                    ) from publish_error
                raise
    except (CLIContractError, OSError, UnicodeError, ValueError) as error:
        raise CLIContractError(f"cannot extract captured source: {error}") from error


def _plan_zones_command(args: argparse.Namespace) -> None:
    if args.max_chars <= 0:
        raise InvalidArgumentsError("max-chars must be a positive integer")
    _validate_run_root(args.run_dir)
    segments = _read_segments(args.run_dir)
    try:
        zones = build_zones(segments, max_chars=args.max_chars)
    except ValueError as error:
        raise CLIContractError(str(error)) from error
    zone_dir = args.run_dir / "zones"
    if zone_dir.exists():
        raise CLIContractError(f"zone directory already exists: {zone_dir}")
    try:
        zone_dir.mkdir(parents=False)
        for zone in zones:
            _write_json_atomic(zone_dir / f"{zone.id}.json", _zone_payload(zone))
    except OSError as error:
        raise CLIContractError(f"cannot write zone assignments: {error}") from error


def _validate_translations_command(args: argparse.Namespace) -> None:
    _validate_run_root(args.run_dir)
    segments = _read_segments(args.run_dir)
    zones = _read_zones(args.run_dir)
    result_dir = _validate_translation_files(args.run_dir, zones)
    merge_translations(segments, zones, result_dir)


def _assemble_command(args: argparse.Namespace) -> None:
    _validate_run_root(args.run_dir)
    capture = _read_capture(args.run_dir)
    segments = _read_segments(args.run_dir)
    zones = _read_zones(args.run_dir)
    result_dir = _validate_translation_files(args.run_dir, zones)
    translations = merge_translations(segments, zones, result_dir)
    glossary = _read_glossary(args.run_dir / "glossary.json")
    assemble_page(
        args.run_dir / "source.html",
        {segment.id: segment for segment in segments if segment.target},
        translations,
        glossary,
        args.output_dir,
        capture["final_url"],
    )


def _qa_command(args: argparse.Namespace) -> None:
    _validate_run_root(args.run_dir)
    _reject_if_link(args.run_dir / "source.html")
    try:
        _validate_qa_output_path(args.output_dir)
    except CLIContractError as error:
        raise QAFailure(str(error)) from error
    capture = _read_capture(args.run_dir)
    segments = _read_segments(args.run_dir)
    zones = _read_zones(args.run_dir)
    result_dir = _validate_translation_files(args.run_dir, zones)
    translations = merge_translations(segments, zones, result_dir)
    review = _read_review(args.run_dir / "review.json", zones)
    result = run_qa(
        QAInputs(
            source_html=args.run_dir / "source.html",
            output_html=args.output_dir / "index.html",
            source_url=capture["final_url"],
            source_segment_ids={segment.id for segment in segments if segment.target},
            translated_segment_ids=set(translations),
            critical_assets=[Path(path) for path in capture["critical_assets"]],
            optional_assets=[Path(path) for path in capture["optional_assets"]],
            screenshot_dir=args.run_dir / "qa-screenshots",
            master_review=review,
            protected_tokens={segment.id: segment.protected for segment in segments},
            translated_texts={key: value.text for key, value in translations.items()},
            capture_metadata=dict(capture),
        )
    )
    try:
        write_manifest(result, args.output_dir / "manifest.json")
        write_review_report(result, review, args.output_dir / "review-report.md")
    except OSError as error:
        raise QAFailure(f"cannot write QA evidence: {error}") from error
    if not result.passed:
        codes = ", ".join(finding.code for finding in result.required_findings)
        raise QAFailure(f"required QA checks failed: {codes or 'unknown finding'}")


def _read_segments(run_dir: Path) -> list[Segment]:
    path = run_dir / "segments.jsonl"
    try:
        _require_safe_file(path)
        return read_segments(path)
    except (CLIContractError, OSError, UnicodeError, ValueError) as error:
        raise CLIContractError(f"cannot read segment manifest {path}: {error}") from error


def _read_zones(run_dir: Path) -> list[Zone]:
    zone_dir = run_dir / "zones"
    _require_safe_directory(zone_dir)
    try:
        entries = sorted(zone_dir.iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise CLIContractError(f"cannot read zone directory {zone_dir}: {error}") from error
    invalid = [
        path.name
        for path in entries
        if not path.is_file()
        or not path.name.startswith("zone-")
        or path.suffix != ".json"
    ]
    if invalid:
        raise CLIContractError(f"unexpected zone entries: {', '.join(invalid)}")
    zones: list[Zone] = []
    for path in entries:
        _require_safe_file(path)
        data = _read_json_object(path)
        try:
            zone = Zone(
                id=_string(data, "id", path),
                heading_path=_string_list(data, "heading_path", path),
                target_ids=_string_list(data, "target_ids", path),
                context_before_ids=_string_list(data, "context_before_ids", path),
                context_after_ids=_string_list(data, "context_after_ids", path),
                attempt=_integer(data, "attempt", path),
                expected_tokens=_string_sequence_mapping(data, "expected_tokens", path),
            )
        except ValueError as error:
            raise CLIContractError(f"invalid zone file {path}: {error}") from error
        if path.stem != zone.id:
            raise CLIContractError(
                f"zone filename {path.name} does not match embedded ID {zone.id}"
            )
        zones.append(zone)
    return zones


def _read_capture(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "capture.json"
    data = _read_json_object(path)
    requested_url = _string(data, "requested_url", path)
    final_url = _string(data, "final_url", path)
    try:
        validate_public_url(requested_url)
        validate_public_url(final_url)
    except ValueError as error:
        raise CLIContractError(f"invalid capture metadata {path}: {error}") from error
    asset_map = _string_mapping(data, "asset_map", path)
    for asset_url, local_path in asset_map.items():
        try:
            validate_public_url(asset_url)
        except ValueError as error:
            raise CLIContractError(
                f"capture asset URL must be public HTTP(S): {asset_url}"
            ) from error
        _validate_asset_path(local_path, path)
    asset_paths = set(asset_map.values())
    critical_assets = _string_list(data, "critical_assets", path)
    optional_assets = _string_list(data, "optional_assets", path)
    if set(critical_assets) & set(optional_assets):
        raise CLIContractError(f"capture asset classes overlap: {path}")
    if set(critical_assets) | set(optional_assets) != asset_paths:
        raise CLIContractError(f"capture asset classes must exactly cover asset_map: {path}")
    expected_critical = {
        value for value in asset_paths if PurePosixPath(value).suffix.lower() == ".css"
    }
    if set(critical_assets) != expected_critical:
        raise CLIContractError(f"capture critical asset classification is inconsistent: {path}")
    fingerprints = _string_mapping(data, "fingerprints", path)
    if set(fingerprints) != {"source.html", *asset_paths}:
        raise CLIContractError(f"capture fingerprints must exactly cover source and assets: {path}")
    if any(_SHA256_PATTERN.fullmatch(value) is None for value in fingerprints.values()):
        raise CLIContractError(f"capture fingerprints must be lowercase SHA-256 values: {path}")
    for relative_path, expected_digest in fingerprints.items():
        captured = run_dir / relative_path
        if relative_path == "source.html" and not captured.exists():
            continue
        _require_safe_file(captured)
        if _sha256_file(captured) != expected_digest:
            raise CLIContractError(
                f"capture fingerprint does not match file bytes: {relative_path}"
            )
    return {
        "asset_map": asset_map,
        "critical_assets": critical_assets,
        "final_url": final_url,
        "fingerprints": fingerprints,
        "missing_optional_assets": _string_list(data, "missing_optional_assets", path),
        "optional_assets": optional_assets,
        "requested_url": requested_url,
    }


def _read_glossary(path: Path) -> dict[str, str]:
    data = _read_json_object(path)
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in data.items()):
        raise CLIContractError(f"glossary keys and values must be strings: {path}")
    return dict(data)  # type: ignore[return-value]


def _read_review(path: Path, zones: Sequence[Zone]) -> MasterReview:
    data = _read_json_object(path)
    retries_raw = _mapping(data, "retries", path)
    findings_raw = _mapping(data, "section_findings", path)
    retries: dict[str, int] = {}
    for key, value in retries_raw.items():
        if not isinstance(key, str) or type(value) is not int or not 0 <= value <= 2:
            raise CLIContractError(
                f"review retries must map strings to integers from 0 through 2: {path}"
            )
        retries[key] = value
    section_findings: dict[str, list[str]] = {}
    for key, value in findings_raw.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
        ):
            raise CLIContractError(
                f"review section findings must map strings to string arrays: {path}"
            )
        section_findings[key] = list(value)
    zone_ids = {zone.id for zone in zones}
    if set(retries) != zone_ids:
        raise CLIContractError(
            f"review retries must exactly cover planned zones: {path}"
        )
    foreign_findings = sorted(set(section_findings) - zone_ids)
    if foreign_findings:
        raise CLIContractError(
            f"review section findings contain foreign zones: {', '.join(foreign_findings)}"
        )
    return MasterReview(
        unresolved_required=_string_list(data, "unresolved_required", path),
        retries=retries,
        section_findings=section_findings,
    )


def _zone_payload(zone: Zone) -> dict[str, object]:
    return {
        "attempt": zone.attempt,
        "context_after_ids": zone.context_after_ids,
        "context_before_ids": zone.context_before_ids,
        "expected_tokens": {key: list(value) for key, value in zone.expected_tokens.items()},
        "heading_path": zone.heading_path,
        "id": zone.id,
        "target_ids": zone.target_ids,
    }


def _validate_translation_files(run_dir: Path, zones: Sequence[Zone]) -> Path:
    result_dir = run_dir / "translations"
    _require_safe_directory(result_dir)
    try:
        entries = sorted(result_dir.iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise CLIContractError(
            f"cannot read translation directory {result_dir}: {error}"
        ) from error
    expected = {f"{zone.id}.jsonl" for zone in zones}
    actual = {path.name for path in entries}
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unexpected:
        raise CLIContractError(
            f"unexpected translation entries: {', '.join(unexpected)}"
        )
    if missing:
        raise CLIContractError(f"missing translation entries: {', '.join(missing)}")
    for path in entries:
        _require_safe_file(path)
    return result_dir


def _validate_asset_path(value: str, source: Path) -> None:
    candidate = PurePosixPath(value)
    if (
        "\\" in value
        or candidate.is_absolute()
        or len(candidate.parts) != 2
        or candidate.parts[0] != "assets"
        or candidate.parts[1] in {"", ".", ".."}
    ):
        raise CLIContractError(
            f"capture asset path must be a safe assets/ path in {source}: {value}"
        )


def _validate_run_root(run_dir: Path) -> None:
    _reject_linked_ancestors(run_dir)
    if run_dir.exists() and not run_dir.is_dir():
        raise CLIContractError(f"run directory path is not a directory: {run_dir}")


def _validate_qa_output_path(output_dir: Path) -> None:
    _reject_linked_ancestors(output_dir)
    if output_dir.exists() and not output_dir.is_dir():
        raise CLIContractError(f"QA output path is not a directory: {output_dir}")
    _reject_if_link(output_dir / "index.html")
    _reject_if_link(output_dir / "manifest.json")
    _reject_if_link(output_dir / "review-report.md")


def _require_safe_directory(path: Path) -> None:
    _reject_linked_ancestors(path)
    if not path.is_dir():
        raise CLIContractError(f"missing or unsafe directory: {path}")


def _require_safe_file(path: Path) -> None:
    _reject_linked_ancestors(path)
    if not path.is_file():
        raise CLIContractError(f"missing or unsafe file: {path}")


def _reject_if_link(path: Path) -> None:
    if _is_link_or_reparse(path):
        raise CLIContractError(f"path is a link or reparse point: {path}")


def _reject_linked_ancestors(path: Path) -> None:
    candidate = path.absolute()
    while not candidate.exists() and not candidate.is_symlink():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    while True:
        if _is_link_or_reparse(candidate):
            raise CLIContractError(
                f"path contains a link or reparse point: {candidate}"
            )
        parent = candidate.parent
        if parent == candidate:
            return
        candidate = parent


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _REPARSE_POINT)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        _require_safe_file(path)
        value = json.loads(path.read_text(encoding="utf-8"))
    except (CLIContractError, OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CLIContractError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise CLIContractError(f"JSON document must be an object: {path}")
    return value


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    serialized = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    atomic_write(path, serialized.encode("utf-8"))


def _replace_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    serialized = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _replace_bytes_atomic(path, serialized)


def _replace_bytes_atomic(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise CLIContractError(f"cannot hash captured file {path}: {error}") from error
    return digest.hexdigest()


def _string(data: Mapping[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise CLIContractError(f"{path.name}.{key} must be a string")
    return value


def _integer(data: Mapping[str, Any], key: str, path: Path) -> int:
    value = data.get(key)
    if type(value) is not int:
        raise CLIContractError(f"{path.name}.{key} must be an integer")
    return value


def _mapping(data: Mapping[str, Any], key: str, path: Path) -> Mapping[object, object]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise CLIContractError(f"{path.name}.{key} must be an object")
    return value


def _string_list(data: Mapping[str, Any], key: str, path: Path) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CLIContractError(f"{path.name}.{key} must be a string array")
    return list(value)


def _string_mapping(data: Mapping[str, Any], key: str, path: Path) -> dict[str, str]:
    value = _mapping(data, key, path)
    if any(
        not isinstance(item_key, str) or not isinstance(item_value, str)
        for item_key, item_value in value.items()
    ):
        raise CLIContractError(f"{path.name}.{key} must map strings to strings")
    return dict(value)  # type: ignore[arg-type]


def _string_sequence_mapping(
    data: Mapping[str, Any], key: str, path: Path
) -> dict[str, tuple[str, ...]]:
    value = _mapping(data, key, path)
    result: dict[str, tuple[str, ...]] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str) or not isinstance(item_value, list) or any(
            not isinstance(entry, str) for entry in item_value
        ):
            raise CLIContractError(f"{path.name}.{key} must map strings to string arrays")
        result[item_key] = tuple(item_value)
    return result


def _fail(command: str, exit_code: int, error: Exception) -> int:
    print(f"{command}: {error}", file=sys.stderr)
    _emit_status(command, "error", exit_code)
    return exit_code


def _emit_status(command: str, status: str, exit_code: int) -> None:
    print(
        json.dumps(
            {"command": command, "exit_code": exit_code, "status": status},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
