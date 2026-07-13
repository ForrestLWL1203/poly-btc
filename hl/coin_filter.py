"""Coin blacklist parsing shared by live copy and offline replay."""

from __future__ import annotations

import re
from collections.abc import Iterable


_SEP = re.compile(r"[\s,;，、]+")

# Curated from the live trade[XYZ] Korea asset directory.  Keep this explicit rather than
# guessing from ticker strings: EWY/KR200 are Korea-wide ETF/index perps, while the single-name
# contracts are Samsung, SK hynix and Hyundai.  New Korea listings must be added here when the
# builder universe changes; an unknown ticker is never silently classified by a loose substring.
KOREAN_STOCK_COINS = frozenset({
    "XYZ:EWY", "XYZ:KR200", "XYZ:HYUNDAI", "XYZ:SMSN", "XYZ:SKHX", "XYZ:SKHY",
})


def normalize_coin(coin) -> str:
    return str(coin or "").strip().upper()


def parse_coin_blacklist(value) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        raw_items = _SEP.split(value)
    elif isinstance(value, Iterable):
        raw_items = value
    else:
        raw_items = [value]
    return {c for c in (normalize_coin(x) for x in raw_items) if c}


def format_coin_blacklist(value) -> str:
    return ", ".join(sorted(parse_coin_blacklist(value)))


def coin_is_blacklisted(coin, blacklist) -> bool:
    if not coin:
        return False
    if not isinstance(blacklist, set):
        blacklist = parse_coin_blacklist(blacklist)
    return normalize_coin(coin) in blacklist


def is_korean_stock(coin) -> bool:
    """Return whether a fully-qualified builder coin is a Korea-linked equity/index perp."""
    return normalize_coin(coin) in KOREAN_STOCK_COINS


def coin_is_blocked(coin, blacklist, *, block_korean_stocks: bool = False) -> bool:
    """Shared new-exposure gate for live observer and historical replay.

    The Korean preset is additive to the operator's exact manual blacklist. Existing positions are
    still allowed to reduce/close; callers should use this for opens and scale-ins only.
    """
    return coin_is_blacklisted(coin, blacklist) or (block_korean_stocks and is_korean_stock(coin))
