"""Registry of per-language number/currency/duration word tables.

To add a new language: create app/lang/<code>.py with the same six names
as app/lang/hi.py (NUMBER_WORDS, SCALE_WORDS, FRACTION_PREFIXES,
STANDALONE_FRACTIONS, CURRENCY_MARKERS, DURATION_UNITS), then register it
in _LANGUAGE_MODULES below. No changes to app/numbers.py are needed.
"""

from types import ModuleType

from app.lang import en, hi, kn, ta, te

_LANGUAGE_MODULES: dict[str, ModuleType] = {
    "en": en,
    "hi": hi,
    "te": te,
    "kn": kn,
    "ta": ta,
}


class LanguageTables:
    def __init__(
        self,
        number_words: dict[str, int],
        scale_words: dict[str, int],
        fraction_prefixes: dict[str, tuple[str, float]],
        standalone_fractions: dict[str, float],
        currency_markers: set[str],
        duration_units: dict[str, str],
    ) -> None:
        self.number_words = number_words
        self.scale_words = scale_words
        self.fraction_prefixes = fraction_prefixes
        self.standalone_fractions = standalone_fractions
        self.currency_markers = currency_markers
        self.duration_units = duration_units


def get_tables(language: str) -> LanguageTables:
    """English word tables are always included, merged with the requested
    language's tables — artisan speech is routinely code-switched (e.g. a
    Hindi sentence carrying an English number like "one twenty rupaye"),
    so a Hindi-only lookup would miss real input. An unrecognized language
    code just falls back to English-only.
    """
    number_words = dict(en.NUMBER_WORDS)
    scale_words = dict(en.SCALE_WORDS)
    fraction_prefixes = dict(en.FRACTION_PREFIXES)
    standalone_fractions = dict(en.STANDALONE_FRACTIONS)
    currency_markers = set(en.CURRENCY_MARKERS)
    duration_units = dict(en.DURATION_UNITS)

    mod = _LANGUAGE_MODULES.get(language.lower())
    if mod is not None and mod is not en:
        number_words.update(mod.NUMBER_WORDS)
        scale_words.update(mod.SCALE_WORDS)
        fraction_prefixes.update(mod.FRACTION_PREFIXES)
        standalone_fractions.update(mod.STANDALONE_FRACTIONS)
        currency_markers.update(mod.CURRENCY_MARKERS)
        duration_units.update(mod.DURATION_UNITS)

    return LanguageTables(
        number_words=number_words,
        scale_words=scale_words,
        fraction_prefixes=fraction_prefixes,
        standalone_fractions=standalone_fractions,
        currency_markers=currency_markers,
        duration_units=duration_units,
    )
