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
