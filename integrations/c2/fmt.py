"""Indian digit grouping (Rs 12,34,567), same rule as C1's `money.py`.

Python's `{:,}` groups in threes, which is wrong for rupees; a voice
script that reads out a payout should say it the way the artisan writes it.
"""

from __future__ import annotations


def inr(amount: int | None) -> str:
    """`None` is a dash, not zero: missing is a question, zero is a claim."""
    if amount is None:
        return "-"
    sign = "-" if amount < 0 else ""
    digits = str(abs(int(amount)))
    if len(digits) <= 3:
        return f"{sign}\u20b9{digits}"
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return f"{sign}\u20b9{','.join(groups)},{tail}"
