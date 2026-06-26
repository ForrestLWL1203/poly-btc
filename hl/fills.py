"""Fill-level logic: spot/perp classification and position-episode reconstruction.

Key rule: fills != trades. One order is matched in many slices, so a round-trip
"trade" can be thousands of fills. An EPISODE = position open (flat->nonzero) until
back to flat, reconstructed from startPosition (authoritative pre-fill position).
"""
from . import config
from .util import f


def is_spot(coin: str) -> bool:
    """Spot coins are 'TOKEN/USDC' or '@<index>'; perps are plain ('BTC') or
    builder perps ('xyz:CL')."""
    return ("/" in coin) or coin.startswith("@")


def build_episodes(fills: list) -> list:
    fills = sorted(fills, key=lambda x: x["time"])
    by_coin: dict = {}
    for x in fills:
        by_coin.setdefault(x["coin"], []).append(x)
    episodes = []
    for coin, fs in by_coin.items():
        ep = None
        for x in fs:
            sz = f(x["sz"])
            signed = sz if x["side"] == "B" else -sz
            pos0 = f(x.get("startPosition"))
            pos1 = pos0 + signed
            if ep is None and abs(pos0) < config.FLAT and abs(pos1) >= config.FLAT:
                ep = {"coin": coin, "side": "long" if pos1 > 0 else "short",
                      "open_ms": x["time"], "open_px": f(x["px"]), "net_pnl": 0.0,
                      "fee": 0.0, "max_notl": 0.0, "n_fills": 0, "_grow_oids": set()}
            if ep is not None:
                ep["net_pnl"] += f(x.get("closedPnl"))
                ep["fee"] += f(x.get("fee"))
                ep["n_fills"] += 1
                ep["max_notl"] = max(ep["max_notl"], abs(pos1) * f(x["px"]))
                ep["close_ms"] = x["time"]
                ep["close_px"] = f(x["px"])
                if abs(pos1) > abs(pos0) + config.FLAT:    # position grew = open or a scale-in ORDER
                    ep["_grow_oids"].add(x.get("oid"))     # distinct orders (dedup same-oid slices)
                if abs(pos1) < config.FLAT:
                    ep["hold_s"] = (ep["close_ms"] - ep["open_ms"]) / 1000.0
                    ep["net_pnl"] -= ep["fee"]
                    ep["n_adds"] = max(0, len(ep.pop("_grow_oids")) - 1)  # exclude the open order
                    episodes.append(ep)
                    ep = None
    return episodes
