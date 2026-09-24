"""Reading the catalogue: the join C1 asks for.

`GET /catalog/entries` returns listing + artisan + inventory, which is
three tables, which is the classic N+1 query — twelve listings become
twenty-five round trips and the storefront gets slow in a way that only
shows up once there is real data in it. Every query here eager-loads the
join in one statement.

**An entry whose artisan is missing is dropped rather than returned with a
blank maker.** The foreign key makes that nearly impossible, but "nearly"
is doing work: a craft marketplace that cannot say who made something has
nothing to sell, and rendering an anonymous tile is a worse failure than
showing eleven products instead of twelve.

**Only published listings leave this module.** A draft is a half-finished
thought an artisan has not confirmed — it has a title the AI guessed and a
price nobody has checked. The filter is here, in the query, rather than in
a route, so that adding a new read endpoint cannot forget it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.contracts import Artisan as ArtisanOut
from app.contracts import CatalogEntry, Channel
from app.contracts import Inventory as InventoryOut
from app.contracts import Listing as ListingOut
from app.models import Artisan, Inventory, Listing

#: What a listing is sold on when nobody has said otherwise. Matches the
#: default C1's stub catalogue used, so the switch from stub to http does
#: not silently change which channels a product appears on.
DEFAULT_CHANNELS = [Channel.OWN_STORE, Channel.B2B, Channel.MELA_QR]


def _artisan_out(artisan: Artisan) -> ArtisanOut:
    return ArtisanOut(
        artisan_id=artisan.artisan_id,
        name=artisan.name,
        village=artisan.village,
        district=artisan.district,
        state=artisan.state,
        craft=artisan.craft,
        cluster_id=artisan.cluster_id,
        years_experience=artisan.years_experience,
        languages=list(artisan.languages or []),
        photo_url=artisan.photo_url,
        story=artisan.story,
    )


def listing_out(listing: Listing) -> ListingOut:
    return ListingOut(
        listing_id=listing.listing_id,
        artisan_id=listing.artisan_id,
        source_language=listing.source_language,
        transcript_raw=listing.transcript_raw,
        title_en=listing.title_en,
        title_hi=listing.title_hi,
        description_en=listing.description_en,
        description_hi=listing.description_hi,
        summary_spoken=listing.summary_spoken,
        category=listing.category,
        craft_type=listing.craft_type,
        material=listing.material,
        colours=listing.colours,
        dimensions=listing.dimensions,
        material_cost_inr=listing.material_cost_inr,
        hours_worked=listing.hours_worked,
        confidence=dict(listing.confidence or {}),
        needs_confirmation=list(listing.needs_confirmation or []),
        image_original_url=listing.image_original_url,
        image_clean_url=listing.image_clean_url,
        created_at=listing.created_at,
    )


def _inventory_out(listing: Listing) -> InventoryOut:
    inventory = listing.inventory
    if inventory is None:
        # Not an error: a listing exists before anyone has counted stock.
        # Every capacity field stays null, which is what "nobody has asked"
        # looks like downstream, and B1's splitter reads it as unknown
        # rather than as zero.
        return InventoryOut(listing_id=listing.listing_id)
    return InventoryOut(
        listing_id=inventory.listing_id,
        in_stock=inventory.in_stock,
        made_to_order=inventory.made_to_order,
        lead_time_days=inventory.lead_time_days,
        monthly_capacity=inventory.monthly_capacity,
        cluster_capacity=inventory.cluster_capacity,
    )


def to_entry(listing: Listing) -> CatalogEntry:
    channels = [Channel(c) for c in (listing.channels or [])] or DEFAULT_CHANNELS
    return CatalogEntry(
        listing=listing_out(listing),
        artisan=_artisan_out(listing.artisan),
        inventory=_inventory_out(listing),
        published=listing.published,
        channels=channels,
    )


def _base_query():
    return (
        select(Listing)
        .where(Listing.published.is_(True))
        .options(
            joinedload(Listing.artisan),
            selectinload(Listing.inventory),
        )
        .order_by(Listing.created_at, Listing.listing_id)
    )


def all_entries(db: Session) -> list[CatalogEntry]:
    rows = db.scalars(_base_query()).unique().all()
    return [to_entry(row) for row in rows if row.artisan is not None]


def get_entry(db: Session, listing_id: str) -> CatalogEntry | None:
    row = db.scalars(_base_query().where(Listing.listing_id == listing_id)).unique().one_or_none()
    if row is None or row.artisan is None:
        return None
    return to_entry(row)


def artisan(db: Session, artisan_id: str) -> ArtisanOut | None:
    row = db.get(Artisan, artisan_id)
    return _artisan_out(row) if row is not None else None


def cluster_members(db: Session, cluster_id: str) -> list[ArtisanOut]:
    """Everyone who pools capacity under this cluster.

    Returned whether or not they currently have a published listing: a
    weaver between orders is still someone a bulk order can be split
    across, and B1 needs the whole candidate pool to answer honestly.
    """
    rows = db.scalars(
        select(Artisan).where(Artisan.cluster_id == cluster_id).order_by(Artisan.name)
    ).all()
    return [_artisan_out(row) for row in rows]


def cluster_entries(
    db: Session, cluster_id: str, craft_type: str | None = None
) -> list[CatalogEntry]:
    query = _base_query().join(Artisan).where(Artisan.cluster_id == cluster_id)
    if craft_type:
        query = query.where(Listing.craft_type == craft_type)
    rows = db.scalars(query).unique().all()
    return [to_entry(row) for row in rows]


def entries_for_artisan(db: Session, artisan_id: str, include_drafts: bool = True) -> list[Listing]:
    """The artisan's own listings, drafts included.

    The only read in this module that returns unpublished rows, because it
    is the one whose caller is the artisan herself: she has to be able to
    see the draft in order to confirm it.
    """
    query = (
        select(Listing)
        .where(Listing.artisan_id == artisan_id)
        .options(joinedload(Listing.artisan), selectinload(Listing.inventory))
        .order_by(Listing.created_at.desc())
    )
    if not include_drafts:
        query = query.where(Listing.published.is_(True))
    return list(db.scalars(query).unique().all())


def set_inventory(db: Session, listing: Listing, **fields: object) -> Inventory:
    """Create or update the inventory row, leaving unmentioned fields alone.

    A partial update must not reset `monthly_capacity` to null just because
    the caller only wanted to change `in_stock` — that would quietly make a
    cluster look incapable of a bulk order it can easily fill.
    """
    inventory = listing.inventory
    if inventory is None:
        inventory = Inventory(listing_id=listing.listing_id)
        db.add(inventory)
        listing.inventory = inventory
    for key, value in fields.items():
        if value is not None and hasattr(inventory, key):
            setattr(inventory, key, value)
    db.flush()
    return inventory
