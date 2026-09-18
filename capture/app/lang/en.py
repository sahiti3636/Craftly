"""English number/currency/duration word tables.

Always merged into every language's table set (see app/lang/__init__.py),
since artisan speech is routinely code-switched — e.g. a Hindi sentence
carrying an English number ("one twenty rupaye").
"""

NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

# Indian English also uses lakh/crore, so they live here rather than only
# in the Hindi table.
SCALE_WORDS: dict[str, int] = {
    "hundred": 100,
    "thousand": 1000,
    "lakh": 100_000,
    "lac": 100_000,
    "crore": 10_000_000,
}

# sava/paune/saadhe-style quarter/half modifiers have no English equivalent.
FRACTION_PREFIXES: dict[str, tuple[str, float]] = {}

# dedh/adhai-style standalone fraction words have no English equivalent.
STANDALONE_FRACTIONS: dict[str, float] = {}

CURRENCY_MARKERS: set[str] = {"rupees", "rupee", "rs", "inr", "₹"}

DURATION_UNITS: dict[str, str] = {
    "hour": "hours",
    "hours": "hours",
    "minute": "minutes",
    "minutes": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "day": "days",
    "days": "days",
}
