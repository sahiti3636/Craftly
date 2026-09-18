"""The mirror of A2's Listing must not drift from A2's Listing.

`market/app/contracts.py` duplicates the shared contract because both
packages are called `app` and neither is installable, so a plain import
would resolve to the wrong module. Duplication is only safe if something
notices when the two diverge — that is this file.

**If this test fails, do not edit the mirror to make it pass.** A change
to the shared schema is a team-wide announcement. A red test here means
the announcement was made somewhere C1 did not hear it, and the fix is a
conversation followed by a deliberate update on both sides, not a quiet
copy-paste.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.contracts import Listing as MirroredListing

CAPTURE_SCHEMA = Path(__file__).resolve().parents[2] / "capture" / "app" / "schema.py"


def _load_upstream():
    if not CAPTURE_SCHEMA.exists():
        pytest.skip(f"A2's schema not found at {CAPTURE_SCHEMA}")
    spec = importlib.util.spec_from_file_location("craftly_capture_schema", CAPTURE_SCHEMA)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Listing


def test_same_field_names():
    upstream = _load_upstream()
    assert set(MirroredListing.model_fields) == set(upstream.model_fields), (
        "The Listing contract in capture/app/schema.py has changed. See this "
        "module's docstring before touching app/contracts.py."
    )


def test_same_types_and_optionality():
    upstream = _load_upstream()
    for name, field in upstream.model_fields.items():
        mirrored = MirroredListing.model_fields[name]
        assert str(mirrored.annotation) == str(field.annotation), f"type drift on {name}"
        assert mirrored.is_required() == field.is_required(), f"optionality drift on {name}"


def test_the_two_numbers_that_must_stay_nullable():
    """The rule the whole pricing chain rests on, asserted directly.

    `material_cost_inr` and `hours_worked` feed a minimum-wage floor. If
    either ever acquires a default of 0, a listing with a missing number
    silently prices as though the work were free.
    """
    for name in ("material_cost_inr", "hours_worked"):
        field = MirroredListing.model_fields[name]
        assert not field.is_required()
        assert field.get_default() is None, f"{name} must default to None, never 0"
