from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

import pytest

from tests.pdf_fixtures import make_pdf_source_record
from tests.pdf_unit_fixtures import make_unit_document
from web_translator import cli
from web_translator.models import Translation, write_segments
from web_translator.paths import create_pdf_run_paths
from web_translator.pdf_extract import build_pdf_unit_segments
from web_translator.pdf_models import PdfDocument
from web_translator.zones import build_zones


BINDING = ".pdf-unit-binding.json"


def binding_api():
    assert importlib.util.find_spec("web_translator.pdf_unit_bindings"), "PDF binding guard is missing"
    return importlib.import_module("web_translator.pdf_unit_bindings")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def make_unit_run(tmp_path):
    paths = create_pdf_run_paths(tmp_path, "source.pdf", datetime(2026, 9, 28, tzinfo=UTC))
    run = paths.work_dir
    run.mkdir(parents=True, exist_ok=True)
    document = make_unit_document()
    blocks, units, segments = build_pdf_unit_segments(document.blocks, document.translation_units)
    document = replace(document, blocks=blocks, translation_units=units)
    payload = document.to_dict()
    payload["extracted_schema_version"] = "1.2"
    write_json(run / "document.json", payload)
    write_json(run / "source.json", replace(make_pdf_source_record(), sha256=document.source_sha256).to_dict())
    (run / "source.pdf").write_bytes(b"synthetic source marker; never rendered")
    write_segments(run / "segments.jsonl", segments)
    write_json(run / "glossary.json", {})
    (run / "document-summary.txt").write_text("Logical unit fixture", encoding="utf-8")
    (run / "zones").mkdir()
    for zone in build_zones(segments):
        write_json(run / "zones" / f"{zone.id}.json", cli._zone_payload(zone))
    return run


def manual_binding(run):
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "schema_version": "1.0",
        "document_sha256": digest(run / "document.json"),
        "segments_sha256": digest(run / "segments.jsonl"),
        "zones": {p.name: digest(p) for p in (run / "zones").iterdir()},
        "assignments": {p.name: digest(p) for p in (run / "assignments").iterdir() if p.name != BINDING},
    }


@pytest.fixture
def bound_unit_run(tmp_path):
    run = make_unit_run(tmp_path)
    (run / "assignments").mkdir()
    write_json(run / "assignments" / "zone-001.json", {"zone_id": "zone-001"})
    write_json(run / "assignments" / BINDING, manual_binding(run))
    (run / "translations").mkdir()
    (run / "translations" / "zone-001.jsonl").write_text(json.dumps(Translation("seg-000001", "번역 문장").to_dict()) + "\n")
    return run


def test_native_origin_is_current_and_readable():
    from web_translator.pdf_models import PDF_DOCUMENT_SCHEMA_VERSION
    payload = make_unit_document().to_dict()
    payload["extracted_schema_version"] = "1.2"
    assert PdfDocument.from_dict(payload).extracted_schema_version == "1.2"
    assert PDF_DOCUMENT_SCHEMA_VERSION == "1.2"


def test_binding_matches_exact_bytes_and_has_immutable_maps(bound_unit_run):
    api = binding_api()
    run = bound_unit_run
    with api.hold_pdf_unit_binding(run) as binding:
        assert binding.to_dict() == manual_binding(run)
        with pytest.raises(TypeError):
            binding.zones["zone-002.json"] = "a" * 64


def test_same_text_different_unit_map_invalidates_binding(bound_unit_run):
    api = binding_api()
    path = bound_unit_run / "document.json"
    value = json.loads(path.read_bytes())
    value["translation_units"][0]["joins"][0]["evidence"]["left"]["text_indent"] = 2.0
    write_json(path, value)
    with pytest.raises(api.PdfUnitBindingError, match="binding"):
        with api.hold_pdf_unit_binding(bound_unit_run):
            pass


@pytest.mark.parametrize("relative", ["document.json", "segments.jsonl", "zones/zone-001.json", "assignments/zone-001.json"])
def test_binding_rejects_changed_input_bytes(bound_unit_run, relative):
    api = binding_api()
    path = bound_unit_run / relative
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(api.PdfUnitBindingError, match="binding"):
        with api.hold_pdf_unit_binding(bound_unit_run):
            pass


@pytest.mark.parametrize("problem", ["missing-file", "missing-entry", "foreign-entry", "malformed-digest", "self-entry", "foreign-file"])
def test_binding_rejects_nonexact_manifest(bound_unit_run, problem):
    api = binding_api()
    path = bound_unit_run / "assignments" / BINDING
    value = json.loads(path.read_bytes())
    if problem == "missing-file":
        path.unlink()
    elif problem == "foreign-file":
        (path.parent / "extra.json").write_text("{}")
    else:
        if problem == "missing-entry":
            value["zones"] = {}
        elif problem == "foreign-entry":
            value["assignments"]["zone-002.json"] = "a" * 64
        elif problem == "self-entry":
            value["assignments"][BINDING] = "a" * 64
        else:
            value["document_sha256"] = "not a digest"
        write_json(path, value)
    with pytest.raises(api.PdfUnitBindingError):
        with api.hold_pdf_unit_binding(bound_unit_run):
            pass


@pytest.mark.parametrize("relative", ["document.json", "segments.jsonl", "zones", "assignments", "assignments/.pdf-unit-binding.json"])
def test_binding_rejects_symlink_input(bound_unit_run, relative):
    api = binding_api()
    path = bound_unit_run / relative
    moved = path.with_name(path.name + "-held")
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(api.PdfUnitBindingError):
        with api.hold_pdf_unit_binding(bound_unit_run):
            pass


@pytest.mark.parametrize("mutation", ["content", "file", "directory", "child-set"])
def test_binding_detects_mutation_during_consumption(bound_unit_run, mutation):
    api = binding_api()
    with pytest.raises(api.PdfUnitBindingError):
        with api.hold_pdf_unit_binding(bound_unit_run):
            path = bound_unit_run / "segments.jsonl"
            if mutation == "content":
                path.write_bytes(path.read_bytes() + b" ")
            elif mutation == "file":
                data = path.read_bytes()
                path.rename(path.with_name("old-segments"))
                path.write_bytes(data)
            elif mutation == "directory":
                directory = bound_unit_run / "zones"
                directory.rename(bound_unit_run / "old-zones")
                directory.mkdir()
            else:
                (bound_unit_run / "assignments" / "zone-002.json").write_text("{}")


@pytest.mark.parametrize("origin", ["1.0", "1.1"])
def test_adapted_document_cannot_be_assigned(origin):
    api = binding_api()
    with pytest.raises(api.PdfUnitBindingError, match="native"):
        api.require_assignable_pdf(replace(make_unit_document(), extracted_schema_version=origin))


def test_direct_assembly_rejects_missing_native_binding_before_staging(bound_unit_run):
    from web_translator.pdf_assemble import PdfAssemblyError, assemble_pdf
    run = bound_unit_run
    (run / "assignments" / BINDING).unlink()
    with pytest.raises(PdfAssemblyError, match="binding"):
        assemble_pdf(run, {}, {}, run.parents[2] / "translated-pdfs" / run.name)
    assert not (run / "staged-output").exists()


def test_direct_assembly_keeps_public_error_on_consumer_failure(bound_unit_run):
    from web_translator.pdf_assemble import PdfAssemblyError, assemble_pdf
    run = bound_unit_run
    with pytest.raises(PdfAssemblyError, match="cover"):
        assemble_pdf(run, {}, {}, run.parents[2] / "translated-pdfs" / run.name)
    assert not (run / "staged-output").exists()


@pytest.mark.parametrize("command", ["prepare", "finalize"])
def test_qa_rejects_missing_binding_before_artifact_consumption(bound_unit_run, command):
    from web_translator.pdf_qa import PdfQAFailure, finalize_pdf_output, prepare_pdf_qa
    run = bound_unit_run
    (run / "assignments" / BINDING).unlink()
    operation = prepare_pdf_qa if command == "prepare" else finalize_pdf_output
    with pytest.raises(PdfQAFailure, match="binding"):
        operation(run, run.parents[2] / "translated-pdfs" / run.name)
    assert not (run / "qa-pages").exists()


def test_binding_rejects_unrelated_source_record(bound_unit_run):
    api = binding_api()
    path = bound_unit_run / "source.json"
    value = json.loads(path.read_bytes())
    value["sha256"] = "b" * 64
    write_json(path, value)
    with pytest.raises(api.PdfUnitBindingError, match="source/document"):
        with api.hold_pdf_unit_binding(bound_unit_run):
            pass
