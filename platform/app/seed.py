"""Fill the database from C1's committed catalogue.

    uv run python -m app.seed
    uv run python -m app.seed --reset

This is the bridge that makes `CRAFTLY_ADAPTERS=http` a config change
rather than a migration. C1 has twelve listings, fourteen artisans and
their inventory in `market/seed/*.json`, and its stub catalogue reads them
directly. This script loads the same three files into the real tables, so
the shop C1 renders against this service is the shop it renders against
its own stub — same products, same photos, same verification codes.

That equivalence is the whole point, and it is tested:
`tests/test_seed_parity.py` asserts the entries this service returns match
what `SeedCatalog` produces from the same files.

Two things it does that are not in the JSON:

**It creates clusters.** The seed files have `cluster_id` on each artisan
but no cluster records — C1 never needed them as rows. B1's splitter does,
so they are derived here from the artisans that reference them.

**It creates a demo login and payout details.** One artisan account with a
known phone number, and UPI ids on every artisan, so that the payment
split has somewhere to send money and a demo can log in as a real maker
rather than as a fixture. Both are skipped with `--no-demo`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select

from app import auth, catalog, config, passports
from app.db import create_all, engine, session_scope
from app.db import Base
from app.models import Artisan, Cluster, Inventory, Listing

#: A number a demo can log in with. Deliberately obvious.
DEMO_ARTISAN_PHONE = "9000000001"


def _read(seed_dir: Path, name: str) -> list[dict]:
    path = seed_dir / name
    if not path.exists():
        raise SystemExit(
            f"Seed file {path} not found.\n"
            f"Point CRAFTLY_SEED_DIR at a directory holding artisans.json, "
            f"listings.json and inventory.json — market/seed/ is the one this "
            f"repo ships."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _cluster_name(cluster_id: str) -> str:
    """`clu_kutch_ajrakh` -> `Kutch Ajrakh`. A label, not an identifier."""
    return cluster_id.removeprefix("clu_").replace("_", " ").title()


def load(seed_dir: Path | None = None, demo: bool = True) -> dict[str, int]:
    """Import the seed catalogue. Idempotent — existing rows are left alone."""
    directory = Path(seed_dir or config.SEED_DIR)
    artisans_raw = _read(directory, "artisans.json")
    listings_raw = _read(directory, "listings.json")
    inventory_raw = _read(directory, "inventory.json")
    inventory_by_listing = {row["listing_id"]: row for row in inventory_raw}

    counts = {"clusters": 0, "artisans": 0, "listings": 0, "passports": 0}

    with session_scope() as db:
        # Clusters first: artisans reference them, and a FK that points at
        # nothing is exactly what foreign keys are switched on to catch.
        for cluster_id in sorted({a.get("cluster_id") for a in artisans_raw if a.get("cluster_id")}):
            if db.get(Cluster, cluster_id) is not None:
                continue
            members = [a for a in artisans_raw if a.get("cluster_id") == cluster_id]
            db.add(
                Cluster(
                    cluster_id=cluster_id,
                    name=_cluster_name(cluster_id),
                    district=members[0].get("district") if members else None,
                    state=members[0].get("state") if members else None,
                )
            )
            counts["clusters"] += 1

        for raw in artisans_raw:
            if db.get(Artisan, raw["artisan_id"]) is not None:
                continue
            db.add(
                Artisan(
                    artisan_id=raw["artisan_id"],
                    name=raw["name"],
                    village=raw.get("village"),
                    district=raw.get("district"),
                    state=raw.get("state"),
                    craft=raw.get("craft"),
                    cluster_id=raw.get("cluster_id"),
                    years_experience=raw.get("years_experience"),
                    languages=raw.get("languages") or [],
                    photo_url=raw.get("photo_url"),
                    story=raw.get("story"),
                    # Without a destination every payout settles as blocked,
                    # which is correct behaviour and a poor demo. A fake UPI
                    # id is obviously fake and lets the split run end to end.
                    payout_upi=(f"{raw['artisan_id']}@craftly.demo" if demo else None),
                    payout_account_name=(raw["name"] if demo else None),
                )
            )
            counts["artisans"] += 1
        db.flush()

        for raw in listings_raw:
            listing_id = raw["listing_id"]
            if db.get(Listing, listing_id) is not None:
                continue
            if db.get(Artisan, raw["artisan_id"]) is None:
                # Same rule as C1's stub: a listing with no maker is skipped,
                # never rendered with a blank name.
                continue

            listing = Listing(
                listing_id=listing_id,
                artisan_id=raw["artisan_id"],
                source_language=raw.get("source_language"),
                transcript_raw=raw.get("transcript_raw"),
                title_en=raw.get("title_en"),
                title_hi=raw.get("title_hi"),
                description_en=raw.get("description_en"),
                description_hi=raw.get("description_hi"),
                summary_spoken=raw.get("summary_spoken"),
                category=raw.get("category"),
                craft_type=raw.get("craft_type"),
                material=raw.get("material"),
                colours=raw.get("colours"),
                dimensions=raw.get("dimensions"),
                # .get() with no default, so a key that is absent stays None
                # rather than becoming 0. lst_77b0c4d8e913 has a null
                # hours_worked on purpose and must keep it — it is the
                # listing every surface uses to prove the refusal path works.
                material_cost_inr=raw.get("material_cost_inr"),
                hours_worked=raw.get("hours_worked"),
                confidence=raw.get("confidence") or {},
                needs_confirmation=raw.get("needs_confirmation") or [],
                image_original_url=raw.get("image_original_url"),
                image_clean_url=raw.get("image_clean_url"),
                making_clip_url=raw.get("making_clip_url"),
                published=True,
                channels=[c.value for c in catalog.DEFAULT_CHANNELS],
            )
            if raw.get("created_at"):
                from datetime import datetime

                listing.created_at = datetime.fromisoformat(raw["created_at"])
            db.add(listing)
            db.flush()

            inv = inventory_by_listing.get(listing_id)
            if inv:
                db.add(
                    Inventory(
                        listing_id=listing_id,
                        in_stock=inv.get("in_stock", 0),
                        made_to_order=inv.get("made_to_order", True),
                        lead_time_days=inv.get("lead_time_days"),
                        monthly_capacity=inv.get("monthly_capacity"),
                        cluster_capacity=inv.get("cluster_capacity"),
                    )
                )

            passports.issue(db, listing)
            counts["listings"] += 1
            counts["passports"] += 1

        if demo:
            _demo_account(db)

    return counts


def _demo_account(db) -> None:
    """One artisan who can log in, attached to a real seeded maker."""
    if auth.get_account_by_phone(db, DEMO_ARTISAN_PHONE) is not None:
        return
    artisan = db.scalar(select(Artisan).order_by(Artisan.artisan_id).limit(1))
    if artisan is None:
        return
    auth.create_artisan_account(
        db,
        phone=DEMO_ARTISAN_PHONE,
        name=artisan.name,
        artisan_id=artisan.artisan_id,
        language=(artisan.languages or ["hi"])[0],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Craftly platform database.")
    parser.add_argument(
        "--seed-dir", type=Path, default=None, help="Defaults to market/seed/."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop every table first. Destroys orders and payouts as well as the catalogue.",
    )
    parser.add_argument(
        "--no-demo",
        action="store_true",
        help="Skip the demo login and the placeholder payout destinations.",
    )
    args = parser.parse_args()

    if args.reset:
        Base.metadata.drop_all(engine)
    create_all()

    counts = load(args.seed_dir, demo=not args.no_demo)
    print(
        f"Seeded {counts['listings']} listings, {counts['artisans']} artisans, "
        f"{counts['clusters']} clusters, {counts['passports']} passports."
    )
    if not args.no_demo:
        print(f"Demo artisan login: phone {DEMO_ARTISAN_PHONE} (request an OTP to sign in).")


if __name__ == "__main__":
    main()
