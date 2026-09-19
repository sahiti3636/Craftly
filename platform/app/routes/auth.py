"""Login: phone + one-time code for artisans, email + password for buyers.

See `app/auth.py` for why the two paths are different. What lives here is
only the HTTP shape of them, plus the one thing worth saying about the
artisan path: `POST /auth/artisan/request-otp` creates the account if the
phone is unknown. There is no separate "register" step, because an artisan
being onboarded at a cluster office by a field worker should not have to
know whether she already exists in a database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app import auth, config, security
from app.db import session
from app.models import Account, Artisan
from app.schemas import (
    AccountOut,
    AccountUpdate,
    BuyerLogin,
    BuyerRegister,
    OtpIssued,
    OtpRequest,
    OtpVerify,
    TokenIssued,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _account_out(account: Account) -> AccountOut:
    return AccountOut(
        account_id=account.account_id,
        role=account.role,
        name=account.name,
        phone=account.phone,
        email=account.email,
        artisan_id=account.artisan_id,
        language=account.language,
        ai_call_consent=account.ai_call_consent,
    )


def _token_out(db: Session, account: Account) -> TokenIssued:
    plaintext = auth.issue_token(db, account)
    return TokenIssued(
        access_token=plaintext,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=config.TOKEN_TTL_SECONDS),
        account_id=account.account_id,
        role=account.role,
        artisan_id=account.artisan_id,
        language=account.language,
    )


# -- artisan ----------------------------------------------------------------


@router.post("/artisan/request-otp", response_model=OtpIssued)
def request_otp(payload: OtpRequest, db: Session = Depends(session)) -> OtpIssued:
    account = auth.get_account_by_phone(db, payload.phone)
    if account is None:
        account = auth.create_artisan_account(
            db, phone=payload.phone, name=payload.name, language=payload.language
        )
    elif account.disabled:
        # Deliberately the same happy-looking response as an enabled
        # account, with no code actually issued. Telling a caller that a
        # number is disabled tells them the number is registered.
        return OtpIssued(
            challenge_id="otp_unavailable",
            expires_at=datetime.now(timezone.utc),
            delivery="none",
        )

    challenge, code = auth.issue_otp(db, payload.phone)
    db.commit()
    return OtpIssued(
        challenge_id=challenge.challenge_id,
        expires_at=challenge.expires_at,
        dev_code=code if config.OTP_ECHO else None,
        delivery="echo" if config.OTP_ECHO else "sms",
    )


@router.post("/artisan/verify", response_model=TokenIssued)
def verify_otp(payload: OtpVerify, db: Session = Depends(session)) -> TokenIssued:
    challenge = auth.verify_otp(db, payload.challenge_id, payload.code)
    account = auth.get_account_by_phone(db, challenge.phone)
    if account is None:  # pragma: no cover - request-otp always creates one
        account = auth.create_artisan_account(db, phone=challenge.phone)
    token = _token_out(db, account)
    db.commit()
    return token


# -- buyer ------------------------------------------------------------------


@router.post("/buyer/register", response_model=TokenIssued)
def register_buyer(payload: BuyerRegister, db: Session = Depends(session)) -> TokenIssued:
    if auth.get_account_by_email(db, payload.email) is not None:
        raise HTTPException(status_code=409, detail="That email is already registered.")
    account = auth.create_buyer_account(
        db, email=payload.email, password=payload.password, name=payload.name, phone=payload.phone
    )
    token = _token_out(db, account)
    db.commit()
    return token


@router.post("/buyer/login", response_model=TokenIssued)
def login_buyer(payload: BuyerLogin, db: Session = Depends(session)) -> TokenIssued:
    account = auth.get_account_by_email(db, payload.email)
    # One answer for a wrong password, an unknown email and a disabled
    # account. Distinguishing them enumerates the buyer list.
    if (
        account is None
        or account.disabled
        or not security.verify_secret(payload.password, account.password_hash)
    ):
        raise auth.AuthError("Those details do not match.")
    token = _token_out(db, account)
    db.commit()
    return token


# -- session ----------------------------------------------------------------


@router.get("/me", response_model=AccountOut)
def me(account: Account = Depends(auth.current_account)) -> AccountOut:
    return _account_out(account)


@router.patch("/me", response_model=AccountOut)
def update_me(
    payload: AccountUpdate,
    account: Account = Depends(auth.current_account),
    db: Session = Depends(session),
) -> AccountOut:
    if payload.name is not None:
        account.name = payload.name
    if payload.language is not None:
        account.language = payload.language
    if payload.ai_call_consent is not None:
        account.ai_call_consent = payload.ai_call_consent

    if payload.payout_upi is not None or payload.payout_account_name is not None:
        if not account.artisan_id:
            raise HTTPException(status_code=400, detail="Only an artisan has a payout account.")
        artisan = db.get(Artisan, account.artisan_id)
        if artisan is not None:
            if payload.payout_upi is not None:
                artisan.payout_upi = payload.payout_upi
            if payload.payout_account_name is not None:
                artisan.payout_account_name = payload.payout_account_name

    db.commit()
    return _account_out(account)


@router.post("/logout", status_code=204)
def logout(
    authorization: str | None = Header(None),
    account: Account = Depends(auth.current_account),
    db: Session = Depends(session),
) -> None:
    """Revoke the token that made this request.

    Possible because tokens are rows, not JWTs. A phone that is lost,
    borrowed or sold is the ordinary case here, not the exotic one.
    """
    if authorization:
        _, _, token = authorization.partition(" ")
        auth.revoke_token(db, token.strip())
    db.commit()
