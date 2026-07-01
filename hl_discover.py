#!/usr/bin/env python3
"""CLI entrypoint for the discovery scanner. Logic lives in hl/ (scanner, metrics,
rest, fills, storage). Run from the repo root so `import hl` resolves.

  python3 hl_discover.py --db data/hl.db scan --days 14 --scan-interval 8   # full sweep, paced
  python3 hl_discover.py --db data/hl.db watchlist
  python3 hl_discover.py --db data/hl.db harvest
"""
import argparse
import calendar
import json
import subprocess
import time
from types import SimpleNamespace

from hl import config, params, scanner, storage
from hl.util import now_iso


AUTO_SCAN_EVERY_H = 24.0          # self-scheduled cadence (no systemd timer); reference = last scan_runs


def _scan_ns():
    """A scan args-namespace with operational defaults (matches the `scan` subparser); gate/harvest
    params get overlaid from the DB by params.apply_scanner_params. scan_interval 10s = conservative
    pace that leaves HL rate headroom for the always-running observer (the priority)."""
    return SimpleNamespace(days=14, limit=100000, order="mon_roi", no_harvest=False, full_scan=False,
                           workers=4, scan_interval=10.0, max_pages=5, min_crypto=0.3,
                           exclude_hft=True, hft_min_hold_min=3.0,
                           gate_loss_pain_max=config.GATE_LOSS_PAIN_MAX,
                           gate_hold_skew_max=config.GATE_HOLD_SKEW_MAX,
                           gate_profit_conc_max=config.GATE_PROFIT_CONC_MAX)


def _hours_since_last_scan(db):
    """Hours since the last COMPLETED scan (scan_runs.finished_at, UTC). Survives daemon restarts ->
    a restart never re-triggers a scan that already ran recently. 1e9 if never scanned."""
    r = db.execute("SELECT MAX(finished_at) m FROM scan_runs").fetchone()
    if not r or not r[0]:
        return 1e9
    try:
        return (time.time() - calendar.timegm(time.strptime(r[0], "%Y-%m-%dT%H:%M:%SZ"))) / 3600.0
    except (ValueError, TypeError):
        return 1e9


def _serve_observer_cmds(db):
    """SUPERVISOR role: consume observer_start / observer_stop commands the dashboard queued and drive the
    observer PROCESS via systemctl (the observer can't start itself, and once stopped can't consume a stop).
    On stop, immediately write process_status(observer)='stopped' so the dashboard flips without waiting for
    the heartbeat to go stale; on start, the observer writes its own 'running' on boot."""
    rows = db.execute("SELECT id,type FROM commands WHERE status='pending' "
                      "AND type IN ('observer_start','observer_stop') ORDER BY id").fetchall()
    for cid, ctype in rows:
        action = "start" if ctype == "observer_start" else "stop"
        try:
            r = subprocess.run(["systemctl", action, config.OBSERVER_UNIT],
                               capture_output=True, text=True, timeout=30)
            ok, detail = r.returncode == 0, (r.stderr or r.stdout or "").strip()[:300]
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, str(exc)[:300]
        if action == "stop":           # killed observer can't write its own down-state -> do it here
            db.execute("INSERT INTO process_status (name,state,pid,heartbeat_at,detail_json) VALUES "
                       "('observer','stopped',NULL,?,?) ON CONFLICT(name) DO UPDATE SET state='stopped',"
                       "pid=NULL,heartbeat_at=excluded.heartbeat_at,detail_json=excluded.detail_json",
                       (now_iso(), json.dumps({"by": "supervisor"})))
        db.execute("UPDATE commands SET status=?,done_at=?,result_json=? WHERE id=?",
                   ("done" if ok else "error", now_iso(),
                    json.dumps({"action": action, "ok": ok, "detail": detail}), cid))
        db.commit()
        print(f"observer {action}: {'ok' if ok else 'FAIL ' + detail}", flush=True)


def _serve_rescan(db):
    """Always-on scan executor: runs a full stop-the-world scan when the dashboard queues a `rescan`
    command OR when AUTO_SCAN_EVERY_H has elapsed since the last completed scan. SINGLE executor (never
    two scans at once) -> the observer's HL rate budget is never double-hit. No systemd timeout ->
    a ~2h slow scan can't be killed mid-run. scanner.scan() writes progress/status + absorbs any rescan
    queued during the scan (no redundant back-to-back run)."""
    config.MIN_POST_INTERVAL = 6.0                   # scan REST pace: ~6s/req uses the budget the observer
    #                                                  (~25-wallet fill-poll) leaves free, ~1.7× faster than
    #                                                  10s. If the observer starts logging 429/rate errors,
    #                                                  bump back up — the observer is still the priority.
    print("scan daemon: on-demand rescans + 24h auto-schedule + observer lifecycle ...", flush=True)
    while True:
        try:
            _serve_observer_cmds(db)                 # process-level start/stop of the observer (supervisor role)
            sp = db.execute("SELECT state FROM scan_progress WHERE id=1").fetchone()
            scanning = bool(sp and sp[0] == "scanning")
            if not scanning:
                scanner._set_scanner_proc(db, "idle", {"watching": True})   # keep heartbeat fresh (alive)
            pend = db.execute("SELECT id FROM commands WHERE status='pending' AND type='rescan' LIMIT 1").fetchone()
            due = _hours_since_last_scan(db) >= AUTO_SCAN_EVERY_H
            if (pend or due) and not scanning:
                ns = params.apply_scanner_params(db, _scan_ns())
                why = f"command #{pend[0]}" if pend else f"auto ({AUTO_SCAN_EVERY_H:g}h elapsed)"
                print(f"-> running full scan [{why}]", flush=True)
                scanner.scan(db, ns)                 # consumes pending rescan(s) + writes progress/status
        except Exception as exc:  # noqa: BLE001
            print(f"scan daemon error: {exc}", flush=True)
        time.sleep(3)


def main() -> int:
    ap = argparse.ArgumentParser(description="Hyperliquid copy-trade rolling scanner (perps)")
    ap.add_argument("--db", default=config.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_gate_args(pr):
        # v3 ELIGIBILITY gates (the few binary thresholds; QUALITY is the continuous score in
        # metrics.score, shaped by config constants). No more hardcoded win/roi/dd cutoffs.
        pr.add_argument("--min-perp", type=float, default=0.6, help="min copyable-perp share of fills")
        pr.add_argument("--inactive-days", type=float, default=3.0, help="reject if no fill within N days")
        pr.add_argument("--max-daily-eps", type=float, default=30.0, help="reject bots: max median episodes/active-day")
        pr.add_argument("--min-activity", type=float, default=0.21,
                        help="MINIMAL floor on active_days/lookback (~3 of 14d) — just rejects one-shot "
                             "noise. Low-freq-but-real traders are NOT killed here; the evidence-shrink "
                             "in score() ranks them DOWN until round-trips accumulate (soft, not hard)")
        pr.add_argument("--grid-max-adds", type=float, default=3.0,
                        help="reject grid/DCA: MEDIAN scale-ins per round-trip above this = habitual "
                             "averaging-down. Our model = open + MAX_ADDS adds, so a wallet that TYPICALLY "
                             "ladders 4+ times we only get the worst pre-average entries on → uncopyable")
        pr.add_argument("--max-single-loss", type=float, default=0.10,
                        help="reject 扛单到爆: worst single round-trip loss as fraction of account "
                             "(cuts-losses-small wallets pass even at 50%% win; one disaster loss = out)")
        pr.add_argument("--gate-loss-pain-max", type=float, default=config.GATE_LOSS_PAIN_MAX,
                        help="reject 小赚大亏: worst loss / median win at or above this (0 = off)")
        pr.add_argument("--gate-hold-skew-max", type=float, default=config.GATE_HOLD_SKEW_MAX,
                        help="reject 抗单: losing-hold / winning-hold at or above this (0 = off)")
        pr.add_argument("--gate-profit-conc-max", type=float, default=config.GATE_PROFIT_CONC_MAX,
                        help="reject 一把行情: best day share of gross profit at or above this (0 = off)")
        pr.add_argument("--no-exclude-hft", dest="exclude_hft", action="store_false", default=True,
                        help="by default reject sub-minute HFT scalpers (uncopyable at our latency); "
                             "pass this to allow them (only once a high-freq feed exists)")
        pr.add_argument("--hft-min-hold-min", type=float, default=3.0,
                        help="when excluding HFT: min median hold time in MINUTES (below = HFT, rejected)")

    def add_harvest_args(pr):
        # STAGE-1 leaderboard BOX (v5; 0 per-wallet API). Gate on HONEST fields only (capital + volume +
        # consistency + plausible pnl/volume); profit magnitude judged in the profile. Defaults in config.
        pr.add_argument("--min-acct", type=float, default=config.HARVEST_MIN_ACCT,
                        help="real-capital floor (we copy by pct, not $)")
        pr.add_argument("--week-vlm-min", type=float, default=config.HARVEST_WEEK_VLM_MIN,
                        help="7d VOLUME floor — genuinely trading this week")
        pr.add_argument("--week-vlm-max", type=float, default=config.HARVEST_WEEK_VLM_MAX,
                        help="7d VOLUME ceiling — above = market-maker/HFT-bot, uncopyable")
        pr.add_argument("--pnl-vol-min", type=float, default=config.HARVEST_PNL_VOL_MIN,
                        help="7d pnl/volume floor — below = razor-thin MM, not directional")
        pr.add_argument("--pnl-vol-max", type=float, default=config.HARVEST_PNL_VOL_MAX,
                        help="7d pnl/volume ceiling — above = profit too big for volume = ghost (not trading)")

    s = sub.add_parser("scan", help="full sweep: re-profile ALL candidates -> rebuild watchlist")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--limit", type=int, default=100000, help="cap workset size (default ~unbounded = full sweep)")
    s.add_argument("--order", choices=["mon_roi", "week_roi", "mon_pnl"], default="mon_roi")
    add_harvest_args(s)
    s.add_argument("--min-crypto", type=float, default=0.3, help="(unused) legacy prescreen arg")
    s.add_argument("--max-pages", type=int, default=5, help="cap fill pages/wallet (aggregateByTime -> "
                   "14d is ~1 page; >5 pages of trade-level fills = HFT/MM we reject anyway)")
    s.add_argument("--workers", type=int, default=4, help="concurrent profiling threads (rate is capped by --scan-interval)")
    s.add_argument("--scan-interval", type=float, default=8.0,
                   help="REST pace (s/request) for the scan PROCESS — slow trickle so it shares the IP "
                        "rate limit with the always-on observer (8s = ~7.5/min, leaves ~67/min for copy)")
    add_gate_args(s)
    s.add_argument("--no-harvest", action="store_true")
    s.add_argument("--full", dest="full_scan", action="store_true",
                   help="force a FULL 30d re-fetch for every candidate (else INCREMENTAL: only delta fills "
                        "since each candidate's cursor, merged onto the cached window). A full re-sync also "
                        "runs automatically every FULL_RESYNC_DAYS to self-heal any gap")

    w = sub.add_parser("watchlist", help="show our curated tiny leaderboard")
    w.add_argument("--top", type=int, default=40)

    h = sub.add_parser("harvest", help="refresh candidate pool only")
    add_harvest_args(h)

    g = sub.add_parser("regate", help="re-apply gate thresholds on STORED profiles (no re-fetch) + rebuild watchlist")
    add_gate_args(g)

    sub.add_parser("serve-rescan", help="daemon: run a full scan on demand when a dashboard rescan command is queued")

    args = ap.parse_args()
    db = storage.connect(args.db, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)  # +control-plane tables
    params.seed_params(db)                               # ensure UI-tunable params exist (idempotent)
    if args.cmd == "scan":
        config.MIN_POST_INTERVAL = args.scan_interval   # slow this PROCESS's REST pace (trickle);
        params.apply_scanner_params(db, args)           # UI-tuned gates/harvest override CLI defaults
        scanner.scan(db, args)                          # the observer process keeps its own fast pace
    elif args.cmd == "serve-rescan":
        _serve_rescan(db)
    elif args.cmd == "watchlist":
        scanner.watchlist(db, args.top)
    elif args.cmd == "harvest":
        print(f"{scanner.harvest(db, args)} candidates")
    elif args.cmd == "regate":
        params.apply_scanner_params(db, args)            # honor UI-tuned gates (incl HFT switch) on regate
        scanner.regate(db, args)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
