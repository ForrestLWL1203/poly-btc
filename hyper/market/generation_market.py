"""Immutable market inputs for scanner generations.

The mutable ``coin_vol`` table remains Observer's live cache.  Scanner qualification uses this module so
every wallet, portfolio replay and tune candidate sees exactly the same per-generation sigma/liquidity and
maintenance inputs.
"""
from __future__ import annotations

import hashlib
import json
import threading

from hyper import config
from hyper.util import now_iso
from . import rest, volatility


class MarketSnapshotError(RuntimeError):
    pass


def _number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_context_snapshot(universe: set[str] | frozenset[str]) -> dict[str, dict]:
    """Fetch the two bulk market-context payloads used by the product."""
    crypto = rest.asset_contexts()
    stocks_raw = rest.asset_contexts("xyz")
    if not crypto:
        raise MarketSnapshotError("crypto_market_context_unavailable")
    if rest.BUILDER_DEXES and not stocks_raw:
        raise MarketSnapshotError("stock_market_context_unavailable")
    stocks = {
        str(name) if ":" in str(name) else f"xyz:{name}": dict(ctx or {})
        for name, ctx in stocks_raw.items()
    }
    combined = {str(name): dict(ctx or {}) for name, ctx in crypto.items()}
    combined.update(stocks)
    # Freeze only executable names. A listing changing later in the scan cannot enter this generation.
    return {coin: combined[coin] for coin in universe if coin in combined}


def load(db, generation: str) -> tuple[dict[str, float], dict[str, dict]]:
    manifest = db.execute(
        "SELECT status FROM generation_market_manifest WHERE generation=?", (generation,),
    ).fetchone()
    if manifest and manifest[0] == "sealed":
        summary(db, generation)  # verifies the stored immutable hash before returning replay inputs
    rows = db.execute(
        "SELECT coin,sigma,day_ntl_vlm,oi_notional,max_leverage "
        "FROM generation_market_snapshot WHERE generation=? ORDER BY coin",
        (generation,),
    ).fetchall()
    sigmas = {row[0]: float(row[1]) for row in rows}
    market_ctx = {
        row[0]: {"day_ntl_vlm": row[2], "oi_notional": row[3], "max_leverage": row[4]}
        for row in rows
    }
    return sigmas, market_ctx


def has_snapshot(db, generation: str | None) -> bool:
    if not generation:
        return False
    return bool(db.execute(
        "SELECT 1 FROM generation_market_manifest WHERE generation=? AND status='sealed' LIMIT 1",
        (generation,),
    ).fetchone())


def summary(db, generation: str) -> dict:
    rows = db.execute(
        "SELECT coin,asof_ms,sigma,sigma_fast,sigma_slow,sigma_n,sigma_source,"
        "day_ntl_vlm,open_interest,mark_px,oi_notional,max_leverage,context_at "
        "FROM generation_market_snapshot WHERE generation=? ORDER BY coin",
        (generation,),
    ).fetchall()
    manifest = db.execute(
        "SELECT asof_ms,context_hash,status,snapshot_hash FROM generation_market_manifest WHERE generation=?",
        (generation,),
    ).fetchone()
    payload = [list(row) for row in rows]
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=False, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    sources = {}
    for row in rows:
        sources[row[6]] = sources.get(row[6], 0) + 1
    if manifest and manifest[2] == "sealed" and manifest[3] != digest:
        raise MarketSnapshotError(f"market_snapshot_hash_mismatch:{generation}")
    return {
        "generation": generation, "coins": len(rows), "hash": digest, "sources": sources,
        "asofMs": manifest[0] if manifest else None,
        "contextHash": manifest[1] if manifest else None,
        "status": manifest[2] if manifest else "missing",
    }


def seal(db, generation: str) -> dict:
    current = summary(db, generation)
    row = db.execute(
        "SELECT status,snapshot_hash FROM generation_market_manifest WHERE generation=?",
        (generation,),
    ).fetchone()
    if not row:
        raise MarketSnapshotError(f"market_manifest_missing:{generation}")
    if row[0] == "sealed":
        if row[1] != current["hash"]:
            raise MarketSnapshotError(f"market_snapshot_hash_mismatch:{generation}")
        return current
    stamp = now_iso()
    db.execute(
        "UPDATE generation_market_manifest SET status='sealed',snapshot_hash=?,sealed_at=? "
        "WHERE generation=? AND status='building'",
        (current["hash"], stamp, generation),
    )
    db.commit()
    return {**current, "status": "sealed"}


def validate_coins(db, generation: str, coins) -> dict:
    if not has_snapshot(db, generation):
        raise MarketSnapshotError(f"market_snapshot_missing_rescan_required:{generation}")
    required = sorted(set(str(coin) for coin in coins if coin))
    if not required:
        return summary(db, generation)
    marks = ",".join("?" for _ in required)
    rows = db.execute(
        f"SELECT coin,day_ntl_vlm,oi_notional,max_leverage FROM generation_market_snapshot "
        f"WHERE generation=? AND coin IN ({marks})",
        (generation, *required),
    ).fetchall()
    by_coin = {row[0]: row for row in rows}
    missing = [coin for coin in required if coin not in by_coin]
    incomplete = []
    for coin, row in by_coin.items():
        if _number(row[3]) is None or _number(row[3]) <= 0:
            incomplete.append(f"{coin}:max_leverage")
        if ":" not in coin and (row[1] is None or row[2] is None):
            incomplete.append(f"{coin}:liquidity")
    if missing or incomplete:
        detail = ",".join([*(f"{coin}:missing" for coin in missing), *incomplete][:12])
        raise MarketSnapshotError(f"market_snapshot_incomplete:{detail}")
    return summary(db, generation)


class Resolver:
    """Generation-scoped, per-coin de-duplicated market-data resolver."""

    def __init__(self, db, generation: str, asof_ms: int, universe, contexts, db_lock=None):
        self.db = db
        self.generation = str(generation)
        self.asof_ms = int(asof_ms)
        self.universe = frozenset(universe or ())
        self.contexts = dict(contexts or {})
        self.context_at = now_iso()
        self.db_lock = db_lock or threading.Lock()
        self.lock = threading.Lock()
        self.cache = {}
        self.errors = {}
        context_hash = hashlib.sha256(
            json.dumps(self.contexts, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        with self.db_lock:
            self.db.execute(
                "INSERT OR IGNORE INTO generation_market_manifest "
                "(generation,asof_ms,context_hash,status,created_at) VALUES (?,?,?,'building',?)",
                (self.generation, self.asof_ms, context_hash, now_iso()),
            )
            manifest = self.db.execute(
                "SELECT asof_ms,context_hash,status FROM generation_market_manifest WHERE generation=?",
                (self.generation,),
            ).fetchone()
            if not manifest or int(manifest[0]) != self.asof_ms or manifest[1] != context_hash:
                self.db.rollback()
                raise MarketSnapshotError(f"market_manifest_context_mismatch:{self.generation}")
            self.db.commit()
        sigmas, market_ctx = load(db, self.generation)
        for coin, sigma in sigmas.items():
            self.cache[coin] = (sigma, market_ctx.get(coin) or {})

    def _context_fields(self, coin):
        ctx = self.contexts.get(coin)
        if not isinstance(ctx, dict):
            raise MarketSnapshotError(f"market_context_missing:{coin}")
        day_ntl_vlm = _number(ctx.get("dayNtlVlm"))
        open_interest = _number(ctx.get("openInterest"))
        mark_px = _number(ctx.get("markPx") or ctx.get("oraclePx") or ctx.get("midPx"))
        max_leverage = _number(ctx.get("universe_maxLeverage"))
        oi_notional = (
            open_interest * mark_px
            if open_interest is not None and mark_px is not None else None
        )
        if max_leverage is None or max_leverage <= 0:
            raise MarketSnapshotError(f"max_leverage_missing:{coin}")
        if ":" not in coin and (day_ntl_vlm is None or oi_notional is None):
            raise MarketSnapshotError(f"crypto_liquidity_context_missing:{coin}")
        return day_ntl_vlm, open_interest, mark_px, oi_notional, max_leverage

    def _resolve_one(self, coin):
        with self.db_lock:
            manifest = self.db.execute(
                "SELECT status FROM generation_market_manifest WHERE generation=?", (self.generation,),
            ).fetchone()
        if not manifest or manifest[0] != "building":
            raise MarketSnapshotError(f"market_snapshot_already_sealed:{self.generation}")
        if coin not in self.universe:
            raise MarketSnapshotError(f"market_not_in_generation_universe:{coin}")
        day_vlm, open_interest, mark_px, oi_notional, max_leverage = self._context_fields(coin)
        # Never source qualification from Observer's mutable ``coin_vol`` cache.  This explicit as-of fetch
        # is de-duplicated by the generation resolver and therefore gives every wallet/replay one identical
        # closed-candle sample without preloading hundreds of markets.
        sample = volatility.compute_at(coin, self.asof_ms)
        if sample["status"] == "request_failed":
            raise MarketSnapshotError(f"sigma_request_failed:{coin}")
        if sample["status"] == "insufficient_history":
            sample = {
                **sample,
                "status": "insufficient_history_default",
                "sigma": float(config.VOL_FALLBACK_SIGMA),
            }
        stamp = now_iso()
        row = (
            self.generation, coin, self.asof_ms, float(sample["sigma"]), sample.get("fast"),
            sample.get("slow"), int(sample.get("n") or 0), sample["status"], day_vlm,
            open_interest, mark_px, oi_notional, max_leverage, self.context_at, stamp,
        )
        with self.db_lock:
            self.db.execute(
                "INSERT OR IGNORE INTO generation_market_snapshot "
                "(generation,coin,asof_ms,sigma,sigma_fast,sigma_slow,sigma_n,sigma_source,"
                "day_ntl_vlm,open_interest,mark_px,oi_notional,max_leverage,context_at,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            self.db.execute(
                "INSERT INTO coin_vol "
                "(coin,sigma,sigma_fast,sigma_slow,n,day_ntl_vlm,open_interest,mark_px,oi_notional,"
                "market_ctx_updated_at,max_leverage,margin_meta_updated_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(coin) DO UPDATE SET "
                "sigma=excluded.sigma,sigma_fast=excluded.sigma_fast,sigma_slow=excluded.sigma_slow,"
                "n=excluded.n,day_ntl_vlm=excluded.day_ntl_vlm,open_interest=excluded.open_interest,"
                "mark_px=excluded.mark_px,oi_notional=excluded.oi_notional,"
                "market_ctx_updated_at=excluded.market_ctx_updated_at,max_leverage=excluded.max_leverage,"
                "margin_meta_updated_at=excluded.margin_meta_updated_at,updated_at=excluded.updated_at",
                (coin, float(sample["sigma"]), sample.get("fast"), sample.get("slow"), int(sample.get("n") or 0),
                 day_vlm, open_interest, mark_px, oi_notional, stamp, max_leverage, stamp, stamp),
            )
            self.db.commit()
        return float(sample["sigma"]), {
            "day_ntl_vlm": day_vlm, "oi_notional": oi_notional, "max_leverage": max_leverage,
        }

    def ensure(self, coins) -> tuple[dict[str, float], dict[str, dict]]:
        # The lock intentionally spans the network call: the REST pacer is global anyway, and this guarantees
        # one request per unique coin even when multiple wallet workers reach it simultaneously.
        with self.lock:
            for coin in sorted(set(str(value) for value in coins if value)):
                if coin in self.errors:
                    raise MarketSnapshotError(self.errors[coin])
                if coin not in self.cache:
                    try:
                        self.cache[coin] = self._resolve_one(coin)
                    except MarketSnapshotError as exc:
                        self.errors[coin] = str(exc)
                        raise
            selected = {coin: self.cache[coin] for coin in coins if coin in self.cache}
        return (
            {coin: value[0] for coin, value in selected.items()},
            {coin: dict(value[1]) for coin, value in selected.items()},
        )
