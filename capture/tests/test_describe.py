"""Tests for app/describe.py.

All tests monkeypatch app.describe._call_groq_marketing and
_call_groq_summary so the suite is fast, deterministic, and never makes a
real network call. The central concern this file tests is the one the
task calls out explicitly: no output may contain a fact absent from the
input ExtractedFields — enforced here via the colour-vocabulary and
cost/hours-leak guardrails, and via the deterministic (LLM-free)
summary_spoken templates for English/Hindi.
"""

import json

import pytest

import app.describe as describe_module
from app.describe import (
    DescriptionError,
    GeneratedDescriptions,
    generate_descriptions,
)
from app.extract import ExtractedFields


@pytest.fixture(autouse=True)
def _isolate_log_file(tmp_path, monkeypatch):
    log_path = tmp_path / "describe.jsonl"
    monkeypatch.setattr(describe_module, "_LOG_PATH", log_path)
    return log_path


def _valid_description(word_count: int = 75) -> str:
    return " ".join(["word"] * word_count)


def _mock_marketing(title: str = "A Handmade Product", description: str | None = None):
    description = description or _valid_description()

    def _fake(lang, facts):
        return "system prompt", json.dumps(facts), json.dumps(
            {"title": title, "description": description}
        )

    return _fake


def _mock_summary(summary: str = "your product cost five hundred rupees and took three hours"):
    def _fake(facts, source_language):
        return "system prompt", json.dumps(facts), json.dumps({"summary": summary})

    return _fake


BASE_FIELDS = ExtractedFields(
    category="home decor",
    craft_type="hand-molded pottery",
    material="terracotta",
    colours=["red", "gold"],
    dimensions="8cm x 8cm",
    product_description_facts=["hand-molded"],
    material_cost_inr=500,
    hours_worked=3.0,
)


# ---------------------------------------------------------------------------
# Empty-input short-circuit
# ---------------------------------------------------------------------------


def test_generate_descriptions_returns_all_null_for_empty_fields(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Groq should not be called when there are no facts at all")

    monkeypatch.setattr(describe_module, "_call_groq_marketing", _fail_if_called)
    monkeypatch.setattr(describe_module, "_call_groq_summary", _fail_if_called)

    result = generate_descriptions(ExtractedFields(), "hi")
    assert result == GeneratedDescriptions()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_generate_descriptions_happy_path_hindi_source(monkeypatch):
    monkeypatch.setattr(describe_module, "_call_groq_marketing", _mock_marketing())

    result = generate_descriptions(BASE_FIELDS, "hi")

    assert result.title_en == "A Handmade Product"
    assert result.title_hi == "A Handmade Product"  # same mock used for both calls
    assert result.description_en == _valid_description()
    assert result.description_hi == _valid_description()
    # Hindi source language -> deterministic template, no LLM call needed.
    assert result.summary_spoken == (
        "यह आपका home decor है। सामान की लागत 500 रुपये है। इसे बनाने में 3 घंटे लगे।"
    )


def test_generate_descriptions_happy_path_english_source(monkeypatch):
    monkeypatch.setattr(describe_module, "_call_groq_marketing", _mock_marketing())

    result = generate_descriptions(BASE_FIELDS, "en")

    assert result.summary_spoken == (
        "This is your home decor. Material cost is 500 rupees. It took 3 hours to make."
    )


def test_generate_descriptions_falls_back_to_llm_for_other_language(monkeypatch):
    monkeypatch.setattr(describe_module, "_call_groq_marketing", _mock_marketing())
    monkeypatch.setattr(
        describe_module,
        "_call_groq_summary",
        _mock_summary("mee vastuvu ధర ఐదు వందల రూపాయలు మరియు మూడు గంటలు పట్టింది"),
    )

    result = generate_descriptions(BASE_FIELDS, "te")

    assert result.summary_spoken == "mee vastuvu ధర ఐదు వందల రూపాయలు మరియు మూడు గంటలు పట్టింది"


def test_generate_descriptions_calls_marketing_exactly_once_per_language(monkeypatch):
    call_langs = []

    def _fake(lang, facts):
        call_langs.append(lang)
        return "sys", "user", json.dumps({"title": "Title", "description": _valid_description()})

    monkeypatch.setattr(describe_module, "_call_groq_marketing", _fake)

    generate_descriptions(BASE_FIELDS, "hi")

    assert call_langs == ["en", "hi"]


# ---------------------------------------------------------------------------
# Deterministic summary templates: exercised directly, all combinations
# ---------------------------------------------------------------------------


def test_summary_en_with_product_cost_and_hours():
    fields = ExtractedFields(category="diya", material_cost_inr=500, hours_worked=3.0)
    assert describe_module._build_summary_en(fields) == (
        "This is your diya. Material cost is 500 rupees. It took 3 hours to make."
    )


def test_summary_en_cost_only():
    fields = ExtractedFields(category="diya", material_cost_inr=500)
    assert describe_module._build_summary_en(fields) == (
        "This is your diya. Material cost is 500 rupees."
    )


def test_summary_en_hours_only():
    fields = ExtractedFields(category="diya", hours_worked=3.0)
    assert describe_module._build_summary_en(fields) == "This is your diya. It took 3 hours to make."


def test_summary_en_no_cost_or_hours():
    fields = ExtractedFields(category="diya")
    assert describe_module._build_summary_en(fields) == "This is your diya."


def test_summary_en_falls_back_to_craft_type_then_material_for_product_label():
    fields = ExtractedFields(craft_type="pottery")
    assert describe_module._build_summary_en(fields) == "This is your pottery."
    fields2 = ExtractedFields(material="terracotta")
    assert describe_module._build_summary_en(fields2) == "This is your terracotta."


def test_summary_en_fractional_hours_and_cost_render_cleanly():
    fields = ExtractedFields(category="diya", material_cost_inr=125.5, hours_worked=1.5)
    result = describe_module._build_summary_en(fields)
    assert "125.5 rupees" in result
    assert "1.5 hours" in result


def test_summary_hi_with_product_cost_and_hours():
    fields = ExtractedFields(category="diya", material_cost_inr=500, hours_worked=3.0)
    assert describe_module._build_summary_hi(fields) == (
        "यह आपका diya है। सामान की लागत 500 रुपये है। इसे बनाने में 3 घंटे लगे।"
    )


def test_summary_hi_no_cost_or_hours():
    fields = ExtractedFields(category="tokri")
    assert describe_module._build_summary_hi(fields) == "यह आपका tokri है।"


def test_summary_word_counts_are_within_spec_around_15_words():
    fields = ExtractedFields(category="diya", material_cost_inr=500, hours_worked=3.0)
    en = describe_module._build_summary_en(fields)
    hi = describe_module._build_summary_hi(fields)
    assert 10 <= describe_module._word_count(en) <= 20
    assert 10 <= describe_module._word_count(hi) <= 20


def test_summary_contains_no_list_or_heading_punctuation():
    fields = ExtractedFields(category="diya", material_cost_inr=500, hours_worked=3.0)
    for text in (describe_module._build_summary_en(fields), describe_module._build_summary_hi(fields)):
        assert "*" not in text
        assert "\n" not in text
        assert ":" not in text


# ---------------------------------------------------------------------------
# needs_confirmation fields must be asked about, never stated as fact
# ---------------------------------------------------------------------------


def test_summary_en_asks_question_when_cost_flagged_for_confirmation():
    fields = ExtractedFields(category="diya", hours_worked=3.0, needs_confirmation=["material_cost_inr"])
    result = describe_module._build_summary_en(fields)
    assert result == "This is your diya. What was the material cost? It took 3 hours to make."
    assert "?" in result


def test_summary_en_asks_question_when_hours_flagged_for_confirmation():
    fields = ExtractedFields(category="diya", material_cost_inr=500, needs_confirmation=["hours_worked"])
    result = describe_module._build_summary_en(fields)
    assert result == "This is your diya. Material cost is 500 rupees. How many hours did it take to make?"


def test_summary_en_asks_both_questions_when_both_flagged():
    fields = ExtractedFields(category="diya", needs_confirmation=["material_cost_inr", "hours_worked"])
    result = describe_module._build_summary_en(fields)
    assert result == (
        "This is your diya. What was the material cost? How many hours did it take to make?"
    )
    assert result.count("?") == 2


def test_summary_hi_asks_question_when_cost_flagged_for_confirmation():
    fields = ExtractedFields(category="diya", hours_worked=3.0, needs_confirmation=["material_cost_inr"])
    result = describe_module._build_summary_hi(fields)
    assert result == "यह आपका diya है। सामान की लागत कितनी थी? इसे बनाने में 3 घंटे लगे।"
    assert "?" in result


def test_summary_never_states_a_flagged_fields_null_value_as_fact():
    # A flagged field is always null (that's how merge.py flags it) — the
    # summary must never render that null as if it were a stated number.
    fields = ExtractedFields(category="diya", needs_confirmation=["material_cost_inr"])
    result = describe_module._build_summary_en(fields)
    assert "None" not in result
    assert "null" not in result.lower()


def test_facts_for_summary_includes_ask_about_flags():
    fields = ExtractedFields(category="diya", needs_confirmation=["material_cost_inr"])
    facts = describe_module._facts_for_summary(fields)
    assert facts["ask_about_material_cost"] is True
    assert facts["ask_about_hours_worked"] is False


def test_generate_spoken_summary_matches_build_summary_en_directly():
    fields = ExtractedFields(category="diya", material_cost_inr=500, hours_worked=3.0)
    assert describe_module.generate_spoken_summary(fields, "en") == describe_module._build_summary_en(fields)


# ---------------------------------------------------------------------------
# generate_descriptions: summary-only path when there's a pending question
# but no descriptive content to write a title/description about
# ---------------------------------------------------------------------------


def test_generate_descriptions_skips_marketing_copy_when_only_a_pending_question(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("marketing copy should not be generated with no descriptive facts")

    monkeypatch.setattr(describe_module, "_call_groq_marketing", _fail_if_called)

    fields = ExtractedFields(needs_confirmation=["material_cost_inr"])
    result = generate_descriptions(fields, "hi")

    assert result.title_en is None
    assert result.description_en is None
    assert result.summary_spoken == "यह आपका प्रोडक्ट है। सामान की लागत कितनी थी?"


def test_generate_descriptions_returns_all_null_when_truly_nothing_at_all(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Groq should not be called with zero facts and no pending question")

    monkeypatch.setattr(describe_module, "_call_groq_marketing", _fail_if_called)

    result = generate_descriptions(ExtractedFields(), "hi")
    assert result == GeneratedDescriptions()


# ---------------------------------------------------------------------------
# No-invented-facts guardrails — the core requirement of this task
# ---------------------------------------------------------------------------


def test_marketing_copy_rejects_invented_colour_not_in_fields(monkeypatch):
    fields = ExtractedFields(category="diya", colours=["red"])
    bad_description = "a blue and red diya, " + _valid_description(66)

    monkeypatch.setattr(
        describe_module,
        "_call_groq_marketing",
        _mock_marketing(description=bad_description),
    )

    with pytest.raises(DescriptionError, match="colour"):
        generate_descriptions(fields, "hi")


def test_marketing_copy_accepts_colour_that_is_in_fields(monkeypatch):
    fields = ExtractedFields(category="diya", colours=["red"])
    good_description = "a red diya, " + _valid_description(67)

    monkeypatch.setattr(
        describe_module,
        "_call_groq_marketing",
        _mock_marketing(description=good_description),
    )

    result = generate_descriptions(fields, "hi")
    assert result.description_en is not None


def test_marketing_copy_rejects_colour_mention_when_no_colours_given(monkeypatch):
    fields = ExtractedFields(category="diya")  # colours is None
    bad_description = "a striking red diya, " + _valid_description(65)

    monkeypatch.setattr(
        describe_module,
        "_call_groq_marketing",
        _mock_marketing(description=bad_description),
    )

    with pytest.raises(DescriptionError, match="colour"):
        generate_descriptions(fields, "hi")


def test_marketing_copy_rejects_leaked_material_cost(monkeypatch):
    fields = ExtractedFields(category="diya", material_cost_inr=500)
    bad_description = "made for just 500 rupees, " + _valid_description(65)

    monkeypatch.setattr(
        describe_module,
        "_call_groq_marketing",
        _mock_marketing(description=bad_description),
    )

    with pytest.raises(DescriptionError, match="material_cost_inr|hours_worked"):
        generate_descriptions(fields, "hi")


def test_marketing_copy_rejects_leaked_hours_worked(monkeypatch):
    fields = ExtractedFields(category="diya", hours_worked=3.0)
    bad_description = "crafted over 3 hours of care, " + _valid_description(64)

    monkeypatch.setattr(
        describe_module,
        "_call_groq_marketing",
        _mock_marketing(description=bad_description),
    )

    with pytest.raises(DescriptionError, match="material_cost_inr|hours_worked"):
        generate_descriptions(fields, "hi")


def test_facts_for_marketing_excludes_cost_and_hours():
    fields = ExtractedFields(category="diya", material_cost_inr=500, hours_worked=3.0)
    facts = describe_module._facts_for_marketing(fields)
    assert "material_cost_inr" not in facts
    assert "hours_worked" not in facts


def test_invented_colours_detects_unauthorized_colour():
    fields = ExtractedFields(colours=["red"])
    assert describe_module._invented_colours("a lovely blue vase", fields) == {"blue"}
    assert describe_module._invented_colours("a lovely red vase", fields) == set()


def test_invented_colours_flags_any_colour_when_none_given():
    fields = ExtractedFields(colours=None)
    assert describe_module._invented_colours("a lovely red vase", fields) == {"red"}


def test_leaks_cost_or_hours_true_when_number_present():
    fields = ExtractedFields(material_cost_inr=500, hours_worked=3.0)
    assert describe_module._leaks_cost_or_hours("only 500 rupees", fields) is True
    assert describe_module._leaks_cost_or_hours("took 3 hours", fields) is True


def test_leaks_cost_or_hours_false_when_absent():
    fields = ExtractedFields(material_cost_inr=500, hours_worked=3.0)
    assert describe_module._leaks_cost_or_hours("a lovely handmade diya", fields) is False


def test_leaks_cost_or_hours_ignores_unrelated_numbers():
    fields = ExtractedFields(material_cost_inr=500, hours_worked=3.0)
    # A dimension-like "8 cm" mention shouldn't trip the 500/3 check.
    assert describe_module._leaks_cost_or_hours("measures 8 cm across", fields) is False


# ---------------------------------------------------------------------------
# Length constraints (title < 70 chars, description 60-90 words)
# ---------------------------------------------------------------------------


def test_marketing_copy_rejects_title_over_70_chars(monkeypatch):
    fields = ExtractedFields(category="diya")
    long_title = "A" * 70

    monkeypatch.setattr(
        describe_module, "_call_groq_marketing", _mock_marketing(title=long_title)
    )

    with pytest.raises(DescriptionError, match="title"):
        generate_descriptions(fields, "hi")


def test_marketing_copy_accepts_title_under_70_chars(monkeypatch):
    fields = ExtractedFields(category="diya")
    short_title = "A" * 69

    monkeypatch.setattr(
        describe_module, "_call_groq_marketing", _mock_marketing(title=short_title)
    )

    result = generate_descriptions(fields, "hi")
    assert result.title_en == short_title


def test_marketing_copy_rejects_description_under_60_words(monkeypatch):
    fields = ExtractedFields(category="diya")

    monkeypatch.setattr(
        describe_module, "_call_groq_marketing", _mock_marketing(description=_valid_description(59))
    )

    with pytest.raises(DescriptionError, match="word"):
        generate_descriptions(fields, "hi")


def test_marketing_copy_rejects_description_over_90_words(monkeypatch):
    fields = ExtractedFields(category="diya")

    monkeypatch.setattr(
        describe_module, "_call_groq_marketing", _mock_marketing(description=_valid_description(91))
    )

    with pytest.raises(DescriptionError, match="word"):
        generate_descriptions(fields, "hi")


def test_marketing_copy_accepts_description_at_boundaries(monkeypatch):
    fields = ExtractedFields(category="diya")

    monkeypatch.setattr(
        describe_module, "_call_groq_marketing", _mock_marketing(description=_valid_description(60))
    )
    assert generate_descriptions(fields, "hi").description_en is not None

    monkeypatch.setattr(
        describe_module, "_call_groq_marketing", _mock_marketing(description=_valid_description(90))
    )
    assert generate_descriptions(fields, "hi").description_en is not None


def test_marketing_copy_rejects_empty_title(monkeypatch):
    fields = ExtractedFields(category="diya")
    monkeypatch.setattr(describe_module, "_call_groq_marketing", _mock_marketing(title="  "))

    with pytest.raises(DescriptionError, match="title"):
        generate_descriptions(fields, "hi")


# ---------------------------------------------------------------------------
# Retry-once-then-fail-clean
# ---------------------------------------------------------------------------


def test_marketing_retries_once_then_succeeds(monkeypatch):
    en_responses = iter(["not json", json.dumps({"title": "Title", "description": _valid_description()})])
    calls = {"n": 0}

    def _fake(lang, facts):
        calls["n"] += 1
        if lang == "en":
            return "sys", "user", next(en_responses)
        return "sys", "user", json.dumps({"title": "Title", "description": _valid_description()})

    monkeypatch.setattr(describe_module, "_call_groq_marketing", _fake)

    result = generate_descriptions(ExtractedFields(category="diya"), "hi")
    assert result.description_en is not None
    # "en" needed 2 attempts (malformed then valid), "hi" succeeded in 1.
    assert calls["n"] == 3


def test_marketing_fails_cleanly_after_two_malformed_responses(monkeypatch):
    calls = {"n": 0}

    def _fake(lang, facts):
        calls["n"] += 1
        return "sys", "user", "not valid json {{"

    monkeypatch.setattr(describe_module, "_call_groq_marketing", _fake)

    with pytest.raises(DescriptionError):
        generate_descriptions(ExtractedFields(category="diya"), "hi")
    assert calls["n"] == 2


def test_marketing_rejects_hallucinated_extra_field(monkeypatch):
    bad = json.dumps(
        {"title": "Title", "description": _valid_description(), "material_cost_inr": 500}
    )
    good = json.dumps({"title": "Title", "description": _valid_description()})
    en_responses = iter([bad, good])

    def _fake(lang, facts):
        if lang == "en":
            return "sys", "user", next(en_responses)
        return "sys", "user", good

    monkeypatch.setattr(describe_module, "_call_groq_marketing", _fake)

    result = generate_descriptions(ExtractedFields(category="diya"), "hi")
    assert result.description_en is not None


def test_summary_llm_retries_once_then_succeeds(monkeypatch):
    monkeypatch.setattr(describe_module, "_call_groq_marketing", _mock_marketing())
    responses = iter(["not json", json.dumps({"summary": "a short factual summary sentence here"})])

    def _fake(facts, source_language):
        return "sys", "user", next(responses)

    monkeypatch.setattr(describe_module, "_call_groq_summary", _fake)

    result = generate_descriptions(ExtractedFields(category="diya"), "te")
    assert result.summary_spoken == "a short factual summary sentence here"


def test_request_failure_raises_description_error_without_retrying(monkeypatch):
    calls = {"n": 0}

    def _fake(lang, facts):
        calls["n"] += 1
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(describe_module, "_call_groq_marketing", _fake)

    with pytest.raises(DescriptionError):
        generate_descriptions(ExtractedFields(category="diya"), "hi")
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_logs_every_attempt(monkeypatch, _isolate_log_file):
    monkeypatch.setattr(describe_module, "_call_groq_marketing", _mock_marketing())

    generate_descriptions(ExtractedFields(category="diya"), "hi")

    lines = _isolate_log_file.read_text().strip().splitlines()
    assert len(lines) == 2  # one call for "en", one for "hi"

    first = json.loads(lines[0])
    assert first["task"] == "title_description_en"
    assert first["attempt"] == 1
    assert first["error"] is None


def test_colour_check_matches_whole_words_only():
    """"red" in "textured", "gold" in "marigold", "kala" in "Kalamkari" are not colours."""
    fields = ExtractedFields(category="art", craft_type="kalamkari", colours=["indigo"])
    assert describe_module._invented_colours("A textured, layered Kalamkari piece inspired by temple art", fields) == set()
    assert describe_module._invented_colours("Marigold motifs", fields) == set()
    assert describe_module._invented_colours("A red-and-gold border", fields) == {"red", "gold"}


def test_a_number_leaks_only_when_said_as_money_or_time():
    """A bare "4" in "set of 4" is not her 4 hours; "₹1,500" is her cost."""
    fields = ExtractedFields(material_cost_inr=1500, hours_worked=4)
    leak = describe_module._leaks_cost_or_hours
    assert not leak("A set of 4 lamps, 4 inches tall", fields)
    assert leak("Made over 4 hours by hand", fields)
    assert leak("Materials cost ₹1,500", fields)
    assert leak("इसे बनाने में 4 घंटे लगे", fields)
    assert leak("लागत 1500 रुपये", fields)


def test_the_readback_names_what_she_made_not_the_technique():
    fields = ExtractedFields(product_type="vase", craft_type="hand-painted", material="terracotta",
                             material_cost_inr=200, hours_worked=5.0)
    assert describe_module._build_summary_en(fields).startswith("This is your vase.")
    assert describe_module._facts_for_marketing(fields)["product_type"] == "vase"
