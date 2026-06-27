"""UI-tunable strategy parameters — single source of truth for the dashboard `params` table.

Two categories, two effect models (see doc/dashboard-landing-plan.md §4):
  scanner (effect=rescan)    -> Scanner reads at scan start; a change needs a rescan to take effect.
  follow  (effect=immediate) -> Observer reads at run time; a change applies to the next new copy.

VALUES ARE STORED IN UI-FACING UNITS (the API contract: percent fields carry the percent number,
e.g. 0.5 means 0.5%). The engines still think in fractions, so when Observer/Scanner switch to
reading from this table (M2/M4) they convert `pct` values /100. Defaults below mirror the running
code (hl/config.py + hl_discover.py argparse) so seeding never changes live behaviour.

level drives UI affordance: green=edit freely, yellow=edit w/ confirm, blue=dev-mode only,
black=read-only. type: usd|pct|x|int|float|nullable|bool|display.
"""
from . import config
from .util import now_iso

# (key, category, level, type, effect, default)  — default already in UI-facing units.
PARAM_SPEC = [
    # ── ① Scanner / watchlist params (effect = rescan) ──────────────────────────────────
    ("HARVEST_MIN_ACCT",     "scanner", "yellow", "usd",     "rescan", config.HARVEST_MIN_ACCT),
    ("HARVEST_MAX_TURNOVER", "scanner", "yellow", "x",       "rescan", config.HARVEST_MAX_TURNOVER),
    ("HARVEST_WEEK_VLM_MIN", "scanner", "yellow", "usd",     "rescan", config.HARVEST_WEEK_VLM_MIN),
    ("HARVEST_MON_ROI_MIN",  "scanner", "yellow", "pct",     "rescan", config.HARVEST_MON_ROI_MIN * 100),
    ("HARVEST_MON_ROI_MAX",  "scanner", "yellow", "pct",     "rescan", config.HARVEST_MON_ROI_MAX * 100),
    ("HARVEST_WEEK_ROI_MIN", "scanner", "yellow", "pct",     "rescan", config.HARVEST_WEEK_ROI_MIN * 100),
    ("min_perp",             "scanner", "blue",   "pct",     "rescan", 60),    # hl_discover --min-perp 0.6
    ("inactive_days",        "scanner", "green",  "int",     "rescan", 3),     # hl_discover --inactive-days
    ("max_daily_eps",        "scanner", "blue",   "int",     "rescan", 30),    # hl_discover --max-daily-eps
    ("min_activity",         "scanner", "blue",   "float",   "rescan", 0.21),  # hl_discover --min-activity
    ("grid_max_adds",        "scanner", "blue",   "int",     "rescan", 5),     # hl_discover --grid-max-adds
    ("max_single_loss",      "scanner", "yellow", "pct",     "rescan", 10),    # hl_discover --max-single-loss 0.10
    ("SCORE_SHRINK_K",       "scanner", "blue",   "int",     "rescan", int(config.SCORE_SHRINK_K)),
    ("SCORE_RAR_CAP",        "scanner", "blue",   "float",   "rescan", config.SCORE_RAR_CAP),
    ("SCORE_K",              "scanner", "blue",   "int",     "rescan", int(config.SCORE_K)),
    ("SCORE_GAMMA",          "scanner", "blue",   "float",   "rescan", config.SCORE_GAMMA),
    ("UW_TOL",               "scanner", "blue",   "display", "rescan", "2% / 10%"),  # UW_TOL / UW_REF (read-only)

    # ── ② Follow / copy-strategy params (effect = immediate) ────────────────────────────
    ("MIN_FOLLOW_SCORE",     "follow",  "green",  "float",   "immediate", config.MIN_FOLLOW_SCORE),
    ("MAX_TARGETS",          "follow",  "green",  "int",     "immediate", config.MAX_TARGETS),
    ("RISK_K",               "follow",  "yellow", "float",   "immediate", config.RISK_K),
    ("RF_MIN",               "follow",  "green",  "pct",     "immediate", config.RF_MIN * 100),
    ("RF_MAX",               "follow",  "green",  "pct",     "immediate", config.RF_MAX * 100),
    ("MAX_LEV",              "follow",  "yellow", "x",       "immediate", config.MAX_LEV),
    ("MIN_LEV",              "follow",  "blue",   "x",       "immediate", config.MIN_LEV),
    ("MIN_OPEN_MARGIN_PCT",  "follow",  "green",  "pct",     "immediate", config.MIN_OPEN_MARGIN_PCT * 100),
    ("ADD_MARGIN_PCT",       "follow",  "yellow", "pct",     "immediate", config.ADD_MARGIN_PCT * 100),
    ("MAX_ADDS",             "follow",  "yellow", "int",     "immediate", config.MAX_ADDS),
    ("MAX_ENTRY_CHASE_PCT",  "follow",  "green",  "nullable","immediate",
        (config.MAX_ENTRY_CHASE_PCT * 100) if config.MAX_ENTRY_CHASE_PCT is not None else None),
    ("EXEC_MAKER_MIRROR",    "follow",  "black",  "bool",    "immediate", config.EXEC_MAKER_MIRROR),
    ("VOL_FAST_DAYS",        "follow",  "blue",   "display", "immediate",
        f"{config.VOL_FAST_DAYS} / {config.VOL_SLOW_DAYS} 天"),  # VOL_FAST/SLOW window (read-only)
    ("VOL_FALLBACK_SIGMA",   "follow",  "blue",   "pct",     "immediate", config.VOL_FALLBACK_SIGMA * 100),
]

_SPEC_BY_KEY = {s[0]: s for s in PARAM_SPEC}


def _to_text(v):
    """Serialize a value for the TEXT `value` column. None -> NULL; bool -> 'true'/'false'."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def parse(value, ptype):
    """Parse the stored TEXT value back to a Python value per `type`. NULL/'' -> None for nullable."""
    if ptype == "display":
        return value
    if value is None or value == "":
        return None
    if ptype == "bool":
        return str(value).lower() in ("1", "true", "yes")
    if ptype in ("int",):
        return int(float(value))
    # usd|pct|x|float|nullable -> float (nullable already handled the empty case above)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def seed_params(db):
    """Insert any missing params from PARAM_SPEC (idempotent — never overwrites operator edits)."""
    stamp = now_iso()
    for key, category, level, ptype, effect, default in PARAM_SPEC:
        dv = _to_text(default)
        db.execute(
            "INSERT OR IGNORE INTO params (key,value,category,level,type,effect,default_value,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (key, dv, category, level, ptype, effect, dv, stamp))
    db.commit()


def get_all(db):
    """Return {scanner:[...], follow:[...]} with parsed values + metadata, in PARAM_SPEC order."""
    rows = {r["key"]: r for r in db.execute(
        "SELECT key,value,category,level,type,effect,default_value FROM params").fetchall()}
    out = {"scanner": [], "follow": []}
    for key, category, level, ptype, effect, default in PARAM_SPEC:
        r = rows.get(key)
        raw = r["value"] if r else _to_text(default)
        out[category].append({
            "key": key, "category": category, "level": level, "type": ptype, "effect": effect,
            "value": parse(raw, ptype),
            "default": parse(_to_text(default), ptype),
        })
    return out


def get(db, key, fallback=None):
    """Read one parsed param value (for Observer/Scanner once they switch to DB-backed params)."""
    spec = _SPEC_BY_KEY.get(key)
    ptype = spec[3] if spec else "float"
    row = db.execute("SELECT value FROM params WHERE key=?", (key,)).fetchone()
    if row is None:
        return fallback
    val = row[0] if not isinstance(row, dict) else row["value"]
    return parse(val, ptype)
