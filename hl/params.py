"""UI-tunable strategy parameters — single source of truth for the dashboard `params` table.

Two categories, two effect models (see doc/dashboard-landing-plan.md §4):
  scanner (effect=rescan)    -> Scanner reads at scan start; a change needs a rescan to take effect.
  follow  (effect=immediate) -> Observer reads at run time; a change applies to the next new copy.

VALUES ARE STORED IN UI-FACING UNITS (the API contract: percent fields carry the percent number,
e.g. 0.5 means 0.5%). The engines still think in fractions, so when Observer/Scanner switch to
reading from this table (M2/M4) they convert `pct` values /100. Defaults below mirror the running
code (hl/config.py + hl_discover.py argparse) so seeding never changes live behaviour.

level drives UI affordance: green=edit freely, yellow=edit w/ confirm, blue=dev-mode only,
black=read-only. type: usd|pct|x|int|float|nullable|bool|text|display.
"""
from . import config
from .util import now_iso

# (key, category, level, type, effect, default, name, desc) — default in UI-facing units; name = 中文显示名,
# desc = 一句话影响说明(UI 在参数后以灰色字体直接展示). level "hidden" = 底层参数,UI 不渲染(引擎仍读取).
# ONLY green/yellow render in the UI. Everything else is "hidden" — kept here so the engine wiring
# (apply_scanner_params / _reload_params) still resolves them, but the operator never sees them.
PARAM_SPEC = [
    # ── ① 采集 watchlist 参数 (effect = rescan) ──────────────────────────────────
    ("HARVEST_MIN_ACCT",     "scanner", "yellow", "usd",     "rescan", config.HARVEST_MIN_ACCT,
        "钱包最低资金", "账户资金 ≥ 此才纳入(过滤噪音小号)"),
    ("HARVEST_WEEK_VLM_MIN", "scanner", "yellow", "usd",     "rescan", config.HARVEST_WEEK_VLM_MIN,
        "周成交量下限", "近7天成交额 ≥ 此(太冷清/囤币号排除)"),
    ("HARVEST_WEEK_VLM_MAX", "scanner", "yellow", "usd",     "rescan", config.HARVEST_WEEK_VLM_MAX,
        "周成交量上限", "近7天成交额 ≤ 此(超过=做市/高频机器人,跟不上)"),
    ("HARVEST_PNL_VOL_MAX",  "scanner", "hidden", "pct",     "rescan", config.HARVEST_PNL_VOL_MAX * 100,
        "盈利/成交量上限(防幽灵号)", ""),   # v10: 藏 —— 冷门幽灵号预筛,和已 hidden 的下限对齐
    ("EXCLUDE_HFT",          "scanner", "green",  "bool",    "rescan", True,
        "排除高频交易", "过滤持仓数秒的高频/量化盘(延迟跟不上)"),
    ("inactive_days",        "scanner", "green",  "int",     "rescan", 1,
        "最长开仓静默天数", "最近一次可复制新开仓超过此天数，资格阶段直接排除；已有跟单持仓仍按Exit-only安全退出"),
    ("DAILY_SCAN_TIME_BUDGET_MIN", "scanner", "hidden", "int", "rescan", config.DAILY_SCAN_TIME_BUDGET_MIN,
        "每日扫描时间预算", ""),
    ("CORE_REFRESH_DEADLINE_MIN", "scanner", "hidden", "int", "rescan", config.CORE_REFRESH_DEADLINE_MIN,
        "核心钱包刷新期限", ""),
    ("SCAN_FINALIZE_RESERVE_MIN", "scanner", "hidden", "int", "rescan", config.SCAN_FINALIZE_RESERVE_MIN,
        "扫描收尾预留时间", ""),
    ("DAILY_PROFILE_BUDGET", "scanner", "hidden", "int", "rescan", config.DAILY_PROFILE_BUDGET,
        "每日画像预算", ""),
    ("CANDIDATE_MAX_RECHECK_DAYS", "scanner", "hidden", "int", "rescan", config.CANDIDATE_MAX_RECHECK_DAYS,
        "候选最长复核间隔", ""),
    ("FULL_REFRESH_SHARDS", "scanner", "hidden", "int", "rescan", config.FULL_REFRESH_SHARDS,
        "完整重拉分片数", ""),
    ("RANDOM_EXPLORATION_RATIO", "scanner", "hidden", "pct", "rescan", config.RANDOM_EXPLORATION_RATIO * 100,
        "随机探索比例", ""),
    # —— hidden 采集底层(细门槛/次要预筛,引擎读取,UI 不显示)——
    ("HARVEST_PNL_VOL_MIN",  "scanner", "hidden", "pct",     "rescan", config.HARVEST_PNL_VOL_MIN * 100, "盈利/成交量下限(防薄利MM)", ""),
    ("min_perp",             "scanner", "hidden", "pct",     "rescan", 60, "合约占比下限", ""),
    ("max_daily_eps",        "scanner", "hidden", "int",     "rescan", 30, "日交易次数上限", ""),
    ("min_activity",         "scanner", "hidden", "float",   "rescan", 0.21, "最低活跃度", ""),
    ("grid_max_adds",        "scanner", "hidden", "int",     "rescan", 3, "网格判定:中位加仓上限(超过=习惯性均摊,跟不动)", ""),
    ("max_single_adds",      "scanner", "hidden", "int",     "rescan", config.MAX_SINGLE_ADDS_PER_EP,
        "重DCA判定:单回合加仓上限(超过=偶发但不可复制的重仓摊价)", ""),
    ("HFT_MIN_HOLD_MIN",     "scanner", "hidden", "float",   "rescan", 3, "高频判定持仓分钟", ""),
    ("max_fills_per_ep",     "scanner", "hidden", "int",     "rescan", 50, "算法拆单判定:单回合成交笔数 p90 上限(看p90不看峰值——只惩罚系统性拆单,不误杀薄盘股偶发拆单)", ""),
    ("PORTFOLIO_MAX_TURNOVER","scanner", "blue",   "x",       "rescan", config.PORTFOLIO_MAX_TURNOVER,
        "换手率上限 (x/周)", "周成交量÷账户权益。超过=高频机器人(我们延迟跟不了+手续费拖累)。趋势客一般<40x,机器人>100x"),
    ("PORTFOLIO_MIN_EDGE_BPS","scanner", "blue",   "float",   "rescan", config.PORTFOLIO_MIN_EDGE_BPS,
        "边际下限 (bps)", "30天净利÷成交量×1e4。低于此≈利润挡不住我们~9bp手续费+滑点,跟了净亏。用月度窗口更稳"),
    ("COPY_MIN_EXPECTED_MARGIN_RETURN", "scanner", "blue", "pct", "rescan",
        config.COPY_MIN_EXPECTED_MARGIN_RETURN * 100,
        "Copy保证金收益下限", "回放扣除费用、滑点后按占用保证金归一并向零收缩；低于此值属于薄利，资格阶段直接排除"),
    # (MIN_PAYOFF removed v10 — the small_win_big_loss hard gate is gone; 盈亏比 now a smooth g_payoff factor in score)
    ("MAX_CONCURRENT_POS",   "scanner", "blue",   "int",     "rescan", config.MAX_CONCURRENT_POS,
        "峰值同时持仓上限", "目标峰值同时持仓 > 此 = 组合客,我们权益均额只能装~5-8个,只能随机抓一片跟不了 → 排除。全池p90=8,15卡在断层不误伤慢波段好钱包"),
    ("MIN_ACTIVE_SCORE",     "scanner", "blue",   "float",   "rescan", config.MIN_ACTIVE_SCORE,
        "入选质量线", "综合评分 < 此 → 不进 active。让 active(watchlist)=全是够优质的好钱包,跟单再从中按资金取前N。低质量尾巴在这一刀切掉"),
    ("EVIDENCE_MIN_DAYS",    "scanner", "blue",   "int",     "rescan", config.EVIDENCE_MIN_DAYS,
        "证据·最低独立天数", "Copy已平episode分布的独立交易日少于此值，资格阶段直接排除"),
    ("EVIDENCE_MIN_TRADES",  "scanner", "blue",   "int",     "rescan", config.EVIDENCE_MIN_TRADES,
        "证据·最低回合数", "30天Copy回放已平episode少于此值，资格阶段直接排除"),
    ("COPY_BT_GATE_ENABLE",  "scanner", "hidden", "bool",    "rescan", config.COPY_BT_GATE_ENABLE,
        "copy回测准入", "用历史 fills 按当前跟单规则回放;目标赚但我们复制亏的钱包不进 active"),
    ("COPY_BT_DAYS",         "scanner", "hidden", "int",     "rescan", config.COPY_BT_DAYS,
        "copy回测窗口", "copy 回测准入使用的天数"),
    ("COPY_BT_MIN_CLOSED",   "scanner", "hidden", "int",     "rescan", config.COPY_BT_MIN_CLOSED,
        "copy回测最低已平仓数", "低于此样本只记录,不作为硬闸"),
    ("COPY_BT_MIN_NET_PNL",  "scanner", "hidden", "usd",     "rescan", config.COPY_BT_MIN_NET_PNL,
        "copy回测最低净收益", "扣费后的 copy 回测净收益必须高于此值才可 active"),
    ("WINDFALL_CONC",        "scanner", "hidden", "pct",     "rescan", config.WINDFALL_CONC * 100,
        "单日利润集中度上限", "单日≥此比例毛利且胜率<下条=靠一笔偶然大赚撑着(亏损未覆盖),排除"),
    ("WINDFALL_WIN_MAX",     "scanner", "hidden", "pct",     "rescan", config.WINDFALL_WIN_MAX * 100,
        "windfall判定·胜率上限", "配合上条:高集中度+胜率低于此=一波流;真高胜率的集中不算(靠稳定胜率不靠一把)"),

    # ── ② 跟单策略参数 (effect = immediate) ────────────────────────────
    # Hidden migration compatibility only; production membership and UI use published Core exclusively.
    ("MIN_FOLLOW_SCORE",     "follow",  "hidden", "float",   "rescan", config.MIN_FOLLOW_SCORE,
        "旧版评分线", "已停用；仅保留迁移兼容"),
    ("FOLLOW_SELECTION_MODE", "follow", "hidden", "text", "immediate", config.FOLLOW_SELECTION_MODE,
        "跟单集合模式", "auto使用已发布Core集合;manual保留人工集合"),
    ("COIN_BLACKLIST",       "follow",  "green",  "text",    "immediate", config.COIN_BLACKLIST,
        "币种黑名单", "命中的币种不再新开仓;已有仓位仍继续跟随减仓/平仓。建议从持仓行一键加入,避免符号别名写错"),
    ("BLOCK_KOREAN_STOCKS",  "follow",  "green",  "bool",    "immediate", config.BLOCK_KOREAN_STOCKS,
        "屏蔽韩股相关标的", "预置屏蔽 EWY、KR200、Samsung(SMSN)、SK hynix(SKHX/SKHY)、Hyundai；已有仓位只减仓/平仓，不新增仓位"),
    ("LOW_LIQUIDITY_FILTER_ENABLE", "follow", "hidden", "bool", "immediate", config.LOW_LIQUIDITY_FILTER_ENABLE,
        "低流动性币过滤", "标准 crypto perp 低于24h成交量/OI名义额阈值时不新开仓"),
    ("MIN_COIN_DAY_NTL_VLM", "follow", "hidden", "usd", "immediate", config.MIN_COIN_DAY_NTL_VLM,
        "币种24h成交量下限", "crypto perp 24h名义成交量低于此值不新开仓"),
    ("MIN_COIN_OI_NOTIONAL", "follow", "hidden", "usd", "immediate", config.MIN_COIN_OI_NOTIONAL,
        "币种OI名义额下限", "crypto perp OI名义额低于此值不新开仓"),
    # (FOLLOW_MIN_TRADES / FOLLOW_MIN_ACTIVE_DAYS removed v10 — redundant with the scanner EVIDENCE gate,
    #  which already enforces a track record (active_days≥5 且 回合≥7) before a wallet can be active)
    # (RISK_BUDGET removed v10 — σ-scaled leverage dropped; leverage = the σ-tier's LEV CAP, redundant with
    #  tier cap + master-lev cap + margin/coin/deploy limits + σ-stop)
    ("AUTO_TUNE_MARGIN_ENABLE", "follow", "green", "bool", "immediate", config.AUTO_TUNE_MARGIN_ENABLE,
        "自动调保证金", "每日采集/重筛后按当前跟单钱包组合回测,小网格微调仓位上限/杠杆/满火力线与智能加仓k/g/硬顶;下限/单币上限/总上限/止损由人工控制"),
    ("AUTO_TUNE_MODE", "follow", "hidden", "text", "immediate", config.AUTO_TUNE_MODE,
        "自动调参模式", "Paper默认apply;仍须通过OOS、Holdout、压力、回撤与爆仓门槛"),
    ("AUTO_TUNE_APPLY_MIN_SHADOW_DAYS", "follow", "hidden", "int", "immediate",
        config.AUTO_TUNE_APPLY_MIN_SHADOW_DAYS,
        "调参最短影子天数", "真钱建议14;Paper完整闭环验证可设0"),
    ("AUTO_TUNE_APPLY_MIN_FORWARD_CLOSED", "follow", "hidden", "int", "immediate",
        config.AUTO_TUNE_APPLY_MIN_FORWARD_CLOSED,
        "调参最低Forward已平", "真钱建议100;Paper完整闭环验证可设0"),
    ("AUTO_TUNE_MIN_DIRECTION_STREAK", "follow", "hidden", "int", "immediate",
        config.AUTO_TUNE_MIN_DIRECTION_STREAK,
        "调参方向确认次数", "真钱建议2;Paper首次自动Bootstrap可设1"),
    ("AUTO_TUNE_MASTER_LEVERAGE_MIN_COVERAGE", "follow", "hidden", "pct", "immediate",
        config.AUTO_TUNE_MASTER_LEVERAGE_MIN_COVERAGE * 100,
        "主钱包杠杆覆盖门槛", "真钱建议80%;Paper验证可设0"),
    ("AUTO_TUNE_PRICE_PATH_MIN_COVERAGE", "follow", "hidden", "pct", "immediate",
        config.AUTO_TUNE_PRICE_PATH_MIN_COVERAGE * 100,
        "价格路径覆盖门槛", "真钱建议95%;Paper验证可设0"),
    ("DEPLOY_FULL_PCT",      "follow",  "yellow", "pct",     "immediate", config.DEPLOY_FULL_PCT * 100,
        "满火力占用线", "总保证金占用≤此比例时按保证金上限开新仓;超过后线性缩到下限"),
    ("STABLE_MARGIN_PCT",    "follow",  "yellow", "pct",     "immediate", config.STABLE_MARGIN_PCT * 100,
        "稳定档·保证金上限", "稳定档低占用时每单保证金上限,占权益%"),
    ("STABLE_MARGIN_MIN_PCT","follow",  "yellow", "pct",     "immediate", config.STABLE_MARGIN_MIN_PCT * 100,
        "稳定档·保证金下限", "稳定档拥挤时线性缩到此下限"),
    ("STABLE_LEV_CAP",       "follow",  "yellow", "x",       "immediate", config.STABLE_LEV_CAP,
        "稳定档·杠杆上限", "稳定档杠杆封顶"),
    ("STABLE_MIN_NOTIONAL",  "follow",  "yellow", "usd",     "immediate", config.STABLE_MIN_NOTIONAL,
        "稳定档·最低名义额", "稳定档(BTC/大饼)单笔名义额低于此(封顶到主力名义额后)就不开,太小没意义"),
    ("MID_MARGIN_PCT",       "follow",  "yellow", "pct",     "immediate", config.MID_MARGIN_PCT * 100,
        "中档·保证金上限", "中档低占用时每单保证金上限,占权益%"),
    ("MID_MARGIN_MIN_PCT",   "follow",  "yellow", "pct",     "immediate", config.MID_MARGIN_MIN_PCT * 100,
        "中档·保证金下限", "中档拥挤时线性缩到此下限"),
    ("MID_LEV_CAP",          "follow",  "yellow", "x",       "immediate", config.MID_LEV_CAP,
        "中档·杠杆上限", "中档杠杆封顶"),
    ("MID_MIN_NOTIONAL",     "follow",  "yellow", "usd",     "immediate", config.MID_MIN_NOTIONAL,
        "中档·最低名义额", "中档(ETH/SOL等)单笔名义额低于此就不开"),
    ("HIGH_MARGIN_PCT",      "follow",  "yellow", "pct",     "immediate", config.HIGH_MARGIN_PCT * 100,
        "剧烈档·保证金上限", "剧烈档低占用时每单保证金上限,占权益%"),
    ("HIGH_MARGIN_MIN_PCT",  "follow",  "yellow", "pct",     "immediate", config.HIGH_MARGIN_MIN_PCT * 100,
        "剧烈档·保证金下限", "剧烈档拥挤时线性缩到此下限"),
    ("HIGH_LEV_CAP",         "follow",  "yellow", "x",       "immediate", config.HIGH_LEV_CAP,
        "剧烈档·杠杆上限", "剧烈档杠杆封顶"),
    ("HIGH_MIN_NOTIONAL",    "follow",  "yellow", "usd",     "immediate", config.HIGH_MIN_NOTIONAL,
        "剧烈档·最低名义额", "剧烈档(meme/野币)单笔名义额低于此就不开(σ高、仓位本就小,门槛设低)"),
    # 分档最多加仓 —— 仅老模式(SMART_ADD 关)生效; 智能加仓走 σ波动闸+ADD_MAX_HARD. v10: 藏,避免占版面
    ("STABLE_MAX_ADDS",      "follow",  "hidden", "int",     "immediate", config.STABLE_MAX_ADDS, "稳定档·最多加仓(legacy)", ""),
    ("MID_MAX_ADDS",         "follow",  "hidden", "int",     "immediate", config.MID_MAX_ADDS, "中档·最多加仓(legacy)", ""),
    ("HIGH_MAX_ADDS",        "follow",  "hidden", "int",     "immediate", config.HIGH_MAX_ADDS, "剧烈档·最多加仓(legacy)", ""),
    ("ADD_FRAC",             "follow",  "yellow", "pct",     "immediate", config.ADD_FRAC * 100,
        "每次加仓比例", "每次加仓额 = 首开保证金 × 此%(50=首开一半)。BTC首开3%+3次加仓 → 满仓7.5%,不是叠成12%"),
    # ── 加仓策略引擎(B 逆向加仓)—— SMART_ADD 开=智能动态,关=老分档硬cap ──
    ("FOLLOW_POS_ADD",       "follow",  "green",  "bool",    "immediate", config.FOLLOW_POS_ADD,
        "A·跟随正向加仓", "目标顺势加仓(拉高成本追盈利)时是否跟。开=还要过顺势波动闸,避免小碎单全跟"),
    ("SMART_ADD",            "follow",  "green",  "bool",    "immediate", config.ADD_STRATEGY == "smart",
        "B·智能动态加仓", "开=σ波动闸+比例镜像+三档预算(推荐);关=老的分档次数硬cap"),
    ("ADD_GAP_K",            "follow",  "yellow", "float",   "immediate", config.ADD_GAP_K,
        "逆向波动闸k", "只有目标加仓相对我们上次加仓价 逆向移动 ≥ k×该币σ 才跟(调大→更少更精的摊价)"),
    ("POS_ADD_GAP_K",        "follow",  "yellow", "float",   "immediate", config.POS_ADD_GAP_K,
        "顺势波动闸k", "FOLLOW_POS_ADD 开时,顺势加仓也必须相对上次跟单价移动 ≥ k×该币σ 才跟,用于合并小碎追单"),
    ("ADD_GAP_SHRINK_G",     "follow",  "yellow", "float",   "immediate", config.ADD_GAP_SHRINK_G,
        "加仓收缩因子g", "每跟一次加仓,波动闸门槛×此(>1 逐步收紧,加仓次数自然收口)"),
    ("ADD_MAX_HARD",         "follow",  "yellow", "int",     "immediate", config.ADD_MAX_HARD,
        "智能加仓硬顶", "智能模式最多加几次(兜底;通常单币预算先触顶)"),
    ("STABLE_COIN_CAP_PCT",  "follow",  "yellow", "pct",     "immediate", config.STABLE_COIN_CAP_PCT * 100,
        "稳定档·单币上限", "稳定档单币所有仓位保证金合计上限(占账户%)—— 加仓的总预算天花板"),
    ("MID_COIN_CAP_PCT",     "follow",  "yellow", "pct",     "immediate", config.MID_COIN_CAP_PCT * 100,
        "中档·单币上限", "中档(ETH/SOL等)单币保证金上限(占账户%)"),
    ("HIGH_COIN_CAP_PCT",    "follow",  "yellow", "pct",     "immediate", config.HIGH_COIN_CAP_PCT * 100,
        "剧烈档·单币上限", "剧烈档(meme/野币/高波股)单币保证金上限——波动大,绝不给到稳定档那么高"),
    ("COPY_STOP_ENABLE",     "follow",  "green",  "bool",    "immediate", config.COPY_STOP_ENABLE,
        "启用止损", "亏损达到止损保证金比例时自动平仓；回测显示开启后收益更低，因此默认关闭"),
    ("STOP_MARGIN_PCT",      "follow",  "yellow", "pct",     "immediate", config.STOP_MARGIN_PCT * 100,
        "止损=亏损保证金%", "亏掉本仓这么多%保证金就平仓(70=亏到70%保证金,爆仓前兜底)。带杠杆自动换算逆向价格:5x→14%、3x→23%、7x→10%"),
    # (COIN_MARGIN_CAP_PCT removed 2026-07-02 — superseded by the σ-tiered 分档单笔上限 in the 加仓策略 tab)
    # —— hidden 跟单底层(sizing/执行细节,引擎读取,UI 不显示)——
    ("STABLE_SIGMA_MAX",     "follow",  "hidden", "pct",     "immediate", config.STABLE_SIGMA_MAX * 100, "BTC稳定档σ上界", ""),
    ("HIGH_SIGMA_MIN",       "follow",  "hidden", "pct",     "immediate", config.HIGH_SIGMA_MIN * 100, "剧烈档σ下界", ""),
    ("MAX_LEV",              "follow",  "hidden", "x",       "immediate", config.MAX_LEV, "杠杆硬上限", ""),
    ("MIN_LEV",              "follow",  "hidden", "x",       "immediate", config.MIN_LEV, "杠杆硬下限", ""),
    ("STOCK_MAX_LEV",        "follow",  "yellow", "x",       "immediate", config.STOCK_MAX_LEV,
        "股票杠杆上限", "股票/大宗 perp(xyz:*)的硬性杠杆上限,不管 σ 落哪档、目标用多少倍。股票会跳空,平静的 σ 低估尾部风险,必须按品类一刀切"),
    ("MAX_DEPLOY_PCT",       "follow",  "yellow", "pct",     "immediate", config.MAX_DEPLOY_PCT * 100,
        "组合部署上限", "总占用保证金到此比例就停开新仓,留下(100-此)%干火药给加仓+新信号+缓冲。加仓可动用这部分储备。防权益开单一路铺满"),
    ("MIN_OPEN_MARGIN_PCT",  "follow",  "hidden", "pct",     "immediate", config.MIN_OPEN_MARGIN_PCT * 100, "单笔最小开仓额", ""),
    ("MAX_ENTRY_CHASE_PCT",  "follow",  "hidden", "nullable","immediate",
        (config.MAX_ENTRY_CHASE_PCT * 100) if config.MAX_ENTRY_CHASE_PCT is not None else None, "追价保护阈值", ""),
    ("VOL_FAST_DAYS",        "follow",  "hidden", "display", "immediate",
        f"{config.VOL_FAST_DAYS} / {config.VOL_SLOW_DAYS} 天", "波动率快/慢窗口", ""),
    ("VOL_FALLBACK_SIGMA",   "follow",  "hidden", "pct",     "immediate", config.VOL_FALLBACK_SIGMA * 100, "默认波动率", ""),
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
    if ptype == "text":
        return "" if value is None else str(value)
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
    """Insert missing params and refresh metadata without overwriting operator-edited values."""
    stamp = now_iso()
    for key, category, level, ptype, effect, default, name, desc in PARAM_SPEC:
        dv = _to_text(default)
        db.execute(
            "INSERT OR IGNORE INTO params (key,value,category,level,type,effect,default_value,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (key, dv, category, level, ptype, effect, dv, stamp))
        db.execute(
            "UPDATE params SET category=?,level=?,type=?,effect=?,default_value=? WHERE key=?",
            (category, level, ptype, effect, dv, key))
    db.commit()


def reset_defaults(db, category=None, *, commit=True):
    """恢复默认配置: FORCE-overwrite params back to PARAM_SPEC (= config.py) defaults.

    Unlike seed_params (INSERT OR IGNORE, which protects operator edits), this OVERWRITES the live
    value with the current code default — the dashboard/launcher "恢复默认" op. category None = all;
    "follow"/"scanner" = just that tab. Also refreshes default_value so the column tracks config.
    Returns the number of params reset. Only touches rows that already exist (seed first on a fresh DB).
    """
    stamp = now_iso()
    n = 0
    for key, cat, level, ptype, effect, default, name, desc in PARAM_SPEC:
        if category and cat != category:
            continue
        dv = _to_text(default)
        cur = db.execute("UPDATE params SET value=?,default_value=?,updated_at=? WHERE key=?",
                         (dv, dv, stamp, key))
        n += cur.rowcount
    if commit:
        db.commit()
    return n


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


def restore_follow_snapshot(db, values, *, keys=None, expected_current=None, stamp=None):
    """Restore an engine-unit follow snapshot without committing.

    Used only by the immutable strategy fail-closed path.  Percent/nullable
    values are converted back to the UI-facing units stored in ``params``.
    """
    stamp = stamp or now_iso()
    allowed = set(keys) if keys is not None else None
    current = load_follow(db) if expected_current else {}
    restored = 0
    for spec in PARAM_SPEC:
        key, category, _level, ptype = spec[:4]
        if (category != "follow" or ptype == "display" or key not in values
                or (allowed is not None and key not in allowed)):
            continue
        if key in (expected_current or {}):
            actual = current.get(key)
            expected = expected_current[key]
            if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
                if abs(float(actual) - float(expected)) > 1e-12:
                    continue
            elif actual != expected:
                continue
        value = values[key]
        if value is not None and ptype in ("pct", "nullable"):
            value = float(value) * 100.0
        cur = db.execute(
            "UPDATE params SET value=?,updated_at=? WHERE key=?",
            (_to_text(value), stamp, key),
        )
        restored += int(cur.rowcount or 0)
    return restored


# DB scanner-param key -> the scan args-namespace attribute the scanner/metrics actually read.
SCANNER_ARG_MAP = {
    "HARVEST_MIN_ACCT": "min_acct",
    "HARVEST_WEEK_VLM_MIN": "week_vlm_min", "HARVEST_WEEK_VLM_MAX": "week_vlm_max",
    "HARVEST_PNL_VOL_MIN": "pnl_vol_min", "HARVEST_PNL_VOL_MAX": "pnl_vol_max",
    "min_perp": "min_perp", "inactive_days": "inactive_days", "max_daily_eps": "max_daily_eps",
    "min_activity": "min_activity", "grid_max_adds": "grid_max_adds",
    "max_single_adds": "max_single_adds",
    "EXCLUDE_HFT": "exclude_hft", "HFT_MIN_HOLD_MIN": "hft_min_hold_min",
    "max_fills_per_ep": "max_fills_per_ep",
    "PORTFOLIO_MAX_TURNOVER": "portfolio_max_turnover", "PORTFOLIO_MIN_EDGE_BPS": "portfolio_min_edge_bps",
    "COPY_MIN_EXPECTED_MARGIN_RETURN": "copy_min_expected_margin_return",
    "COPY_BT_GATE_ENABLE": "copy_bt_gate_enable", "COPY_BT_DAYS": "copy_bt_days",
    "COPY_BT_MIN_CLOSED": "copy_bt_min_closed", "COPY_BT_MIN_NET_PNL": "copy_bt_min_net_pnl",
    "WINDFALL_CONC": "windfall_conc", "WINDFALL_WIN_MAX": "windfall_win_max",
    "MAX_CONCURRENT_POS": "max_concurrent_pos", "MIN_ACTIVE_SCORE": "min_active_score",
    "EVIDENCE_MIN_DAYS": "evidence_min_days", "EVIDENCE_MIN_TRADES": "evidence_min_trades",
    "DAILY_SCAN_TIME_BUDGET_MIN": "daily_scan_time_budget_min",
    "CORE_REFRESH_DEADLINE_MIN": "core_refresh_deadline_min",
    "SCAN_FINALIZE_RESERVE_MIN": "scan_finalize_reserve_min",
    "DAILY_PROFILE_BUDGET": "daily_profile_budget",
    "CANDIDATE_MAX_RECHECK_DAYS": "candidate_max_recheck_days",
    "FULL_REFRESH_SHARDS": "full_refresh_shards",
    "RANDOM_EXPLORATION_RATIO": "random_exploration_ratio",
}


def apply_scanner_params(db, ns):
    """Overlay DB scanner params (engine units) onto a scan args namespace so a scan uses UI-tuned gates."""
    vals = load_category(db, "scanner")
    for key, attr in SCANNER_ARG_MAP.items():
        if vals.get(key) is not None:
            setattr(ns, attr, vals[key])
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
