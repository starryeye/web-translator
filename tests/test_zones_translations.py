from __future__ import annotations

import json
from pathlib import Path

import pytest

from web_translator.models import ProtectedToken, Segment, Translation
from web_translator.translations import (
    TranslationContractError,
    merge_translations,
    validate_zone_results,
)
from web_translator.zones import Zone, build_zones


def segment(
    segment_id: str,
    text: str,
    *,
    semantic_type: str = "paragraph",
    heading_path: list[str] | None = None,
    protected: list[ProtectedToken] | None = None,
    target: bool = True,
) -> Segment:
    return Segment(
        id=segment_id,
        locator=f"[data-wt-segment='{segment_id}']",
        semantic_type=semantic_type,
        heading_path=[] if heading_path is None else heading_path,
        source_text=text,
        protected=[] if protected is None else protected,
        context_ids=[],
        target=target,
    )


def translation(segment_id: object, text: object) -> Translation:
    return Translation(segment_id=segment_id, text=text)  # type: ignore[arg-type]


def sample_zone(
    target_ids: list[str],
    *,
    expected_tokens: dict[str, tuple[str, ...]] | None = None,
) -> Zone:
    return Zone(
        id="zone-001",
        heading_path=["Section"],
        target_ids=target_ids,
        context_before_ids=[],
        context_after_ids=[],
        expected_tokens=(
            {segment_id: () for segment_id in target_ids}
            if expected_tokens is None
            else expected_tokens
        ),
    )


def write_zone_result(path: Path, records: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def test_zones_pack_complete_sections_and_include_read_only_neighbors() -> None:
    segments = [
        segment("seg-000001", "A" * 7_000, semantic_type="heading"),
        segment("seg-000002", "B" * 7_000, semantic_type="heading"),
        segment("seg-000003", "C" * 2_000, semantic_type="heading"),
    ]

    zones = build_zones(segments, max_chars=12_000)

    assert len(zones) == 2
    assert set(zones[0].target_ids).isdisjoint(zones[1].target_ids)
    assert zones[1].context_before_ids[-1] == zones[0].target_ids[-1]
    assert zones[1].target_ids == ["seg-000002", "seg-000003"]


def test_zone_count_changes_when_complete_sections_fit_the_limit() -> None:
    segments = [
        segment(f"seg-{index:06d}", "X" * 1_000, semantic_type="heading")
        for index in range(1, 4)
    ]

    assert len(build_zones(segments, max_chars=1_500)) == 3
    assert len(build_zones(segments, max_chars=2_500)) == 2
    assert len(build_zones(segments, max_chars=10_000)) == 1


def test_zone_keeps_a_complete_oversized_section_together() -> None:
    segments = [
        segment("seg-000001", "Large", semantic_type="heading"),
        segment("seg-000002", "A" * 8_000, heading_path=["Large"]),
        segment("seg-000003", "B" * 8_000, heading_path=["Large"]),
        segment("seg-000004", "Next", semantic_type="heading"),
    ]

    zones = build_zones(segments, max_chars=12_000)

    assert zones[0].target_ids == ["seg-000001", "seg-000002", "seg-000003"]
    assert zones[1].target_ids == ["seg-000004"]


def test_zone_context_is_bounded_to_two_neighbors_on_each_side() -> None:
    segments = [
        segment(f"seg-{index:06d}", str(index), semantic_type="heading")
        for index in range(1, 6)
    ]

    zones = build_zones(segments, max_chars=1)

    assert zones[2].context_before_ids == ["seg-000001", "seg-000002"]
    assert zones[2].context_after_ids == ["seg-000004", "seg-000005"]


def test_zone_partition_contains_only_every_target_once() -> None:
    segments = [
        segment("seg-000001", "Preamble"),
        segment("ctx-000001", "Read only", target=False),
        segment("seg-000002", "Section", semantic_type="heading"),
        segment("seg-000003", "Body", heading_path=["Section"]),
    ]

    zones = build_zones(segments)

    assert [item for zone in zones for item in zone.target_ids] == [
        "seg-000001",
        "seg-000002",
        "seg-000003",
    ]
    assert "ctx-000001" not in {item for zone in zones for item in zone.target_ids}


@pytest.mark.parametrize("max_chars", [0, -1, True])
def test_zone_builder_rejects_invalid_character_limit(max_chars: object) -> None:
    with pytest.raises(ValueError, match="max_chars"):
        build_zones([segment("seg-000001", "Text")], max_chars=max_chars)  # type: ignore[arg-type]


def test_zone_builder_rejects_duplicate_segment_ids() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        build_zones(
            [segment("seg-000001", "One"), segment("seg-000001", "Two")]
        )


def test_translation_contract_rejects_missing_foreign_and_changed_tokens() -> None:
    token = "⟦WT:000000⟧"
    zone = sample_zone(
        ["seg-000001", "seg-000002"],
        expected_tokens={"seg-000001": (token,), "seg-000002": ()},
    )

    with pytest.raises(TranslationContractError, match="missing"):
        validate_zone_results(zone, [translation("seg-000001", f"번역 {token}")])
    with pytest.raises(TranslationContractError, match="unassigned"):
        validate_zone_results(zone, [translation("seg-999999", "번역")])
    with pytest.raises(TranslationContractError, match="protected-token"):
        validate_zone_results(
            zone,
            [translation("seg-000001", "번역"), translation("seg-000002", "번역")],
        )


def test_translation_contract_rejects_duplicate_and_foreign_tokens() -> None:
    token = "⟦WT:000000⟧"
    zone = sample_zone(
        ["seg-000001"], expected_tokens={"seg-000001": (token,)}
    )

    with pytest.raises(TranslationContractError, match="duplicate"):
        validate_zone_results(
            zone,
            [translation("seg-000001", token), translation("seg-000001", token)],
        )
    with pytest.raises(TranslationContractError, match="protected-token"):
        validate_zone_results(
            zone,
            [translation("seg-000001", f"{token} ⟦WT:999999⟧")],
        )


def test_zone_requires_immutable_exact_token_expectations() -> None:
    source_expectations = {"seg-000001": ("⟦WT:000000⟧",)}
    zone = sample_zone(
        ["seg-000001"], expected_tokens=source_expectations
    )
    source_expectations["seg-000001"] = ()

    assert zone.expected_tokens["seg-000001"] == ("⟦WT:000000⟧",)
    with pytest.raises(TypeError):
        zone.expected_tokens["seg-000001"] = ()  # type: ignore[index]

    with pytest.raises(ValueError, match="expected_tokens"):
        Zone(
            id="zone-001",
            heading_path=[],
            target_ids=["seg-000001"],
            context_before_ids=[],
            context_after_ids=[],
            expected_tokens={},
        )


@pytest.mark.parametrize(
    "record",
    [
        Translation(segment_id=7, text="번역"),  # type: ignore[arg-type]
        Translation(segment_id="seg-000001", text=7),  # type: ignore[arg-type]
        Translation(segment_id="seg-000001", text="번역", notes=7),  # type: ignore[arg-type]
        Translation(
            segment_id="seg-000001",
            text="번역",
            glossary_observations={"term": 7},  # type: ignore[dict-item]
        ),
    ],
)
def test_translation_contract_rejects_non_string_fields(record: Translation) -> None:
    with pytest.raises(TranslationContractError, match="string"):
        validate_zone_results(sample_zone(["seg-000001"]), [record])


def test_merge_reads_windows_safe_zone_files_and_returns_source_order(
    tmp_path: Path,
) -> None:
    result_dir = tmp_path / "번역 결과 공간"
    segments = [
        segment("seg-000001", "One"),
        segment(
            "seg-000002",
            "Two ⟦WT:000000⟧",
            protected=[ProtectedToken("⟦WT:000000⟧", "keyword", "MUST")],
        ),
    ]
    zones = build_zones(segments)
    write_zone_result(
        result_dir / "zone-001.jsonl",
        [
            {"segment_id": "seg-000001", "text": "하나"},
            {"segment_id": "seg-000002", "text": "둘 ⟦WT:000000⟧"},
        ],
    )

    merged = merge_translations(segments, zones, result_dir)

    assert list(merged) == ["seg-000001", "seg-000002"]
    assert merged["seg-000002"].text == "둘 ⟦WT:000000⟧"


def test_merge_rejects_non_exact_zone_partition(tmp_path: Path) -> None:
    segments = [segment("seg-000001", "One"), segment("seg-000002", "Two")]
    zones = [sample_zone(["seg-000001", "seg-000001"])]

    with pytest.raises(TranslationContractError, match="partition"):
        merge_translations(segments, zones, tmp_path)


def test_merge_rejects_zone_id_path_traversal(tmp_path: Path) -> None:
    segments = [segment("seg-000001", "One")]
    zone = Zone(
        id="..\\outside",
        heading_path=[],
        target_ids=["seg-000001"],
        context_before_ids=[],
        context_after_ids=[],
        expected_tokens={"seg-000001": ()},
    )

    with pytest.raises(TranslationContractError, match="zone ID"):
        merge_translations(segments, [zone], tmp_path / "번역 결과")


def test_merge_rejects_missing_file_invalid_json_and_non_object(
    tmp_path: Path,
) -> None:
    segments = [segment("seg-000001", "One")]
    zones = build_zones(segments)

    with pytest.raises(TranslationContractError, match="missing zone result"):
        merge_translations(segments, zones, tmp_path)

    path = tmp_path / "zone-001.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(TranslationContractError, match="invalid JSON"):
        merge_translations(segments, zones, tmp_path)

    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(TranslationContractError, match="object"):
        merge_translations(segments, zones, tmp_path)


def test_merge_rejects_missing_duplicate_and_foreign_final_ids(tmp_path: Path) -> None:
    segments = [segment("seg-000001", "One"), segment("seg-000002", "Two")]
    zones = build_zones(segments)
    path = tmp_path / "zone-001.jsonl"

    write_zone_result(path, [{"segment_id": "seg-000001", "text": "하나"}])
    with pytest.raises(TranslationContractError, match="missing"):
        merge_translations(segments, zones, tmp_path)

    write_zone_result(
        path,
        [
            {"segment_id": "seg-000001", "text": "하나"},
            {"segment_id": "seg-000001", "text": "중복"},
        ],
    )
    with pytest.raises(TranslationContractError, match="duplicate"):
        merge_translations(segments, zones, tmp_path)

    write_zone_result(
        path,
        [
            {"segment_id": "seg-000001", "text": "하나"},
            {"segment_id": "seg-999999", "text": "외부"},
        ],
    )
    with pytest.raises(TranslationContractError, match="unassigned"):
        merge_translations(segments, zones, tmp_path)


def test_merge_rejects_non_string_json_translation_fields(tmp_path: Path) -> None:
    segments = [segment("seg-000001", "One")]
    zones = build_zones(segments)
    write_zone_result(
        tmp_path / "zone-001.jsonl",
        [{"segment_id": "seg-000001", "text": 7}],
    )

    with pytest.raises(TranslationContractError, match="string"):
        merge_translations(segments, zones, tmp_path)
