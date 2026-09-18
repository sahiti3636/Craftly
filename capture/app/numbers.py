"""Deterministic money/duration parsing for Indian-language artisan speech.

No LLM, no network calls — this is a rule-based parser over the word
tables in app/lang/. It feeds a minimum-wage floor calculation, so it is
deliberately conservative: a number is only extracted as money or duration
when a currency/duration marker is actually adjacent to it. A bare number
with no marker (a year, a phone number, "hundred percent") is left alone
rather than guessed at, since a wrong guess here is worse than a miss.

Public API:
    extract_money(text, language) -> list[{"value_inr", "span", "raw"}]
    extract_duration(text, language) -> list[{"hours", "unit",
        "assumed_hours_per_day", "span", "raw"}]
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.lang import LanguageTables, get_tables

_TOKEN_RE = re.compile(r"₹|\w+")

HOURS_PER_WORKING_DAY = 8


@dataclass(frozen=True)
class _Token:
    text: str
    start: int
    end: int

    @property
    def lower(self) -> str:
        return self.text.lower()


def _tokenize(text: str) -> list[_Token]:
    return [_Token(m.group(), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]


def _is_digit_token(text: str) -> bool:
    return text.isdigit()


def _digit_value(text: str) -> int:
    return int(text)


def _scan_digit_runs(
    text: str, tokens: list[_Token]
) -> tuple[dict[int, tuple[float, int]], set[int]]:
    """Classify runs of adjacent digit tokens.

    Two digit tokens joined by exactly '.' in the source text are a
    decimal number (e.g. "3.5"). Two digit tokens adjacent for any other
    reason (comma-grouping like "1,20,000", stray whitespace-joined
    fragments) can't be reassembled reliably, so every token in that run
    is marked unsafe and excluded from extraction entirely — silently
    returning a fragment's value (e.g. "000" -> 0) would be far worse than
    not extracting it at all.

    Returns (decimal_pairs, unsafe_indices) where decimal_pairs maps a
    start index to (combined_value, end_index_exclusive).
    """
    decimal_pairs: dict[int, tuple[float, int]] = {}
    unsafe: set[int] = set()

    i = 0
    n = len(tokens)
    while i < n:
        if not _is_digit_token(tokens[i].text):
            i += 1
            continue
        if i + 1 < n and _is_digit_token(tokens[i + 1].text):
            sep = text[tokens[i].end : tokens[i + 1].start]
            if sep == ".":
                combined = float(f"{_digit_value(tokens[i].text)}.{tokens[i + 1].text}")
                decimal_pairs[i] = (combined, i + 2)
                i += 2
                continue
            run = [i, i + 1]
            j = i + 1
            while j + 1 < n and _is_digit_token(tokens[j + 1].text):
                run.append(j + 1)
                j += 1
            unsafe.update(run)
            i = j + 1
            continue
        i += 1

    return decimal_pairs, unsafe


def _parse_composite(tokens: list[_Token], start: int, tables: LanguageTables) -> tuple[float, int] | None:
    """Parse a word-based composite number starting at `start` (English or
    the requested language's grammar: accumulate ones/tens into `current`,
    and on a scale word flush `current * scale` into `total`).

    Also handles the Indian-English "digit-group" elision pattern, e.g.
    "one twenty" meaning 120 (hundred implied): a leading single-digit
    word (1-9) directly followed by a 10-99 word, with no scale word
    between them.
    """
    tok = tokens[start].lower
    if tok not in tables.number_words and tok not in tables.scale_words:
        return None

    total = 0.0
    current = 0.0
    consumed_any = False
    j = start
    n = len(tokens)

    while j < n:
        t = tokens[j].lower
        if t in tables.number_words:
            value = tables.number_words[t]
            if (
                current == 0
                and 1 <= value <= 9
                and j + 1 < n
                and tokens[j + 1].lower in tables.number_words
                and 10 <= tables.number_words[tokens[j + 1].lower] <= 99
            ):
                current = value * 100 + tables.number_words[tokens[j + 1].lower]
                j += 2
                consumed_any = True
                continue
            current += value
            j += 1
            consumed_any = True
            continue
        if t in tables.scale_words:
            scale = tables.scale_words[t]
            current = (current if current else 1) * scale
            total += current
            current = 0
            j += 1
            consumed_any = True
            continue
        break

    if not consumed_any:
        return None
    total += current
    return total, j


def _parse_number_phrase(
    tokens: list[_Token],
    start: int,
    tables: LanguageTables,
    decimal_pairs: dict[int, tuple[float, int]],
    unsafe_digits: set[int],
) -> tuple[float, int] | None:
    """Try to parse a number phrase starting at token index `start`.
    Returns (value, end_index_exclusive) or None.
    """
    if start in decimal_pairs:
        return decimal_pairs[start]

    tok = tokens[start].lower

    if _is_digit_token(tokens[start].text):
        if start in unsafe_digits:
            return None
        return float(_digit_value(tokens[start].text)), start + 1

    if tok in tables.fraction_prefixes:
        mode, amount = tables.fraction_prefixes[tok]
        if start + 1 < len(tokens):
            nxt = tokens[start + 1].lower
            if nxt in tables.scale_words:
                scale = tables.scale_words[nxt]
                multiplier = 1 + amount if mode == "add" else 1 - amount
                return scale * multiplier, start + 2
            if nxt in tables.number_words:
                base = tables.number_words[nxt]
                value = base + amount if mode == "add" else base - amount
                return value, start + 2
        # No recognized base follows (e.g. bare "sava ghanta"): the prefix
        # implicitly modifies 1, same as "sava" meaning "1.25" on its own.
        return (1 + amount if mode == "add" else 1 - amount), start + 1

    if tok in tables.standalone_fractions:
        base_value = tables.standalone_fractions[tok]
        if start + 1 < len(tokens) and tokens[start + 1].lower in tables.scale_words:
            return base_value * tables.scale_words[tokens[start + 1].lower], start + 2
        return base_value, start + 1

    return _parse_composite(tokens, start, tables)


def _finalize_money(value: float) -> int | float:
    if abs(value - round(value)) < 1e-9:
        return int(round(value))
    return round(value, 2)


def _finalize_hours(value: float) -> float:
    return round(value, 2)


def _scan(text: str, language: str, classify) -> list[dict]:
    tables = get_tables(language)
    tokens = _tokenize(text)
    decimal_pairs, unsafe_digits = _scan_digit_runs(text, tokens)

    results: list[dict] = []
    # Indices of marker tokens already used to qualify a match. Without
    # this, a single marker token can be double-counted: e.g. "paanch sau
    # rupaye, teen ghante" — "rupaye" legitimately qualifies "paanch sau"
    # (500) as marker-after, but the scan then resumes right at "rupaye"
    # itself, and since it's still sitting immediately before "teen", it
    # would also (wrongly) qualify "teen" as a second, spurious 3-rupee
    # match — "teen" is actually the start of an unrelated duration
    # phrase ("teen ghante"), not a second cost. A marker can only ever
    # belong to the one match it was actually adjacent to.
    consumed_markers: set[int] = set()
    i = 0
    n = len(tokens)
    while i < n:
        parsed = _parse_number_phrase(tokens, i, tables, decimal_pairs, unsafe_digits)
        if parsed is None:
            i += 1
            continue
        value, end = parsed

        before_idx = i - 1
        after_idx = end
        before = tokens[before_idx] if before_idx >= 0 and before_idx not in consumed_markers else None
        after = tokens[after_idx] if after_idx < n and after_idx not in consumed_markers else None

        classification = classify(value, before, after, tables)
        if classification is not None:
            extend_before = classification.pop("_extend_before", False)
            extend_after = classification.pop("_extend_after", False)

            start_off = tokens[i].start
            end_off = tokens[end - 1].end
            if extend_before and before is not None:
                start_off = min(start_off, before.start)
                consumed_markers.add(before_idx)
            if extend_after and after is not None:
                end_off = max(end_off, after.end)
                consumed_markers.add(after_idx)

            classification["span"] = (start_off, end_off)
            classification["raw"] = text[start_off:end_off]
            results.append(classification)

        i = end

    return results


def _classify_money(value: float, before: _Token | None, after: _Token | None, tables: LanguageTables) -> dict | None:
    before_hit = before is not None and before.lower in tables.currency_markers
    after_hit = after is not None and after.lower in tables.currency_markers
    if not (before_hit or after_hit):
        return None
    return {
        "value_inr": _finalize_money(value),
        "_extend_before": before_hit,
        "_extend_after": after_hit,
    }


def _classify_duration(value: float, before: _Token | None, after: _Token | None, tables: LanguageTables) -> dict | None:
    unit = None
    extend_before = False
    extend_after = False

    if after is not None and after.lower in tables.duration_units:
        unit = tables.duration_units[after.lower]
        extend_after = True
    elif before is not None and before.lower in tables.duration_units:
        unit = tables.duration_units[before.lower]
        extend_before = True

    if unit is None:
        return None

    if unit == "hours":
        hours = value
        assumed_hours_per_day = None
    elif unit == "minutes":
        hours = value / 60.0
        assumed_hours_per_day = None
    else:  # days
        hours = value * HOURS_PER_WORKING_DAY
        assumed_hours_per_day = HOURS_PER_WORKING_DAY

    return {
        "hours": _finalize_hours(hours),
        "unit": unit,
        "assumed_hours_per_day": assumed_hours_per_day,
        "_extend_before": extend_before,
        "_extend_after": extend_after,
    }


def extract_money(text: str, language: str) -> list[dict]:
    """Find money mentions in `text`.

    A number only counts as money when a currency marker (rupees, rupaye,
    ₹, rs, ...) is immediately adjacent to it — a bare number is never
    assumed to be money.

    Returns a list of {"value_inr": int | float, "span": (start, end),
    "raw": str}, in order of appearance. `span` is a (start, end)
    character offset pair into `text` (end exclusive); `raw` is the
    matched substring, including whichever currency marker qualified it.
    """
    return _scan(text, language, _classify_money)


def extract_duration(text: str, language: str) -> list[dict]:
    """Find duration mentions in `text`.

    A number only counts as a duration when an hours/days/minutes unit
    word is immediately adjacent to it. Days are converted to hours at
    8 working hours per day; that assumption is flagged in the result via
    `assumed_hours_per_day` (present only when the source unit was days).

    Returns a list of {"hours": float, "unit": "hours" | "minutes" |
    "days", "assumed_hours_per_day": int | None, "span": (start, end),
    "raw": str}, in order of appearance.
    """
    return _scan(text, language, _classify_duration)
