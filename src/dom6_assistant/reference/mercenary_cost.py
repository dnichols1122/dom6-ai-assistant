"""Exact nation-adjusted minimum bids from the client pricing routine.

The auction record stores an asking price and up to seven nation-specific
percentage overrides.  The client starts at 100 plus nation attribute 271,
then replaces that percentage when the company names the bidding nation,
clamps it to at least 10, and truncates ``asking * percentage / 100``.

Two live controls pin the important Marignon case independently: Nergash's
Damned Legion is stored at 350 and displayed at 700; Ghoul Father is stored at
300 and displayed at 600.  Both records carry the explicit pair ``61, 200``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from dom6_assistant.file_reader.formats.trn import Mercenary


NATION_MERCENARY_PRICE_ATTRIBUTE = 271


class MinimumBidUnavailable(ValueError):
    """The company is decoded but its nation-wide modifier is unavailable."""


@dataclass(frozen=True)
class MinimumBid:
    asking_price: int
    percentage: int
    amount: int
    basis: str


def calculate_minimum_bid(
    company: Mercenary,
    nation_id: int,
    reference_conn: sqlite3.Connection | None,
) -> MinimumBid:
    """Return the same legal minimum the Hire Mercenaries screen displays.

    A company-specific pair is self-contained in the ``.trn`` and therefore
    does not need the reference database.  Otherwise the nation definition is
    needed even when the eventual result is the unmodified asking price: only
    a successful lookup can distinguish "no modifier" from missing data.
    """
    specific = dict(company.nation_bid_percentages).get(nation_id)
    if specific is not None:
        percentage = max(int(specific), 10)
        basis = "company-specific nation percentage"
    else:
        if reference_conn is None:
            raise MinimumBidUnavailable(
                "reference database is required to check the nation's "
                "mercenary-price modifier"
            )
        row = reference_conn.execute(
            "SELECT raw_value FROM attributes_by_nation "
            "WHERE nation_number=? AND attribute=? LIMIT 1",
            (nation_id, NATION_MERCENARY_PRICE_ATTRIBUTE),
        ).fetchone()
        modifier = int(row[0]) if row is not None else 0
        percentage = max(100 + modifier, 10)
        basis = (
            f"nation-wide {modifier:+d}% modifier"
            if modifier
            else "unmodified asking price"
        )

    return MinimumBid(
        asking_price=company.price,
        percentage=percentage,
        amount=company.price * percentage // 100,
        basis=basis,
    )
