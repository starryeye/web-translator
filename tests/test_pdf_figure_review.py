from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import json

import pdfplumber
import pytest
from reportlab.pdfbase.pdfmetrics import stringWidth

from tests.pdf_figure_review_fixtures import FIGURE_ID, LABEL_LINES, make_figure_review_run
from web_translator.pdf_models import PdfDocument


def api():
    return importlib.import_module("web_translator.pdf_figure_review")


def build(run):
    document_bytes = (run / "document.json").read_bytes()
    document = PdfDocument.from_dict(json.loads(document_bytes))
    return api().build_pdf_figure_text_input(document, document_bytes=document_bytes,
        source_pdf_bytes=(run / "source.pdf").read_bytes(),
        media_payloads={b.id: (run / b.media_path).read_bytes() for b in document.blocks if b.kind == "figure"})


def publication_case(root, *, case="diagram", evidence=True):
    """TEST ONLY fixed synthetic artwork decisions, not a semantic classifier."""
    from tests.pdf_unit_fixtures import rebind_native_fixture
    from tests.test_pdf_qa import PdfQARun, _write_review
    from web_translator.models import Segment, Translation, write_segments
    from web_translator.cli import _zone_payload
    from web_translator.zones import build_zones
    run, output = make_figure_review_run(root, case=case)
    document = PdfDocument.from_dict(json.loads((run / "document.json").read_bytes()))
    if case != "figure-free":
        from PIL import Image
        from web_translator.pdf_media import render_pdf_pages
        page = render_pdf_pages(run / "source.pdf", root / "publication-source-views", dpi=144)[0]
        with Image.open(page) as image:
            image.crop((144, 164, 1080, 384)).save(run / "media/figure-0001.png")
    block = document.blocks[0]
    segment = Segment("seg-000001", block.id, "paragraph", [], block.source_text, [], [], True)
    write_segments(run / "segments.jsonl", [segment])
    (run / "zones").mkdir()
    for zone in build_zones([segment]):
        (run / "zones" / f"{zone.id}.json").write_text(json.dumps(_zone_payload(zone)))
    rebind_native_fixture(run)
    (run / "glossary.json").write_text("{}\n")
    translation = Translation("seg-000001", "독립적인 본문은 원본 그림 밖에서 선택 가능한 한국어로 보존됩니다.", "TEST ONLY", {})
    (run / "translations").mkdir()
    (run / "translations/zone-001.jsonl").write_text(json.dumps(translation.to_dict(), ensure_ascii=False) + "\n")
    if evidence or case != "figure-free":
        api().write_pdf_figure_review_input(run)
    _write_review(run)
    if not evidence and case != "figure-free":
        (run / "figure-text-input.json").unlink()
    if evidence:
        master_path = run / "review.json"
        master = json.loads(master_path.read_bytes())
        payload = (run / "figure-text-input.json").read_bytes()
        labels = []
        offset = 0
        for _, text in LABEL_LINES[case]:
            labels.append({"character_indexes": [offset + i for i, char in enumerate(text) if char != " "],
                           "text": text.replace(" ", ""), "reason": "TEST ONLY inspected synthetic graph node or arrow label."})
            offset += len(text)
        master["figure_text_review"] = {"schema_version": "1.0", "inventory_sha256": hashlib.sha256(payload).hexdigest(),
            "figures": {} if case == "figure-free" else {FIGURE_ID: {
                "verdict": "required-fix" if case in {"numeric-prose", "colon-prose", "indent-20", "indent-36"} else "pass",
                "evidence": "TEST ONLY page 1: artwork inspected against the independent body above; numeric/colon/indented paragraph must be translated, not preserved.",
                "labels": labels}}}
        master_path.write_text(json.dumps(master, ensure_ascii=False) + "\n")
    return PdfQARun(run, output), {translation.segment_id: translation}


def test_figure_input_is_in_semantic_digest(tmp_path):
    from web_translator.pdf_review import build_pdf_semantic_review_input
    run, _ = publication_case(tmp_path)
    first = build_pdf_semantic_review_input(run.run_dir)
    record = next((f for f in first.files if f.path == "figure-text-input.json"), None)
    assert record is not None, "figure inventory is missing from held semantic evidence"
    payload = (run.run_dir / "figure-text-input.json").read_bytes()
    assert (record.sha256, record.byte_length) == (hashlib.sha256(payload).hexdigest(), len(payload))
    (run.run_dir / "figure-text-input.json").write_bytes(payload + b" ")
    assert build_pdf_semantic_review_input(run.run_dir).semantic_input_sha256 != first.semantic_input_sha256


@pytest.mark.parametrize("entry", ["direct", "cli"])
def test_figure_review_missing_inventory_refuses_assembly(tmp_path, entry):
    from web_translator.pdf_assemble import assemble_pdf, PdfAssemblyError
    from web_translator.cli import main
    run, translations = publication_case(tmp_path, evidence=False)
    if entry == "direct":
        with pytest.raises(PdfAssemblyError, match="figure|inventory"):
            assemble_pdf(run.run_dir, translations, {}, run.output_dir)
    else:
        assert main(["pdf-assemble", "--run-dir", str(run.run_dir), "--output-dir", str(run.output_dir)]) != 0
    assert not (run.run_dir / "staged-output").exists()
    assert not (run.run_dir / "layout.json").exists()
    assert not run.output_dir.exists()


def mutate_figure_evidence(run, case):
    from tests.pdf_unit_fixtures import rebind_native_fixture
    from web_translator.pdf_review import build_pdf_semantic_review_input
    master_path = run / "review.json"
    master = json.loads(master_path.read_bytes())
    inventory_path = run / "figure-text-input.json"
    inventory = json.loads(inventory_path.read_bytes())
    figure = master["figure_text_review"]["figures"][FIGURE_ID]
    label = figure["labels"][0] if figure["labels"] else None
    if case == "missing-inventory": inventory_path.unlink()
    elif case == "missing-review": master.pop("figure_text_review")
    elif case == "foreign-figure": master["figure_text_review"]["figures"]["pdf:page-0001:block-9999"] = deepcopy(figure)
    elif case == "omitted-character": label.update(character_indexes=[0, 1], text="AP")
    elif case == "duplicate-character": figure["labels"].append(deepcopy(label))
    elif case == "changed-text": inventory["figures"][0]["characters"][0]["text"] = "X"; label["text"] = "XPI"
    elif case == "changed-bbox": inventory["figures"][0]["characters"][0]["bbox"][0] += 0.1
    elif case == "stale-source": (run / "source.pdf").write_bytes((run / "source.pdf").read_bytes() + b"\n")
    elif case == "stale-document":
        path = run / "document.json"; path.write_bytes(path.read_bytes() + b" "); rebind_native_fixture(run)
    elif case == "stale-media": (run / "media/figure-0001.png").write_bytes((run / "media/figure-0001.png").read_bytes() + b" ")
    elif case == "stale-inventory": master["figure_text_review"]["inventory_sha256"] = "f" * 64
    elif case == "required-fix": figure["verdict"] = "required-fix"
    if case in {"changed-text", "changed-bbox"}:
        payload = (json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        inventory_path.write_bytes(payload)
        master["figure_text_review"]["inventory_sha256"] = hashlib.sha256(payload).hexdigest()
    # Reapprove the outer digest where possible; source recomputation is independent.
    if case != "missing-inventory":
        master["semantic_input_sha256"] = build_pdf_semantic_review_input(run).semantic_input_sha256
    master_path.write_text(json.dumps(master, ensure_ascii=False) + "\n")


INVALID_PUBLICATION = ["missing-inventory", "missing-review", "foreign-figure", "omitted-character", "duplicate-character",
    "changed-text", "changed-bbox", "stale-source", "stale-document", "stale-media", "stale-inventory", "required-fix"]


@pytest.mark.parametrize("case", INVALID_PUBLICATION)
def test_figure_review_direct_assembly_refuses_invalid_evidence(tmp_path, case):
    from web_translator.pdf_assemble import assemble_pdf, PdfAssemblyError
    run, translations = publication_case(tmp_path)
    mutate_figure_evidence(run.run_dir, case)
    with pytest.raises(PdfAssemblyError):
        assemble_pdf(run.run_dir, translations, {}, run.output_dir)
    assert not (run.run_dir / "staged-output").exists()
    assert not (run.run_dir / "layout.json").exists()
    assert not run.output_dir.exists()


@pytest.mark.parametrize("case", ["diagram", "sentence-labels", "empty-figure", "figure-free"])
def test_figure_review_prepare_accepts_only_explicit_fixed_approval(tmp_path, case):
    from web_translator.pdf_assemble import assemble_pdf
    from web_translator.pdf_qa import prepare_pdf_qa
    run, translations = publication_case(tmp_path, case=case)
    assemble_pdf(run.run_dir, translations, {}, run.output_dir)
    qa = prepare_pdf_qa(run.run_dir, run.output_dir)
    inventory = (run.run_dir / "figure-text-input.json").read_bytes()
    finding = next(f for f in qa.findings if f.code == "publication.text_image_separation")
    assert "inventory_sha256=" + hashlib.sha256(inventory).hexdigest() in finding.evidence
    assert "review_sha256=" + hashlib.sha256((run.run_dir / "review.json").read_bytes()).hexdigest() in finding.evidence
    assert set(qa.metrics) == {"contact_sheet_count", "embedded_font_count", "figure_count", "link_count",
        "output_page_count", "rendered_page_count", "translated_block_count", "translation_unit_count"}


def test_figure_review_report_parser_retains_exact_groups(tmp_path):
    from web_translator.pdf_report import _semantic_review_from_value
    run, _ = publication_case(tmp_path)
    master = json.loads((run.run_dir / "review.json").read_bytes())
    result = _semantic_review_from_value(master, {"zone-001"}, {"zone-001": 0})
    assert result["figure_text_review"] == master["figure_text_review"]
    master["preserved_names"] = {}
    result = _semantic_review_from_value(master, {"zone-001"}, {"zone-001": 0})
    assert result["preserved_names"] == {}
    assert result["figure_text_review"] == master["figure_text_review"]


@pytest.fixture
def prepared_figure_publication(tmp_path):
    from tests.test_pdf_qa import _write_passing_layout_review
    from web_translator.pdf_assemble import assemble_pdf
    from web_translator.pdf_qa import prepare_pdf_qa
    run, translations = publication_case(tmp_path)
    assemble_pdf(run.run_dir, translations, {}, run.output_dir)
    prepare_pdf_qa(run.run_dir, run.output_dir)
    _write_passing_layout_review(run.run_dir)
    return run


@pytest.mark.parametrize("change", ["reason", "evidence", "verdict", "whitespace"])
def test_figure_review_changed_review_refuses_unchanged_pdf(prepared_figure_publication, change):
    from web_translator.pdf_qa import finalize_pdf_output, PdfQAFailure
    from web_translator.pdf_review import build_pdf_semantic_review_input
    run = prepared_figure_publication
    staged = run.run_dir / "staged-output/translated.pdf"
    original = staged.read_bytes()
    path = run.run_dir / "review.json"
    semantic_digest = build_pdf_semantic_review_input(run.run_dir).semantic_input_sha256
    value = json.loads(path.read_bytes())
    figure = value["figure_text_review"]["figures"][FIGURE_ID]
    if change == "reason": figure["labels"][0]["reason"] += " Revised after QA."
    elif change == "evidence": figure["evidence"] += " Revised after QA."
    elif change == "verdict": figure["verdict"] = "required-fix"
    payload = json.dumps(value, ensure_ascii=False) + "\n" + (" " if change == "whitespace" else "")
    path.write_text(payload)
    assert build_pdf_semantic_review_input(run.run_dir).semantic_input_sha256 == semantic_digest
    with pytest.raises(PdfQAFailure): finalize_pdf_output(run.run_dir, run.output_dir)
    assert staged.read_bytes() == original
    assert not run.output_dir.exists()
    assert sorted(p.name for p in staged.parent.iterdir()) == ["translated.pdf"]


@pytest.mark.parametrize("evidence", [False, True])
def test_figure_free_legacy_or_optional_empty_review_round_trips(tmp_path, evidence):
    from tests.test_pdf_qa import _write_passing_layout_review
    from web_translator.pdf_assemble import assemble_pdf
    from web_translator.pdf_qa import prepare_pdf_qa, finalize_pdf_output
    from web_translator.pdf_report import PdfFinalManifest, _semantic_review_from_value
    run, translations = publication_case(tmp_path, case="figure-free", evidence=evidence)
    master = json.loads((run.run_dir / "review.json").read_bytes())
    expected = _semantic_review_from_value(master, {"zone-001"}, {"zone-001": 0})
    assert ("figure_text_review" in expected) is evidence
    assemble_pdf(run.run_dir, translations, {}, run.output_dir)
    prepare_pdf_qa(run.run_dir, run.output_dir)
    _write_passing_layout_review(run.run_dir)
    finalize_pdf_output(run.run_dir, run.output_dir)
    manifest = json.loads((run.output_dir / "manifest.json").read_bytes())
    assert PdfFinalManifest.from_dict(manifest).to_dict() == manifest
    assert manifest["translation"]["master_semantic_review"] == expected
    files = {record["path"] for record in manifest["semantic_input"]["files"]}
    assert ("figure-text-input.json" in files) is evidence


def test_figure_review_final_manifest_and_report_retain_complete_evidence(prepared_figure_publication):
    from web_translator.pdf_qa import finalize_pdf_output
    from web_translator.pdf_report import PdfFinalManifest
    run = prepared_figure_publication
    original_review = json.loads((run.run_dir / "review.json").read_bytes())["figure_text_review"]
    finalize_pdf_output(run.run_dir, run.output_dir)
    manifest = json.loads((run.output_dir / "manifest.json").read_bytes())
    assert PdfFinalManifest.from_dict(manifest).to_dict() == manifest
    assert manifest["translation"]["master_semantic_review"]["figure_text_review"] == original_review
    record = next(f for f in manifest["semantic_input"]["files"] if f["path"] == "figure-text-input.json")
    assert record["sha256"] == original_review["inventory_sha256"]
    assert record["byte_length"] == len((run.run_dir / "figure-text-input.json").read_bytes())
    report = (run.output_dir / "review-report.md").read_text()
    embedded = json.loads(report.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert embedded == manifest
    human = report.split("```json\n", 1)[0]
    for expected in [FIGURE_ID, "API", "Intrinsic", original_review["inventory_sha256"]]:
        if expected == "Intrinsic": expected = original_review["figures"][FIGURE_ID]["labels"][0]["reason"]
        assert expected in human
    from web_translator.pdf_qa import PdfQAFailure
    for mutation in ["foreign-page", "translated-block", "inventory-hash", "required-fix", "extra-label-field", "duplicate-index"]:
        bad = deepcopy(manifest)
        evidence = bad["translation"]["master_semantic_review"]["figure_text_review"]
        figure = evidence["figures"][FIGURE_ID]
        if mutation == "foreign-page": evidence["figures"] = {"pdf:page-0002:block-0002": figure}
        elif mutation == "translated-block": evidence["figures"] = {"pdf:page-0001:block-0001": figure}
        elif mutation == "inventory-hash": evidence["inventory_sha256"] = "f" * 64
        elif mutation == "required-fix": figure["verdict"] = "required-fix"
        elif mutation == "extra-label-field": figure["labels"][0]["bbox"] = [0, 0, 1, 1]
        elif mutation == "duplicate-index": figure["labels"].append(deepcopy(figure["labels"][0]))
        with pytest.raises(PdfQAFailure): PdfFinalManifest.from_dict(bad)


@pytest.mark.parametrize("stage", ["prepare", "finalize"])
@pytest.mark.parametrize("case", INVALID_PUBLICATION)
def test_figure_review_later_stage_refuses_invalid_preserving_evidence(prepared_figure_publication, stage, case):
    from web_translator.pdf_qa import prepare_pdf_qa, finalize_pdf_output, PdfQAFailure
    run = prepared_figure_publication
    staged = run.run_dir / "staged-output/translated.pdf"
    retained = {p.relative_to(run.run_dir): p.read_bytes() for p in [staged, run.run_dir / "pdf-qa.json", *sorted((run.run_dir / "qa-pages").iterdir())]}
    mutate_figure_evidence(run.run_dir, case)
    action = prepare_pdf_qa if stage == "prepare" else finalize_pdf_output
    with pytest.raises(PdfQAFailure): action(run.run_dir, run.output_dir)
    assert not run.output_dir.exists()
    assert all((run.run_dir / path).read_bytes() == payload for path, payload in retained.items())
    assert sorted(p.name for p in staged.parent.iterdir()) == ["translated.pdf"]


@pytest.mark.parametrize("case", ["numeric-prose", "colon-prose", "indent-20", "indent-36", "empty-figure"])
@pytest.mark.parametrize("evidence", [False, True])
def test_figure_review_numeric_colon_indent_prose_and_empty_require_decision(tmp_path, case, evidence):
    from web_translator.pdf_assemble import assemble_pdf, PdfAssemblyError
    run, translations = publication_case(tmp_path, case=case, evidence=evidence)
    if evidence and case == "empty-figure":
        assert assemble_pdf(run.run_dir, translations, {}, run.output_dir).is_file()
    else:
        with pytest.raises(PdfAssemblyError): assemble_pdf(run.run_dir, translations, {}, run.output_dir)
        assert not (run.run_dir / "staged-output").exists()


@pytest.mark.parametrize("stage", ["prepare", "finalize"])
@pytest.mark.parametrize("case,evidence", [
    (case, evidence) for case in ["numeric-prose", "colon-prose", "indent-20", "indent-36"]
    for evidence in [False, True]
] + [("empty-figure", False)])
def test_figure_review_prose_later_stages_never_automatically_approve(
    prepared_figure_publication, tmp_path, stage, case, evidence,
):
    import shutil
    from tests.pdf_unit_fixtures import rebind_native_fixture
    from web_translator.pdf_review import build_pdf_semantic_review_input
    from web_translator.pdf_qa import prepare_pdf_qa, finalize_pdf_output, PdfQAFailure
    run = prepared_figure_publication
    candidate, _ = publication_case(tmp_path / "new-case", case=case, evidence=evidence)
    staged = run.run_dir / "staged-output/translated.pdf"
    old_pdf, old_qa = staged.read_bytes(), (run.run_dir / "pdf-qa.json").read_bytes()
    for name in ["source.pdf", "source.json", "document.json", "media/figure-0001.png", "review.json"]:
        shutil.copyfile(candidate.run_dir / name, run.run_dir / name)
    inventory_path = run.run_dir / "figure-text-input.json"
    if evidence: shutil.copyfile(candidate.run_dir / "figure-text-input.json", inventory_path)
    else: inventory_path.unlink()
    rebind_native_fixture(run.run_dir)
    if evidence:
        path = run.run_dir / "review.json"
        master = json.loads(path.read_bytes())
        master["semantic_input_sha256"] = build_pdf_semantic_review_input(run.run_dir).semantic_input_sha256
        path.write_text(json.dumps(master, ensure_ascii=False) + "\n")
    action = prepare_pdf_qa if stage == "prepare" else finalize_pdf_output
    with pytest.raises(PdfQAFailure): action(run.run_dir, run.output_dir)
    assert staged.read_bytes() == old_pdf
    assert (run.run_dir / "pdf-qa.json").read_bytes() == old_qa
    assert not run.output_dir.exists()


def test_figure_review_horizontally_separated_sentence_labels_pass_exact_approval(tmp_path, monkeypatch):
    from reportlab.pdfgen.canvas import Canvas
    from web_translator.pdf_assemble import assemble_pdf
    from web_translator.pdf_qa import prepare_pdf_qa
    original = Canvas.drawString
    def place_second_node(canvas, x, y, text, *args, **kwargs):
        if text == "Responses leave after processing.": x, y = 330, 680
        return original(canvas, x, y, text, *args, **kwargs)
    # Only source construction changes; assertions inspect the real resulting PDF.
    with monkeypatch.context() as source_layout:
        source_layout.setattr(Canvas, "drawString", place_second_node)
        run, translations = publication_case(tmp_path, case="sentence-labels")
    master_path = run.run_dir / "review.json"
    original_review = master_path.read_bytes()
    master = json.loads(original_review)
    master.pop("figure_text_review")
    master_path.write_text(json.dumps(master))
    from web_translator.pdf_assemble import PdfAssemblyError
    with pytest.raises(PdfAssemblyError): assemble_pdf(run.run_dir, translations, {}, run.output_dir)
    master_path.write_bytes(original_review)
    with pdfplumber.open(run.run_dir / "source.pdf") as pdf:
        assert pdf.pages[0].chars[0]["top"] == pdf.pages[0].chars[len(LABEL_LINES["sentence-labels"][0][1])]["top"]
        assert pdf.pages[0].chars[len(LABEL_LINES["sentence-labels"][0][1])]["x0"] == 330
    assemble_pdf(run.run_dir, translations, {}, run.output_dir)
    assert prepare_pdf_qa(run.run_dir, run.output_dir).passed


@pytest.mark.parametrize("stage", ["assembly", "prepare", "finalize"])
def test_figure_review_approval_cannot_bypass_translatable_geometry_overlap(tmp_path, stage):
    from tests.pdf_unit_fixtures import rebind_native_fixture
    from tests.test_pdf_qa import _write_passing_layout_review
    from web_translator.pdf_assemble import assemble_pdf, PdfAssemblyError
    from web_translator.pdf_qa import prepare_pdf_qa, finalize_pdf_output, PdfQAFailure
    from web_translator.pdf_review import build_pdf_semantic_review_input
    run, translations = publication_case(tmp_path)
    if stage != "assembly": assemble_pdf(run.run_dir, translations, {}, run.output_dir)
    if stage == "finalize":
        prepare_pdf_qa(run.run_dir, run.output_dir)
        _write_passing_layout_review(run.run_dir)
    document_path = run.run_dir / "document.json"
    document = json.loads(document_path.read_bytes())
    document["blocks"][0]["bbox"] = [72., 32., 540., 305.]
    document_path.write_text(json.dumps(document) + "\n")
    rebind_native_fixture(run.run_dir)
    inventory_path = run.run_dir / "figure-text-input.json"
    inventory = json.loads(inventory_path.read_bytes())
    inventory["document_sha256"] = hashlib.sha256(document_path.read_bytes()).hexdigest()
    payload = api().canonical_figure_text_input_bytes(inventory)
    inventory_path.write_bytes(payload)
    review_path = run.run_dir / "review.json"
    master = json.loads(review_path.read_bytes())
    master["figure_text_review"]["inventory_sha256"] = hashlib.sha256(payload).hexdigest()
    master["semantic_input_sha256"] = build_pdf_semantic_review_input(run.run_dir).semantic_input_sha256
    review_path.write_text(json.dumps(master, ensure_ascii=False) + "\n")
    with pytest.raises(PdfAssemblyError if stage == "assembly" else PdfQAFailure, match="overlap"):
        if stage == "assembly": assemble_pdf(run.run_dir, translations, {}, run.output_dir)
        else: (prepare_pdf_qa if stage == "prepare" else finalize_pdf_output)(run.run_dir, run.output_dir)
    assert not run.output_dir.exists()


@pytest.mark.parametrize("stage", ["assembly", "prepare", "finalize"])
@pytest.mark.parametrize("artifact", ["source.pdf", "media/figure-0001.png", "figure-text-input.json", "review.json"])
@pytest.mark.parametrize("mutation", ["identity", "content"])
def test_figure_review_held_identity_and_content_races_refuse(tmp_path, monkeypatch, stage, artifact, mutation):
    import web_translator.pdf_assemble as assembly
    import web_translator.pdf_qa as qa
    from tests.test_pdf_qa import _write_passing_layout_review
    run, translations = publication_case(tmp_path)
    if stage != "assembly": assembly.assemble_pdf(run.run_dir, translations, {}, run.output_dir)
    if stage == "finalize":
        qa.prepare_pdf_qa(run.run_dir, run.output_dir)
        _write_passing_layout_review(run.run_dir)
    path = run.run_dir / artifact
    payload = path.read_bytes()
    fired = False
    def mutate():
        nonlocal fired
        if fired: return
        fired = True
        if mutation == "identity":
            path.rename(run.run_dir / ("saved-" + path.name))
            path.write_bytes(payload)
        else: path.write_bytes(payload + b" ")
    owner, hook = (assembly, "_build_rich_document") if stage == "assembly" else (
        (qa, "render_pdf_pages") if stage == "prepare" else (qa, "_rename_anchored_directory_no_replace"))
    original = getattr(owner, hook)
    def raced(*args, **kwargs):
        if stage == "finalize": mutate()  # immediately before final rename
        result = original(*args, **kwargs)
        if stage != "finalize": mutate()
        return result
    monkeypatch.setattr(owner, hook, raced)
    expected_error = assembly.PdfAssemblyError if stage == "assembly" else qa.PdfQAFailure
    with pytest.raises(expected_error):
        if stage == "assembly": assembly.assemble_pdf(run.run_dir, translations, {}, run.output_dir)
        elif stage == "prepare": qa.prepare_pdf_qa(run.run_dir, run.output_dir)
        else: qa.finalize_pdf_output(run.run_dir, run.output_dir)
    assert fired
    assert not run.output_dir.exists()
    assert (path.read_bytes() == payload) if mutation == "identity" else (path.read_bytes() == payload + b" ")
    if stage == "assembly": assert not (run.run_dir / "staged-output").exists()
    else: assert list(run.run_dir.rglob("translated.pdf")), "staged PDF must remain recoverable"
    if stage == "finalize": assert (run.run_dir / "pdf-qa.json").exists()


@pytest.mark.parametrize("missing", ["inventory", "review"])
def test_figure_review_optional_empty_pair_cannot_be_split(tmp_path, missing):
    from web_translator.pdf_assemble import assemble_pdf, PdfAssemblyError
    from web_translator.pdf_review import build_pdf_semantic_review_input
    run, translations = publication_case(tmp_path, case="figure-free")
    path = run.run_dir / "review.json"
    master = json.loads(path.read_bytes())
    if missing == "inventory":
        (run.run_dir / "figure-text-input.json").unlink()
        master["semantic_input_sha256"] = build_pdf_semantic_review_input(run.run_dir).semantic_input_sha256
    else: master.pop("figure_text_review")
    path.write_text(json.dumps(master) + "\n")
    with pytest.raises(PdfAssemblyError): assemble_pdf(run.run_dir, translations, {}, run.output_dir)
    assert not (run.run_dir / "staged-output").exists()


def test_figure_review_duplicate_review_fields_refuse_direct_assembly(tmp_path):
    from web_translator.pdf_assemble import assemble_pdf, PdfAssemblyError
    run, translations = publication_case(tmp_path)
    path = run.run_dir / "review.json"
    payload = path.read_text().replace('"verdict": "pass", "evidence": "TEST ONLY page 1',
        '"verdict": "required-fix", "verdict": "pass", "evidence": "TEST ONLY page 1')
    assert '"verdict": "required-fix", "verdict": "pass"' in payload
    path.write_text(payload)
    with pytest.raises(PdfAssemblyError): assemble_pdf(run.run_dir, translations, {}, run.output_dir)
    assert not (run.run_dir / "staged-output").exists()


def test_figure_review_direct_assembly_cannot_hide_required_semantic_finding(tmp_path):
    from web_translator.pdf_assemble import assemble_pdf, PdfAssemblyError
    run, translations = publication_case(tmp_path)
    path = run.run_dir / "review.json"
    master = json.loads(path.read_bytes())
    master["section_findings"]["zone-001"][0]["verdict"] = "required-fix"
    path.write_text(json.dumps(master, ensure_ascii=False) + "\n")
    with pytest.raises(PdfAssemblyError): assemble_pdf(run.run_dir, translations, {}, run.output_dir)
    assert not (run.run_dir / "staged-output").exists()


@pytest.fixture
def approved_figure_case(tmp_path):
    run, _ = make_figure_review_run(tmp_path)
    x = 90.
    characters = []
    for index, text in enumerate("API"):
        width = stringWidth(text, "Helvetica", 12)
        characters.append({"index": index, "text": text, "bbox": [x, 102.48400000000004, x + width, 114.48400000000004]})
        x += width
    inventory = {"schema_version": "1.0", "source_sha256": hashlib.sha256((run / "source.pdf").read_bytes()).hexdigest(),
                 "document_sha256": hashlib.sha256((run / "document.json").read_bytes()).hexdigest(), "figures": [{
                     "block_id": FIGURE_ID, "page_number": 1, "bbox": [72., 82., 540., 192.],
                     "media_sha256": hashlib.sha256((run / "media/figure-0001.png").read_bytes()).hexdigest(), "characters": characters}]}
    # Independent known font metrics and literal source geometry, never the builder.
    inventory_bytes = (json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    review = {"schema_version": "1.0", "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(), "figures": {
        FIGURE_ID: {"verdict": "pass", "evidence": "Page 1: API is the node label inside the graph; body is above.", "labels": [
            {"character_indexes": [0, 1, 2], "text": "API", "reason": "Intrinsic graph node name."}]}}}
    return inventory_bytes, review


def test_fixed_inventory_bytes(tmp_path, approved_figure_case):
    run, _ = make_figure_review_run(tmp_path / "second")
    assert api().canonical_figure_text_input_bytes(build(run)) == approved_figure_case[0]


def test_exact_approved_label(approved_figure_case):
    inventory_bytes, review = approved_figure_case
    label = api().validate_pdf_figure_text_review(review, inventory_bytes=inventory_bytes)["figures"][FIGURE_ID]["labels"][0]
    assert label["character_indexes"] == [0, 1, 2]
    assert label["text"] == "API"


@pytest.mark.parametrize("case", ["numeric-prose", "colon-prose", "indent-20", "indent-36", "sentence-labels"])
def test_all_visible_glyphs_are_inventory_not_inferred_labels(tmp_path, case):
    run, _ = make_figure_review_run(tmp_path, case=case)
    records = build(run)["figures"][0]["characters"]
    assert "".join(c["text"] for c in records) == "".join(text.replace(" ", "") for _, text in LABEL_LINES[case])
    with pdfplumber.open(run / "source.pdf") as pdf:
        expected = [{"index": i, "text": c["text"], "bbox": [c["x0"], c["top"], c["x1"], c["bottom"]]}
                    for i, c in enumerate(pdf.pages[0].chars) if c["text"].strip()
                    and 72 <= c["x0"] <= c["x1"] <= 540 and 82 <= c["top"] <= c["bottom"] <= 192]
    assert records == expected


def test_sentence_shaped_labels_pass_only_exact_manual_groups(tmp_path):
    run, _ = make_figure_review_run(tmp_path, case="sentence-labels")
    inventory_bytes = api().canonical_figure_text_input_bytes(build(run))
    # Hand-specified two actual graph labels; indexes count source spaces, not labels.
    labels = []
    offset = 0
    for _, text in LABEL_LINES["sentence-labels"]:
        labels.append({"character_indexes": [offset + i for i, c in enumerate(text) if c != " "],
                       "text": text.replace(" ", ""), "reason": "TEST ONLY graph arrow label, visually distinct from body above."})
        offset += len(text)
    review = {"schema_version": "1.0", "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(), "figures": {
        FIGURE_ID: {"verdict": "pass", "evidence": "TEST ONLY page 1: two graph arrow labels below separate prose.", "labels": labels}}}
    assert api().validate_pdf_figure_text_review(review, inventory_bytes=inventory_bytes) == review


def test_passing_partial_coverage_still_refuses_in_diagnostic_mode(approved_figure_case):
    payload, review = approved_figure_case
    review["figures"][FIGURE_ID]["labels"][0].update(character_indexes=[0], text="A")
    with pytest.raises(api().PdfFigureReviewError):
        api().validate_pdf_figure_text_review(review, inventory_bytes=payload, require_pass=False)


@pytest.mark.parametrize("mutation", ["missing", "foreign", "duplicate", "bool", "unordered", "text", "evidence", "reason", "extra", "digest", "required-fix", "figure"])
def test_invalid_approval_refuses(approved_figure_case, mutation):
    inventory_bytes, review = approved_figure_case
    figure = review["figures"][FIGURE_ID]
    label = figure["labels"][0]
    if mutation == "missing": label.update(character_indexes=[0, 1], text="AP")
    elif mutation == "foreign": label["character_indexes"] = [0, 1, 99]
    elif mutation == "duplicate": figure["labels"].append(deepcopy(label))
    elif mutation == "bool": label["character_indexes"] = [False, 1, 2]
    elif mutation == "unordered": label["character_indexes"] = [2, 1, 0]
    elif mutation == "text": label["text"] = "Api"
    elif mutation in {"evidence", "reason"}: (figure if mutation == "evidence" else label)[mutation] = " "
    elif mutation == "extra": label["bbox"] = [0, 0, 1, 1]
    elif mutation == "digest": review["inventory_sha256"] = "f" * 64
    elif mutation == "required-fix": figure["verdict"] = "required-fix"
    elif mutation == "figure": review["figures"]["pdf:page-0001:block-9999"] = deepcopy(figure)
    with pytest.raises(api().PdfFigureReviewError):
        api().validate_pdf_figure_text_review(review, inventory_bytes=inventory_bytes)


def test_diagnostic_required_fix_allows_partial_but_valid_text(approved_figure_case):
    payload, review = approved_figure_case
    figure = review["figures"][FIGURE_ID]
    figure.update(verdict="required-fix", labels=[{"character_indexes": [0], "text": "A", "reason": "Absorbed prose."}])
    assert api().validate_pdf_figure_text_review(review, inventory_bytes=payload, require_pass=False) == review
    figure["labels"][0]["text"] = "X"
    with pytest.raises(api().PdfFigureReviewError):
        api().validate_pdf_figure_text_review(review, inventory_bytes=payload, require_pass=False)


@pytest.mark.parametrize("mutation", ["duplicate-index", "bool", "unordered", "page", "nan", "backwards", "extra", "space", "outside"])
def test_invalid_inventory_refuses(approved_figure_case, mutation):
    value = json.loads(approved_figure_case[0])
    figure = value["figures"][0]
    if mutation == "duplicate-index": figure["characters"].append(deepcopy(figure["characters"][0]))
    elif mutation == "bool": figure["characters"][0]["index"] = False
    elif mutation == "unordered": figure["characters"].reverse()
    elif mutation == "page": figure["page_number"] = 2
    elif mutation == "nan": figure["bbox"][0] = float("nan")
    elif mutation == "backwards": figure["bbox"] = [10, 10, 1, 1]
    elif mutation == "extra": figure["characters"][0]["extra"] = True
    elif mutation == "space": figure["characters"][0]["text"] = " \t"
    elif mutation == "outside": figure["characters"][0]["bbox"][0] = 1
    with pytest.raises(api().PdfFigureReviewError): api().parse_pdf_figure_text_input(value)


def test_exact_unicode_glyph_strings_survive(approved_figure_case):
    value = json.loads(approved_figure_case[0])
    value["figures"][0]["characters"][0]["text"] = " e\u0301한 "
    encoded = api().canonical_figure_text_input_bytes(value)
    assert " e\u0301한 " in encoded.decode()
    assert api().parse_pdf_figure_text_input(json.loads(encoded)) == value


@pytest.mark.parametrize("glyph_text", [" \t", " e\u0301한 "])
def test_source_glyph_whitespace_and_unicode_strings_survive_exactly(tmp_path, glyph_text):
    import reportlab
    from pathlib import Path
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen.canvas import Canvas
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    run, _ = make_figure_review_run(tmp_path)
    body = json.loads((run / "document.json").read_bytes())["blocks"][0]["source_text"]
    pdfmetrics.registerFont(TTFont("TestOnlyVera", str(Path(reportlab.__file__).parent / "fonts/Vera.ttf")))
    canvas = Canvas(str(run / "source.pdf"), pagesize=(612, 792), invariant=1)
    canvas.setFont("TestOnlyVera", 12)
    canvas.drawString(90, 680, "API")
    canvas.rect(72, 600, 468, 110)
    canvas.line(80, 610, 520, 690)
    for index in range(3): canvas.drawString(72, 750 - index * 14, body[index * (len(body) // 3):(index + 1) * (len(body) // 3)])
    canvas.save()
    reader = PdfReader(run / "source.pdf")
    # Real PDF ToUnicode glyph mapping: one visible glyph may map to several codepoints.
    stream = DecodedStreamObject()
    mappings = [f"<{n:02x}> <{(glyph_text if n == ord('A') else chr(n)).encode('utf-16-be').hex()}>" for n in range(32, 127)]
    stream.set_data(("/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        "/CMapName /TestOnlyUnicode def /CMapType 2 def\n"
        "1 begincodespacerange <00> <ff> endcodespacerange\n95 beginbfchar\n"
        + "\n".join(mappings) + "\nendbfchar endcmap CMapName currentdict /CMap defineresource pop end end").encode())
    font = next(value.get_object() for value in reader.pages[0]["/Resources"]["/Font"].values() if value.get_object()["/Subtype"] == "/TrueType")
    font[NameObject("/ToUnicode")] = stream
    writer = PdfWriter(); writer.add_page(reader.pages[0])
    with (run / "source.pdf").open("wb") as output: writer.write(output)
    digest = hashlib.sha256((run / "source.pdf").read_bytes()).hexdigest()
    source_path = run / "source.json"
    source = json.loads(source_path.read_bytes()); source.update(sha256=digest, byte_length=(run / "source.pdf").stat().st_size)
    source_path.write_text(json.dumps(source))
    document_path = run / "document.json"
    document = json.loads(document_path.read_bytes()); document["source_sha256"] = digest
    if not glyph_text.strip(): document["selectable_characters"] -= 1
    document_path.write_text(json.dumps(document))
    chars = build(run)["figures"][0]["characters"]
    assert [(c["index"], c["text"]) for c in chars] == ([(0, glyph_text), (1, "P"), (2, "I")] if glyph_text.strip() else [(1, "P"), (2, "I")])


def _write_zero_extent_source(run, extent):
    from reportlab.pdfgen.canvas import Canvas
    document_path = run / "document.json"
    document = json.loads(document_path.read_bytes())
    canvas = Canvas(str(run / "source.pdf"), pagesize=(612, 792), invariant=1)
    obj = canvas.beginText(90, 680)
    obj.setFont("Helvetica", 12)
    if extent == "zero-width": obj.setHorizScale(0)
    else: obj.setTextTransform(1, 0, 0, 0, 90, 680)
    obj.textOut("API")
    obj.setHorizScale(100)
    canvas.drawText(obj)
    canvas.setFont("Helvetica", 12)
    body = document["blocks"][0]["source_text"]
    for index in range(3): canvas.drawString(72, 750 - index * 14, body[index * (len(body) // 3):(index + 1) * (len(body) // 3)])
    canvas.save()
    payload = (run / "source.pdf").read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    document["source_sha256"] = digest
    document_path.write_text(json.dumps(document))
    source_path = run / "source.json"
    source = json.loads(source_path.read_bytes()); source.update(sha256=digest, byte_length=len(payload))
    source_path.write_text(json.dumps(source))


@pytest.mark.parametrize("extent", ["zero-width", "zero-height"])
def test_owned_source_glyph_zero_extents_remain_exact(tmp_path, extent):
    run, _ = make_figure_review_run(tmp_path)
    _write_zero_extent_source(run, extent)
    with pdfplumber.open(run / "source.pdf") as pdf:
        expected = [{"index": index, "text": c["text"], "bbox": [c["x0"], c["top"], c["x1"], c["bottom"]]}
                    for index, c in enumerate(pdf.pages[0].chars[:3])]
    assert [(c["index"], c["text"]) for c in expected] == [(0, "A"), (1, "P"), (2, "I")]
    assert all(c["bbox"][0] == c["bbox"][2] if extent == "zero-width" else c["bbox"][1] == c["bbox"][3] for c in expected)
    assert build(run)["figures"][0]["characters"] == expected


@pytest.mark.parametrize("extent", ["zero-width", "zero-height", "partial-glyph"])
def test_shared_translatable_ownership_refuses_zero_extent_boundaries(tmp_path, extent):
    run, _ = make_figure_review_run(tmp_path)
    if extent != "partial-glyph": _write_zero_extent_source(run, extent)
    path = run / "document.json"
    document = json.loads(path.read_bytes())
    document["blocks"][0]["bbox"] = ([72., 82., 90., 305.] if extent == "zero-width" else
                                      [72., 112., 540., 305.] if extent == "zero-height" else
                                      [72., 82., 94., 305.])
    path.write_text(json.dumps(document))
    with pytest.raises(api().PdfFigureReviewError): build(run)


@pytest.mark.parametrize("extent", ["zero-width", "zero-height"])
def test_shared_duplicate_figure_ownership_refuses_zero_extent_glyphs(tmp_path, extent):
    run, _ = make_figure_review_run(tmp_path)
    _write_zero_extent_source(run, extent)
    path = run / "document.json"
    document = json.loads(path.read_bytes())
    other = deepcopy(document["blocks"][1]); other.update(id="pdf:page-0001:block-0003", order=2)
    document["blocks"].append(other)
    path.write_text(json.dumps(document))
    with pytest.raises(api().PdfFigureReviewError): build(run)


@pytest.mark.parametrize("extent", ["zero-width", "zero-height", "reversed-width", "reversed-height"])
def test_glyph_rectangle_zero_extents_or_reversed_contract(approved_figure_case, extent):
    inventory = json.loads(approved_figure_case[0])
    char_box = inventory["figures"][0]["characters"][0]["bbox"]
    side = 0 if extent.endswith("width") else 1
    char_box[side + 2] = char_box[side] if extent.startswith("zero") else char_box[side] - 1
    if extent.startswith("zero"):
        assert api().parse_pdf_figure_text_input(inventory) == inventory
    else:
        with pytest.raises(api().PdfFigureReviewError): api().parse_pdf_figure_text_input(inventory)


@pytest.mark.parametrize("case", ["partial-glyph", "overlapping-figures", "body-overlap"])
def test_unsafe_whole_glyph_ownership_refuses(tmp_path, case):
    run, _ = make_figure_review_run(tmp_path, case=case)
    with pytest.raises(api().PdfFigureReviewError): build(run)


@pytest.mark.parametrize("case", ["empty-figure", "figure-free"])
def test_empty_artwork_and_figure_free_inventory(tmp_path, case):
    run, _ = make_figure_review_run(tmp_path, case=case)
    assert build(run)["figures"] == ([] if case == "figure-free" else [{
        "block_id": FIGURE_ID, "page_number": 1, "bbox": [72., 82., 540., 192.],
        "media_sha256": hashlib.sha256((run / "media/figure-0001.png").read_bytes()).hexdigest(), "characters": []}])


def test_figure_input_repeat_preserves_identity_and_all_old_artifacts(tmp_path):
    run, _ = make_figure_review_run(tmp_path)
    before = {p.relative_to(run): p.read_bytes() for p in run.rglob("*") if p.is_file()}
    path = api().write_pdf_figure_review_input(run)
    identity = path.stat().st_ino
    payload = path.read_bytes()
    assert api().write_pdf_figure_review_input(run) == path
    assert path.stat().st_ino == identity
    assert payload == api().canonical_figure_text_input_bytes(build(run))
    assert {p: (run / p).read_bytes() for p in before} == before
    assert not (run / "staged-output").exists()


def test_figure_input_differing_existing_file_is_not_overwritten(tmp_path):
    run, _ = make_figure_review_run(tmp_path)
    path = run / "figure-text-input.json"
    path.write_bytes(b"old review evidence\n")
    with pytest.raises(api().PdfFigureReviewError): api().write_pdf_figure_review_input(run)
    assert path.read_bytes() == b"old review evidence\n"


@pytest.mark.parametrize("relative", ["source.pdf", "source.json", "document.json", "media/figure-0001.png", "figure-text-input.json"])
@pytest.mark.parametrize("change", ["replace", "content"])
def test_figure_input_held_evidence_detects_identity_and_content(tmp_path, relative, change):
    run, _ = make_figure_review_run(tmp_path)
    api().write_pdf_figure_review_input(run)
    with api().hold_pdf_figure_inputs(run) as snapshot:
        path = run / relative
        payload = path.read_bytes()
        if change == "replace":
            path.rename(path.with_name("old-" + path.name))
            path.write_bytes(payload)
        else:
            path.write_bytes(payload + b" ")
        with pytest.raises(api().PdfFigureReviewError): snapshot.verify()


@pytest.mark.parametrize("relative", ["source.pdf", "document.json", "media/figure-0001.png", "media", "figure-text-input.json"])
def test_figure_input_linked_evidence_refuses(tmp_path, relative):
    run, _ = make_figure_review_run(tmp_path)
    api().write_pdf_figure_review_input(run)
    path = run / relative
    old = path.with_name("old-" + path.name)
    path.rename(old)
    path.symlink_to(old, target_is_directory=old.is_dir())
    with pytest.raises(api().PdfFigureReviewError): api().write_pdf_figure_review_input(run)


def test_figure_input_held_run_and_media_directory_replacement_refuse(tmp_path):
    run, _ = make_figure_review_run(tmp_path)
    with api().hold_pdf_figure_inputs(run) as snapshot:
        (run / "media").rename(run / "old-media")
        (run / "media").mkdir()
        with pytest.raises(api().PdfFigureReviewError): snapshot.verify()
    run, _ = make_figure_review_run(tmp_path / "replaced-run")
    with api().hold_pdf_figure_inputs(run) as snapshot:
        run.rename(run.with_name("old-run"))
        run.mkdir()
        with pytest.raises(api().PdfFigureReviewError): snapshot.verify()


@pytest.mark.parametrize("mutation", ["source", "source-length", "page", "selectable", "media", "document-bytes", "native"])
def test_source_bound_input_rejects_inconsistent_evidence(tmp_path, mutation):
    run, _ = make_figure_review_run(tmp_path)
    if mutation == "source":
        path = run / "source.pdf"
        path.write_bytes(path.read_bytes().replace(b"API", b"XYZ"))
        # ReportLab compression may hide literal text; guaranteed exact-byte change.
        path.write_bytes(path.read_bytes() + b" \n")
    elif mutation == "source-length":
        path = run / "source.json"
        data = json.loads(path.read_bytes()); data["byte_length"] += 1
        path.write_text(json.dumps(data))
    elif mutation in {"page", "selectable", "native"}:
        path = run / "document.json"
        data = json.loads(path.read_bytes())
        if mutation == "page": data["pages"][0]["width"] = 600.
        elif mutation == "selectable": data["selectable_characters"] += 1
        else: data["extracted_schema_version"] = "1.1"
        path.write_text(json.dumps(data))
    elif mutation == "media": (run / "media/figure-0001.png").unlink()
    elif mutation == "document-bytes": (run / "document.json").write_bytes(b"{}\n")
    with pytest.raises(api().PdfFigureReviewError): api().write_pdf_figure_review_input(run)
    assert not (run / "figure-text-input.json").exists()


@pytest.mark.parametrize("mutation", ["fractional-rotation", "count", "catalog-type", "pages-type"])
def test_source_structure_refuses_even_with_matching_source_digest(tmp_path, mutation):
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import FloatObject, NameObject, NumberObject, TextStringObject
    run, _ = make_figure_review_run(tmp_path, case="figure-free")
    reader = PdfReader(run / "source.pdf")
    writer = PdfWriter(); writer.clone_document_from_reader(reader)
    if mutation == "fractional-rotation": writer.pages[0][NameObject("/Rotate")] = FloatObject(.5)
    elif mutation == "count": writer._root_object["/Pages"][NameObject("/Count")] = NumberObject(2)
    elif mutation == "catalog-type": writer._root_object[NameObject("/Type")] = TextStringObject("/Catalog")
    else: writer._root_object["/Pages"][NameObject("/Type")] = TextStringObject("/Pages")
    with (run / "source.pdf").open("wb") as output: writer.write(output)
    payload = (run / "source.pdf").read_bytes(); digest = hashlib.sha256(payload).hexdigest()
    source_path = run / "source.json"
    source = json.loads(source_path.read_bytes()); source.update(sha256=digest, byte_length=len(payload)); source_path.write_text(json.dumps(source))
    document_path = run / "document.json"
    document = json.loads(document_path.read_bytes()); document["source_sha256"] = digest; document_path.write_text(json.dumps(document))
    with pytest.raises(api().PdfFigureReviewError): api().write_pdf_figure_review_input(run)
    assert not (run / "figure-text-input.json").exists()


@pytest.mark.parametrize("event", ["failure", "source-replace", "media-replace", "run-replace", "racing-inventory", "post-publish-source", "post-publish-inventory"])
def test_figure_input_atomic_publication_failure_and_races(tmp_path, monkeypatch, event):
    import web_translator.pdf_assemble as anchored
    run, _ = make_figure_review_run(tmp_path)
    original = anchored._publish_new_file
    foreign = b"foreign evidence\n"
    def publish(source, source_name, destination, destination_name):
        if event == "failure": raise anchored.PdfAssemblyError("injected publication failure")
        if event in {"source-replace", "media-replace"}:
            path = run / ("source.pdf" if event == "source-replace" else "media/figure-0001.png")
            payload = path.read_bytes(); path.rename(path.with_name("old-" + path.name)); path.write_bytes(payload)
        elif event == "run-replace":
            run.rename(run.with_name("old-run")); run.mkdir()
            (run / "keep").write_bytes(foreign)
        elif event == "racing-inventory": (run / destination_name).write_bytes(foreign)
        result = original(source, source_name, destination, destination_name)
        if event == "post-publish-source":
            path = run / "source.pdf"; path.write_bytes(path.read_bytes() + b" ")
        elif event == "post-publish-inventory":
            path = run / destination_name; path.rename(run / "old-inventory.json"); path.write_bytes(foreign)
        return result
    monkeypatch.setattr(anchored, "_publish_new_file", publish)
    with pytest.raises(api().PdfFigureReviewError): api().write_pdf_figure_review_input(run)
    path = run / "figure-text-input.json"
    if event in {"racing-inventory", "post-publish-inventory"}: assert path.read_bytes() == foreign
    else: assert not path.exists()
    assert not list(run.glob(".figure-text-input-*"))


def test_current_figure_review_recomputes_exact_inventory(tmp_path, approved_figure_case):
    run, _ = make_figure_review_run(tmp_path / "current")
    payload, review = approved_figure_case
    with api().hold_pdf_figure_inputs(run) as snapshot:
        assert api().validate_current_pdf_figure_review(snapshot, inventory_bytes=payload, review_value=review) == review
        changed = json.loads(payload)
        changed["figures"][0]["characters"][0]["bbox"][2] -= 1
        altered = api().canonical_figure_text_input_bytes(changed)
        with pytest.raises(api().PdfFigureReviewError):
            api().validate_current_pdf_figure_review(snapshot, inventory_bytes=altered, review_value=review)
