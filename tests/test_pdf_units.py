"""Logical PDF unit contracts and source-text projection."""

from dataclasses import replace

import pytest

from tests.pdf_unit_fixtures import make_flow_case, make_observed_flow, make_unit_document
from web_translator import pdf_extract
from web_translator.pdf_models import PdfContractError, PdfDocument, PdfFlowFinding, upgrade_pdf_document_to_units
from web_translator.pdf_units import project_unit_text, unit_for_block, validate_unit_membership
from web_translator import pdf_units


def build_flow(case, *, scale=1.0):
    assert hasattr(pdf_units, "build_translation_units"), "logical flow builder is missing"
    blocks, pages, boundaries = make_flow_case(case, scale=scale)
    return blocks, pdf_units.build_translation_units(blocks, pages, boundaries)


@pytest.mark.parametrize("scale", [0.75, 1.0, 1.7])
def test_cross_page_discretionary_word_is_one_unit(scale):
    blocks, (units, findings) = build_flow("discretionary", scale=scale)
    assert not findings
    assert len(units) == 1
    assert units[0].source_block_ids == tuple(b.id for b in blocks)
    assert units[0].segment_id is None
    assert project_unit_text(units[0], {b.id: b for b in blocks}).text == "A paragraph continues."


@pytest.mark.parametrize("case", ["blank-page", "new-indent", "new-list", "nested-list", "heading", "table", "figure", "column-change"])
def test_structural_boundary_is_not_joined(case):
    _, (units, findings) = build_flow(case)
    assert all(len(u.source_block_ids) == 1 for u in units)
    assert not findings


def test_three_page_chain_preserves_order():
    blocks, (units, findings) = build_flow("three-page")
    assert not findings
    assert len(units) == 1
    assert units[0].source_block_ids == tuple(b.id for b in blocks)
    assert project_unit_text(units[0], {b.id: b for b in blocks}).text == "A paragraph continues."


def test_list_continuation_keeps_one_marker():
    blocks, (units, findings) = build_flow("list")
    assert not findings
    assert len(units) == 1
    assert units[0].kind == "list-item"
    text = project_unit_text(units[0], {b.id: b for b in blocks}).text
    assert text == "• A paragraph continues."
    assert text.count("•") == 1


def test_recto_verso_normalizes_observed_column_margins():
    _, (units, findings) = build_flow("recto-verso")
    assert len(units) == 1 and not findings
    evidence = units[0].joins[0].evidence
    assert evidence.left.column_bbox == (72, 72, 540, 720)
    assert evidence.right.column_bbox == (90, 72, 558, 720)


def test_conflicting_continuation_has_required_finding():
    blocks, (units, findings) = build_flow("conflict")
    assert len(units) == 2
    assert len(findings) == 1
    assert findings[0].severity == "required"
    assert (findings[0].left_block_id, findings[0].right_block_id) == tuple(b.id for b in blocks)
    assert findings[0].code == "ambiguous-page-continuation"
    assert "font" in findings[0].message


@pytest.mark.parametrize("case,status", [("space", "join"), ("short-tail", "ambiguous"), ("sentence-end", "ambiguous")])
def test_nonhyphen_join_requires_filled_tail(case, status):
    assert hasattr(pdf_units, "decide_page_join"), "page join decision is missing"
    blocks, pages, boundaries = make_flow_case(case)
    decision = pdf_units.decide_page_join(*blocks, *(boundaries[b.id] for b in blocks), {p.number: p for p in pages})
    assert decision.status == status
    assert (decision.join is not None) == (status == "join")
    assert (decision.finding is not None) == (status == "ambiguous")


@pytest.mark.parametrize("field,value", [("line_pitch", 0), ("line_pitch", float("nan")), ("line_pitch", -1), ("column_count", 0), ("column_index", 1)])
def test_boundary_rejects_invalid_observed_context(field, value):
    from web_translator.pdf_models import PdfBlockBoundary
    data = make_unit_document().translation_units[0].joins[0].evidence.left.to_dict()
    data.update(line_pitch=18, column_index=0, column_count=1)
    data[field] = value
    with pytest.raises(PdfContractError):
        PdfBlockBoundary.from_dict(data)


def test_boundary_roundtrip_retains_optional_observed_context():
    from web_translator.pdf_models import PdfBlockBoundary
    data = make_unit_document().translation_units[0].joins[0].evidence.left.to_dict()
    data.update(line_pitch=18, column_index=1, column_count=2)
    assert PdfBlockBoundary.from_dict(data).to_dict() == data
    data.update(line_pitch=None, column_index=None, column_count=None)
    assert PdfBlockBoundary.from_dict(data).to_dict() == data


@pytest.mark.parametrize("columns", [1, 2])
@pytest.mark.parametrize("scale", [0.75, 1.0, 1.7])
def test_collected_owned_lines_supply_real_column_and_pitch_evidence(columns, scale):
    from web_translator import pdf_extract
    assert hasattr(pdf_extract, "collect_flow_boundaries"), "boundary producer is missing"
    blocks, pages, lines = make_observed_flow(columns=columns, scale=scale)
    boundaries = pdf_extract.collect_flow_boundaries(blocks, pages, lines)
    tail = next(b for b in blocks if b.source_text.endswith("A para‐"))
    head = next(b for b in blocks if b.source_text.startswith("graph continues."))
    assert boundaries[tail.id].last_line.text == "A para‐"
    assert boundaries[tail.id].last_line.bbox[1] == 708 * scale
    assert boundaries[tail.id].first_line.bbox[1] == 672 * scale
    assert boundaries[tail.id].line_pitch == pytest.approx(18 * scale)
    assert boundaries[tail.id].column_index == columns - 1
    assert boundaries[head.id].column_index == 0
    assert boundaries[tail.id].column_count == columns
    units, findings = pdf_units.build_translation_units(blocks, pages, boundaries)
    assert not findings
    assert [u.source_block_ids for u in units if len(u.source_block_ids) > 1] == [(tail.id, head.id)]
    assert len(units) == len(blocks) - 1


@pytest.mark.parametrize("problem", ["duplicate-owner", "wrong-text", "missing-lines", "sparse-frame"])
def test_collector_does_not_invent_unproven_boundaries(problem):
    from web_translator import pdf_extract
    assert hasattr(pdf_extract, "collect_flow_boundaries"), "boundary producer is missing"
    blocks, pages, lines = make_observed_flow()
    target = blocks[0]
    if problem == "duplicate-owner":
        blocks.append(replace(target, id="pdf:page-0001:block-0099"))
    elif problem == "wrong-text":
        blocks[0] = replace(target, source_text="not owned by these lines")
    elif problem == "missing-lines":
        lines[1] = lines[1][1:]
    else:
        lines[1] = lines[1][:1]
    boundaries = pdf_extract.collect_flow_boundaries(blocks, pages, lines)
    assert target.id not in boundaries


@pytest.mark.parametrize("field,value", [("line_pitch", None), ("column_index", None), ("column_count", None)])
def test_missing_observed_context_cannot_prove_join(field, value):
    blocks, pages, boundaries = make_flow_case("discretionary")
    boundaries[blocks[0].id] = replace(boundaries[blocks[0].id], **{field: value})
    units, findings = pdf_units.build_translation_units(blocks, pages, boundaries)
    assert len(units) == 2 and len(findings) == 1
    assert findings[0].severity == "required"


@pytest.mark.parametrize("left_index,right_index,status", [(1, 0, "join"), (0, 0, "separate"), (1, 1, "separate")])
def test_only_outer_reading_order_column_edges_may_join(left_index, right_index, status):
    blocks, pages, boundaries = make_flow_case("discretionary")
    left, right = [boundaries[b.id] for b in blocks]
    decision = pdf_units.decide_page_join(*blocks,
        replace(left, column_count=2, column_index=left_index),
        replace(right, column_count=2, column_index=right_index), {p.number: p for p in pages})
    assert decision.status == status


@pytest.mark.parametrize("dimension,inside,outside", [("font", 1.8, 1.801), ("indent", 3.0, 3.001), ("filled-tail", 12.0, 12.001), ("placement", 9.0, 9.001), ("width", 3.0, 3.001)])
def test_join_tolerance_has_exact_inside_outside_boundary(dimension, inside, outside):
    for delta, want in ((inside, "join"), (outside, "separate" if dimension in {"indent", "width"} else "ambiguous")):
        blocks, pages, boundaries = make_flow_case("space")
        left, right = [boundaries[b.id] for b in blocks]
        if dimension == "font":
            left = replace(left, last_line=replace(left.last_line, font_size=12 - delta))
        elif dimension == "indent":
            right = replace(right, first_line=replace(right.first_line, bbox=(72 + delta, 72, 540, 84)))
        elif dimension == "filled-tail":
            left = replace(left, last_line=replace(left.last_line, bbox=(72, 708, 540 - delta, 720)))
        elif dimension == "placement":
            left = replace(left, last_line=replace(left.last_line, bbox=(72, 708 - delta, 540, 720 - delta)))
        else:
            right = replace(right, column_bbox=(72, 72, 540 - delta, 720))
        decision = pdf_units.decide_page_join(*blocks, left, right, {p.number: p for p in pages})
        assert decision.status == want, (dimension, delta)


@pytest.mark.parametrize("kind", ["heading", "figure", "table", "caption"])
def test_builder_does_not_chain_through_intervening_structure(kind):
    blocks, pages, boundaries = make_flow_case("discretionary")
    blocker = replace(blocks[0], id="pdf:page-0001:block-0002", order=1, kind=kind)
    blocks[1] = replace(blocks[1], order=2)
    units, findings = pdf_units.build_translation_units([blocks[0], blocker, blocks[1]], pages, boundaries)
    assert all(len(u.source_block_ids) == 1 for u in units)
    assert not findings


def test_linked_page_edge_note_and_furniture_do_not_break_body_flow():
    blocks, pages, boundaries = make_flow_case("discretionary")
    note = replace(blocks[0], id="pdf:page-0001:block-0002", order=1, kind="footnote", source_text="1 A note.")
    footer = replace(note, id="pdf:page-0001:block-0003", order=2, kind="footer")
    blocks[0] = replace(blocks[0], destination=note.id)
    blocks[1] = replace(blocks[1], order=3)
    units, findings = pdf_units.build_translation_units([blocks[0], note, footer, blocks[1]], pages, boundaries)
    assert not findings
    assert [u.source_block_ids for u in units] == [(blocks[0].id, blocks[1].id), (note.id,)]


def test_multiple_candidate_successors_create_required_finding():
    blocks, pages, boundaries = make_flow_case("discretionary")
    competitor = replace(blocks[1], id="pdf:page-0002:block-0002", order=2)
    boundaries[competitor.id] = replace(boundaries[blocks[1].id], block_id=competitor.id)
    units, findings = pdf_units.build_translation_units([*blocks, competitor], pages, boundaries)
    assert len(units) == 3 and len(findings) == 1
    assert findings[0].severity == "required"


@pytest.mark.parametrize("case", ["sentence-end"])
def test_sentence_end_without_boundary_requires_review(case):
    blocks, pages, _ = make_flow_case(case)
    units, findings = pdf_units.build_translation_units(blocks, pages, {})
    assert len(units) == 2 and len(findings) == 1
    assert findings[0].severity == "required"


@pytest.mark.parametrize("text", ["A paragraph.\"", "A paragraph.)"])
def test_quoted_sentence_end_does_not_prove_paragraph_boundary(text):
    blocks, pages, boundaries = make_flow_case("space")
    blocks[0] = replace(blocks[0], source_text=text)
    left = boundaries[blocks[0].id]
    boundaries[blocks[0].id] = replace(left, last_line=replace(left.last_line, text=text))
    units, findings = pdf_units.build_translation_units(blocks, pages, boundaries)
    assert len(units) == 2 and len(findings) == 1
    assert findings[0].severity == "required"


@pytest.mark.parametrize("new_indent", [False, True])
def test_multisentence_page_break_requires_independent_paragraph_evidence(new_indent):
    blocks, pages, boundaries = make_flow_case("sentence-end")
    # A new sentence may begin with a capital even within the same paragraph.
    blocks[1] = replace(blocks[1], source_text="Another sentence follows.")
    right = boundaries[blocks[1].id]
    head = replace(right.first_line, text=blocks[1].source_text,
                   bbox=(96 if new_indent else 72, 72, 540, 84))
    boundaries[blocks[1].id] = replace(right, first_line=head)
    units, findings = pdf_units.build_translation_units(blocks, pages, boundaries)
    assert len(units) == 2  # Neither condition authorizes an automatic join.
    if new_indent:
        assert not findings
    else:
        assert len(findings) == 1
        assert findings[0].severity == "required"
        assert "sentence" in findings[0].message


@pytest.mark.parametrize("mark", ["-", "−", "–", "—"])
def test_non_discretionary_marks_are_never_removed(mark):
    blocks, pages, boundaries = make_flow_case("discretionary")
    text = "A para" + mark
    blocks[0] = replace(blocks[0], source_text=text)
    left = boundaries[blocks[0].id]
    boundaries[blocks[0].id] = replace(left, last_line=replace(left.last_line, text=text))
    units, findings = pdf_units.build_translation_units(blocks, pages, boundaries)
    assert not findings and len(units) == 1
    assert project_unit_text(units[0], {b.id: b for b in blocks}).text == text + " graph continues."


@pytest.mark.parametrize("problem", ["short-frame", "outside-frame", "wrong-own-tail", "wrong-own-head"])
def test_incomplete_or_conflicting_frame_does_not_prove_overflow(problem):
    blocks, pages, boundaries = make_flow_case("space")
    left, right = [boundaries[b.id] for b in blocks]
    if problem == "short-frame":
        left = replace(left, column_bbox=(72, 672, 540, 720))
        right = replace(right, column_bbox=(72, 672, 540, 720), first_line=replace(right.first_line, bbox=(72, 672, 540, 684)))
    elif problem == "outside-frame":
        left = replace(left, last_line=replace(left.last_line, bbox=(72, 708, 560, 720)))
    elif problem == "wrong-own-tail":
        left = replace(left, column_bbox=(72, 72, 540, 760))
    else:
        right = replace(right, column_bbox=(72, 32, 540, 720))
    decision = pdf_units.decide_page_join(*blocks, left, right, {p.number: p for p in pages})
    assert decision.status == "ambiguous" and decision.finding.severity == "required"


def test_potential_competitor_without_provable_boundary_prevents_join():
    blocks, pages, boundaries = make_flow_case("discretionary")
    competitor = replace(blocks[1], id="pdf:page-0002:block-0002", order=2)
    units, findings = pdf_units.build_translation_units([*blocks, competitor], pages, boundaries)
    assert len(units) == 3 and len(findings) == 1


def test_collector_does_not_mistake_paragraph_spacing_for_line_pitch():
    from web_translator.pdf_extract import collect_flow_boundaries
    from web_translator.pdf_layout import build_text_blocks
    blocks, pages, lines = make_observed_flow()
    selected = []
    for line, top in zip(lines[1][:3], (72, 200, 328)):
        selected.append(replace(line, words=tuple(replace(word, top=top, bottom=top + 12) for word in line.words)))
    blocks = build_text_blocks(selected, 1)
    boundaries = collect_flow_boundaries(blocks, pages[:1], {1: selected})
    assert all(boundary.line_pitch is None for boundary in boundaries.values())


def test_builder_preserves_every_physical_field_and_order():
    blocks, pages, boundaries = make_flow_case("three-page")
    before = [b.to_dict() for b in blocks]
    units, findings = pdf_units.build_translation_units(list(reversed(blocks)), pages, boundaries)
    assert len(units) == 1 and not findings
    assert [b.to_dict() for b in blocks] == before
    assert units[0].source_block_ids == tuple(b.id for b in blocks)


@pytest.mark.parametrize("height,status", [(72.0, "join"), (71.999, "ambiguous")])
def test_observed_frame_needs_four_line_pitches(height, status):
    blocks, pages, boundaries = make_flow_case("space")
    left, right = [boundaries[b.id] for b in blocks]
    frame = (72, 72, 540, 72 + height)
    left = replace(left, column_bbox=frame, last_line=replace(left.last_line, bbox=(72, 60 + height, 540, 72 + height)))
    right = replace(right, column_bbox=frame)
    decision = pdf_units.decide_page_join(*blocks, left, right, {p.number: p for p in pages})
    assert decision.status == status


def test_observed_list_continuation_uses_marker_body_word_margin():
    from web_translator.pdf_extract import collect_flow_boundaries
    blocks, pages, lines = make_observed_flow(list_item=True)
    boundaries = collect_flow_boundaries(blocks, pages, lines)
    tail = next(b for b in blocks if b.kind == "list-item")
    head = next(b for b in blocks if b.source_text.startswith("graph continues."))
    assert boundaries[tail.id].text_indent == 18
    units, findings = pdf_units.build_translation_units(blocks, pages, boundaries)
    assert not findings
    joined = [u for u in units if len(u.source_block_ids) > 1]
    assert len(joined) == 1
    assert joined[0].source_block_ids == (tail.id, head.id)
    assert project_unit_text(joined[0], {b.id: b for b in blocks}).text.count("•") == 1


def test_raw_head_discretionary_line_matches_page_local_normalized_block():
    from web_translator.pdf_extract import collect_flow_boundaries
    blocks, pages, lines = make_observed_flow(head_discretionary=True)
    boundaries = collect_flow_boundaries(blocks, pages, lines)
    tail = next(b for b in blocks if b.source_text.endswith("A para‐"))
    head = next(b for b in blocks if b.source_text.startswith("graph continues within"))
    assert boundaries[head.id].first_line.text == "graph contin‐"
    assert head.source_text == "graph continues within the page. Observed prose 2 0 2."
    units, findings = pdf_units.build_translation_units(blocks, pages, boundaries)
    assert not findings
    joined = [unit for unit in units if len(unit.source_block_ids) > 1]
    assert len(joined) == 1
    assert joined[0].source_block_ids == (tail.id, head.id)
    assert joined[0].joins[0].evidence.right.first_line.text == "graph contin‐"
    assert project_unit_text(joined[0], {b.id: b for b in blocks}).text == (
        "Observed prose 1 0 3. Observed prose 1 0 4. "
        "A paragraph continues within the page. Observed prose 2 0 2."
    )


def test_observed_sentence_boundary_overflow_has_required_finding():
    from web_translator.pdf_extract import collect_flow_boundaries
    blocks, pages, lines = make_observed_flow(sentence_boundary=True)
    boundaries = collect_flow_boundaries(blocks, pages, lines)
    tail = next(b for b in blocks if b.source_text.endswith("A sentence ends."))
    head = next(b for b in blocks if b.source_text.startswith("Another sentence follows."))
    units, findings = pdf_units.build_translation_units(blocks, pages, boundaries)
    assert all(len(unit.source_block_ids) == 1 for unit in units)
    assert len(findings) == 1
    assert findings[0].severity == "required"
    assert (findings[0].left_block_id, findings[0].right_block_id) == (tail.id, head.id)
    assert "sentence end" in findings[0].message


@pytest.mark.parametrize("mismatch", ["ascii-hyphen", "changed-word", "inserted-space", "single-line"])
def test_normalized_head_matching_does_not_allow_unrelated_text_changes(mismatch):
    from web_translator.pdf_extract import collect_flow_boundaries
    blocks, pages, lines = make_observed_flow(head_discretionary=True)
    boundaries = collect_flow_boundaries(blocks, pages, lines)
    tail = next(b for b in blocks if b.source_text.endswith("A para‐"))
    head = next(b for b in blocks if b.source_text.startswith("graph continues within"))
    boundary = boundaries[head.id]
    if mismatch == "ascii-hyphen":
        boundary = replace(boundary, first_line=replace(boundary.first_line, text="graph contin-"))
    elif mismatch == "changed-word":
        head = replace(head, source_text="unrelated words follow")
    elif mismatch == "inserted-space":
        head = replace(head, source_text="graph contin ues within the page.")
    else:
        boundary = replace(boundary, last_line=boundary.first_line)
    decision = pdf_units.decide_page_join(tail, head, boundaries[tail.id], boundary,
                                          {page.number: page for page in pages})
    assert decision.status == "ambiguous"
    assert "text does not match" in decision.finding.message


@pytest.mark.parametrize("role", ["reference-entry", "callout-body", "epigraph", "chapter-title"])
def test_semantic_boundary_stays_separate(role):
    blocks, pages, boundaries = make_flow_case("discretionary")
    blocks[1] = replace(blocks[1], semantic_role=role)
    units, findings = pdf_units.build_translation_units(blocks, pages, boundaries)
    assert len(units) == 2 and not findings


@pytest.mark.parametrize("field,value", [("column_index", None), ("column_count", None), ("column_index", -1), ("column_count", True), ("line_pitch", True)])
def test_observed_context_rejects_unpaired_or_wrong_types(field, value):
    from web_translator.pdf_models import PdfBlockBoundary
    data = make_flow_case("discretionary")[2]["pdf:page-0001:block-0001"].to_dict()
    data[field] = value
    with pytest.raises(PdfContractError):
        PdfBlockBoundary.from_dict(data)


def test_unit_roundtrip_preserves_physical_members() -> None:
    doc = make_unit_document()
    loaded = PdfDocument.from_dict(doc.to_dict())
    assert loaded.to_dict() == doc.to_dict()
    assert len(loaded.blocks) == 2
    assert len(loaded.translation_units) == 1
    projection = project_unit_text(loaded.translation_units[0], {b.id: b for b in loaded.blocks})
    assert projection.text == "A paragraph continues."
    assert {s.block_id for s in projection.spans} == {b.id for b in doc.blocks}
    assert [(s.source_start, s.source_end, s.unit_start, s.unit_end) for s in projection.spans] == [
        (0, 6, 0, 6), (6, 7, 6, 6), (0, 16, 6, 22),
    ]


def test_unit_segments_have_one_target_and_all_member_mappings():
    doc = make_unit_document()
    blocks, units, segments = pdf_extract.build_pdf_unit_segments(doc.blocks, doc.translation_units)
    assert len(segments) == 1
    assert segments[0].locator == blocks[0].id
    assert {b.segment_id for b in blocks} == {segments[0].id}
    assert units[0].segment_id == segments[0].id


def test_unit_segments_keep_heading_paths_and_neighbor_context_in_unit_order():
    from web_translator.pdf_models import PdfTranslationUnit
    doc = make_unit_document(("Scope", "The body follows"), operation="space")
    blocks = [replace(doc.blocks[0], kind="heading"), doc.blocks[1]]
    units = [
        PdfTranslationUnit("pdf:unit-000001", (blocks[0].id,), "heading", "body", None, ()),
        PdfTranslationUnit("pdf:unit-000002", (blocks[1].id,), "paragraph", "body", None, ()),
    ]
    assigned_blocks, assigned_units, segments = pdf_extract.build_pdf_unit_segments(blocks, units)
    assert [segment.locator for segment in segments] == [block.id for block in blocks]
    assert [segment.heading_path for segment in segments] == [[], ["Scope"]]
    assert [segment.context_ids for segment in segments] == [["seg-000002"], ["seg-000001"]]
    assert [block.segment_id for block in assigned_blocks] == [unit.segment_id for unit in assigned_units]


def test_oversized_logical_unit_is_not_split():
    from web_translator.zones import ZoneContractError, build_zones
    doc = make_unit_document(("A" * 6001, "B" * 6000), operation="space")
    _, _, segments = pdf_extract.build_pdf_unit_segments(doc.blocks, doc.translation_units)
    with pytest.raises(PdfContractError, match="12002") as error:
        pdf_units.require_pdf_unit_budget(doc, segments, max_chars=12000)
    assert doc.translation_units[0].id in str(error.value)
    assert all(b.id in str(error.value) for b in doc.blocks)
    with pytest.raises(ZoneContractError):
        build_zones(segments, max_chars=12000)


def test_joined_product_and_protected_spans_keep_source_offsets():
    from web_translator.protection import restore_tokens
    doc = make_unit_document(("Use Postgre‐", "SQL at https://example.org/2024."))
    blocks, units, segments = pdf_extract.build_pdf_unit_segments(doc.blocks, doc.translation_units)
    segment = segments[0]
    occurrences = pdf_units.project_protected_occurrences(units[0], {b.id: b for b in blocks})
    assert restore_tokens(segment.source_text, segment.protected) == "Use PostgreSQL at https://example.org/2024."
    assert "PostgreSQL" in segment.source_text
    assert [(o.placeholder, o.value, o.kind) for o in occurrences] == [
        (t.token, t.value, t.kind) for t in segment.protected
    ]
    url = next(o for o in occurrences if o.kind == "url")
    assert [(s.block_id, s.source_start, s.source_end) for s in url.source_spans] == [
        (blocks[1].id, 7, 31),
    ]


def test_protected_token_crossing_removed_mark_retains_both_physical_spans():
    from web_translator.protection import restore_tokens
    doc = make_unit_document(("Go to https://exam‐", "ple.org/path now"))
    blocks, units, segments = pdf_extract.build_pdf_unit_segments(doc.blocks, doc.translation_units)
    occurrence = pdf_units.project_protected_occurrences(units[0], {b.id: b for b in blocks})[0]
    assert occurrence.kind == "url"
    assert occurrence.value == "https://example.org/path"
    assert [(s.block_id, s.source_start, s.source_end, s.unit_start, s.unit_end)
            for s in occurrence.source_spans] == [
        (blocks[0].id, 6, 18, 6, 18),
        (blocks[0].id, 18, 19, 18, 18),
        (blocks[1].id, 0, 12, 18, 30),
    ]
    assert restore_tokens(segments[0].source_text, segments[0].protected) == "Go to https://example.org/path now"


def test_number_after_removed_mark_keeps_physical_offset():
    from web_translator.protection import restore_tokens
    doc = make_unit_document(("Postgre‐", "SQL 2024 is ready"))
    blocks, units, segments = pdf_extract.build_pdf_unit_segments(doc.blocks, doc.translation_units)
    occurrence = pdf_units.project_protected_occurrences(units[0], {b.id: b for b in blocks})[0]
    assert (occurrence.kind, occurrence.value) == ("number", "2024")
    assert [(span.block_id, span.source_start, span.source_end) for span in occurrence.source_spans] == [
        (blocks[1].id, 4, 8),
    ]
    assert restore_tokens(segments[0].source_text, segments[0].protected) == "PostgreSQL 2024 is ready"


def test_recognized_literal_code_crossing_join_restores_exact_text():
    from web_translator.protection import restore_tokens
    doc = make_unit_document(("Run <code>SELECT‐", "1</code> now"))
    blocks, units, segments = pdf_extract.build_pdf_unit_segments(doc.blocks, doc.translation_units)
    occurrence = pdf_units.project_protected_occurrences(units[0], {b.id: b for b in blocks})[0]
    assert (occurrence.kind, occurrence.value) == ("code", "<code>SELECT1</code>")
    assert [span.block_id for span in occurrence.source_spans] == [blocks[0].id, blocks[0].id, blocks[1].id]
    assert restore_tokens(segments[0].source_text, segments[0].protected) == "Run <code>SELECT1</code> now"


def test_reference_singletons_keep_existing_core_protection():
    from web_translator.protection import restore_tokens
    citation = '[1] A. Writer: "Data replication", Press, 2024.'
    doc = make_unit_document((citation + " Note: Use https://example.org/2025.",))
    doc.blocks[0] = replace(doc.blocks[0], semantic_role="reference-entry")
    doc.translation_units[0] = replace(doc.translation_units[0], semantic_role="reference-entry")
    _, units, segments = pdf_extract.build_pdf_unit_segments(doc.blocks, doc.translation_units)
    occurrences = pdf_units.project_protected_occurrences(units[0], {b.id: b for b in doc.blocks})
    assert occurrences[0].kind == "bibliography"
    assert occurrences[0].value == citation
    assert occurrences[0].source_spans[0].source_start == 0
    assert occurrences[0].source_spans[0].source_end == len(citation)
    assert restore_tokens(segments[0].source_text, segments[0].protected) == doc.blocks[0].source_text


def test_joined_reference_unit_is_rejected_at_every_entry_point():
    doc = make_unit_document((
        '[1] A. Writer: "Data replication",',
        'Press, 2024. Note: Commentary',
    ), operation="space")
    payload = doc.to_dict()
    for block in payload["blocks"]:
        block["semantic_role"] = "reference-entry"
    payload["translation_units"][0]["semantic_role"] = "reference-entry"
    doc.blocks[:] = [replace(block, semantic_role="reference-entry") for block in doc.blocks]
    doc.translation_units[0] = replace(doc.translation_units[0], semantic_role="reference-entry")
    unit = doc.translation_units[0]
    by_id = {block.id: block for block in doc.blocks}

    with pytest.raises(PdfContractError, match="multi-member"):
        validate_unit_membership(doc)
    with pytest.raises(PdfContractError, match="multi-member"):
        project_unit_text(unit, by_id)
    with pytest.raises(PdfContractError, match="multi-member"):
        pdf_units.project_protected_occurrences(unit, by_id)
    with pytest.raises(PdfContractError, match="multi-member"):
        pdf_extract.build_pdf_unit_segments(doc.blocks, doc.translation_units)
    with pytest.raises(PdfContractError, match="multi-member"):
        PdfDocument.from_dict(payload)


def test_repeated_visible_note_markers_keep_distinct_occurrences():
    from web_translator.pdf_models import PdfTranslationUnit
    doc = make_unit_document(("First i appears", "and second i appears"), operation="space")
    owners = [replace(block, destination=f"pdf:page-{index + 1:04d}:block-0002")
              for index, block in enumerate(doc.blocks)]
    notes = [replace(block, id=owner.destination, kind="footnote", order=index + 2,
                     source_text=f"i. Note {index + 1}", destination=None)
             for index, (block, owner) in enumerate(zip(doc.blocks, owners, strict=True))]
    blocks = [*owners, *notes]
    units = [doc.translation_units[0], *[
        PdfTranslationUnit(f"pdf:unit-{index + 2:06d}", (note.id,), "footnote", "body", None, ())
        for index, note in enumerate(notes)
    ]]
    _, assigned_units, segments = pdf_extract.build_pdf_unit_segments(blocks, units)
    occurrences = pdf_units.project_protected_occurrences(assigned_units[0], {b.id: b for b in blocks})
    markers = [o for o in occurrences if o.kind == "footnote-marker"]
    assert len(markers) == 2
    assert len({marker.placeholder for marker in markers}) == 2
    assert [(marker.owner_block_id, marker.note_id, marker.value) for marker in markers] == [
        (owners[0].id, notes[0].id, "i"), (owners[1].id, notes[1].id, "i"),
    ]
    assert [(marker.source_spans[0].block_id, marker.source_spans[0].source_start)
            for marker in markers] == [(owners[0].id, 6), (owners[1].id, 11)]
    assert all(segments[0].source_text.count(marker.placeholder) == 1 for marker in markers)


def test_shared_protection_uses_actual_match_not_earlier_unprotected_substring():
    doc = make_unit_document(("MUSTARD MUST",))
    _, units, segments = pdf_extract.build_pdf_unit_segments(doc.blocks, doc.translation_units)
    occurrences = pdf_units.project_protected_occurrences(units[0], {b.id: b for b in doc.blocks})
    assert [(item.value, item.source_spans[0].source_start, item.source_spans[0].source_end)
            for item in occurrences] == [("MUST", 8, 12)]
    assert segments[0].source_text == "MUSTARD ⟦WT:000000⟧"


def test_footnote_leading_marker_retains_physical_owner_and_note_identity():
    from web_translator.pdf_models import PdfTranslationUnit
    doc = make_unit_document(("A note i",))
    owner = replace(doc.blocks[0], destination="pdf:page-0001:block-0002")
    note = replace(owner, id=owner.destination, order=1, kind="footnote",
                   source_text="i. Footnote text", destination=None)
    units = [doc.translation_units[0], PdfTranslationUnit(
        "pdf:unit-000002", (note.id,), "footnote", "body", None, (),
    )]
    _, assigned_units, _ = pdf_extract.build_pdf_unit_segments([owner, note], units)
    occurrence = pdf_units.project_protected_occurrences(assigned_units[1],
                                                         {owner.id: owner, note.id: note})[0]
    assert (occurrence.kind, occurrence.value) == ("footnote-marker", "i.")
    assert (occurrence.owner_block_id, occurrence.note_id) == (note.id, note.id)


def test_duplicate_member_is_rejected() -> None:
    data = make_unit_document().to_dict()
    data["translation_units"][0]["source_block_ids"].append(data["blocks"][0]["id"])
    with pytest.raises(PdfContractError, match="member"):
        PdfDocument.from_dict(data)


@pytest.mark.parametrize("mutation", ["missing", "reordered", "foreign", "unassigned", "segment", "unit-id"])
def test_unit_membership_rejects_missing_reordered_or_foreign_blocks(mutation: str) -> None:
    data = make_unit_document().to_dict()
    unit = data["translation_units"][0]
    if mutation == "missing":
        unit["source_block_ids"].pop()
        unit["joins"].pop()
    elif mutation == "reordered":
        unit["source_block_ids"].reverse()
    elif mutation == "foreign":
        unit["source_block_ids"][1] = "pdf:page-0003:block-0001"
    elif mutation == "unassigned":
        unit["segment_id"] = None
    elif mutation == "segment":
        data["blocks"][1]["segment_id"] = "seg-000002"
    else:
        unit["id"] = "pdf:unit-000002"
    with pytest.raises(PdfContractError):
        PdfDocument.from_dict(data)


@pytest.mark.parametrize("origin", ["1.0", "1.1"])
def test_legacy_unit_upgrade_preserves_origin_and_locators(origin: str) -> None:
    from tests.pdf_fixtures import make_pdf_document
    legacy = make_pdf_document().to_dict()
    legacy["schema_version"] = origin
    if origin == "1.0":
        legacy["blocks"][0].pop("semantic_role")
        legacy["blocks"][0].pop("continuation_of")
    upgraded = upgrade_pdf_document_to_units(legacy)
    loaded = PdfDocument.from_dict(upgraded)
    assert loaded.schema_version == "1.2"
    assert loaded.extracted_schema_version == origin
    assert loaded.blocks[0].id == legacy["blocks"][0]["id"]
    assert loaded.blocks[0].bbox == tuple(legacy["blocks"][0]["bbox"])
    assert loaded.translation_units[0].source_block_ids == (loaded.blocks[0].id,)
    assert loaded.translation_units[0].segment_id == loaded.blocks[0].segment_id
    assert unit_for_block(loaded, loaded.blocks[0].id) == loaded.translation_units[0]


def test_projection_preserves_ascii_hyphen() -> None:
    doc = make_unit_document(("well-", "known"), operation="space")
    projected = project_unit_text(doc.translation_units[0], {b.id: b for b in doc.blocks})
    assert projected.text == "well- known"
    assert (projected.spans[1].source_start, projected.spans[1].source_end) == (5, 5)
    assert (projected.spans[1].unit_start, projected.spans[1].unit_end) == (5, 6)


@pytest.mark.parametrize("field,value", [
    ("schema_version", "1.3"),
    ("extracted_schema_version", "1.2"),
])
def test_units_reject_bad_versions(field: str, value: str) -> None:
    data = make_unit_document().to_dict()
    data[field] = value
    with pytest.raises(PdfContractError):
        PdfDocument.from_dict(data)


def test_units_reject_nonfinite_evidence_and_invalid_operations() -> None:
    data = make_unit_document().to_dict()
    evidence = data["translation_units"][0]["joins"][0]["evidence"]
    evidence["left_page_size"][0] = float("nan")
    with pytest.raises(PdfContractError, match="left_page_size"):
        PdfDocument.from_dict(data)
    data = make_unit_document().to_dict()
    data["translation_units"][0]["joins"][0]["operation"] = "concatenate"
    with pytest.raises(PdfContractError, match="operation"):
        PdfDocument.from_dict(data)


def test_unit_validation_rejects_duplicate_or_missing_ownership() -> None:
    doc = make_unit_document()
    doc.translation_units.append(doc.translation_units[0])
    with pytest.raises(PdfContractError):
        validate_unit_membership(doc)


def test_projection_refuses_undeclared_hyphen_removal() -> None:
    doc = make_unit_document(("well-", "known"))
    with pytest.raises(PdfContractError, match="discretionary"):
        project_unit_text(doc.translation_units[0], {b.id: b for b in doc.blocks})


def test_internal_unassigned_unit_validates_but_cannot_serialize() -> None:
    doc = make_unit_document()
    doc = replace(
        doc,
        blocks=[replace(block, segment_id=None) for block in doc.blocks],
        translation_units=[replace(doc.translation_units[0], segment_id=None)],
    )
    validate_unit_membership(doc)
    with pytest.raises(PdfContractError, match="assigned segment"):
        doc.to_dict()


def test_list_unit_may_include_body_but_not_a_heading() -> None:
    doc = make_unit_document()
    doc.blocks[0] = replace(doc.blocks[0], kind="list-item")
    doc.translation_units[0] = replace(doc.translation_units[0], kind="list-item")
    validate_unit_membership(doc)
    doc.blocks[1] = replace(doc.blocks[1], kind="heading")
    with pytest.raises(PdfContractError, match="mix member kinds"):
        validate_unit_membership(doc)


def test_list_unit_does_not_absorb_a_second_list_item() -> None:
    doc = make_unit_document()
    doc.blocks[:] = [replace(block, kind="list-item") for block in doc.blocks]
    doc.translation_units[0] = replace(doc.translation_units[0], kind="list-item")
    with pytest.raises(PdfContractError, match="list-item"):
        validate_unit_membership(doc)


def test_flow_finding_requires_known_blocks_and_required_severity() -> None:
    doc = make_unit_document()
    finding = PdfFlowFinding("ambiguous-break", doc.blocks[0].id, doc.blocks[1].id,
                             "required", "A possible continuation needs review.")
    doc = replace(doc, flow_findings=[finding])
    assert PdfDocument.from_dict(doc.to_dict()).flow_findings == [finding]
    payload = doc.to_dict()
    payload["flow_findings"][0]["severity"] = "warning"
    with pytest.raises(PdfContractError, match="severity"):
        PdfDocument.from_dict(payload)


@pytest.mark.parametrize("malformed", ["operation", "evidence"])
def test_unit_document_refuses_to_serialize_invalid_nested_join(malformed: str) -> None:
    doc = make_unit_document()
    unit = doc.translation_units[0]
    join = unit.joins[0]
    if malformed == "operation":
        join = replace(join, operation="concatenate")
    else:
        line = replace(join.evidence.left.first_line, font_size=float("nan"))
        boundary = replace(join.evidence.left, first_line=line)
        join = replace(join, evidence=replace(join.evidence, left=boundary))
    doc.translation_units[0] = replace(unit, joins=(join,))

    with pytest.raises(PdfContractError, match="operation|font_size"):
        doc.to_dict()
