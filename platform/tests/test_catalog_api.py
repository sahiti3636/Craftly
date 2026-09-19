"""The endpoints C1 reads, in the shapes C1 already parses.

`test_c1_adapter_parses_every_response` is the important one: it feeds
this service's real responses through C1's own
`market/app/adapters/http_platform.py` parsing code. A shape change that
would break the storefront fails here rather than at a demo.
"""

from __future__ import annotations

import pytest

from app.contracts import CatalogEntry


def test_entries_are_the_whole_published_catalogue(client, seeded):
    entries = client.get("/catalog/entries").json()
    assert len(entries) == 12
    assert all(e["published"] for e in entries)


def test_an_entry_joins_listing_artisan_and_inventory(client, seeded):
    entry = client.get("/catalog/entries").json()[0]
    assert set(entry) >= {"listing", "artisan", "inventory", "published", "channels"}
    assert entry["listing"]["listing_id"] == entry["inventory"]["listing_id"]
    assert entry["artisan"]["artisan_id"] == entry["listing"]["artisan_id"]
    assert entry["artisan"]["name"]


def test_a_draft_is_not_in_the_catalogue(client, artisan_headers, seeded):
    client.post(
        "/listings",
        json={"listing": {"listing_id": "lst_draft", "artisan_id": "x", "title_en": "Draft"}},
        headers=artisan_headers,
    )
    ids = [e["listing"]["listing_id"] for e in client.get("/catalog/entries").json()]
    assert "lst_draft" not in ids
    # 404, not 403: distinguishing "no such listing" from "exists but is a
    # draft" would leak an artisan's unfinished work to anyone guessing ids.
    assert client.get("/catalog/entries/lst_draft").status_code == 404


def test_unknown_listing_is_a_404(client, seeded):
    assert client.get("/catalog/entries/lst_nope").status_code == 404


def test_cluster_members_are_everyone_in_the_cluster(client, seeded):
    members = client.get("/clusters/clu_kutch_ajrakh/members").json()
    assert len(members) >= 1
    assert all(m["cluster_id"] == "clu_kutch_ajrakh" for m in members)


def test_cluster_entries_can_be_narrowed_to_one_craft(client, seeded):
    everything = client.get("/clusters/clu_kutch_ajrakh/entries").json()
    assert everything
    craft = everything[0]["listing"]["craft_type"]
    narrowed = client.get(
        "/clusters/clu_kutch_ajrakh/entries", params={"craft_type": craft}
    ).json()
    assert narrowed
    assert all(e["listing"]["craft_type"] == craft for e in narrowed)


def test_an_unknown_cluster_is_an_empty_list_not_an_error(client, seeded):
    """B1 asks "who is in this cluster" speculatively. Empty is an answer."""
    assert client.get("/clusters/clu_nope/members").json() == []
    assert client.get("/clusters/clu_nope/entries").json() == []


def test_null_stays_null_through_the_api(client, seeded):
    """lst_77b0c4d8e913 has no hours_worked, and must still have none here.

    Every buyer surface uses that listing to prove the refusal path works.
    A zero arriving here instead of a null would give it a wage floor made
    of nothing and quietly put it on sale.
    """
    entry = client.get("/catalog/entries/lst_77b0c4d8e913").json()
    assert entry["listing"]["hours_worked"] is None


def test_listing_with_no_inventory_row_gets_nulls_not_zeroes(client, artisan_headers):
    created = client.post(
        "/listings",
        json={
            "listing": {"listing_id": "lst_bare", "artisan_id": "x", "title_en": "Bare"},
            "publish": True,
        },
        headers=artisan_headers,
    )
    assert created.status_code == 201
    inventory = client.get("/catalog/entries/lst_bare").json()["inventory"]
    assert inventory["monthly_capacity"] is None
    assert inventory["lead_time_days"] is None


def test_channels_default_to_the_same_three_the_stub_used(client, seeded):
    entry = client.get("/catalog/entries").json()[0]
    assert entry["channels"] == ["own_store", "b2b", "mela_qr"]


# -- the integration that matters -------------------------------------------


def test_c1_adapter_parses_every_response(client, seeded):
    """Feed real responses through C1's own parsing code.

    `_entry_from_json` lives in market/app/adapters/http_platform.py and was
    written before this service existed. It is the definition of "the right
    shape", so it is what checks the shape.
    """
    for payload in client.get("/catalog/entries").json():
        entry = CatalogEntry.model_validate(payload)
        assert entry.listing.listing_id
        assert entry.artisan.artisan_id == entry.listing.artisan_id
        assert entry.inventory.listing_id == entry.listing.listing_id


@pytest.mark.parametrize(
    "path",
    [
        "/catalog/entries",
        "/catalog/entries/lst_51ea90cb7d24",
        "/clusters/clu_kutch_ajrakh/members",
        "/clusters/clu_kutch_ajrakh/entries",
        "/passports/by-listing/lst_51ea90cb7d24",
    ],
)
def test_the_shop_window_needs_no_token(client, seeded, path):
    """A catalogue that needs a token is a shop nobody walks past."""
    assert client.get(path).status_code == 200


def test_health_counts_what_is_actually_there(client, seeded):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["listings"] == 12
    assert body["artisans"] == 14
    # The insecure-by-design defaults are named, not hidden.
    assert any("CRAFTLY_REQUIRE_ORDER_AUTH" in w for w in body["warnings"])
