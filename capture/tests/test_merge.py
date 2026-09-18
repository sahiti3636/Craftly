"""Tests for app/merge.py.

Runs the real app/numbers.py parser against all 15 fixture transcripts
(tests/fixtures/transcripts.json) and checks the merge rule holds in
every case: an exact single money/duration match is used as-is; zero or
multiple matches null the field and add it to needs_confirmation. Using
the real parser here (rather than hand-picked match lists) means these
tests catch a regression in either module, not just in merge.py's
branching.
"""

import json
from pathlib import Path

import pytest

from app.extract import ExtractedFields
from app.merge import merge_extraction
from app.numbers import extract_duration, extract_money

FIXTURES_PATH = Path(__file__).resolve().parent / "fixtures" / "transcripts.json"
FIXTURES = json.loads(FIXTURES_PATH.read_text())


def _llm_result_for(case: dict) -> ExtractedFields:
    return ExtractedFields(**case["mock_llm_extraction"])


@pytest.mark.parametrize("case", FIXTURES, ids=[c["id"] for c in FIXTURES])
def test_merge_rule_holds_for_every_fixture_transcript(case):
    text, lang = case["transcript"], case["language"]
    money_matches = extract_money(text, lang)
    duration_matches = extract_duration(text, lang)

    merged = merge_extraction(_llm_result_for(case), money_matches, duration_matches)

    if len(money_matches) == 1:
        assert merged.material_cost_inr == money_matches[0]["value_inr"]
        assert "material_cost_inr" not in merged.needs_confirmation
    else:
        assert merged.material_cost_inr is None
        assert "material_cost_inr" in merged.needs_confirmation

    if len(duration_matches) == 1:
        assert merged.hours_worked == duration_matches[0]["hours"]
        assert "hours_worked" not in merged.needs_confirmation
    else:
        assert merged.hours_worked is None
        assert "hours_worked" in merged.needs_confirmation


@pytest.mark.parametrize("case", FIXTURES, ids=[c["id"] for c in FIXTURES])
def test_merge_preserves_llm_fields_unchanged(case):
    text, lang = case["transcript"], case["language"]
    money_matches = extract_money(text, lang)
    duration_matches = extract_duration(text, lang)
    llm_result = _llm_result_for(case)

    merged = merge_extraction(llm_result, money_matches, duration_matches)

    assert merged.category == llm_result.category
    assert merged.craft_type == llm_result.craft_type
    assert merged.material == llm_result.material
    assert merged.colours == llm_result.colours
    assert merged.dimensions == llm_result.dimensions
    assert merged.product_description_facts == llm_result.product_description_facts
    assert merged.confidence == llm_result.confidence


# ---------------------------------------------------------------------------
# Direct, explicit checks of the merge rule (not fixture-driven), so the
# rule itself is legible without cross-referencing app/numbers.py output.
# ---------------------------------------------------------------------------


def _base_llm_result() -> ExtractedFields:
    return ExtractedFields(category="pottery")


def test_merge_uses_single_money_match_directly():
    merged = merge_extraction(_base_llm_result(), [{"value_inr": 500}], [])
    assert merged.material_cost_inr == 500
    assert "material_cost_inr" not in merged.needs_confirmation


def test_merge_nulls_money_and_flags_when_zero_matches():
    merged = merge_extraction(_base_llm_result(), [], [])
    assert merged.material_cost_inr is None
    assert "material_cost_inr" in merged.needs_confirmation


def test_merge_nulls_money_and_flags_when_multiple_matches():
    merged = merge_extraction(
        _base_llm_result(), [{"value_inr": 500}, {"value_inr": 300}], []
    )
    assert merged.material_cost_inr is None
    assert "material_cost_inr" in merged.needs_confirmation


def test_merge_uses_single_duration_match_directly():
    merged = merge_extraction(_base_llm_result(), [], [{"hours": 1.5}])
    assert merged.hours_worked == 1.5
    assert "hours_worked" not in merged.needs_confirmation


def test_merge_nulls_hours_and_flags_when_zero_matches():
    merged = merge_extraction(_base_llm_result(), [], [])
    assert merged.hours_worked is None
    assert "hours_worked" in merged.needs_confirmation


def test_merge_nulls_hours_and_flags_when_multiple_matches():
    merged = merge_extraction(
        _base_llm_result(), [], [{"hours": 2.0}, {"hours": 3.0}]
    )
    assert merged.hours_worked is None
    assert "hours_worked" in merged.needs_confirmation


def test_merge_does_not_mutate_the_input_llm_result():
    llm_result = _base_llm_result()
    merge_extraction(llm_result, [], [])
    assert llm_result.material_cost_inr is None
    assert llm_result.needs_confirmation == []


def test_merge_does_not_duplicate_needs_confirmation_entries():
    llm_result = ExtractedFields(category="pottery", needs_confirmation=["material_cost_inr"])
    merged = merge_extraction(llm_result, [], [])
    assert merged.needs_confirmation.count("material_cost_inr") == 1
