"""Hindi (Romanized) number/currency/duration word tables.

Values 1-20 and the round tens/hundred/thousand/lakh/crore are common and
high-confidence. Hindi's 21-99 are irregular (not compositional the way
English "twenty-five" is), so they're enumerated individually rather than
computed — this is exactly the kind of table this module is meant to hold
as data. Transliteration spelling varies a lot in practice (ASR output,
regional habits); a few common alternate spellings are included per word,
but this list is not exhaustive. Given this feeds a wage-floor
calculation, entries beyond the common ones (1-20, round tens, 100/1000)
should be spot-checked by a native/fluent speaker before relying on them
for anything beyond this prototype.
"""

NUMBER_WORDS: dict[str, int] = {
    "ek": 1,
    "do": 2,
    "teen": 3,
    "tin": 3,
    "chaar": 4,
    "char": 4,
    "paanch": 5,
    "panch": 5,
    "che": 6,
    "chhe": 6,
    "chhah": 6,
    "saat": 7,
    "aath": 8,
    "aaath": 8,
    "nau": 9,
    "das": 10,
    "dus": 10,
    "gyarah": 11,
    "baarah": 12,
    "barah": 12,
    "terah": 13,
    "chaudah": 14,
    "chaudh": 14,
    "pandrah": 15,
    "pandra": 15,
    "solah": 16,
    "satrah": 17,
    "satra": 17,
    "atharah": 18,
    "athara": 18,
    "unnees": 19,
    "unnis": 19,
    "bees": 20,
    "bis": 20,
    "ikkis": 21,
    "baais": 22,
    "bais": 22,
    "teis": 23,
    "chaubis": 24,
    "chaubees": 24,
    "pachees": 25,
    "pachis": 25,
    "chhabbis": 26,
    "sattais": 27,
    "atthais": 28,
    "unatis": 29,
    "unattees": 29,
    "tees": 30,
    "tis": 30,
    "ikatees": 31,
    "battees": 32,
    "batis": 32,
    "taintees": 33,
    "chauntees": 34,
    "paintees": 35,
    "chhattees": 36,
    "saintees": 37,
    "adtees": 38,
    "untaalees": 39,
    "chaalees": 40,
    "chalis": 40,
    "iktaalees": 41,
    "bayaalees": 42,
    "taintaalees": 43,
    "chavaalees": 44,
    "paintaalees": 45,
    "chhiyaalees": 46,
    "saintaalees": 47,
    "adtaalees": 48,
    "unchaas": 49,
    "pachaas": 50,
    "pachas": 50,
    "ikyaavan": 51,
    "baavan": 52,
    "tirpan": 53,
    "chauvan": 54,
    "pachpan": 55,
    "chhappan": 56,
    "sattaavan": 57,
    "atthaavan": 58,
    "unsath": 59,
    "saath": 60,
    "sath": 60,
    "iksath": 61,
    "baasath": 62,
    "tirsath": 63,
    "chausath": 64,
    "painsath": 65,
    "chhiyaasath": 66,
    "sarsath": 67,
    "arsath": 68,
    "unhattar": 69,
    "sattar": 70,
    "ikhattar": 71,
    "bahattar": 72,
    "tihattar": 73,
    "chauhattar": 74,
    "pachhattar": 75,
    "pachattar": 75,
    "chhihattar": 76,
    "sathattar": 77,
    "athhattar": 78,
    "unaasi": 79,
    "assi": 80,
    "ikyaasi": 81,
    "bayaasi": 82,
    "tiraasi": 83,
    "chauraasi": 84,
    "pachaasi": 85,
    "chhiyaasi": 86,
    "sattaasi": 87,
    "atthaasi": 88,
    "navaasi": 89,
    "nabbe": 90,
    "ikyaanave": 91,
    "baanave": 92,
    "tiraanave": 93,
    "chauraanave": 94,
    "pachaanave": 95,
    "chhiyaanave": 96,
    "sattaanave": 97,
    "atthaanave": 98,
    "ninyaanave": 99,
    "nirranve": 99,
}

SCALE_WORDS: dict[str, int] = {
    "sau": 100,
    "hazaar": 1000,
    "hazar": 1000,
    "lakh": 100_000,
    "laakh": 100_000,
    "crore": 10_000_000,
    "karod": 10_000_000,
}

# mode "add" -> value + amount * unit; mode "sub" -> value - amount * unit
# (unit = 1 for a plain count word, or the scale itself for a scale word;
# see app/numbers.py for how the two cases are distinguished).
FRACTION_PREFIXES: dict[str, tuple[str, float]] = {
    "sava": ("add", 0.25),
    "savaa": ("add", 0.25),
    "paune": ("sub", 0.25),
    "saadhe": ("add", 0.5),
    "sadhe": ("add", 0.5),
}

# Words that are themselves a fixed fraction, optionally scaled by a
# following scale word (e.g. "dedh sau" = 1.5 * 100 = 150).
STANDALONE_FRACTIONS: dict[str, float] = {
    "dedh": 1.5,
    "adhai": 2.5,
    "dhai": 2.5,
}

CURRENCY_MARKERS: set[str] = {"rupaye", "rupya", "rupye", "rupees", "rupee", "₹"}

DURATION_UNITS: dict[str, str] = {
    "ghanta": "hours",
    "ghante": "hours",
    "ghanton": "hours",
    "din": "days",
    "dino": "days",
    "minute": "minutes",
    "minat": "minutes",
}

# ---------------------------------------------------------------------------
# Devanagari
# ---------------------------------------------------------------------------
# Whisper writes Hindi speech in Devanagari, so the same words appear in
# that script too. Spelled as app/numbers.py folds them before lookup: no
# nukta (हजार, not हज़ार) and anusvara for chandrabindu (पांच, not पाँच) —
# tests/test_numbers.py checks every key here is already in that form. The
# same native-speaker caveat as the tables above applies beyond 1-20.

NUMBER_WORDS.update({
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "छह": 6, "छः": 6, "छे": 6,
    "सात": 7, "आठ": 8, "नौ": 9, "पाच": 5, "दस": 10, "ग्यारह": 11, "बारह": 12, "तेरह": 13,
    "चौदह": 14, "पंद्रह": 15, "पन्द्रह": 15, "सोलह": 16, "सत्रह": 17, "अठारह": 18,
    "उन्नीस": 19, "बीस": 20, "इक्कीस": 21, "बाईस": 22, "तेईस": 23, "चौबीस": 24,
    "पच्चीस": 25, "छब्बीस": 26, "सत्ताईस": 27, "अट्ठाईस": 28, "उनतीस": 29, "तीस": 30,
    "इकतीस": 31, "बत्तीस": 32, "तैंतीस": 33, "चौंतीस": 34, "पैंतीस": 35, "छत्तीस": 36,
    "सैंतीस": 37, "अडतीस": 38, "उनतालीस": 39, "चालीस": 40, "इकतालीस": 41,
    "बयालीस": 42, "तैंतालीस": 43, "चवालीस": 44, "चौवालीस": 44, "पैंतालीस": 45,
    "छियालीस": 46, "सैंतालीस": 47, "अडतालीस": 48, "उनचास": 49, "पचास": 50,
    "इक्यावन": 51, "बावन": 52, "तिरेपन": 53, "तिरपन": 53, "चौवन": 54, "पचपन": 55,
    "छप्पन": 56, "सत्तावन": 57, "अट्ठावन": 58, "उनसठ": 59, "साठ": 60, "इकसठ": 61,
    "बासठ": 62, "तिरेसठ": 63, "तिरसठ": 63, "चौंसठ": 64, "पैंसठ": 65, "छियासठ": 66,
    "सडसठ": 67, "सरसठ": 67, "अडसठ": 68, "उनहत्तर": 69, "सत्तर": 70, "इकहत्तर": 71,
    "बहत्तर": 72, "तिहत्तर": 73, "चौहत्तर": 74, "पचहत्तर": 75, "छिहत्तर": 76,
    "सतहत्तर": 77, "अठहत्तर": 78, "उन्यासी": 79, "उनासी": 79, "अस्सी": 80,
    "इक्यासी": 81, "बयासी": 82, "तिरासी": 83, "चौरासी": 84, "पचासी": 85,
    "छियासी": 86, "सत्तासी": 87, "अट्ठासी": 88, "नवासी": 89, "नब्बे": 90,
    "इक्यानवे": 91, "बानवे": 92, "तिरानवे": 93, "चौरानवे": 94, "पंचानवे": 95,
    "छियानवे": 96, "सत्तानवे": 97, "अट्ठानवे": 98, "निन्यानवे": 99,
})

# "सो" is how Whisper often spells सौ; like any scale word it only counts after a number.
SCALE_WORDS.update({"सौ": 100, "सो": 100, "हजार": 1000, "लाख": 100_000, "करोड": 10_000_000})

FRACTION_PREFIXES.update({"सवा": ("add", 0.25), "पौने": ("sub", 0.25), "साढे": ("add", 0.5)})

STANDALONE_FRACTIONS.update({"डेढ": 1.5, "ढाई": 2.5, "अढाई": 2.5})

# Whisper spells rupees several ways (long ू, a half य); all are the same word.
CURRENCY_MARKERS.update({"रुपये", "रुपए", "रुपया", "रुपयों", "रुपयो", "रु", "रूपये", "रूपए", "रूपया", "रूप्ये", "रुप्ये", "रूपयों",
                         "रुपिये", "रुपीये", "रुप्य", "रूप्य", "रुपे"})

DURATION_UNITS.update({
    "घंटा": "hours", "घंटे": "hours", "घंटों": "hours", "घण्टे": "hours", "घण्टा": "hours",
    "घन्टे": "hours", "घन्टा": "hours",
    # How Whisper mishears घंटे; only ever read straight after a number.
    "खंटे": "hours", "खंटा": "hours", "गंटे": "hours", "गंते": "hours", "कंटे": "hours",
    "दिन": "days", "दिनों": "days", "मिनट": "minutes",
})
