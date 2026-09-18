"""Rupee formatting.

Indian digit grouping is not three-three-three: it is the last three, then
twos. 1234567 is 12,34,567, not 1,234,567. Python's `{:,}` gets this
wrong, and a marketplace for Indian craft that prints Indian prices in
American grouping looks, to the people it is for, like it was built for
someone else.
"""

from __future__ import annotations

RUPEE = "₹"


def group_inr(amount: int) -> str:
    """12,34,567 — last three digits, then groups of two."""
    negative = amount < 0
    digits = str(abs(int(amount)))

    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join([*parts, tail])

    return f"-{grouped}" if negative else grouped


def rupees(amount: int | None, *, dash: str = "—") -> str:
    """`₹12,34,567`, or a dash when the number is genuinely unknown.

    `None` prints the dash rather than `₹0`. The difference matters: a
    price of zero is a claim, a missing price is a question.
    """
    if amount is None:
        return dash
    return f"{RUPEE}{group_inr(amount)}"
