"""Three copies of one contract, checked against each other.

`Listing` is defined in `capture/app/schema.py` (A2 owns it), mirrored in
`market/app/contracts.py` (C1) and mirrored again in
`platform/app/contracts.py` (B2). The duplication is forced — all three
packages are called `app` and none is installable — so the only thing
keeping it honest is this file.

**If these go red, do not edit the mirror to make them pass.** A schema
change is a team-wide announcement. A red parity test means the
announcement happened somewhere B2 did not hear it, and the fix is a
conversation, not a commit.

The files are loaded from disk by path rather than imported, because
importing either would collide with this package's own `app`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
A2_SCHEMA = REPO / "capture" / "app" / "schema.py"
C1_CONTRACTS = REPO / "market" / "app" / "contracts.py"
B2_CONTRACTS = REPO / "platform" / "app" / "contracts.py"


def _fields(path: Path, class_name: str) -> dict[str, str]:
    """Field names to their annotation source, read without importing."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                statement.target.id: ast.unparse(statement.annotation)
                for statement in node.body
                if isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
            }
    raise AssertionError(f"{class_name} not found in {path}")


def _require(path: Path) -> Path:
    if not path.exists():  # pragma: no cover - slice not checked out
        pytest.skip(f"{path.relative_to(REPO)} is not present")
    return path


def test_b2_listing_matches_a2s_source_of_truth():
    a2 = _fields(_require(A2_SCHEMA), "Listing")
    b2 = _fields(_require(B2_CONTRACTS), "Listing")

    assert set(a2) == set(b2), (
        "B2's Listing mirror has drifted from capture/app/schema.py. "
        f"Only in A2: {sorted(set(a2) - set(b2))}. "
        f"Only in B2: {sorted(set(b2) - set(a2))}."
    )
    for name, annotation in a2.items():
        assert b2[name] == annotation, f"{name}: A2 says {annotation}, B2 says {b2[name]}"


def test_b2_listing_matches_c1s_mirror():
    c1 = _fields(_require(C1_CONTRACTS), "Listing")
    b2 = _fields(_require(B2_CONTRACTS), "Listing")
    assert set(c1) == set(b2)
    for name, annotation in c1.items():
        assert b2[name] == annotation, f"{name}: C1 says {annotation}, B2 says {b2[name]}"


@pytest.mark.parametrize(
    "class_name",
    ["Artisan", "Inventory", "Passport", "PassportStep", "Buyer", "OrderLine", "Order", "Allocation", "SplitPlan"],
)
def test_cross_slice_types_agree_with_c1(class_name):
    """The types C1 proposed and B2 produces.

    C1 wrote these down before B2 existed, as a request for what the real
    service should return. B2 answering with a different shape is the
    integration failing quietly, which is the thing this test exists to
    make loud.
    """
    c1 = _fields(_require(C1_CONTRACTS), class_name)
    b2 = _fields(_require(B2_CONTRACTS), class_name)
    assert set(c1) == set(b2), (
        f"{class_name} differs. Only in C1: {sorted(set(c1) - set(b2))}. "
        f"Only in B2: {sorted(set(b2) - set(c1))}."
    )


def test_the_two_money_fields_are_nullable_everywhere():
    """The rule the whole repo turns on, asserted rather than assumed.

    `material_cost_inr` and `hours_worked` feed a minimum-wage floor. A
    non-optional annotation on either, anywhere, means some slice has
    started defaulting them — and a defaulted 0 is a floor of nothing.
    """
    for path in (A2_SCHEMA, C1_CONTRACTS, B2_CONTRACTS):
        fields = _fields(_require(path), "Listing")
        assert "None" in fields["material_cost_inr"], path
        assert "None" in fields["hours_worked"], path


def test_the_database_column_is_nullable_too():
    """The mirror being right is no use if the table underneath is not."""
    from app.models import Listing as Row

    assert Row.__table__.c.material_cost_inr.nullable
    assert Row.__table__.c.hours_worked.nullable
    # And no server-side default quietly filling them in.
    assert Row.__table__.c.material_cost_inr.server_default is None
    assert Row.__table__.c.hours_worked.server_default is None


def test_order_status_values_match_c1s():
    """C1 sends these strings and reads them back."""
    c1_source = _require(C1_CONTRACTS).read_text(encoding="utf-8")
    from app.contracts import OrderStatus

    for status in OrderStatus:
        assert f'"{status.value}"' in c1_source, status.value
