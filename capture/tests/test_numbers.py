"""Tests for app/numbers.py.

This module feeds a minimum-wage floor calculation, so correctness here
matters more than almost anywhere else in the codebase. Alongside the
documented patterns (digits, spoken words, Hindi fractions, scale words,
currency/duration markers, code-switching), this file leans hard on
adversarial cases: numbers that must NOT be extracted (years, phone
numbers, "hundred percent"), multiple numbers in one sentence, and
malformed/ambiguous numeric input (comma-grouped digits) that must fail
safe (empty result) rather than silently return a wrong value.
"""

import pytest

from app.numbers import extract_duration, extract_money

# ---------------------------------------------------------------------------
# extract_money — digits
# ---------------------------------------------------------------------------


def test_money_latin_digits_before_marker_word():
    result = extract_money("500 rupaye mila", "hi")
    assert result == [{"value_inr": 500, "span": (0, 10), "raw": "500 rupaye"}]


def test_money_latin_digits_with_rupees_english_word():
    result = extract_money("I got 500 rupees for it", "en")
    assert result == [{"value_inr": 500, "span": (6, 16), "raw": "500 rupees"}]


def test_money_devanagari_digits():
    result = extract_money("१२० rupaye diye", "hi")
    assert result == [{"value_inr": 120, "span": (0, 10), "raw": "१२० rupaye"}]


def test_money_rupee_symbol_prefix_no_space():
    result = extract_money("₹500 mein bik gaya", "hi")
    assert result == [{"value_inr": 500, "span": (0, 4), "raw": "₹500"}]


def test_money_rupee_symbol_with_devanagari_digits():
    result = extract_money("₹१२० mein bana", "hi")
    assert result == [{"value_inr": 120, "span": (0, 4), "raw": "₹१२०"}]


def test_money_rs_abbreviation_with_period_and_space():
    result = extract_money("Rs. 500 mein bana", "hi")
    assert result == [{"value_inr": 500, "span": (0, 7), "raw": "Rs. 500"}]


def test_money_rs_abbreviation_no_space():
    result = extract_money("cost is Rs.500 total", "en")
    assert result == [{"value_inr": 500, "span": (8, 14), "raw": "Rs.500"}]


def test_money_marker_before_the_number():
    result = extract_money("rupaye paanch sau diye", "hi")
    assert result == [{"value_inr": 500, "span": (0, 17), "raw": "rupaye paanch sau"}]


# ---------------------------------------------------------------------------
# extract_money — spoken English numbers
# ---------------------------------------------------------------------------


def test_money_english_words_hundred_compound():
    result = extract_money("material cost was one hundred twenty rupees", "en")
    assert result == [
        {"value_inr": 120, "span": (18, 43), "raw": "one hundred twenty rupees"}
    ]


def test_money_english_words_thousand():
    result = extract_money("she paid two thousand five hundred rupees", "en")
    assert result == [
        {"value_inr": 2500, "span": (9, 41), "raw": "two thousand five hundred rupees"}
    ]


def test_money_indian_english_elision_one_twenty():
    result = extract_money("material ka cost one twenty rupaye", "hi")
    assert result == [{"value_inr": 120, "span": (17, 34), "raw": "one twenty rupaye"}]


def test_money_indian_english_elision_two_fifty():
    result = extract_money("kimat two fifty rupees hai", "en")
    assert result == [{"value_inr": 250, "span": (6, 22), "raw": "two fifty rupees"}]


def test_money_plain_tens_and_ones_not_elided():
    # "twenty five" must resolve to 25, not trigger the leading-digit
    # elision pattern (which only fires when the first word is 1-9).
    result = extract_money("twenty five rupees only", "en")
    assert result == [{"value_inr": 25, "span": (0, 18), "raw": "twenty five rupees"}]


# ---------------------------------------------------------------------------
# extract_money — Hindi scale words
# ---------------------------------------------------------------------------


def test_money_hindi_sau():
    result = extract_money("paanch sau rupaye", "hi")
    assert result == [{"value_inr": 500, "span": (0, 17), "raw": "paanch sau rupaye"}]


def test_money_hindi_hazaar():
    result = extract_money("do hazaar rupaye mile", "hi")
    assert result == [{"value_inr": 2000, "span": (0, 16), "raw": "do hazaar rupaye"}]


def test_money_hindi_lakh():
    result = extract_money("ek lakh rupaye ka order", "hi")
    assert result == [{"value_inr": 100_000, "span": (0, 14), "raw": "ek lakh rupaye"}]


def test_money_hindi_lakh_and_hazaar_compound():
    result = extract_money("teen lakh pachaas hazaar rupaye", "hi")
    assert result == [
        {"value_inr": 350_000, "span": (0, 31), "raw": "teen lakh pachaas hazaar rupaye"}
    ]


def test_money_bare_scale_word_hundred():
    result = extract_money("hundred rupees only", "en")
    assert result == [{"value_inr": 100, "span": (0, 14), "raw": "hundred rupees"}]


# ---------------------------------------------------------------------------
# extract_money — Hindi fraction words
# ---------------------------------------------------------------------------


def test_money_sava_sau_equals_125():
    result = extract_money("sava sau rupaye", "hi")
    assert result == [{"value_inr": 125, "span": (0, 15), "raw": "sava sau rupaye"}]


def test_money_dedh_sau_equals_150():
    result = extract_money("dedh sau rupaye", "hi")
    assert result == [{"value_inr": 150, "span": (0, 15), "raw": "dedh sau rupaye"}]


def test_money_adhai_sau_equals_250():
    result = extract_money("adhai sau rupaye", "hi")
    assert result == [{"value_inr": 250, "span": (0, 16), "raw": "adhai sau rupaye"}]


def test_money_paune_sau_equals_75():
    result = extract_money("paune sau rupaye", "hi")
    assert result == [{"value_inr": 75, "span": (0, 16), "raw": "paune sau rupaye"}]


def test_money_saadhe_sau_equals_150():
    result = extract_money("saadhe sau rupaye", "hi")
    assert result == [{"value_inr": 150, "span": (0, 17), "raw": "saadhe sau rupaye"}]


def test_money_dedh_hazaar_equals_1500():
    result = extract_money("dedh hazaar rupaye", "hi")
    assert result == [{"value_inr": 1500, "span": (0, 18), "raw": "dedh hazaar rupaye"}]


def test_money_sava_applied_to_small_count():
    # sava + a plain count word means count + 0.25, not count * 1.25.
    result = extract_money("sava do rupaye", "hi")
    assert result == [{"value_inr": 2.25, "span": (0, 14), "raw": "sava do rupaye"}]


def test_money_paune_applied_to_small_count():
    result = extract_money("paune do rupaye", "hi")
    assert result == [{"value_inr": 1.75, "span": (0, 15), "raw": "paune do rupaye"}]


# ---------------------------------------------------------------------------
# extract_money — currency markers
# ---------------------------------------------------------------------------


def test_money_marker_rupaye():
    assert extract_money("teen sau rupaye", "hi")[0]["value_inr"] == 300


def test_money_marker_rupya_alt_spelling():
    assert extract_money("teen sau rupya", "hi")[0]["value_inr"] == 300


def test_money_marker_rs_bare():
    assert extract_money("rs 300", "en")[0]["value_inr"] == 300


# ---------------------------------------------------------------------------
# extract_money — code-switching
# ---------------------------------------------------------------------------


def test_money_code_switched_hindi_sentence_english_number():
    result = extract_money("material ka cost one twenty rupaye tha", "hi")
    assert result == [{"value_inr": 120, "span": (17, 34), "raw": "one twenty rupaye"}]


def test_money_code_switched_english_number_hindi_marker():
    result = extract_money("cost five hundred rupaye hai", "hi")
    assert result == [{"value_inr": 500, "span": (5, 24), "raw": "five hundred rupaye"}]


# ---------------------------------------------------------------------------
# extract_money — multiple numbers / no false positives
# ---------------------------------------------------------------------------


def test_money_multiple_mentions_in_one_sentence():
    result = extract_money("paanch sau rupaye ki mitti aur teen sau rupaye ka rang", "hi")
    assert [r["value_inr"] for r in result] == [500, 300]


def test_money_ignores_duration_number_in_same_sentence():
    result = extract_money("paanch sau rupaye ki mitti aur dedh ghante ka kaam", "hi")
    assert len(result) == 1
    assert result[0]["value_inr"] == 500


def test_money_marker_is_not_reused_for_an_adjacent_unrelated_number():
    # Regression test: "rupaye" legitimately qualifies "paanch sau" (500)
    # as its marker-after. The scan resumes right at "rupaye" itself, and
    # since "rupaye" is also immediately before "teen", a marker-reuse bug
    # made "teen" wrongly qualify as a second, spurious 3-rupee match —
    # "teen" is actually the start of an unrelated duration phrase ("teen
    # ghante"), not a second cost. A marker must only ever qualify the one
    # match it was actually adjacent to.
    result = extract_money("paanch sau rupaye, teen ghante", "hi")
    assert result == [{"value_inr": 500, "span": (0, 17), "raw": "paanch sau rupaye"}]


def test_duration_marker_is_not_reused_for_an_adjacent_unrelated_number():
    # Same bug, mirrored for extract_duration: "ghante" must not get
    # reused as a marker for a following unrelated number.
    result = extract_duration("teen ghante, paanch sau", "hi")
    assert result == [{"hours": 3.0, "unit": "hours", "assumed_hours_per_day": None, "span": (0, 11), "raw": "teen ghante"}]


def test_money_two_markers_back_to_back_each_qualify_their_own_number():
    # Confirms the fix doesn't over-suppress: two independent
    # marker-qualified numbers right next to each other must both still
    # be found, each keyed to its own (distinct) marker token.
    result = extract_money("500 rupaye 500 rupaye", "hi")
    assert [r["value_inr"] for r in result] == [500, 500]


def test_money_rejects_hundred_percent():
    assert extract_money("yeh hundred percent silk hai", "hi") == []


def test_money_rejects_year_mention():
    assert extract_money("yeh diya 2020 mein banaya tha", "hi") == []


def test_money_rejects_phone_number():
    assert extract_money("mera number hai 9876543210", "hi") == []


def test_money_rejects_phone_number_with_unrelated_marker_nearby():
    assert extract_money("mera number hai 9876543210, ghante baad call karo", "hi") == []


def test_money_rejects_bare_number_with_no_marker():
    assert extract_money("humne teen sau banaye", "hi") == []


def test_money_rejects_comma_grouped_digits_rather_than_returning_wrong_value():
    # "1,20,000" tokenizes to fragments "1", "20", "000" — none of these
    # may be reported (in particular NOT the dangerous {"value_inr": 0}
    # that a naive parser would emit for the trailing "000" fragment).
    assert extract_money("1,20,000 rupaye mile", "hi") == []


def test_money_accepts_decimal_digit_amount():
    result = extract_money("3.5 rupaye prati kilo", "hi")
    assert result == [{"value_inr": 3.5, "span": (0, 10), "raw": "3.5 rupaye"}]


# ---------------------------------------------------------------------------
# extract_duration — units
# ---------------------------------------------------------------------------


def test_duration_hours_english():
    result = extract_duration("it took three hours", "en")
    assert result == [
        {
            "hours": 3.0,
            "unit": "hours",
            "assumed_hours_per_day": None,
            "span": (8, 19),
            "raw": "three hours",
        }
    ]


def test_duration_ghante_hindi():
    result = extract_duration("teen ghante ka kaam tha", "hi")
    assert result[0]["hours"] == 3.0
    assert result[0]["unit"] == "hours"
    assert result[0]["assumed_hours_per_day"] is None


def test_duration_ghanta_singular():
    result = extract_duration("ek ghanta laga", "hi")
    assert result[0]["hours"] == 1.0


def test_duration_minutes_english():
    result = extract_duration("it took forty five minutes", "en")
    assert result[0]["hours"] == 0.75
    assert result[0]["unit"] == "minutes"


def test_duration_minute_hindi():
    result = extract_duration("45 minute laga isme", "hi")
    assert result[0]["hours"] == 0.75
    assert result[0]["unit"] == "minutes"


def test_duration_days_converted_to_hours_with_flag():
    result = extract_duration("teen din laga banane mein", "hi")
    assert result == [
        {
            "hours": 24.0,
            "unit": "days",
            "assumed_hours_per_day": 8,
            "span": (0, 8),
            "raw": "teen din",
        }
    ]


def test_duration_days_english_flags_assumption():
    result = extract_duration("it took two days to finish", "en")
    assert result[0]["hours"] == 16.0
    assert result[0]["assumed_hours_per_day"] == 8


def test_duration_fractional_day():
    result = extract_duration("dedh din laga", "hi")
    assert result[0]["hours"] == 12.0
    assert result[0]["assumed_hours_per_day"] == 8


# ---------------------------------------------------------------------------
# extract_duration — Hindi fraction words
# ---------------------------------------------------------------------------


def test_duration_dedh_ghante_equals_1_5_hours():
    result = extract_duration("dedh ghante lagte hain", "hi")
    assert result[0]["hours"] == 1.5


def test_duration_adhai_ghante_equals_2_5_hours():
    result = extract_duration("adhai ghante lage isme", "hi")
    assert result[0]["hours"] == 2.5


def test_duration_bare_sava_before_unit_equals_1_25_hours():
    # "sava ghanta" with no explicit base number implicitly means 1.25.
    result = extract_duration("sava ghanta laga", "hi")
    assert result[0]["hours"] == 1.25


def test_duration_bare_paune_before_unit_equals_0_75_hours():
    result = extract_duration("paune ghante mein khatam", "hi")
    assert result[0]["hours"] == 0.75


def test_duration_saadhe_teen_ghante_equals_3_5_hours():
    result = extract_duration("saadhe teen ghante lage", "hi")
    assert result[0]["hours"] == 3.5


# ---------------------------------------------------------------------------
# extract_duration — code-switching, multiple mentions, adversarial
# ---------------------------------------------------------------------------


def test_duration_code_switched_sentence():
    result = extract_duration("kaam mein three hours lage the", "hi")
    assert result[0]["hours"] == 3.0


def test_duration_multiple_mentions_in_one_sentence():
    result = extract_duration("teen ghante ka kaam, phir tees minute ka rest", "hi")
    assert [r["hours"] for r in result] == [3.0, 0.5]


def test_duration_ignores_money_number_in_same_sentence():
    result = extract_duration("paanch sau rupaye ki mitti aur dedh ghante ka kaam", "hi")
    assert len(result) == 1
    assert result[0]["hours"] == 1.5


def test_duration_rejects_years_saal_not_a_unit():
    # "saal" (year) is deliberately not in DURATION_UNITS.
    assert extract_duration("do saal se yeh kaam kar raha hoon", "hi") == []


def test_duration_rejects_bare_number_with_no_unit():
    assert extract_duration("humne teen banaye", "hi") == []


def test_duration_rejects_phone_number():
    assert extract_duration("mera number hai 9876543210", "hi") == []


def test_duration_rejects_comma_grouped_digits():
    assert extract_duration("1,20,000 ghante", "hi") == []


# ---------------------------------------------------------------------------
# empty stub languages (te/kn/ta) — should not crash, and fall back to
# English-only recognition since their tables are empty by design.
# ---------------------------------------------------------------------------


def test_stub_language_te_falls_back_to_english_only():
    assert extract_money("500 rupees", "te") == [
        {"value_inr": 500, "span": (0, 10), "raw": "500 rupees"}
    ]
    assert extract_money("paanch sau rupaye", "te") == []


def test_stub_language_kn_does_not_crash_on_empty_text():
    assert extract_money("", "kn") == []
    assert extract_duration("", "kn") == []


def test_stub_language_ta_falls_back_to_english_only():
    assert extract_duration("three hours", "ta") == [
        {
            "hours": 3.0,
            "unit": "hours",
            "assumed_hours_per_day": None,
            "span": (0, 11),
            "raw": "three hours",
        }
    ]


# ---------------------------------------------------------------------------
# both functions — empty / no-match input
# ---------------------------------------------------------------------------


def test_money_empty_text_returns_empty_list():
    assert extract_money("", "hi") == []


def test_duration_empty_text_returns_empty_list():
    assert extract_duration("", "hi") == []


def test_money_no_numbers_at_all():
    assert extract_money("bahut sundar kaam hai", "hi") == []


def test_duration_no_numbers_at_all():
    assert extract_duration("bahut sundar kaam hai", "hi") == []


# ---------------------------------------------------------------------------
# Devanagari — how Whisper writes Hindi speech
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,money,hours",
    [
        ("सामान की लागत दो सौ रुपये है और इसे बनाने में पाँच घंटे लगे।", 200, 5.0),
        ("लागत डेढ़ हज़ार रुपये, साढ़े तीन घंटे", 1500, 3.5),
        ("पांच सौ रुपए का सामान, बारह घंटे", 500, 12.0),
        ("₹२०० और ४ घंटे", 200, 4.0),
        ("सवा सौ रुपये, ढाई घंटे", 125, 2.5),
    ],
)
def test_devanagari_money_and_hours(text, money, hours):
    assert extract_money(text, "hi")[0]["value_inr"] == money
    assert extract_duration(text, "hi")[0]["hours"] == hours


def test_devanagari_words_are_not_split_at_vowel_signs():
    """\w alone split "दो" into "द" and read no number at all."""
    assert extract_money("दो सौ रुपये", "hi")[0]["value_inr"] == 200


def test_devanagari_tables_are_stored_folded():
    """Keys must match what _Token.lower produces, or they never match."""
    from app.lang import hi
    from app.numbers import _fold_devanagari

    tables = (hi.NUMBER_WORDS, hi.SCALE_WORDS, hi.FRACTION_PREFIXES, hi.STANDALONE_FRACTIONS,
              hi.DURATION_UNITS, hi.CURRENCY_MARKERS)
    assert [k for table in tables for k in table if _fold_devanagari(k) != k] == []
