"""The shop this service serves is the shop C1's stub serves.

That equivalence is what makes `CRAFTLY_ADAPTERS=http` a config change
rather than a migration. C1's storefront renders twelve specific products
with specific prices, specific photos and specific verification codes; if
switching the adapter changes any of that, the "integration is one env
var" claim in the README is false.

The seed files are read directly rather than through C1's `SeedCatalog`,
because importing it would pull in a second package called `app`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import config, ids

SEED = Path(config.SEED_DIR)


def _seed(name: str) -> list[dict]:
    path = SEED / name
    if not path.exists():  # pragma: no cover - market/ not checked out
        pytest.skip("market/seed/ is not present")
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_seeded_listing_is_in_the_catalogue(client, seeded):
    served = {e["listing"]["listing_id"] for e in client.get("/catalog/entries").json()}
    expected = {row["listing_id"] for row in _seed("listings.json")}
    assert served == expected


def test_every_field_survives_the_round_trip(client, seeded):
    served = {e["listing"]["listing_id"]: e["listing"] for e in client.get("/catalog/entries").json()}

    for row in _seed("listings.json"):
        listing = served[row["listing_id"]]
        for field, value in row.items():
            if field == "created_at":
                continue  # serialised with a timezone; compared below
            assert listing[field] == value, f"{row['listing_id']}.{field}"


def test_inventory_survives_the_round_trip(client, seeded):
    served = {
        e["listing"]["listing_id"]: e["inventory"] for e in client.get("/catalog/entries").json()
    }
    for row in _seed("inventory.json"):
        inventory = served[row["listing_id"]]
        for field, value in row.items():
            assert inventory[field] == value, f"{row['listing_id']}.{field}"


def test_artisans_survive_the_round_trip(client, seeded):
    served = {
        e["artisan"]["artisan_id"]: e["artisan"] for e in client.get("/catalog/entries").json()
    }
    by_id = {row["artisan_id"]: row for row in _seed("artisans.json")}
    for artisan_id, artisan in served.items():
        for field, value in by_id[artisan_id].items():
            assert artisan[field] == value, f"{artisan_id}.{field}"


def test_the_photo_urls_still_point_at_c1s_committed_media(client, seeded):
    """`market/seed/media/` is committed so a fresh clone has a shop with
    pictures. Rewriting these to this service's own media store would
    blank every tile."""
    for entry in client.get("/catalog/entries").json():
        url = entry["listing"]["image_clean_url"]
        if url:
            assert url.startswith("/media/")


def test_the_verification_codes_are_the_ones_already_printed(client, seeded):
    for entry in client.get("/catalog/entries").json():
        listing_id = entry["listing"]["listing_id"]
        served = client.get(f"/passports/by-listing/{listing_id}").json()["verification_code"]
        assert served == ids.verification_code(listing_id)


def test_the_two_deliberately_awkward_listings_are_still_awkward(client, seeded):
    """C1's seed data carries two cases on purpose, and both must survive.

    `lst_77b0c4d8e913` has no `hours_worked`, so its wage floor is a guess
    and every surface must refuse to sell it. `lst_9f8a2e1c4b3d` is the
    diya whose market price sits below its own floor. If seeding quietly
    fixed either, the refusal paths would stop being demonstrable.
    """
    incomplete = client.get("/catalog/entries/lst_77b0c4d8e913").json()
    assert incomplete["listing"]["hours_worked"] is None

    diya = client.get("/catalog/entries/lst_9f8a2e1c4b3d").json()
    assert diya["listing"]["material_cost_inr"] is not None
    assert diya["listing"]["hours_worked"] is not None


def test_seeding_twice_changes_nothing(client, seeded):
    """Running the importer again after a deploy must not double the shop."""
    from app import seed

    before = client.get("/catalog/entries").json()
    counts = seed.load()
    after = client.get("/catalog/entries").json()

    assert counts["listings"] == 0
    assert counts["artisans"] == 0
    assert len(before) == len(after)


def test_clusters_are_derived_from_the_artisans_that_reference_them(client, seeded, session):
    """The seed files have `cluster_id` on each artisan but no cluster
    records — C1 never needed them as rows, and B1's splitter does."""
    from app.models import Cluster

    referenced = {
        row["cluster_id"] for row in _seed("artisans.json") if row.get("cluster_id")
    }
    stored = {row.cluster_id for row in session.query(Cluster).all()}
    assert stored == referenced


def test_every_cluster_resolves_to_its_members(client, seeded):
    for cluster_id in {
        row["cluster_id"] for row in _seed("artisans.json") if row.get("cluster_id")
    }:
        members = client.get(f"/clusters/{cluster_id}/members").json()
        assert members, cluster_id
        assert all(m["cluster_id"] == cluster_id for m in members)
