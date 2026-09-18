"""Telugu number/currency/duration word tables — not populated yet.

Same shape as app/lang/hi.py. Adding Telugu support is a matter of filling
these dicts in, not touching app/numbers.py.
"""

NUMBER_WORDS: dict[str, int] = {}
SCALE_WORDS: dict[str, int] = {}
FRACTION_PREFIXES: dict[str, tuple[str, float]] = {}
STANDALONE_FRACTIONS: dict[str, float] = {}
CURRENCY_MARKERS: set[str] = set()
DURATION_UNITS: dict[str, str] = {}
