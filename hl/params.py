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

# (key, category, level, type, effect, default, name, desc) — default in UI-facing units; name = 中文显示名,
# desc = 一句话影响说明(UI 在参数后以灰色字体直接展示). level "hidden" = 底层参数,UI 不渲染(引擎仍读取).
# ONLY green/yellow render in the UI (the 12 high-impact). Everything else is "hidden" — kept here so the
# engine wiring (apply_scanner_params / _reload_params) still resolves them, but the operator never sees them.
PARAM_SPEC = [
    # ── ① 采集 watchlist 参数 (effect = rescan) ──────────────────────────────────
    ("HARVEST_MIN_ACCT",     "scanner", "yellow", "usd",     "rescan", config.HARVEST_MIN_ACCT,
        "钱包最低资金", "只看账户资金≥此的钱包(过滤噪音小号)。调高=只跟大资金、候选更少;调低=纳入小资金、候选更多更杂"),
    ("HARVEST_MON_ROI_MIN",  "scanner", "yellow", "pct",     "rescan", config.HARVEST_MON_ROI_MIN * 100,
        "近30天最低收益", "近30天收益≥此才入选。调高=只要赚得多的、候选少;调低=放进收益平平的"),
    ("max_single_loss",      "scanner", "yellow", "pct",     "rescan", 10,
        "单笔最大亏损容忍", "钱包历史单笔亏损超过权益此比例=扛单到爆,淘汰。调高=容忍止损不干脆的;调低=只留止损极严的"),
    ("EXCLUDE_HFT",          "scanner", "green",  "bool",    "rescan", True,
        "排除高频交易", "过滤持仓数秒的高频/量化盘(我们延迟跟不上)。开=只留人能跟的;关=高频也进候选"),
    ("inactive_days",        "scanner", "green",  "int",     "rescan", 3,
        "最长不活跃天数", "超过此天数没成交、且手上也没持仓=失活淘汰(拿着仓位的趋势手不算失活)。调高=容忍更久没动的;调低=只留近期活跃的"),
    ("DISP_PENALTY_K",       "scanner", "yellow", "float",   "rescan", config.DISP_PENALTY_K,
        "扛单降权强度", "直接量'不止损'行为打分降权:① 当前扛着的浮亏单(几笔×多深×多久)② 历史被强平次数 ③ 小赚大亏(很少认亏、却一笔亏吃掉很多笔赢)。跟胜率无关——止损干脆的高胜率钱包不受影响。调高=扛单/小赚大亏的更易跌出跟单线、候选更干净;调低/0=不降权"),
    # —— hidden 采集底层(评分形状/细门槛/次要预筛,引擎读取,UI 不显示)——
    ("HARVEST_MAX_TURNOVER", "scanner", "hidden", "x",       "rescan", config.HARVEST_MAX_TURNOVER, "日换手上限", ""),
    ("HARVEST_WEEK_VLM_MIN", "scanner", "hidden", "usd",     "rescan", config.HARVEST_WEEK_VLM_MIN, "7天成交量下限", ""),
    ("HARVEST_MON_ROI_MAX",  "scanner", "hidden", "pct",     "rescan", config.HARVEST_MON_ROI_MAX * 100, "30天收益上限", ""),
    ("HARVEST_WEEK_ROI_MIN", "scanner", "hidden", "pct",     "rescan", config.HARVEST_WEEK_ROI_MIN * 100, "7天收益下限", ""),
    ("min_perp",             "scanner", "hidden", "pct",     "rescan", 60, "合约占比下限", ""),
    ("max_daily_eps",        "scanner", "hidden", "int",     "rescan", 30, "日交易次数上限", ""),
    ("min_activity",         "scanner", "hidden", "float",   "rescan", 0.21, "最低活跃度", ""),
    ("grid_max_adds",        "scanner", "hidden", "int",     "rescan", 5, "单笔加仓上限(防网格)", ""),
    ("HFT_MIN_HOLD_MIN",     "scanner", "hidden", "float",   "rescan", 3, "高频判定持仓分钟", ""),
    ("SCORE_SHRINK_K",       "scanner", "hidden", "int",     "rescan", int(config.SCORE_SHRINK_K), "样本不足惩罚强度", ""),
    ("SCORE_RAR_CAP",        "scanner", "hidden", "float",   "rescan", config.SCORE_RAR_CAP, "收益评分上限", ""),
    ("SCORE_K",              "scanner", "hidden", "int",     "rescan", int(config.SCORE_K), "评分置信度", ""),
    ("SCORE_GAMMA",          "scanner", "hidden", "float",   "rescan", config.SCORE_GAMMA, "稳定性严格度", ""),
    ("UW_TOL",               "scanner", "hidden", "display", "rescan", "2% / 10%", "浮亏容忍/危险线", ""),

    # ── ② 跟单策略参数 (effect = immediate) ────────────────────────────
    ("MIN_FOLLOW_SCORE",     "follow",  "green",  "float",   "immediate", config.MIN_FOLLOW_SCORE,
        "跟单评分线", "只跟评分≥此的钱包。调高=只跟最强的、少而精;调低=跟更多、纳入次一档、质量略降"),
    ("MIN_TIMES_ACTIVE",     "follow",  "green",  "int",     "immediate", 2,
        "钱包验证轮数", "只跟连续通过≥N轮采集验证的钱包(1=关闭)。调高=只跟久经考验的、更稳更少;调低=新发现的也跟"),
    ("MAX_TARGETS",          "follow",  "green",  "int",     "immediate", config.MAX_TARGETS,
        "最多跟单钱包数", "同时最多跟几个钱包。调高=更分散但每个轮询更慢;调低=更集中"),
    ("LEV_LOWVOL_X",         "follow",  "yellow", "x",       "immediate", config.LEV_LOWVOL_X,
        "稳定币最高杠杆", "BTC这类低波动币的杠杆上限(全场最高档)。调高=BTC/指数/蓝筹放大收益、但爆仓线更近;调低=全场更保守"),
    ("LEV_HIGHVOL_X",        "follow",  "yellow", "x",       "immediate", config.LEV_HIGHVOL_X,
        "Meme最高杠杆", "最颠的meme/山寨的杠杆上限(全场最低档)。调高=颠的币也上杠杆、易被插针扫爆;调低=颠的币压更死、更安全但仓位更小"),
    ("RF_MAX",               "follow",  "green",  "pct",     "immediate", config.RF_MAX * 100,
        "单仓最大投入", "目标重仓时,每笔最多投入多少(占可用余额)。调高=跟得更重、赚亏放大;调低=更稳"),
    ("CONVICTION_DIVISOR",   "follow",  "yellow", "float",   "immediate", config.CONVICTION_DIVISOR,
        "信心折算(全仓→逐仓)", "目标'押了自己账户百分之几'要先除以此值再映射成我们的下注比例。因为他们全仓(整个账户兜底)、我们逐仓(这笔保证金就是全部风险)。调高=把他们的下注当得更轻、我们跟得更小更稳;调低(趋近1)=越接近1:1照搬他们的仓位比例"),
    ("MAX_ADDS",             "follow",  "yellow", "int",     "immediate", config.MAX_ADDS,
        "最多加仓次数", "一笔最多跟几次加仓(防被网格拖死)。调高=跟更多加仓、单仓变重;调低=更克制"),
    ("COPY_STOP_PCT",        "follow",  "yellow", "pct",     "immediate", config.COPY_STOP_PCT * 100,
        "止损线(逆向幅度)", "价格逆向跑这么多就提前平仓,不陪目标死扛(逐仓兜底)。3x下18%价格≈亏54%保证金。调低=砍得更早、少扛但会误杀慢回本的赢单;调高=给更多回旋、接近不止损(设很大≈关闭)"),
    ("COIN_MARGIN_CAP_PCT",  "follow",  "green",  "pct",     "immediate", config.COIN_MARGIN_CAP_PCT * 100,
        "单币最大占用", "同一个币上所有仓位的保证金合计上限(占账户)。防止一波行情下 N 个钱包都开同一个币、我们全跟导致过度集中。满了就缩小或不跟。调低=更分散、单币风险更小;调高=允许在单个币上压更重"),
    # —— hidden 跟单底层(sizing/执行细节,引擎读取,UI 不显示)——
    ("RISK_K",               "follow",  "hidden", "float",   "immediate", config.RISK_K, "保证金倍数", ""),
    ("RF_MIN",               "follow",  "hidden", "pct",     "immediate", config.RF_MIN * 100, "单仓最小投入", ""),
    ("MAX_LEV",              "follow",  "hidden", "x",       "immediate", config.MAX_LEV, "杠杆硬上限", ""),
    ("MIN_LEV",              "follow",  "hidden", "x",       "immediate", config.MIN_LEV, "杠杆硬下限", ""),
    ("MIN_OPEN_MARGIN_PCT",  "follow",  "hidden", "pct",     "immediate", config.MIN_OPEN_MARGIN_PCT * 100, "单笔最小开仓额", ""),
    ("ADD_MARGIN_PCT",       "follow",  "hidden", "pct",     "immediate", config.ADD_MARGIN_PCT * 100, "每次加仓比例", ""),
    ("MAX_ENTRY_CHASE_PCT",  "follow",  "hidden", "nullable","immediate",
        (config.MAX_ENTRY_CHASE_PCT * 100) if config.MAX_ENTRY_CHASE_PCT is not None else None, "追价保护阈值", ""),
    ("EXEC_MAKER_MIRROR",    "follow",  "hidden", "bool",    "immediate", config.EXEC_MAKER_MIRROR, "镜像挂单(未就绪)", ""),
    ("VOL_FAST_DAYS",        "follow",  "hidden", "display", "immediate",
        f"{config.VOL_FAST_DAYS} / {config.VOL_SLOW_DAYS} 天", "波动率快/慢窗口", ""),
    ("VOL_FALLBACK_SIGMA",   "follow",  "hidden", "pct",     "immediate", config.VOL_FALLBACK_SIGMA * 100, "默认波动率", ""),
    ("COPY_STOP_ENABLE",     "follow",  "hidden", "bool",    "immediate", config.COPY_STOP_ENABLE, "扛单止损开关", ""),
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
    for key, category, level, ptype, effect, default, name, desc in PARAM_SPEC:
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
    for key, category, level, ptype, effect, default, name, desc in PARAM_SPEC:
        if level == "hidden":           # 底层参数:引擎仍读取,但不暴露给 UI
            continue
        r = rows.get(key)
        raw = r["value"] if r else _to_text(default)
        out[category].append({
            "key": key, "name": name, "desc": desc,         # 中文名 + 灰字影响说明(UI 直接展示)
            "category": category, "level": level, "type": ptype, "effect": effect,
            "value": parse(raw, ptype),
            "default": parse(_to_text(default), ptype),
        })
    return out


# ── engine-side reads: convert the UI-facing stored value back to engine units ──
# pct/nullable are stored as UI percent (0.5 == 0.5%) -> engine wants the fraction (÷100). Everything
# else is stored in engine units already (incl MIN_FOLLOW_SCORE, stored NATIVE; the API does the 0–100
# display conversion only at its boundary). So the rule is purely type-based.
def _engine_val(spec, raw):
    ptype = spec[3]
    v = parse(raw, ptype)
    if v is None:
        return None
    if ptype in ("pct", "nullable"):
        return v / 100.0
    return v


def load_category(db, category):
    """{KEY: engine-unit value} for a category, falling back to the seed default per key. Skips display."""
    rows = {r[0]: r[1] for r in db.execute(
        "SELECT key,value FROM params WHERE category=?", (category,)).fetchall()}
    out = {}
    for s in PARAM_SPEC:
        if s[1] != category or s[3] == "display":
            continue
        key = s[0]
        raw = rows[key] if key in rows else _to_text(s[5])      # s[5] = default (UI units)
        try:
            out[key] = _engine_val(s, raw)
        except Exception:  # noqa: BLE001 — a bad stored value must not break the engine
            out[key] = _engine_val(s, _to_text(s[5]))
    return out


def load_follow(db):
    return load_category(db, "follow")


# DB scanner-param key -> the scan args-namespace attribute the scanner/metrics actually read.
SCANNER_ARG_MAP = {
    "HARVEST_MIN_ACCT": "min_acct", "HARVEST_MAX_TURNOVER": "max_turnover",
    "HARVEST_WEEK_VLM_MIN": "week_vlm_min", "HARVEST_MON_ROI_MIN": "mon_roi_min",
    "HARVEST_MON_ROI_MAX": "mon_roi_max", "HARVEST_WEEK_ROI_MIN": "week_roi_min",
    "min_perp": "min_perp", "inactive_days": "inactive_days", "max_daily_eps": "max_daily_eps",
    "min_activity": "min_activity", "grid_max_adds": "grid_max_adds", "max_single_loss": "max_single_loss",
    "EXCLUDE_HFT": "exclude_hft", "HFT_MIN_HOLD_MIN": "hft_min_hold_min",
}


def apply_scanner_params(db, ns):
    """Overlay DB scanner params (engine units) onto a scan args namespace so a scan uses UI-tuned gates.
    (SCORE_*/UW_* shape constants stay in config for now — advanced/blue, rarely tuned.)"""
    vals = load_category(db, "scanner")
    for key, attr in SCANNER_ARG_MAP.items():
        if vals.get(key) is not None:
            setattr(ns, attr, vals[key])
    # score-shape constants live in config (score() reads them directly, not via ns) — push the few
    # UI-tunable ones onto config so a scan/regate honors the dashboard value.
    if vals.get("DISP_PENALTY_K") is not None:
        config.DISP_PENALTY_K = vals["DISP_PENALTY_K"]
    return ns


def get(db, key, fallback=None):
    """Read one parsed param value (for Observer/Scanner once they switch to DB-backed params)."""
    spec = _SPEC_BY_KEY.get(key)
    ptype = spec[3] if spec else "float"
    row = db.execute("SELECT value FROM params WHERE key=?", (key,)).fetchone()
    if row is None:
        return fallback
    val = row[0] if not isinstance(row, dict) else row["value"]
    return parse(val, ptype)
