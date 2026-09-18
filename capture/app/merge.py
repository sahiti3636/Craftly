"""Combine app/numbers.py's deterministic parse with app/extract.py's LLM
extraction into one ExtractedFields object.

Money and hours are never trusted from a single ambiguous mention: if the
parser found more than one candidate in the transcript, we can't tell
which one is the real cost/hours, and guessing wrong here is worse than
asking the artisan. Zero mentions is the same problem from the other
side — nothing was actually stated. Only an exact single match is used
directly; every other case nulls the field and flags it in
needs_confirmation.
"""

from __future__ import annotations

from app.extract import ExtractedFields


def merge_extraction(
    llm_result: ExtractedFields,
    money_matches: list[dict],
    duration_matches: list[dict],
) -> ExtractedFields:
    merged = llm_result.model_copy(deep=True)

    if len(money_matches) == 1:
        merged.material_cost_inr = money_matches[0]["value_inr"]
    else:
        merged.material_cost_inr = None
        if "material_cost_inr" not in merged.needs_confirmation:
            merged.needs_confirmation.append("material_cost_inr")

    if len(duration_matches) == 1:
        merged.hours_worked = duration_matches[0]["hours"]
    else:
        merged.hours_worked = None
        if "hours_worked" not in merged.needs_confirmation:
            merged.needs_confirmation.append("hours_worked")

    return merged
