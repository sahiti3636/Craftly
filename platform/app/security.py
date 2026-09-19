"""Password hashing, OTP minting, token minting. No third-party crypto.

Everything here is `hashlib` and `secrets` from the standard library, and
that is a deliberate limit rather than a compromise:

* **Passwords and OTPs go through `hashlib.scrypt`.** It is memory-hard,
  it is in the standard library, and it means this slice adds no wheel
  that has to compile on a teammate's machine at 2am. Parameters are
  stored in the hash string, so raising them later does not invalidate
  existing hashes.
* **Tokens are random, then stored as a plain SHA-256 of themselves.**
  Deliberately *not* scrypt: a token is already 256 bits of entropy from
  `secrets`, so there is nothing to brute-force, and a slow hash on every
  authenticated request would cost more than it buys. A password is short
  and human-chosen, so it gets the slow hash. The two are different
  problems and get different answers.
* **Every comparison is `compare_digest`.** A timing side channel on an
  OTP check is not theoretical when the code is six digits.

What is *not* here: JWT. A JWT would let this service verify a token
without a database lookup, and in exchange would make revoking one
impossible before it expires. This system hands tokens to phones that get
sold, lost and shared in a village; being able to revoke instantly is
worth a row lookup.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: scrypt parameters. n=2**14 is roughly 16MB and a few milliseconds, which
#: is the right trade for a login that happens once a month on a slow phone.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16


def hash_secret(plaintext: str) -> str:
    """Hash a password or an OTP. Returns a self-describing string."""
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        plaintext.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_secret(plaintext: str, stored: str | None) -> bool:
    """Constant-time check of a plaintext against a stored hash.

    A missing or malformed hash returns False rather than raising: an
    account with no password set must fail to log in, not 500.
    """
    if not stored:
        return False
    try:
        scheme, n, r, p, salt_hex, want_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            plaintext.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived.hex(), want_hex)


def new_token() -> str:
    """A bearer token. Shown to the client once and never stored in clear."""
    return f"crf_{secrets.token_urlsafe(32)}"


def token_fingerprint(token: str) -> str:
    """The stored form of a token. See the module docstring for why SHA-256."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_otp(digits: int = 6) -> str:
    """A numeric one-time code.

    `secrets.randbelow`, not `random`: the OTP is the whole of the artisan
    login, and `random` is seeded predictably enough to matter.
    """
    upper = 10**digits
    return str(secrets.randbelow(upper)).zfill(digits)


def normalise_phone(phone: str) -> str:
    """Reduce a phone number to a comparable form.

    An artisan enters her number as `98765 43210`, the app sends
    `+91 98765-43210`, and a field worker registering her types
    `098765 43210`. All three are the same phone and must be the same
    account, or she ends up with three and loses her listings.

    Indian numbers are normalised to a bare ten digits; anything else keeps
    its `+` and digits, because guessing a country code for a number this
    service does not recognise is how you merge two different people.
    """
    cleaned = "".join(ch for ch in phone.strip() if ch.isdigit() or ch == "+")
    if cleaned.startswith("+91"):
        cleaned = cleaned[3:]
    elif cleaned.startswith("91") and len(cleaned) == 12:
        cleaned = cleaned[2:]
    elif cleaned.startswith("0") and len(cleaned) == 11:
        cleaned = cleaned[1:]
    return cleaned
