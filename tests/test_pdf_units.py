"""Logical PDF unit contracts and source-text projection."""

from dataclasses import replace

import pytest

from tests.pdf_unit_fixtures import make_unit_document
from web_translator.pdf_models import PdfContractError, PdfDocument, PdfFlowFinding, upgrade_pdf_document_to_units
from web_translator.pdf_units import project_unit_text, unit_for_block, validate_unit_membership


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
