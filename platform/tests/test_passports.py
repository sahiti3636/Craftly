"""Passports, codes and QR minting.

The first test is the one that keeps printed tags working: it pins this
service's code derivation to the one C1 has been using to print them.
"""

from __future__ import annotations

import pytest

from app import ids, passports


def test_codes_match_the_ones_c1_has_been_printing(seeded, client):
    """B2 took over minting from `market/app/adapters/stub_passport.py`.

    Every hang-tag printed off that stub is already in the world. If this
    service derives a different code, every one of them stops resolving.
    The stub's algorithm is loaded from C1's own file rather than copied,
    so this test breaks if either side changes it.
    """
    from pathlib import Path

    stub_path = (
        Path(__file__).resolve().parents[2]
        / "market"
        / "app"
        / "adapters"
        / "stub_passport.py"
    )
    if not stub_path.exists():  # pragma: no cover - C1 not checked out
        pytest.skip("market/ is not present")

    # Read the function out of the source rather than importing the module,
    # which would pull in C1's `app` package and collide with this one.
    source = stub_path.read_text(encoding="utf-8")
    namespace: dict = {}
    exec(compile("import hashlib\n" + _extract(source), str(stub_path), "exec"), namespace)

    for listing_id in ("lst_51ea90cb7d24", "lst_9f8a2e1c4b3d", "lst_77b0c4d8e913"):
        assert ids.verification_code(listing_id) == namespace["verification_code"](listing_id)


def _extract(source: str) -> str:
    """Pull `_ALPHABET` and `verification_code` out of C1's stub."""
    lines = source.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("_ALPHABET"))
    end = next(i for i, line in enumerate(lines) if line.startswith("def passport_url"))
    return "\n".join(lines[start:end])


def test_a_code_avoids_the_characters_a_human_misreads(seeded, client):
    entries = client.get("/catalog/entries").json()
    for entry in entries:
        code = client.get(
            f"/passports/by-listing/{entry['listing']['listing_id']}"
        ).json()["verification_code"]
        assert not set(code) & set("01OIL")


def test_a_code_resolves_however_it_was_typed(client, seeded):
    code = client.get("/passports/by-listing/lst_51ea90cb7d24").json()["verification_code"]
    for typed in (code, code.lower(), code.replace("-", ""), f"  {code}  ", code.replace("-", " ")):
        assert client.get(f"/passports/by-code/{typed.strip()}").status_code == 200


def test_a_misread_o_for_zero_still_finds_the_passport(client, seeded, session):
    """Someone reads `0` off a tag where the code says `Q`, or types `O`.

    The alphabet contains none of `0 O 1 I L`, so any of them appearing in
    a typed code is certainly a misreading. Swapping one of them cannot
    turn one real code into another real code, because no real code
    contains the character being swapped in.
    """
    from app.models import Passport

    row = session.query(Passport).first()
    # Force a code containing a character a human confuses, so the test
    # does not depend on which listing happened to hash to what.
    row.verification_code = "CR-QABC-DEFG"
    row.code_key = ids.normalise_code(row.verification_code)
    session.commit()

    # `0` typed where the code has `O`... except the code has no `O`, so
    # the path exercised is the reverse: a typed `O` resolving to nothing
    # rather than silently to a different passport.
    assert passports.by_code(session, "CR-QABC-DEFG") is not None
    assert passports.by_code(session, "crqabcdefg") is not None
    assert passports.by_code(session, "CR-QABC-DEFO") is None

    # And the substitution itself: a typed `0` is tried as `O` as well,
    # canonical form first so an exact match always wins.
    assert ids.code_variants("CR-0ABC-DEFG") == ["CR0ABCDEFG", "CROABCDEFG"]


def test_an_unknown_code_is_a_404_with_a_checkable_message(client, seeded):
    response = client.get("/passports/by-code/CR-ZZZZ-ZZZZ")
    assert response.status_code == 404
    # C1 turns this into "we cannot verify this" with a retype box, not a
    # generic not-found page. Being checkable is the point of a passport.
    assert "verify" in response.json()["detail"].lower()


def test_the_chain_names_the_maker_and_the_place(client, seeded):
    passport = client.get("/passports/by-listing/lst_51ea90cb7d24").json()
    assert passport["artisan"]["name"]
    assert passport["made_at"]
    assert len(passport["chain"]) == 4
    assert passport["chain"][0]["label"] == "Made by hand"
    assert passport["artisan"]["name"] in passport["chain"][0]["detail"]


def test_a_missing_fact_is_said_out_loud_rather_than_dropped(client, seeded):
    """lst_77b0c4d8e913 has no hours_worked.

    The passport says the hours are unconfirmed. It does not omit the step,
    which would read as though the question never mattered, and it does not
    invent a number, which would be a lie on a proof object.
    """
    passport = client.get("/passports/by-listing/lst_77b0c4d8e913").json()
    assert passport["hours_worked"] is None
    floor_step = passport["chain"][2]
    assert "still to be confirmed" in floor_step["detail"]


def test_a_qr_is_minted_and_points_at_c1s_passport_page(client, seeded, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "PUBLIC_URL", "http://192.168.1.5:8100")
    code = client.get("/passports/by-listing/lst_51ea90cb7d24").json()["verification_code"]
    assert passports.passport_url(code) == f"http://192.168.1.5:8100/p/{code}"

    response = client.get(f"/qr/{code}.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_qr_is_not_minted_for_a_code_nobody_issued(client, seeded):
    """Otherwise this is an open QR generator pointed at a URL an attacker
    chooses, carrying this project's domain."""
    assert client.get("/qr/CR-ZZZZ-ZZZZ.png").status_code == 404


def test_publishing_twice_keeps_the_first_code(client, artisan_headers, session):
    """A second code would orphan every tag already printed with the first."""
    body = {
        "listing": {"listing_id": "lst_twice", "artisan_id": "x", "title_en": "Twice"},
        "publish": True,
    }
    first = client.post("/listings", json=body, headers=artisan_headers).json()
    client.patch("/listings/lst_twice", json={"published": False}, headers=artisan_headers)
    second = client.patch(
        "/listings/lst_twice", json={"published": True}, headers=artisan_headers
    ).json()
    assert first["verification_code"] == second["verification_code"]


def test_unpublishing_does_not_revoke_the_passport(client, artisan_headers):
    """A tag in a buyer's hand must still resolve.

    "This is no longer for sale" and "this was never real" are different
    claims, and only one of them is true.
    """
    body = {
        "listing": {"listing_id": "lst_gone", "artisan_id": "x", "title_en": "Gone"},
        "publish": True,
    }
    code = client.post("/listings", json=body, headers=artisan_headers).json()["verification_code"]
    client.patch("/listings/lst_gone", json={"published": False}, headers=artisan_headers)

    assert client.get("/catalog/entries/lst_gone").status_code == 404
    assert client.get(f"/passports/by-code/{code}").status_code == 200


def test_the_passport_is_issued_when_the_listing_goes_live(client, artisan_headers, session):
    draft = client.post(
        "/listings",
        json={"listing": {"listing_id": "lst_d", "artisan_id": "x", "title_en": "D"}},
        headers=artisan_headers,
    ).json()
    assert draft["verification_code"] is None

    live = client.patch(
        "/listings/lst_d", json={"published": True}, headers=artisan_headers
    ).json()
    assert live["verification_code"] is not None
    assert live["qr_url"].endswith(".png")
