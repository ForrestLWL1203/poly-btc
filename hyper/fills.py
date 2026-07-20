"""Fill-level logic: spot/perp classification and position-episode reconstruction.

Key rule: fills != trades. One order is matched in many slices, so a round-trip
"trade" can be thousands of fills. An EPISODE = a run where the position stays on
ONE side (long/short) until it returns to flat OR flips to the other side,
reconstructed from startPosition (authoritative pre-fill position).

Each fill's realized PnL (HL's authoritative `closedPnl`) is attributed to exactly
ONE episode and never dropped — including fills on a position that was already open
when the window began, and fills that CROSS ZERO (close-and-flip in one fill, split
into two episodes). The old flat->flat-only reconstruction silently dropped any fill
that didn't start from flat (pre-existing positions, post-flip fills), which under-
counted losses and inflated win rate (the BTC-flip bug).
"""
from . import config
from .util import f


def is_spot(coin: str) -> bool:
    """Spot coins are 'TOKEN/USDC' or '@<index>'; perps are plain ('BTC') or
    builder perps ('xyz:CL')."""
    return ("/" in coin) or coin.startswith("@")


def _new_ep(coin, side, x, *, open_complete=None):
    if open_complete is None:
        open_complete = abs(f(x.get("startPosition"))) < config.FLAT
    return {"coin": coin, "side": side, "open_ms": x["time"], "open_px": f(x["px"]),
            "net_pnl": 0.0, "fee": 0.0, "max_notl": 0.0, "n_fills": 0,
            "close_ms": x["time"], "close_px": f(x["px"]), "open_complete": bool(open_complete),
            "_grow_oids": set()}


def _finalize(ep):
    ep["hold_s"] = (ep["close_ms"] - ep["open_ms"]) / 1000.0
    ep["net_pnl"] -= ep["fee"]                          # closedPnl is gross; net it of fees
    ep["n_adds"] = max(0, len(ep.pop("_grow_oids")) - 1)  # distinct grow-orders minus the open
    return ep


def build_episodes(fills: list):
    """Reconstruct round-trips from fills. Returns (closed, open):
      closed = finalized flat->flat (or flip) episodes with realized PnL (the historical record).
      open   = the still-held position per coin at the end of the window (entry run not yet closed) —
               {coin, side, open_ms, open_px, cur_size}. These were previously DROPPED, which made the
               whole pipeline blind to a wallet's live positions (a trend trader's winning holds AND a
               扛单's losing holds). Caller marks them to current price via clearinghouseState.
    """
    fills = sorted(fills, key=lambda x: x["time"])
    by_coin: dict = {}
    for x in fills:
        by_coin.setdefault(x["coin"], []).append(x)
    episodes, open_eps = [], []
    for coin, fs in by_coin.items():
        ep = None
        cur_pos = 0.0                                   # signed position after the latest fill (for open size)
        for x in fs:
            sz = f(x["sz"])
            signed = sz if x["side"] == "B" else -sz
            pos0 = f(x.get("startPosition"))
            pos1 = pos0 + signed
            cur_pos = pos1
            px = f(x["px"])
            # Open an episode whenever a position is involved and we aren't tracking one — covers a
            # normal flat->open AND a position already open before the window (pos0 non-flat), incl. the
            # fill that CLOSES such a pre-existing position straight to flat (pos1 flat). Without the
            # pos0 check, that first closing fill's realized PnL (often a big +/-) was silently dropped.
            if ep is None and (abs(pos0) >= config.FLAT or abs(pos1) >= config.FLAT):
                base = pos0 if abs(pos0) >= config.FLAT else pos1
                ep = _new_ep(coin, "long" if base > 0 else "short", x)
            if ep is None:
                continue                                # flat before and after (self-contained scalp) — rare; skip
            ep["net_pnl"] += f(x.get("closedPnl"))      # this fill realizes PnL on the CURRENT side
            ep["fee"] += f(x.get("fee"))
            ep["n_fills"] += 1
            ep["max_notl"] = max(ep["max_notl"], abs(pos1) * px)
            ep["close_ms"] = x["time"]
            ep["close_px"] = px
            if abs(pos1) > abs(pos0) + config.FLAT:     # position grew = open or scale-in ORDER
                ep["_grow_oids"].add(x.get("oid"))
            flipped = (abs(pos1) >= config.FLAT and abs(pos0) >= config.FLAT
                       and (pos1 > 0) != (pos0 > 0))     # crossed zero in one fill
            if abs(pos1) < config.FLAT or flipped:
                episodes.append(_finalize(ep))
                ep = None
                if flipped:                             # same fill opened the opposite side -> new episode
                    ep = _new_ep(coin, "long" if pos1 > 0 else "short", x, open_complete=True)
                    ep["n_fills"] = 1                    # the flip fill opened it (its closedPnl already
                    ep["max_notl"] = abs(pos1) * px      # counted to the old side; don't double-count)
                    ep["_grow_oids"].add(x.get("oid"))
        if ep is not None and abs(cur_pos) >= config.FLAT:   # window ended with the position STILL OPEN
            open_eps.append({"coin": coin, "side": ep["side"], "open_ms": ep["open_ms"],
                             "open_px": ep["open_px"], "cur_size": abs(cur_pos)})
    return episodes, open_eps
