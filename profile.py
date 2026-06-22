#!/usr/bin/env python3
"""Analyse collected BTC 5min data — pair-arb centric.

  profile.py rank [--min-windows N]      rank recurring two-sided profitable wallets
  profile.py wallet <addr>               per-window breakdown + pair-cost / book detail

PnL and pair_cost are reconstructed from the unbiased market trade feed; windows
flagged `incomplete` (net shares went negative => off-book acquisition we could
not see) are excluded from PnL stats.
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics as st


def _connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def cmd_rank(db: sqlite3.Connection, min_windows: int, dual_only: bool) -> None:
    dual_clause = "AND ww.dual=1" if dual_only else ""
    rows = db.execute(f"""
        SELECT ww.wallet, MAX(ww.name) name,
               COUNT(*)                                   n_windows,
               SUM(ww.dual)                               n_dual,
               SUM(CASE WHEN ww.incomplete=0 THEN ww.realized_pnl END)        pnl,
               AVG(CASE WHEN ww.incomplete=0 AND ww.realized_pnl>0 THEN 1.0
                        WHEN ww.incomplete=0 THEN 0.0 END)                    win_rate,
               AVG(ww.pair_cost)                          avg_pair_cost,
               SUM(CASE WHEN ww.pair_cost<1.0 THEN 1 ELSE 0 END)             n_pair_lt1,
               SUM(CASE WHEN ww.pair_cost IS NOT NULL THEN 1 ELSE 0 END)     n_pair,
               SUM(ww.n_trades)                           trades,
               SUM(ww.incomplete)                         n_incomplete
        FROM wallet_window ww
        JOIN windows w ON w.slug = ww.window_slug
        WHERE w.settled = 1 {dual_clause}
        GROUP BY ww.wallet
        HAVING n_windows >= ?
        ORDER BY n_windows DESC, pnl DESC
        LIMIT 40
    """, (min_windows,)).fetchall()

    n_settled = db.execute("SELECT COUNT(*) FROM windows WHERE settled=1").fetchone()[0]
    print(f"settled windows in db: {n_settled}   (ranking {'two-sided' if dual_only else 'all'} wallets, "
          f">= {min_windows} windows)\n")
    hdr = f"{'wallet':42} {'name':14} {'win':>4} {'dual':>4} {'pnl':>10} {'wr':>5} {'pairC':>6} {'<1':>6} {'trd':>6}"
    print(hdr); print("-" * len(hdr))
    for r in rows:
        pair = f"{r['avg_pair_cost']:.3f}" if r['avg_pair_cost'] is not None else "  -  "
        lt1 = f"{r['n_pair_lt1']}/{r['n_pair']}" if r['n_pair'] else "  -  "
        wr = f"{100*r['win_rate']:.0f}%" if r['win_rate'] is not None else "  - "
        pnl = f"{r['pnl']:.1f}" if r['pnl'] is not None else "  -  "
        inc = f"  !inc{r['n_incomplete']}" if r['n_incomplete'] else ""
        print(f"{r['wallet']:42} {(r['name'] or '')[:14]:14} {r['n_windows']:>4} {r['n_dual']:>4} "
              f"{pnl:>10} {wr:>5} {pair:>6} {lt1:>6} {r['trades']:>6}{inc}")


def cmd_wallet(db: sqlite3.Connection, wallet: str) -> None:
    wallet = wallet.lower()
    rows = db.execute("""
        SELECT ww.*, w.winning_side, w.settled
        FROM wallet_window ww JOIN windows w ON w.slug = ww.window_slug
        WHERE ww.wallet = ? AND w.settled = 1
        ORDER BY ww.first_ts
    """, (wallet,)).fetchall()
    if not rows:
        print(f"no settled windows for {wallet}")
        return

    name = next((r["name"] for r in rows if r["name"]), "")
    pnls = [r["realized_pnl"] for r in rows if not r["incomplete"]]
    pair_costs = [r["pair_cost"] for r in rows if r["pair_cost"] is not None]
    n_dual = sum(r["dual"] for r in rows)
    n_lt1 = sum(1 for c in pair_costs if c < 1.0)
    print(f"wallet {wallet}  name={name!r}")
    print(f"  settled windows: {len(rows)}   two-sided(dual): {n_dual}   incomplete: {sum(r['incomplete'] for r in rows)}")
    if pnls:
        print(f"  realized PnL: total={sum(pnls):.2f}  mean/window={st.mean(pnls):.3f}  "
              f"win-window rate={100*sum(1 for p in pnls if p>0)/len(pnls):.0f}%")
    if pair_costs:
        print(f"  pair_cost: windows={len(pair_costs)}  <$1: {n_lt1} ({100*n_lt1/len(pair_costs):.0f}%)  "
              f"median={st.median(pair_costs):.4f}  min={min(pair_costs):.4f}  max={max(pair_costs):.4f}")
        verdict = "PURE PAIR-ARB (sum<$1)" if n_lt1 / len(pair_costs) > 0.8 else \
                  "NOT consistently <$1 — check directional tilt (spot)"
        print(f"  => {verdict}")

    print(f"\n  {'window':26} {'win':>4} {'upB':>4} {'dnB':>4} {'pairC':>7} {'pnl':>9} {'flag':>6}")
    for r in rows[-25:]:
        pc = f"{r['pair_cost']:.4f}" if r["pair_cost"] is not None else "   -   "
        flag = "inc" if r["incomplete"] else ("dual" if r["dual"] else "")
        wend = r["window_slug"].split("-")[-1]
        print(f"  {wend:26} {r['winning_side'] or '?':>4} {r['up_buys']:>4} {r['down_buys']:>4} "
              f"{pc:>7} {r['realized_pnl']:>9.2f} {flag:>6}")

    ntr = db.execute("SELECT COUNT(*) FROM trades WHERE wallet=?", (wallet,)).fetchone()[0]
    print(f"\n  per-fill detail rows stored (two-sided windows): {ntr}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyse BTC 5min collected data")
    ap.add_argument("--db", default="btc5min.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("rank")
    pr.add_argument("--min-windows", type=int, default=2)
    pr.add_argument("--all", action="store_true", help="include one-sided wallets")
    pw = sub.add_parser("wallet")
    pw.add_argument("addr")
    args = ap.parse_args()

    db = _connect(args.db)
    if args.cmd == "rank":
        cmd_rank(db, args.min_windows, dual_only=not args.all)
    elif args.cmd == "wallet":
        cmd_wallet(db, args.addr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
