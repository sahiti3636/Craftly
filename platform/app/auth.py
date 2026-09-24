"""Accounts, tokens and the FastAPI dependencies that guard a route.

Two login paths, because there are two kinds of person here and only one
of them can comfortably read.

**Artisans log in with a phone number and a one-time code.** No password.
The target user of this system may not read English, may not read at all,
and is being asked to use a smartphone for the first time; "choose a
password with a number and a symbol, and remember it" is a barrier
invented for a different user. A six-digit code read out by a voice she
understands is not. C2 owns the actual SMS or voice delivery — until that
exists, `CRAFTLY_OTP_ECHO` returns the code in the response so the flow is
completable end to end.

**Buyers log in with an email and a password.** A buyer placing a B2B
order for ninety thousand rupees of textiles is a procurement officer at a
desk, and a magic link to a shared purchasing inbox is worse for them than
a password.

The failure answers are deliberately flat. A wrong password, a disabled
account and an email nobody has registered all return the same
"those details do not match" — an endpoint that distinguishes them is an
endpoint that will enumerate the buyer list for anyone who asks.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, ids, security
from app.db import session
from app.models import Account, Artisan, OtpChallenge, Token


class AuthError(HTTPException):
    def __init__(self, detail: str, code: int = status.HTTP_401_UNAUTHORIZED) -> None:
        super().__init__(status_code=code, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes even from a timezone=True column."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def get_account_by_phone(db: Session, phone: str) -> Account | None:
    return db.scalar(
        select(Account).where(Account.phone == security.normalise_phone(phone))
    )


def get_account_by_email(db: Session, email: str) -> Account | None:
    return db.scalar(select(Account).where(Account.email == email.strip().lower()))


def create_artisan_account(
    db: Session,
    phone: str,
    name: str | None = None,
    artisan_id: str | None = None,
    language: str = "hi",
) -> Account:
    """Register an artisan, creating her Artisan record if she has none.

    The Artisan row is created here rather than left for later because a
    listing cannot exist without a maker, and an account that can capture
    but cannot publish is a dead end an artisan has no way to diagnose.
    """
    if artisan_id is None:
        artisan_id = ids.artisan_id()
        db.add(Artisan(artisan_id=artisan_id, name=name or "Artisan"))
        # Flushed before the Account that points at it. There is no ORM
        # relationship between the two — see the Account docstring for why
        # they are separate — so SQLAlchemy has no dependency to sort by
        # and would otherwise be free to insert the Account first, which
        # trips the foreign key.
        db.flush()
    account = Account(
        account_id=ids.account_id(),
        role="artisan",
        phone=security.normalise_phone(phone),
        name=name,
        artisan_id=artisan_id,
        language=language,
    )
    db.add(account)
    db.flush()
    return account


def create_buyer_account(
    db: Session, email: str, password: str, name: str, phone: str | None = None
) -> Account:
    account = Account(
        account_id=ids.account_id(),
        role="buyer",
        email=email.strip().lower(),
        phone=security.normalise_phone(phone) if phone else None,
        name=name,
        password_hash=security.hash_secret(password),
        language="en",
    )
    db.add(account)
    db.flush()
    return account


# ---------------------------------------------------------------------------
# OTP
# ---------------------------------------------------------------------------


def issue_otp(db: Session, phone: str) -> tuple[OtpChallenge, str]:
    """Mint a challenge. Returns the row and the plaintext code.

    The plaintext is returned to the *caller*, not stored, so the route can
    hand it to C2's delivery channel — or echo it in dev — without this
    module knowing how a code reaches a phone.
    """
    code = security.new_otp()
    challenge = OtpChallenge(
        challenge_id=ids.challenge_id(),
        phone=security.normalise_phone(phone),
        code_hash=security.hash_secret(code),
        expires_at=_utcnow() + timedelta(seconds=config.OTP_TTL_SECONDS),
    )
    db.add(challenge)
    db.flush()
    return challenge, code


def verify_otp(db: Session, challenge_id: str, code: str) -> OtpChallenge:
    """Consume a challenge, or raise with a reason the app can show.

    Attempts are counted on the row and the row is burned on success, so a
    code cannot be replayed and cannot be guessed more than
    `CRAFTLY_OTP_MAX_ATTEMPTS` times.
    """
    challenge = db.get(OtpChallenge, challenge_id)
    if challenge is None:
        raise AuthError("That code has expired. Ask for a new one.")
    if challenge.consumed_at is not None:
        raise AuthError("That code has already been used. Ask for a new one.")
    expires_at = _aware(challenge.expires_at)
    if expires_at is not None and expires_at < _utcnow():
        raise AuthError("That code has expired. Ask for a new one.")
    if challenge.attempts >= config.OTP_MAX_ATTEMPTS:
        raise AuthError("Too many attempts. Ask for a new code.", status.HTTP_429_TOO_MANY_REQUESTS)

    challenge.attempts += 1
    if not security.verify_secret(code, challenge.code_hash):
        # Committed, not flushed. The route is about to raise, the request
        # session is discarded without a commit, and a flushed increment
        # would roll back with it — leaving the attempt counter permanently
        # at zero and the cap below a decoration. A six-digit code with
        # unlimited guesses is a six-digit code with no security.
        db.commit()
        raise AuthError("That code is not right.")

    challenge.consumed_at = _utcnow()
    db.flush()
    return challenge


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def issue_token(db: Session, account: Account, label: str | None = None) -> str:
    """Mint a bearer token. The plaintext is returned once and never again."""
    plaintext = security.new_token()
    db.add(
        Token(
            token_hash=security.token_fingerprint(plaintext),
            account_id=account.account_id,
            expires_at=_utcnow() + timedelta(seconds=config.TOKEN_TTL_SECONDS),
            label=label,
        )
    )
    db.flush()
    return plaintext


def revoke_token(db: Session, plaintext: str) -> bool:
    row = db.get(Token, security.token_fingerprint(plaintext))
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = _utcnow()
    db.flush()
    return True


#: The one service account: C2, acting with CRAFTLY_SERVICE_TOKEN.
SERVICE_ACCOUNT_ID = "acc_service_integrations"


def service_account(db: Session) -> Account:
    """The account a CRAFTLY_SERVICE_TOKEN request acts as, made on first use.

    A real Account row rather than a special case, so an order moved along
    by the courier integration records who moved it like any other change.
    """
    account = db.get(Account, SERVICE_ACCOUNT_ID)
    if account is None:
        account = Account(account_id=SERVICE_ACCOUNT_ID, role="service", name="Integrations (C2)", language="en")
        db.add(account)
        db.flush()
    return account


def account_for_token(db: Session, plaintext: str) -> Account | None:
    row = db.get(Token, security.token_fingerprint(plaintext))
    if row is None or row.revoked_at is not None:
        return None
    expires_at = _aware(row.expires_at)
    if expires_at is not None and expires_at < _utcnow():
        return None
    account = db.get(Account, row.account_id)
    if account is None or account.disabled:
        return None
    return account


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def current_account_optional(
    authorization: str | None = Header(None),
    db: Session = Depends(session),
) -> Account | None:
    token = _bearer(authorization)
    if token is None:
        return None
    if config.SERVICE_TOKEN and hmac.compare_digest(token, config.SERVICE_TOKEN):
        return service_account(db)
    return account_for_token(db, token)


def current_account(
    account: Account | None = Depends(current_account_optional),
) -> Account:
    if account is None:
        raise AuthError("Sign in to do that.")
    return account


def current_service(account: Account = Depends(current_account)) -> Account:
    if account.role != "service":
        raise AuthError("That is a service action.", status.HTTP_403_FORBIDDEN)
    return account


def current_artisan(account: Account = Depends(current_account)) -> Account:
    if account.role != "artisan" or not account.artisan_id:
        raise AuthError("That is an artisan action.", status.HTTP_403_FORBIDDEN)
    return account


def current_buyer(account: Account = Depends(current_account)) -> Account:
    if account.role not in {"buyer", "service"}:
        raise AuthError("That is a buyer action.", status.HTTP_403_FORBIDDEN)
    return account
