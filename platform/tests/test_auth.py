"""Login, tokens and who is allowed to do what."""

from __future__ import annotations

from app import security
from app.models import Account, OtpChallenge


# -- artisan: phone + one-time code ------------------------------------------


def test_requesting_a_code_for_an_unknown_phone_creates_the_account(client, session):
    response = client.post("/auth/artisan/request-otp", json={"phone": "9812345678"})
    assert response.status_code == 200
    assert len(response.json()["dev_code"]) == 6

    account = session.query(Account).filter(Account.phone == "9812345678").one()
    # She gets an Artisan record in the same breath: an account that can
    # capture but not publish is a dead end she cannot diagnose.
    assert account.artisan_id is not None


def test_the_code_is_never_stored_in_clear(client, session):
    issued = client.post("/auth/artisan/request-otp", json={"phone": "9812345678"}).json()
    row = session.get(OtpChallenge, issued["challenge_id"])
    assert issued["dev_code"] not in row.code_hash
    assert row.code_hash.startswith("scrypt$")


def test_a_wrong_code_is_refused_and_counted(client, session):
    issued = client.post("/auth/artisan/request-otp", json={"phone": "9812345678"}).json()
    response = client.post(
        "/auth/artisan/verify", json={"challenge_id": issued["challenge_id"], "code": "000000"}
    )
    assert response.status_code == 401
    assert session.get(OtpChallenge, issued["challenge_id"]).attempts == 1


def test_a_code_cannot_be_used_twice(client):
    issued = client.post("/auth/artisan/request-otp", json={"phone": "9812345678"}).json()
    body = {"challenge_id": issued["challenge_id"], "code": issued["dev_code"]}
    assert client.post("/auth/artisan/verify", json=body).status_code == 200
    assert client.post("/auth/artisan/verify", json=body).status_code == 401


def test_guessing_is_capped(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "OTP_MAX_ATTEMPTS", 3)
    issued = client.post("/auth/artisan/request-otp", json={"phone": "9812345678"}).json()
    body = {"challenge_id": issued["challenge_id"], "code": "000000"}
    for _ in range(3):
        client.post("/auth/artisan/verify", json=body)
    assert client.post("/auth/artisan/verify", json=body).status_code == 429


def test_the_same_phone_written_three_ways_is_one_account(client, session):
    for phone in ("9812345678", "+91 98123 45678", "098123-45678"):
        client.post("/auth/artisan/request-otp", json={"phone": phone})
    assert session.query(Account).count() == 1


# -- buyer: email + password -------------------------------------------------


def test_buyer_register_then_login(client):
    registered = client.post(
        "/auth/buyer/register",
        json={"email": "Procurement@Example.com", "password": "correct horse", "name": "A Buyer"},
    )
    assert registered.status_code == 200
    assert registered.json()["role"] == "buyer"

    # Email is case-folded, so a buyer who capitalises differently next
    # time still reaches their own account rather than creating a second.
    logged_in = client.post(
        "/auth/buyer/login", json={"email": "procurement@example.com", "password": "correct horse"}
    )
    assert logged_in.status_code == 200


def test_a_wrong_password_and_an_unknown_email_look_identical(client):
    client.post(
        "/auth/buyer/register",
        json={"email": "a@example.com", "password": "correct horse", "name": "A"},
    )
    wrong = client.post("/auth/buyer/login", json={"email": "a@example.com", "password": "nope"})
    unknown = client.post(
        "/auth/buyer/login", json={"email": "nobody@example.com", "password": "nope"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_passwords_are_hashed_not_stored(client, session):
    client.post(
        "/auth/buyer/register",
        json={"email": "a@example.com", "password": "correct horse", "name": "A"},
    )
    account = session.query(Account).filter(Account.email == "a@example.com").one()
    assert "correct horse" not in (account.password_hash or "")
    assert security.verify_secret("correct horse", account.password_hash)


def test_a_duplicate_registration_is_refused(client):
    body = {"email": "a@example.com", "password": "correct horse", "name": "A"}
    client.post("/auth/buyer/register", json=body)
    assert client.post("/auth/buyer/register", json=body).status_code == 409


# -- tokens ------------------------------------------------------------------


def test_the_token_is_stored_only_as_a_hash(client, session):
    from app.models import Token

    issued = client.post(
        "/auth/buyer/register",
        json={"email": "a@example.com", "password": "correct horse", "name": "A"},
    ).json()
    rows = session.query(Token).all()
    assert len(rows) == 1
    assert rows[0].token_hash != issued["access_token"]
    assert rows[0].token_hash == security.token_fingerprint(issued["access_token"])


def test_logout_revokes_that_token(client, artisan_headers):
    assert client.get("/auth/me", headers=artisan_headers).status_code == 200
    assert client.post("/auth/logout", headers=artisan_headers).status_code == 204
    assert client.get("/auth/me", headers=artisan_headers).status_code == 401


def test_an_expired_token_stops_working(client, artisan_headers, session, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.models import Token

    row = session.query(Token).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.commit()
    assert client.get("/auth/me", headers=artisan_headers).status_code == 401


def test_a_made_up_token_is_refused(client):
    assert client.get("/auth/me", headers={"Authorization": "Bearer crf_nonsense"}).status_code == 401


def test_a_malformed_header_is_refused(client):
    assert client.get("/auth/me", headers={"Authorization": "crf_nonsense"}).status_code == 401


# -- roles -------------------------------------------------------------------


def test_a_buyer_cannot_use_an_artisan_endpoint(client):
    token = client.post(
        "/auth/buyer/register",
        json={"email": "a@example.com", "password": "correct horse", "name": "A"},
    ).json()["access_token"]
    response = client.get("/listings/mine", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


# -- profile -----------------------------------------------------------------


def test_ai_call_consent_is_off_until_she_says_yes(client, artisan_headers):
    assert client.get("/auth/me", headers=artisan_headers).json()["ai_call_consent"] is False
    updated = client.patch(
        "/auth/me", json={"ai_call_consent": True}, headers=artisan_headers
    ).json()
    assert updated["ai_call_consent"] is True


def test_an_artisan_can_set_where_her_money_goes(client, artisan_headers, artisan_id, session):
    from app.models import Artisan

    client.patch("/auth/me", json={"payout_upi": "hansaben@upi"}, headers=artisan_headers)
    session.expire_all()
    assert session.get(Artisan, artisan_id).payout_upi == "hansaben@upi"


def test_phone_normalisation():
    assert security.normalise_phone("+91 98765 43210") == "9876543210"
    assert security.normalise_phone("098765-43210") == "9876543210"
    assert security.normalise_phone("919876543210") == "9876543210"
    # An unrecognised country code keeps its digits rather than being
    # guessed into an Indian number and merged with somebody else.
    assert security.normalise_phone("+1 415 555 0100") == "+14155550100"
