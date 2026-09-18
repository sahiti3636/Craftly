"""A catalogue backed by `seed/*.json`. Stands in for B2's database.

Loads three files — listings, artisans, inventory — joins them into
`CatalogEntry` objects and holds them in memory. An entry whose artisan
is missing from `artisans.json` is dropped rather than rendered with a
blank maker: a craft marketplace that cannot say who made something has
nothing to sell.

The load is cached at module level and can be reset with `reload()`,
which tests use to point at a different seed directory.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import config
from app.contracts import Artisan, Channel, Inventory, Listing
from app.ports import CatalogEntry

_DEFAULT_CHANNELS = (Channel.OWN_STORE, Channel.B2B, Channel.MELA_QR)


class SeedCatalog:
    """In-memory catalogue over a seed directory."""

    def __init__(self, seed_dir: Path | None = None) -> None:
        self.seed_dir = Path(seed_dir or config.SEED_DIR)
        self._entries: dict[str, CatalogEntry] = {}
        self._artisans: dict[str, Artisan] = {}
        self._order: list[str] = []
        self._load()

    # -- loading ---------------------------------------------------------

    def _read(self, name: str) -> object:
        path = self.seed_dir / name
        if not path.exists():
            raise FileNotFoundError(
                f"Seed file {path} not found. The stub catalogue needs "
                f"listings.json, artisans.json and inventory.json in {self.seed_dir}."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def _load(self) -> None:
        artisans_raw = self._read("artisans.json")
        listings_raw = self._read("listings.json")
        inventory_raw = self._read("inventory.json")

        self._artisans = {
            a["artisan_id"]: Artisan.model_validate(a) for a in artisans_raw  # type: ignore[union-attr]
        }
        inventory = {
            i["listing_id"]: Inventory.model_validate(i) for i in inventory_raw  # type: ignore[union-attr]
        }

        for raw in listings_raw:  # type: ignore[union-attr]
            listing = Listing.model_validate(raw)
            artisan = self._artisans.get(listing.artisan_id)
            if artisan is None:
                # Deliberately skipped, not defaulted. See module docstring.
                continue
            entry = CatalogEntry(
                listing=listing,
                artisan=artisan,
                inventory=inventory.get(
                    listing.listing_id, Inventory(listing_id=listing.listing_id)
                ),
                published=True,
                channels=_DEFAULT_CHANNELS,
            )
            self._entries[listing.listing_id] = entry
            self._order.append(listing.listing_id)

    # -- CatalogSource ---------------------------------------------------

    def all_entries(self) -> list[CatalogEntry]:
        return [self._entries[lid] for lid in self._order if self._entries[lid].published]

    def get_entry(self, listing_id: str) -> CatalogEntry | None:
        entry = self._entries.get(listing_id)
        if entry is None or not entry.published:
            return None
        return entry

    def cluster_members(self, cluster_id: str) -> list[Artisan]:
        return [a for a in self._artisans.values() if a.cluster_id == cluster_id]

    def cluster_entries(
        self, cluster_id: str, craft_type: str | None = None
    ) -> list[CatalogEntry]:
        out = []
        for entry in self.all_entries():
            if entry.artisan.cluster_id != cluster_id:
                continue
            if craft_type and entry.listing.craft_type != craft_type:
                continue
            out.append(entry)
        return out

    # -- extras the stub exposes that the real B2 need not --------------

    def get_artisan(self, artisan_id: str) -> Artisan | None:
        return self._artisans.get(artisan_id)

    def entries_by_artisan(self, artisan_id: str) -> list[CatalogEntry]:
        return [e for e in self.all_entries() if e.artisan.artisan_id == artisan_id]


_catalog: SeedCatalog | None = None


def catalog() -> SeedCatalog:
    global _catalog
    if _catalog is None:
        _catalog = SeedCatalog()
    return _catalog


def reload(seed_dir: Path | None = None) -> SeedCatalog:
    """Rebuild the cached catalogue, optionally from a different directory."""
    global _catalog
    _catalog = SeedCatalog(seed_dir)
    return _catalog
