#!/usr/bin/env python3
"""CLI entrypoint for the discovery scanner. Logic lives in :mod:`hyper`.

  python3 -m hyper.cli.discover --db data/hl.db scan --days 14 --scan-interval 8
  python3 -m hyper.cli.discover --db data/hl.db watchlist
  python3 -m hyper.cli.discover --db data/hl.db harvest
"""
import argparse
import calendar
import json
import sqlite3
import subprocess
import time
from types import SimpleNamespace

import threading

from hyper import config, params, storage
from hyper.discovery import frozen_audit, scanner
from hyper.discovery import shadow_scan
from hyper.ops import paper_reset, procman
from hyper.util import now_iso


def _start_adaptive_pace(db_path, slow_interval):
    """Scan REST pace adapts to whether copy-trading is live. Observer RUNNING → slow (`slow_interval`,
    the --scan-interval trickle) so the scan yields the IP's HL weight budget to live fill-polling.
    Observer STOPPED → full speed (SCAN_IDLE_INTERVAL) — nothing else is competing, so a manual rescan
    finishes in ~15min instead of ~2h. Re-polls every 20s so it adapts if you start/stop the observer
    mid-scan (config.MIN_POST_INTERVAL is read live by rest.post)."""
    def _observer_has_work():
        if not procman.observer_running(db_path):
            return False
        try:
            con = sqlite3.connect(db_path, timeout=2)
            generation = con.execute(
                "SELECT generation FROM scan_generation WHERE status='published' AND complete=1 "
                "AND is_current=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            open_n = con.execute(
                "SELECT COUNT(*) FROM copy_position WHERE status='open'"
            ).fetchone()[0]
            if generation:
                target_n = con.execute(
                    "SELECT COUNT(*) FROM follow_selection fs LEFT JOIN target_controls tc ON tc.addr=fs.addr "
                    "WHERE fs.generation=? AND fs.role='core' AND fs.enabled=1 AND COALESCE(tc.enabled,1)=1",
                    (generation[0],),
                ).fetchone()[0]
            else:
                target_n = con.execute(
                    "SELECT COUNT(*) FROM watchlist w LEFT JOIN target_controls tc ON tc.addr=w.addr "
                    "WHERE COALESCE(tc.enabled,1)=1"
                ).fetchone()[0]
            con.close()
            return bool(open_n or target_n)
        except Exception:  # old/in-flight DB: preserve observer priority conservatively
            return True

    def _pace():
        return slow_interval if _observer_has_work() else config.SCAN_IDLE_INTERVAL
    config.MIN_POST_INTERVAL = _pace()                      # set the starting pace before the sweep begins
    def _tick():
        while True:
            time.sleep(20)
            config.MIN_POST_INTERVAL = _pace()
    threading.Thread(target=_tick, daemon=True).start()


AUTO_SCAN_EVERY_H = 72.0          # local daemon fallback; VPS uses the Monday/Thursday systemd timer


def _scan_ns():
    """A scan args-namespace with operational defaults (matches the `scan` subparser); gate/harvest
    params get overlaid from the DB by params.apply_scanner_params. scan_interval 10s = conservative
    pace that leaves HL rate headroom for the always-running observer (the priority)."""
    return SimpleNamespace(days=14, limit=100000, order="mon_roi", no_harvest=False, full_scan=False,
                           workers=4, scan_interval=10.0, max_pages=5, min_crypto=0.3,
                           exclude_hft=True, hft_min_hold_min=3.0,
                           max_single_adds=config.MAX_SINGLE_ADDS_PER_EP)


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


def _configure_scan_cadence(db, ns, *, manual: bool):
    """Every run refreshes Leaderboard and reevaluates the complete strict candidate set."""
    published = db.execute(
        "SELECT 1 FROM scan_generation WHERE status='published' AND is_current=1 LIMIT 1"
    ).fetchone()
    ns.full_scan = True
    ns.no_harvest = False
    if not published:
        return "cold_full"
    return "manual_complete" if manual else "scheduled_complete"


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
    """Always-on scan executor: runs a scan when the dashboard queues a `rescan`
    command or the configured automatic cadence is due. A single executor (never
    two scans at once) -> the observer's HL rate budget is never double-hit. No systemd timeout ->
    a ~2h slow scan can't be killed mid-run. scanner.scan() writes progress/status + absorbs any rescan
    queued during the scan (no redundant back-to-back run)."""
    config.MIN_POST_INTERVAL = 6.0                   # scan REST pace: ~6s/req uses the budget the observer
    #                                                  (~25-wallet fill-poll) leaves free, ~1.7× faster than
    #                                                  10s. If the observer starts logging 429/rate errors,
    #                                                  bump back up — the observer is still the priority.
    print("scan daemon: on-demand + scheduled scans; observer command bridge ready", flush=True)
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
                cadence = _configure_scan_cadence(db, ns, manual=bool(pend))
                why = f"command #{pend[0]}" if pend else "auto 72h complete candidate reevaluation"
                print(f"-> running scan [{why}]", flush=True)
                scanner.scan(db, ns)                 # consumes pending rescan(s) + writes progress/status
        except Exception as exc:  # noqa: BLE001
            print(f"scan daemon error: {exc}", flush=True)
            try:
                n = scanner.ensure_watchlist_current(db)
                scanner._set_scan_progress(db, state="idle", stage="error")
                scanner._set_scanner_proc(db, "idle", {"last_error": str(exc)[:300], "active": n})
            except Exception:
                pass
        time.sleep(3)


def main() -> int:
    ap = argparse.ArgumentParser(description="Hyperliquid copy-trade rolling scanner (perps)")
    ap.add_argument("--db", default=config.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_gate_args(pr):
        # v3 ELIGIBILITY gates (the few binary thresholds; QUALITY is the continuous score in
        # metrics.score, shaped by config constants). No more hardcoded win/roi/dd cutoffs.
        pr.add_argument("--min-perp", type=float, default=0.6, help="min copyable-perp share of fills")
        pr.add_argument("--inactive-days", type=float, default=config.INACTIVE_DAYS,
                        help="reject if no copyable open within N days")
        pr.add_argument("--max-daily-eps", type=float, default=30.0, help="reject bots: max median episodes/active-day")
        pr.add_argument("--min-activity", type=float, default=0.21,
                        help="MINIMAL floor on active_days/lookback (~3 of 14d) — just rejects one-shot "
                             "noise. Low-freq-but-real traders are NOT killed here; the evidence-shrink "
                             "in score() ranks them DOWN until round-trips accumulate (soft, not hard)")
        pr.add_argument("--grid-max-adds", type=float, default=3.0,
                        help="reject grid/DCA: MEDIAN scale-ins per round-trip above this = habitual "
                             "averaging-down. Our model = open + MAX_ADDS adds, so a wallet that TYPICALLY "
                             "ladders 4+ times we only get the worst pre-average entries on → uncopyable")
        pr.add_argument("--max-single-adds", type=float, default=config.MAX_SINGLE_ADDS_PER_EP,
                        help="reject heavy DCA: any single round-trip with more scale-ins than this is "
                             "uncopyable even when the median is low")
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
        # Nominal contract volume is activity only and never a profitability denominator because leverage
        # makes that ratio incomparable. Official four-week return quality comes from Portfolio history.
        pr.add_argument("--min-acct", type=float, default=config.HARVEST_MIN_ACCT,
                        help="real-capital floor (we copy by pct, not $)")
        pr.add_argument("--week-vlm-min", type=float, default=config.HARVEST_WEEK_VLM_MIN,
                        help="7d VOLUME floor — genuinely trading this week")
        pr.add_argument("--week-pnl-min", type=float, default=config.HARVEST_WEEK_PNL_MIN)
        pr.add_argument("--month-pnl-min", type=float, default=config.HARVEST_MONTH_PNL_MIN)
        pr.add_argument("--all-pnl-min", type=float, default=config.HARVEST_ALL_PNL_MIN)
        pr.add_argument("--perp-pnl-share-min", type=float, default=config.HARVEST_PERP_PNL_SHARE_MIN)

    s = sub.add_parser("scan", help="full sweep: re-profile ALL candidates -> rebuild watchlist")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--limit", type=int, default=100000, help="cap workset size (default ~unbounded = full sweep)")
    s.add_argument("--order", choices=["mon_roi", "week_roi", "mon_pnl"], default="mon_roi")
    add_harvest_args(s)
    s.add_argument("--max-pages", type=int, default=5, help="cap fill pages/wallet (aggregateByTime -> "
                   "14d is ~1 page; >5 pages of trade-level fills = HFT/MM we reject anyway)")
    s.add_argument("--workers", type=int, default=4, help="concurrent profiling threads (rate is capped by --scan-interval)")
    s.add_argument("--scan-interval", type=float, default=8.0,
                   help="REST pace (s/request) for the scan PROCESS — slow trickle so it shares the IP "
                        "rate limit with the always-on observer (8s = ~7.5/min, leaves ~67/min for copy)")
    add_gate_args(s)
    s.add_argument("--no-harvest", action="store_true")
    s.add_argument("--full", dest="full_scan", action="store_true", help=argparse.SUPPRESS)

    w = sub.add_parser("watchlist", help="show our curated tiny leaderboard")
    w.add_argument("--top", type=int, default=40)

    h = sub.add_parser("harvest", help="refresh candidate pool only")
    add_harvest_args(h)

    g = sub.add_parser("regate", help="re-apply gate thresholds on STORED profiles (no re-fetch) + rebuild watchlist")
    add_gate_args(g)

    sub.add_parser("repair-watchlist", help="rebuild watchlist if it drifted from active profiles")
    sub.add_parser("serve-rescan", help="daemon: run a full scan on demand when a dashboard rescan command is queued")
    t = sub.add_parser("tune", help=argparse.SUPPRESS)
    t.add_argument("--generation", required=True)
    t.add_argument("--stamp")
    opt = sub.add_parser("optimize", help="qualify Core first, then tune one shared copy-parameter surface")
    opt.add_argument("--generation")
    opt.add_argument("--stamp")
    rs = sub.add_parser("repair-selection", help=argparse.SUPPRESS)
    rs.add_argument("--generation")
    rs.add_argument("--stamp")
    rs.add_argument("--replace-existing", action="store_true")
    fg = sub.add_parser("finalize-profiled", help="finish a cached profiled generation without wallet refetch")
    fg.add_argument("--generation")
    fg.add_argument("--stamp")
    fg.add_argument("--no-retune", action="store_true",
                    help="seal the active parameter surface while retaining strict path/portfolio gates")
    reset = sub.add_parser("reset-paper", help="clear discovery/Paper state while preserving operator params")
    reset.add_argument("--factory-params", action="store_true",
                       help="also restore all params to code defaults")
    reset.add_argument("--yes", action="store_true", help="required destructive-operation confirmation")
    shadow = sub.add_parser("shadow-scan", help="isolated full discovery on an online SQLite backup")
    shadow.add_argument("--report", required=True, help="0600 redacted JSON report path")
    shadow.add_argument("--scan-interval", type=float, default=10.0)
    shadow.add_argument("--max-pages", type=int, default=5)
    shadow.add_argument("--workers", type=int, default=4)
    shadow.add_argument("--week-pnl-min", type=float)
    shadow.add_argument("--month-pnl-min", type=float)
    shadow.add_argument("--all-pnl-min", type=float)
    audit = sub.add_parser("audit-pipeline", help="read-only frozen generation waterfall; no network")
    audit.add_argument("--report", required=True, help="0600 redacted JSON report path")
    audit.add_argument("--generation")
    audit.add_argument("--stamp")

    args = ap.parse_args()
    if args.cmd == "audit-pipeline":
        result = frozen_audit.build(
            args.db, args.report, generation=args.generation, stamp=args.stamp,
        )
        print(json.dumps({
            "status": "ok", "report": args.report, "generation": result["generation"]["id"],
            "funnel": result["funnel"],
        }, sort_keys=True))
        return 0
    if args.cmd == "shadow-scan":
        ns = _scan_ns()
        ns.scan_interval, ns.max_pages, ns.workers = args.scan_interval, args.max_pages, args.workers
        config.MIN_POST_INTERVAL = args.scan_interval
        overrides = {
            key: value for key, value in {
                "HARVEST_WEEK_PNL_MIN": args.week_pnl_min,
                "HARVEST_MONTH_PNL_MIN": args.month_pnl_min,
                "HARVEST_ALL_PNL_MIN": args.all_pnl_min,
            }.items() if value is not None
        }
        if any(float(value) < 0 for value in overrides.values()):
            ap.error("shadow scan ROI/PnL overrides must be non-negative")
        result = shadow_scan.run(args.db, args.report, ns, param_overrides=overrides)
        print(json.dumps({"status": result["generation"]["status"], "report": args.report,
                          "funnel": result["funnel"], "roles": result["roles"]}, sort_keys=True))
        return 0
    db = storage.connect(args.db, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)  # +control-plane tables
    params.seed_params(db)                               # ensure UI-tunable params exist (idempotent)
    if args.cmd == "scan":
        pending_manual = db.execute(
            "SELECT 1 FROM commands WHERE status='pending' AND type='rescan' LIMIT 1"
        ).fetchone()
        _configure_scan_cadence(db, args, manual=bool(pending_manual))
        _start_adaptive_pace(args.db, args.scan_interval)  # observer live → slow trickle; idle → full speed
        params.apply_scanner_params(db, args)           # UI-tuned gates/harvest override CLI defaults
        try:
            scanner.scan(db, args)                      # the observer (when up) keeps its own fast pace
        except Exception as exc:  # noqa: BLE001
            n = scanner.ensure_watchlist_current(db)
            scanner._set_scan_progress(db, state="idle", stage="error")
            scanner._set_scanner_proc(db, "idle", {"last_error": str(exc)[:300], "active": n})
            raise
    elif args.cmd == "serve-rescan":
        _serve_rescan(db)
    elif args.cmd == "watchlist":
        scanner.watchlist(db, args.top)
    elif args.cmd == "harvest":
        print(f"{scanner.harvest(db, args)} candidates")
    elif args.cmd == "regate":
        params.apply_scanner_params(db, args)            # honor UI-tuned gates (incl HFT switch) on regate
        scanner.regate(db, args)
    elif args.cmd == "repair-watchlist":
        n = scanner.ensure_watchlist_current(db)
        scanner._set_scan_progress(db, state="idle", stage="repair_watchlist",
                                   candidates_scanned=0, candidates_total=0)
        scanner._set_scanner_proc(db, "idle", {"last_repair_at": now_iso(), "active": n})
        print(f"watchlist {n} active")
    elif args.cmd == "tune":
        # Keep the legacy hidden verb as a compatibility alias. Formation qualifies the complete bounded
        # quality pool first and tunes that pool once; tuning no longer decides wallet count.
        result = scanner.optimize_published_generation(db, args.generation, stamp=args.stamp)
        print(json.dumps(result, sort_keys=True, default=str))
    elif args.cmd == "optimize":
        result = scanner.optimize_published_generation(db, args.generation, stamp=args.stamp)
        print(json.dumps(result, sort_keys=True, default=str))
    elif args.cmd == "repair-selection":
        result = scanner.repair_published_selection(
            db, args.generation, stamp=args.stamp, replace_existing=args.replace_existing,
        )
        print(json.dumps(result, sort_keys=True, default=str))
    elif args.cmd == "finalize-profiled":
        result = scanner.finalize_profiled_generation(
            db, generation_id=args.generation, stamp=args.stamp,
            retune=not bool(args.no_retune),
        )
        print(json.dumps(result, sort_keys=True, default=str))
    elif args.cmd == "reset-paper":
        if not args.yes:
            raise RuntimeError("reset-paper requires --yes")
        if procman.observer_running(args.db) or procman.scan_running(args.db):
            raise RuntimeError("stop Observer and Scanner before reset-paper")
        result = paper_reset.reset(db, factory_params=bool(args.factory_params))
        print(json.dumps(result, sort_keys=True, default=str))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
