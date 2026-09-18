"""The four seams between C1 and the rest of the system.

C1 owns screens. It does not own a database, a price, or a courier. Every
fact a buyer surface displays comes from somewhere else:

    CatalogSource   -> B2   what exists, and how much of it
    PriceEngine     -> B1   what it costs, and where the wage floor sits
    OrderEngine     -> B1   how a bulk order splits across a cluster
    PassportSource  -> B2   who made it, and the proof
    OrderSink       -> B2   where a placed order goes

Those slices did not exist when these screens were built, so each seam is
a ``Protocol`` here and a fake in ``app/adapters/``. Nothing above this
line — no route, no template, no search or quote calculation — is allowed
to know which implementation is behind it.

Two consequences worth stating, because they are the point:

* C1 is demoable alone. ``CRAFTLY_ADAPTERS=stub`` runs the entire buyer
  side off seed JSON with no other service up.
* Integration is a config change, not a rewrite. When B1 and B2 ship,
  ``CRAFTLY_ADAPTERS=http`` swaps the implementations and the screens do
  not change.

These are ``Protocol`` classes, not base classes, so an adapter does not
import anything from here to satisfy one — it just has the methods.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.contracts import (
    Artisan,
    Channel,
    Inventory,
    Listing,
    Order,
    Passport,
    PriceQuote,
    SplitPlan,
)


class CatalogEntry:
    """A listing plus everything C1 needs to put it on a shelf.

    Deliberately a plain object rather than a pydantic model: it is a
    join of three records owned by other slices, not a contract anyone
    serialises. Search and sort work over these.
    """

    __slots__ = ("listing", "artisan", "inventory", "published", "channels")

    def __init__(
        self,
        listing: Listing,
        artisan: Artisan,
        inventory: Inventory,
        published: bool = True,
        channels: tuple[Channel, ...] = (Channel.OWN_STORE,),
    ) -> None:
        self.listing = listing
        self.artisan = artisan
        self.inventory = inventory
        self.published = published
        self.channels = channels

    @property
    def listing_id(self) -> str:
        return self.listing.listing_id

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CatalogEntry {self.listing_id} {self.listing.title_en!r}>"


@runtime_checkable
class CatalogSource(Protocol):
    """B2: what is on sale."""

    def all_entries(self) -> list[CatalogEntry]:
        """Every published entry. C1 filters in-process — see app/search.py.

        This is honest for a catalogue of a few thousand items and wrong
        for a million. The seam is here so that the day it is wrong, the
        query pushes down into the adapter without the screens noticing.
        """
        ...

    def get_entry(self, listing_id: str) -> CatalogEntry | None:
        """One entry, or None if it does not exist or is not published."""
        ...

    def cluster_members(self, cluster_id: str) -> list[Artisan]:
        """Everyone who pools capacity under `cluster_id`.

        Used to answer "can this cluster take 500 units?" before a B2B
        buyer commits. B1 owns the real allocation; this only supplies the
        candidate pool.
        """
        ...

    def cluster_entries(self, cluster_id: str, craft_type: str | None = None) -> list[CatalogEntry]:
        """Listings from one cluster, optionally narrowed to one craft.

        Separate from `cluster_members` because capacity lives on a
        listing (this artisan can make 30 runners a month) while identity
        lives on an artisan. A splitter needs both: who is in the cluster,
        and which of them have shown they can make this thing.
        """
        ...


@runtime_checkable
class PriceEngine(Protocol):
    """B1: what it costs, and what the artisan keeps."""

    def quote(
        self, entry: CatalogEntry, channel: Channel = Channel.OWN_STORE, quantity: int = 1
    ) -> PriceQuote:
        """Price one listing on one channel at one quantity.

        `quantity` is passed because bulk pricing is not retail pricing
        divided by nothing — B1 applies volume tiers, and the floor still
        has to hold at the discounted unit price.
        """
        ...


@runtime_checkable
class OrderEngine(Protocol):
    """B1: capacity pooling and order splitting."""

    def split(
        self, entry: CatalogEntry, quantity: int, deadline_days: int | None = None
    ) -> SplitPlan:
        """Spread `quantity` units across the artisan's cluster.

        Must return a plan with `feasible=False` and a `shortfall` rather
        than raising when the cluster cannot meet the order. A B2B buyer
        being told "this cluster can do 340 of the 500 by your date" is a
        useful answer; an error page is not.
        """
        ...


@runtime_checkable
class PassportSource(Protocol):
    """B2: the proof object behind a QR code."""

    def by_listing(self, listing_id: str) -> Passport | None:
        ...

    def by_code(self, verification_code: str) -> Passport | None:
        """Resolve the short code printed on a physical tag.

        Separate from `by_listing` because the thing printed on a hang-tag
        at a mela is not a listing id — it is a short code a human can
        read out over a phone when the QR is scuffed.
        """
        ...


@runtime_checkable
class OrderSink(Protocol):
    """B2: where a placed order goes to become real."""

    def place(self, order: Order) -> Order:
        """Persist the order and return it as stored (ids, status settled)."""
        ...

    def get(self, order_id: str) -> Order | None:
        ...
