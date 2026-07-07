"""Coin blacklist parsing shared by live copy and offline replay."""

from __future__ import annotations

import re
from collections.abc import Iterable


_SEP = re.compile(r"[\s,;，、]+")


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
