"""Mock observer+scanner consumer for the PREVIEW: runs the REAL observer command loop AND a
scanner-progress / rolling-status simulator against the mock db, so UI actions (pause/close/toggle)
and the采集 page (rolling status + rescan mask) work live without the real engines.
Usage (from repo root):  python3 dashboard/web/dev/mock_consumer.py data/hl_mock.db

NOTE: production replaces this — the real Scanner must write process_status('scanner','rolling',...)
per profiled wallet, consume rescan commands, and write scan_progress.
"""
import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from hyper import storage
from hyper.execution import observer
from hyper.util import now_iso

DB = sys.argv[1] if len(sys.argv) > 1 else "data/hl_mock.db"
STAGES = ["scan_leaderboard", "fetch_history", "score_filter", "rebuild_watchlist", "persist"]


async def rolling_sim(db):
    """Simulate the ALWAYS-ON rolling scanner: trickle through the work-set, updating the scanner
    heartbeat + sweep position each tick. Shows 'scanning' while a full rescan runs."""
    addrs = [r[0] for r in db.execute("SELECT addr FROM watchlist ORDER BY rank").fetchall()] or ["0x—"]
    total, pos, profiled = max(len(addrs) * 64, 386), 0, 0
    while True:
        sp = db.execute("SELECT state FROM scan_progress WHERE id=1").fetchone()
        rescanning = sp and sp[0] == "scanning"
        pos = (pos + 1) % total
        profiled += 1
        detail = {"cycle_pos": pos, "cycle_total": total, "interval_s": 8,
                  "last_addr": addrs[pos % len(addrs)], "last_at": now_iso(), "profiled_session": profiled}
        db.execute("INSERT INTO process_status (name,state,pid,heartbeat_at,detail_json) VALUES "
                   "('scanner',?,0,?,?) ON CONFLICT(name) DO UPDATE SET state=excluded.state,"
                   "heartbeat_at=excluded.heartbeat_at,detail_json=excluded.detail_json",
                   ("scanning" if rescanning else "rolling", now_iso(), json.dumps(detail)))
        db.commit()
        await asyncio.sleep(2.0)


async def scanner_sim(db):
    """Consume rescan commands and animate scan_progress over ~12s, then write a scan_runs row."""
    while True:
        row = db.execute("SELECT id FROM commands WHERE status='pending' AND type='rescan' ORDER BY id LIMIT 1").fetchone()
        if row:
            cid = row[0]
            db.execute("UPDATE commands SET status='acked',acked_at=? WHERE id=?", (now_iso(), cid)); db.commit()
            total, started = 1240, now_iso()
            db.execute("INSERT OR REPLACE INTO scan_progress "
                       "(id,state,started_at,stage,candidates_scanned,candidates_total,eta_sec,manual,updated_at) "
                       "VALUES (1,'scanning',?,?,0,?,?,1,?)", (started, STAGES[0], total, 20, now_iso()))
            db.commit()
            steps = len(STAGES) * 4
            cancelled = False
            for i, stage in enumerate(STAGES):
                for step in range(4):
                    await asyncio.sleep(0.6)
                    status = db.execute("SELECT status FROM commands WHERE id=?", (cid,)).fetchone()
                    if not status or status[0] != "acked":
                        cancelled = True
                        break
                    scanned = int(total * ((i * 4 + step + 1) / steps))
                    db.execute("UPDATE scan_progress SET stage=?,candidates_scanned=?,updated_at=? WHERE id=1",
                               (stage, scanned, now_iso())); db.commit()
                if cancelled:
                    break
            if cancelled:
                db.execute("UPDATE scan_progress SET state='idle',stage='cancelled',updated_at=? WHERE id=1",
                           (now_iso(),)); db.commit()
                print("scanner-sim: rescan cancelled", flush=True)
                continue
            db.execute("INSERT INTO scan_runs (started_at,finished_at,duration_s,candidates,probed_new,added,"
                       "retired,kept,rejected,n_active) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (started, now_iso(), 12.0, total, 55, 2, 1, 24, 860, 26))
            db.execute("UPDATE scan_progress SET state='idle',updated_at=? WHERE id=1", (now_iso(),))
            db.execute("UPDATE commands SET status='done',done_at=? WHERE id=?", (now_iso(), cid)); db.commit()
            print("scanner-sim: rescan done", flush=True)
        await asyncio.sleep(1.0)


async def main():
    db = storage.connect(DB, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
    obs = observer.Observer(db, [], {})
    obs._load_account()
    obs._reload_open()
    for coin, mark in db.execute("SELECT coin, mark_px FROM copy_position WHERE status='open'").fetchall():
        if mark:
            obs.bbo[coin] = (mark * 0.9995, mark * 1.0005)
    obs._write_proc_status("running")
    db.execute("INSERT OR REPLACE INTO scan_progress (id,state,updated_at) VALUES (1,'idle',?)", (now_iso(),))
    db.commit()
    print("mock consumer running against", DB, flush=True)
    await asyncio.gather(obs.consume_commands(), scanner_sim(db), rolling_sim(db))

asyncio.run(main())
