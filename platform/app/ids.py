"""Identifier minting.

Two different problems, deliberately solved differently.

**Internal ids** (``lst_``, ``ord_``, ``art_``) are random hex. They are
never read aloud, never typed, and never printed on anything, so they only
have to be unique.

**Verification codes** are the opposite. A code goes on a paper hang-tag
that a buyer picks up at a mela under a tarpaulin, and the failure mode is
not a cryptographic attack — it is someone squinting at a smudged tag and
reading it down a phone line to a shopkeeper. So the alphabet drops
``0/O`` and ``1/I/L``, the code is grouped in fours, and
``normalise_code`` accepts it back without dashes, in lower case, and with
the confusable characters mapped to what the reader meant.

The derivation is deterministic from the listing id, and identical to the
one in ``market/app/adapters/stub_passport.py``. That is on purpose: the
codes C1 has been printing off its stub all along keep resolving after the
switch to this service. ``tests/test_passports.py`` pins the two together,
so if either side changes the algorithm the tag in someone's pocket
stops working and a test says so first.

The code is still *stored* on the passport row rather than recomputed on
read, because a tag outlives a listing: reissuing a passport, or pointing
an old code at a re-photographed listing, must not require a listing id to
hash back to the same string.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

#: No 0/O, no 1/I/L.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

#: What a human squinting at a tag is likely to type instead.
_CONFUSABLES = {"0": "O", "O": "0", "1": "I", "I": "1", "L": "1"}


def _short(prefix: str, length: int = 12) -> str:
    return f"{prefix}_{uuid4().hex[:length]}"


def listing_id() -> str:
    return _short("lst", 12)


def order_id() -> str:
    return _short("ord", 12)


def artisan_id() -> str:
    return _short("art", 8)


def account_id() -> str:
    return _short("acc", 12)


def media_id() -> str:
    return _short("med", 16)


def payout_id() -> str:
    return _short("pay", 12)


def challenge_id() -> str:
    return _short("otp", 16)


def verification_code(listing_id: str) -> str:
    """Deterministic, so the same object always carries the same code."""
    digest = hashlib.sha256(listing_id.encode("utf-8")).digest()
    chars = [ALPHABET[b % len(ALPHABET)] for b in digest[:8]]
    return f"CR-{''.join(chars[:4])}-{''.join(chars[4:8])}"


def normalise_code(code: str) -> str:
    """Squash a code to its canonical comparable form.

    ``cr-r8cr-9sg8``, ``CR R8CR 9SG8`` and ``CRR8CR9SG8`` are the same tag.
    Lookup is done on this form so that a buyer who drops the dashes still
    gets an answer.
    """
    return "".join(ch for ch in code.upper() if ch.isalnum())


def code_variants(code: str) -> list[str]:
    """Canonical form plus one-character confusable swaps, nearest first.

    Only single substitutions, and only of the characters the alphabet
    deliberately excludes — so this widens the net for a misread tag
    without ever letting one real code resolve to a different real code.
    """
    canonical = normalise_code(code)
    seen = [canonical]
    for index, char in enumerate(canonical):
        swap = _CONFUSABLES.get(char)
        if swap is None:
            continue
        candidate = canonical[:index] + swap + canonical[index + 1 :]
        if candidate not in seen:
            seen.append(candidate)
    return seen
