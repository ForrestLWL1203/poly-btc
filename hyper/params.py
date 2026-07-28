"""UI-tunable strategy parameters — single source of truth for the dashboard `params` table.

Two categories, two effect models (see docs/dashboard-landing-plan.md §4):
  scanner (effect=rescan)    -> Scanner reads at scan start; a change needs a rescan to take effect.
  follow  (effect=immediate) -> Observer reads at run time; a change applies to the next new copy.

VALUES ARE STORED IN UI-FACING UNITS (the API contract: percent fields carry the percent number,
e.g. 0.5 means 0.5%). The engines still think in fractions, so when Observer/Scanner switch to
reading from this table (M2/M4) they convert `pct` values /100. Defaults below mirror the running
code (hyper/config.py + hyper/cli/discover.py argparse) so seeding never changes live behaviour.

level drives UI affordance: green=edit freely, yellow=edit w/ confirm, blue=dev-mode only,
black=read-only. type: usd|pct|x|int|float|nullable|bool|text|display.
"""
from . import config
from .util import now_iso

# (key, category, level, type, effect, default, name, desc) — default in UI-facing units; name = 中文显示名,
# desc = 一句话影响说明(UI 在参数后以灰色字体直接展示). level "hidden" = 底层参数,UI 不渲染(引擎仍读取).
# Non-hidden rows render in the UI; black/display rows are read-only. Hidden rows remain here so engine
# wiring (apply_scanner_params / _reload_params) still resolves them without adding operator clutter.
PARAM_SPEC = [
    # ── ① 采集 watchlist 参数 (effect = rescan) ──────────────────────────────────
    ("HARVEST_MIN_ACCT",     "scanner", "yellow", "usd",     "rescan", config.HARVEST_MIN_ACCT,
        "钱包最低资金", "账户资金 ≥ 此才纳入(过滤噪音小号)"),
    ("HARVEST_WEEK_VLM_MIN", "scanner", "yellow", "usd",     "rescan", config.HARVEST_WEEK_VLM_MIN,
        "周成交量下限", "近7天成交额 ≥ 此(太冷清/囤币号排除)"),
    ("HARVEST_WEEK_PNL_MIN", "scanner", "hidden", "usd", "rescan", config.HARVEST_WEEK_PNL_MIN,
        "新钱包近7日 PnL 下限", "默认0表示必须盈利；不按绝对利润偏向大账户"),
    ("HARVEST_MONTH_PNL_MIN", "scanner", "hidden", "usd", "rescan", config.HARVEST_MONTH_PNL_MIN,
        "新钱包近30日 PnL 下限", "默认0表示必须盈利；Core/Challenger和持仓钱包始终进入保留回放"),
    ("HARVEST_ALL_PNL_MIN", "scanner", "hidden", "usd", "rescan", config.HARVEST_ALL_PNL_MIN,
        "历史绝对 PnL 参考线", "仅作审计，不参与候选硬筛"),
    ("HARVEST_PERP_PNL_SHARE_MIN", "scanner", "hidden", "pct", "rescan",
        config.HARVEST_PERP_PNL_SHARE_MIN * 100, "Perp盈利占比下限",
        "只检查30日Perp盈利占比；7日和历史窗口仅记录审计，不参与淘汰"),
    ("EXCLUDE_HFT",          "scanner", "green",  "bool",    "rescan", True,
        "排除高频交易", "过滤持仓数秒的高频/量化盘(延迟跟不上)"),
    ("inactive_days",        "scanner", "green",  "int",     "rescan", config.INACTIVE_DAYS,
        "最长开仓静默天数", "最近一次可复制新开仓超过此天数，资格阶段直接排除；已有跟单持仓仍按Exit-only安全退出"),
    ("DAILY_SCAN_TIME_BUDGET_MIN", "scanner", "hidden", "int", "rescan", config.DAILY_SCAN_TIME_BUDGET_MIN,
        "每日扫描时间预算", ""),
    ("CORE_REFRESH_DEADLINE_MIN", "scanner", "hidden", "int", "rescan", config.CORE_REFRESH_DEADLINE_MIN,
        "核心钱包刷新期限", ""),
    ("SCAN_FINALIZE_RESERVE_MIN", "scanner", "hidden", "int", "rescan", config.SCAN_FINALIZE_RESERVE_MIN,
        "扫描收尾预留时间", ""),
    ("CORE_INITIAL_MAX_N", "scanner", "green", "int", "rescan", config.CORE_INITIAL_MAX_N,
        "Core容量上限", "仅限制最多可发布多少个Core；不是目标数量，系统不会为了接近上限而补位"),
    ("CORE_REBALANCE_INTERVAL_DAYS", "scanner", "hidden", "int", "rescan",
        config.CORE_REBALANCE_INTERVAL_DAYS, "显式调参节流周期", "只供维护命令使用；开启自动调参后，每个新代际均强制重新调参"),
    # —— hidden 采集底层(细门槛/次要预筛,引擎读取,UI 不显示)——
    ("min_perp",             "scanner", "hidden", "pct",     "rescan", 60, "合约占比下限", ""),
    ("max_daily_eps",        "scanner", "hidden", "int",     "rescan", 30, "日交易次数上限", ""),
    ("grid_max_adds",        "scanner", "hidden", "int",     "rescan", 3, "网格判定:中位加仓上限(超过=习惯性均摊,跟不动)", ""),
    ("max_single_adds",      "scanner", "hidden", "int",     "rescan", config.MAX_SINGLE_ADDS_PER_EP,
        "重DCA判定:单回合加仓上限(超过=偶发但不可复制的重仓摊价)", ""),
    ("HFT_MIN_HOLD_MIN",     "scanner", "hidden", "float",   "rescan", 3, "高频判定持仓分钟", ""),
    ("max_fills_per_ep",     "scanner", "hidden", "int",     "rescan", 50, "算法执行判定:单回合不同OID数p90上限；同一OID的成交分片不重复计数", ""),
    ("MAX_CONCURRENT_POS",   "scanner", "blue",   "int",     "rescan", config.MAX_CONCURRENT_POS,
        "峰值同时持仓上限", "目标峰值同时持仓 > 此 = 组合客,我们权益均额只能装~5-8个,只能随机抓一片跟不了 → 排除。全池p90=8,15卡在断层不误伤慢波段好钱包"),
    ("SOURCE_QUALITY_MAX_N", "scanner", "hidden", "int", "rescan",
        config.SOURCE_QUALITY_MAX_N, "源质量池上限", "深采源钱包按质量预评分最多保留40个"),
    ("SOURCE_MIN_EPISODES_30D", "scanner", "black", "int", "rescan",
        config.SOURCE_MIN_EPISODES_30D, "源钱包30日最低完整回合", "边界不完整回合不计入样本或胜率"),
    ("SOURCE_MIN_EPISODE_WIN_RATE", "scanner", "black", "pct", "rescan",
        config.SOURCE_MIN_EPISODE_WIN_RATE * 100, "源钱包30日最低胜率", "手续费后完整Episode胜率至少70%"),
    ("SOURCE_LOW_FREQ_MIN_EPISODES_30D", "scanner", "black", "int", "rescan",
        config.SOURCE_LOW_FREQ_MIN_EPISODES_30D, "强力低频最低完整回合", "仅7–9个完整Episode时启用强力低频通道"),
    ("SOURCE_LOW_FREQ_MAX_EPISODES_30D", "scanner", "black", "int", "rescan",
        config.SOURCE_LOW_FREQ_MAX_EPISODES_30D, "强力低频最高完整回合", "达到10个Episode后使用标准源质量通道"),
    ("SOURCE_LOW_FREQ_MIN_EPISODE_WIN_RATE", "scanner", "black", "pct", "rescan",
        config.SOURCE_LOW_FREQ_MIN_EPISODE_WIN_RATE * 100, "强力低频最低胜率", "低频样本必须达到85%手续费后胜率"),
    ("SOURCE_LOW_FREQ_MIN_OFFICIAL_RETURN", "scanner", "black", "pct", "rescan",
        config.SOURCE_LOW_FREQ_MIN_OFFICIAL_RETURN * 100, "强力低频官方收益", "低频样本要求官方Perp资格窗口收益至少30%"),
    ("SOURCE_TOP3_CONCENTRATION_TRIGGER", "scanner", "hidden", "pct", "rescan",
        config.SOURCE_TOP3_CONCENTRATION_TRIGGER * 100, "前三大赢家集中检查线", "低于70%不触发主体检查"),
    ("SOURCE_BODY_MIN_WIN_RATE", "scanner", "hidden", "pct", "rescan",
        config.SOURCE_BODY_MIN_WIN_RATE * 100, "高集中钱包主体最低胜率", "仅当前三大赢家占毛盈利至少70%时启用"),
    ("COPY_BT_GATE_ENABLE",  "scanner", "hidden", "bool",    "rescan", config.COPY_BT_GATE_ENABLE,
        "copy回测准入", "用历史 fills 按当前跟单规则回放;目标赚但我们复制亏的钱包不进 active"),
    ("COPY_BT_DAYS",         "scanner", "hidden", "int",     "rescan", config.COPY_BT_DAYS,
        "copy回测窗口", "copy 回测准入使用的天数"),
    ("ROUGH_COPY_MIN_CLOSED_30D", "scanner", "black", "int", "rescan",
        config.ROUGH_COPY_MIN_CLOSED_30D, "粗略Copy最低完整回合", "样本不足标记证据不足，不伪装为经济失败"),
    ("ROUGH_COPY_MIN_WIN_RATE", "scanner", "black", "pct", "rescan",
        config.ROUGH_COPY_MIN_WIN_RATE * 100, "粗略Copy最低胜率", "Copy完整Episode胜率至少60%"),
    ("CORE_MIN_DYNAMIC_COPY_RETURN_30D", "scanner", "black", "pct", "rescan",
        config.CORE_MIN_DYNAMIC_COPY_RETURN_30D * 100, "Core动态Copy 30日收益",
        "连续回放全程按浮动权益复利；期末相对30日起始浮动权益至少增长10%"),
    ("CORE_MIN_DYNAMIC_COPY_RETURN_7D", "scanner", "black", "pct", "rescan",
        config.CORE_MIN_DYNAMIC_COPY_RETURN_7D * 100, "Core动态Copy最近7日收益",
        "从同一条复利曲线截取最近7日，期末相对第23天边界浮动权益至少增长3%"),
    ("CORE_COPY_MIN_WIN_RATE", "scanner", "black", "pct", "rescan",
        config.CORE_COPY_MIN_WIN_RATE * 100, "最终严格Copy最低胜率", "评分只排序，胜率是明确业务条件"),
    ("CORE_PROFITABILITY_CONTRACT", "scanner", "black", "display", "rescan",
        f"官方Perp ≥{config.OFFICIAL_PERP_LONG_HISTORY_DAYS}d时收益≥"
        f"{config.OFFICIAL_PERP_MIN_RETURN_30D * 100:g}%；"
        f"{config.OFFICIAL_PERP_SHORT_HISTORY_DAYS}–"
        f"{config.OFFICIAL_PERP_LONG_HISTORY_DAYS - 1}d时最近7d≥"
        f"{config.OFFICIAL_PERP_MIN_RETURN_7D * 100:g}%；"
        "粗略Copy 30d与最近7d均须净盈利，收益幅度只参与排序；"
        f"动态Copy 30d≥{config.CORE_MIN_DYNAMIC_COPY_RETURN_30D * 100:g}% / "
        f"最近7d≥{config.CORE_MIN_DYNAMIC_COPY_RETURN_7D * 100:g}%",
        "源钱包、粗略Copy与最终严格Copy盈利线",
        "所有Copy收益均来自同一条动态复利权益曲线；评分只排序，不设75分硬线"),
    ("CORE_COPY_MAX_LIQUIDATIONS_30D", "scanner", "black", "display", "rescan",
        f"≤ {config.CORE_COPY_MAX_LIQUIDATIONS_30D} 次",
        "最终回放爆仓上限", "使用我们最大杠杆的代理回放允许至多3次；调参在保留盈利前提下优先减少，第四次才拒绝"),
    ("CORE_COPY_MAX_SINGLE_LIQUIDATION_LOSS_PCT", "scanner", "black", "display", "rescan",
        f"< {config.CORE_COPY_MAX_SINGLE_LIQUIDATION_LOSS_PCT * 100:g}%",
        "单次重大爆仓硬上限",
        "按该复制回合开仓时的动态账户权益归一；达到5%时绕过“最多3次”容忍并直接排除"),
    ("COPY_DEEP_BAG_EVENT_PCT", "scanner", "black", "pct", "rescan",
        config.COPY_DEEP_BAG_EVENT_PCT * 100, "深亏事件线", "从成员周期权益高点回撤达到此比例并持续至少4小时计为深亏事件"),
    ("COPY_DEEP_BAG_EVENT_MIN_HOURS", "scanner", "blue", "float", "rescan",
        config.COPY_DEEP_BAG_EVENT_MIN_HOURS, "深亏事件最短时长", "达到深亏比例后持续至少此小时数才形成事件"),
    ("COPY_DEEP_BAG_LONG_HOURS", "scanner", "blue", "float", "rescan",
        config.COPY_DEEP_BAG_LONG_HOURS, "长时间深亏时长", "已恢复但持续达到此时长最多只允许Challenger"),
    # ── ② 跟单策略参数 (effect = immediate) ────────────────────────────
    ("FOLLOW_SELECTION_MODE", "follow", "hidden", "text", "immediate", config.FOLLOW_SELECTION_MODE,
        "跟单集合模式", "auto使用已发布Core集合;manual保留人工集合"),
    ("PORTFOLIO_DRAWDOWN_STOP_ENABLE", "follow", "black", "bool", "immediate",
        config.PORTFOLIO_DRAWDOWN_STOP_ENABLE,
        "总体权益回撤止损", "开启后按我们账户权益高水位监控；触线立即暂停新开仓并平掉全部持仓"),
    ("PORTFOLIO_DRAWDOWN_STOP_PCT", "follow", "black", "pct", "immediate",
        config.PORTFOLIO_DRAWDOWN_STOP_PCT * 100,
        "总体权益回撤止损线", "达到此回撤比例时主动止损；手动恢复会按当时账户权益重设高水位"),
    ("COIN_BLACKLIST",       "follow",  "green",  "text",    "immediate", config.COIN_BLACKLIST,
        "币种黑名单", "命中的币种不再新开仓;已有仓位仍继续跟随减仓/平仓。建议从持仓行一键加入,避免符号别名写错"),
    ("BLOCK_KOREAN_STOCKS",  "follow",  "green",  "bool",    "immediate", config.BLOCK_KOREAN_STOCKS,
        "屏蔽韩股相关标的", "预置屏蔽 EWY、KR200、Samsung(SMSN)、SK hynix(SKHX/SKHY)、Hyundai；已有仓位只减仓/平仓，不新增仓位"),
    ("LOW_LIQUIDITY_FILTER_ENABLE", "follow", "hidden", "bool", "immediate", config.LOW_LIQUIDITY_FILTER_ENABLE,
        "实时盘口承载过滤", "按我方实际计划订单检查实时L2完整深度、价差和平均冲击；历史回放不使用"),
    ("LIVE_BOOK_MAX_SPREAD_BPS", "follow", "hidden", "float", "immediate",
        config.LIVE_BOOK_MAX_SPREAD_BPS, "实时盘口最大价差(bps)", "超过则不新增实时敞口"),
    ("LIVE_BOOK_MAX_IMPACT_BPS", "follow", "hidden", "float", "immediate",
        config.LIVE_BOOK_MAX_IMPACT_BPS, "实时订单最大平均冲击(bps)", "按我方实际计划名义金额逐层吃单计算"),
    ("MIN_COIN_DAY_NTL_VLM", "follow", "hidden", "usd", "immediate", config.MIN_COIN_DAY_NTL_VLM,
        "盘口不可用时24h成交量下限", "仅实时L2不可用时兜底，并且成交量与OI必须同时不足才拒绝"),
    ("MIN_COIN_OI_NOTIONAL", "follow", "hidden", "usd", "immediate", config.MIN_COIN_OI_NOTIONAL,
        "盘口不可用时OI名义额下限", "仅实时L2不可用时兜底，并且成交量与OI必须同时不足才拒绝"),
    # (FOLLOW_MIN_TRADES / FOLLOW_MIN_ACTIVE_DAYS removed v10 — redundant with the scanner EVIDENCE gate,
    #  which already enforces a track record (active_days≥5 且 回合≥7) before a wallet can be active)
    # (RISK_BUDGET removed v10 — σ-scaled leverage dropped; leverage = the σ-tier's LEV CAP, redundant with
    #  tier cap + master-lev cap + margin/coin/deploy limits)
    ("AUTO_TUNE_MARGIN_ENABLE", "follow", "green", "bool", "immediate", config.AUTO_TUNE_MARGIN_ENABLE,
        "自动调参", "开启后，每个新代际都对本代际Top Core统一优化保证金、杠杆和加仓参数，并在最终严格回放通过后原子发布；失败则保留上一代"),
    ("AUTO_TUNE_MODE", "follow", "hidden", "text", "immediate", config.AUTO_TUNE_MODE,
        "自动调参模式", "Paper默认apply;仍须通过OOS、Holdout、盈利压力与最终爆仓≤3规则"),
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
    ("MARGIN_EQUITY_PCT",   "follow",  "yellow", "pct",     "immediate", config.MARGIN_EQUITY_PCT * 100,
        "保证金权益额度", "单笔开仓按此比例的权益计算保证金；剩余权益仍可被其他钱包、加仓和缓冲使用，并非冻结"),
    ("DEPLOY_FULL_PCT",      "follow",  "hidden", "pct",     "immediate", config.DEPLOY_FULL_PCT * 100,
        "旧满火力占用线", "兼容旧策略快照；新仓在组合部署上限前始终使用调参后的档位保证金"),
    ("STABLE_MARGIN_PCT",    "follow",  "yellow", "pct",     "immediate", config.STABLE_MARGIN_PCT * 100,
        "稳定档·保证金上限", "稳定档低占用时每单保证金上限,占权益%"),
    ("STABLE_MARGIN_MIN_PCT","follow",  "hidden", "pct",     "immediate", config.STABLE_MARGIN_MIN_PCT * 100,
        "旧稳定档·保证金下限", "兼容旧策略快照；满火力线退役后不再参与新仓计算"),
    ("STABLE_LEV_CAP",       "follow",  "yellow", "x",       "immediate", config.STABLE_LEV_CAP,
        "稳定档·杠杆上限", "稳定档杠杆封顶"),
    ("STABLE_MIN_NOTIONAL",  "follow",  "yellow", "usd",     "immediate", config.STABLE_MIN_NOTIONAL,
        "稳定档·最低名义额", "稳定档(BTC/大饼)单笔名义额低于此(封顶到主力名义额后)就不开,太小没意义"),
    ("MID_MARGIN_PCT",       "follow",  "yellow", "pct",     "immediate", config.MID_MARGIN_PCT * 100,
        "中档·保证金上限", "中档低占用时每单保证金上限,占权益%"),
    ("MID_MARGIN_MIN_PCT",   "follow",  "hidden", "pct",     "immediate", config.MID_MARGIN_MIN_PCT * 100,
        "旧中档·保证金下限", "兼容旧策略快照；满火力线退役后不再参与新仓计算"),
    ("MID_LEV_CAP",          "follow",  "yellow", "x",       "immediate", config.MID_LEV_CAP,
        "中档·杠杆上限", "中档杠杆封顶"),
    ("MID_MIN_NOTIONAL",     "follow",  "yellow", "usd",     "immediate", config.MID_MIN_NOTIONAL,
        "中档·最低名义额", "中档(ETH/SOL等)单笔名义额低于此就不开"),
    ("HIGH_MARGIN_PCT",      "follow",  "yellow", "pct",     "immediate", config.HIGH_MARGIN_PCT * 100,
        "剧烈档·保证金上限", "剧烈档低占用时每单保证金上限,占权益%"),
    ("HIGH_MARGIN_MIN_PCT",  "follow",  "hidden", "pct",     "immediate", config.HIGH_MARGIN_MIN_PCT * 100,
        "旧剧烈档·保证金下限", "兼容旧策略快照；满火力线退役后不再参与新仓计算"),
    ("HIGH_LEV_CAP",         "follow",  "yellow", "x",       "immediate", config.HIGH_LEV_CAP,
        "剧烈档·杠杆上限", "剧烈档杠杆封顶"),
    ("HIGH_MIN_NOTIONAL",    "follow",  "yellow", "usd",     "immediate", config.HIGH_MIN_NOTIONAL,
        "剧烈档·最低名义额", "剧烈档(meme/野币)单笔名义额低于此就不开(σ高、仓位本就小,门槛设低)"),
    # 分档最多加仓 —— 仅老模式(SMART_ADD 关)生效; 智能加仓走 σ波动闸+ADD_MAX_HARD. v10: 藏,避免占版面
    ("STABLE_MAX_ADDS",      "follow",  "hidden", "int",     "immediate", config.STABLE_MAX_ADDS, "稳定档·硬上限加仓", ""),
    ("MID_MAX_ADDS",         "follow",  "hidden", "int",     "immediate", config.MID_MAX_ADDS, "中档·硬上限加仓", ""),
    ("HIGH_MAX_ADDS",        "follow",  "hidden", "int",     "immediate", config.HIGH_MAX_ADDS, "剧烈档·硬上限加仓", ""),
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
    ("TAIL_CLOSE_ENABLE",    "follow",  "green",  "bool",    "immediate", config.TAIL_CLOSE_ENABLE,
        "盈利尾仓保护", "智能动态止盈关闭时，目标分批减仓后按剩余比例和强平风险决定是否一次性锁定利润"),
    ("TAIL_CLOSE_HARD_REMAIN_PCT", "follow", "yellow", "pct", "immediate",
        config.TAIL_CLOSE_HARD_REMAIN_PCT * 100,
        "尾仓直接清理线", "整笔仍盈利时，剩余仓位不超过历史峰值此比例就直接全平"),
    ("TAIL_CLOSE_RISK_REMAIN_PCT", "follow", "yellow", "pct", "immediate",
        config.TAIL_CLOSE_RISK_REMAIN_PCT * 100,
        "尾仓风险评估线", "剩余仓位低于此比例后，按当前价到该币种强平价的利润回吐风险评估"),
    ("TAIL_CLOSE_PROFIT_GIVEBACK_PCT", "follow", "yellow", "pct", "immediate",
        config.TAIL_CLOSE_PROFIT_GIVEBACK_PCT * 100,
        "尾仓最大利润回吐", "尾仓继续持有至强平可能吃掉当前整笔利润达到此比例时，立即全平"),
    ("SMART_TP_ENABLE", "follow", "green", "bool", "immediate", config.SMART_TP_ENABLE,
        "智能动态止盈", "开启后接管尾仓保护：按波动率激活高水位，回撤20/35/50%止盈原仓20/25/25%，保留30%，默认关闭"),
    ("SMART_TP_STABLE_ARM_SIGMA", "follow", "hidden", "float", "immediate",
        config.SMART_TP_STABLE_ARM_SIGMA, "智能止盈·稳定档激活σ", ""),
    ("SMART_TP_MID_ARM_SIGMA", "follow", "hidden", "float", "immediate",
        config.SMART_TP_MID_ARM_SIGMA, "智能止盈·中档激活σ", ""),
    ("SMART_TP_HIGH_ARM_SIGMA", "follow", "hidden", "float", "immediate",
        config.SMART_TP_HIGH_ARM_SIGMA, "智能止盈·剧烈档激活σ", ""),
    ("SMART_TP_GIVEBACK_1_PCT", "follow", "hidden", "pct", "immediate",
        config.SMART_TP_GIVEBACK_1_PCT * 100, "智能止盈·一级回撤", ""),
    ("SMART_TP_GIVEBACK_2_PCT", "follow", "hidden", "pct", "immediate",
        config.SMART_TP_GIVEBACK_2_PCT * 100, "智能止盈·二级回撤", ""),
    ("SMART_TP_GIVEBACK_3_PCT", "follow", "hidden", "pct", "immediate",
        config.SMART_TP_GIVEBACK_3_PCT * 100, "智能止盈·三级回撤", ""),
    ("SMART_TP_CLOSE_1_PCT", "follow", "hidden", "pct", "immediate",
        config.SMART_TP_CLOSE_1_PCT * 100, "智能止盈·一级止盈仓位", ""),
    ("SMART_TP_CLOSE_2_PCT", "follow", "hidden", "pct", "immediate",
        config.SMART_TP_CLOSE_2_PCT * 100, "智能止盈·二级止盈仓位", ""),
    ("SMART_TP_CLOSE_3_PCT", "follow", "hidden", "pct", "immediate",
        config.SMART_TP_CLOSE_3_PCT * 100, "智能止盈·三级止盈仓位", ""),
    ("SMART_TP_TAIL_REMAIN_PCT", "follow", "hidden", "pct", "immediate",
        config.SMART_TP_TAIL_REMAIN_PCT * 100, "智能止盈·保留尾仓", ""),
    ("SMART_TP_TARGET_REDUCE_EXIT_PCT", "follow", "hidden", "pct", "immediate",
        config.SMART_TP_TARGET_REDUCE_EXIT_PCT * 100, "智能止盈·目标减仓退出线", ""),
    ("SMART_TP_MIN_FEE_MULT", "follow", "hidden", "float", "immediate",
        config.SMART_TP_MIN_FEE_MULT, "智能止盈·最低手续费覆盖倍数", ""),
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

# Known predecessor defaults that are policy-migrated on deploy. Values are stored in UI units.
_HARVEST_PREVIOUS_DEFAULTS = {
    "HARVEST_MIN_ACCT": ("5000", "5000.0", "10000", "10000.0", "30000", "30000.0"),
    "HARVEST_WEEK_VLM_MIN": (
        "50000", "50000.0", "150000", "150000.0", "300000", "300000.0",
    ),
    "HARVEST_WEEK_PNL_MIN": ("0", "0.0", "250", "250.0", "2000", "2000.0", "5000", "5000.0"),
    "HARVEST_MONTH_PNL_MIN": (
        "0", "0.0", "500", "500.0", "1000", "1000.0", "5000", "5000.0",
        "8000", "8000.0", "15000", "15000.0",
    ),
    "HARVEST_ALL_PNL_MIN": ("20000", "20000.0"),
    "HARVEST_PERP_PNL_SHARE_MIN": ("60", "60.0", "80", "80.0"),
}

_RISK_PREVIOUS_DEFAULTS = {
    "DEPLOY_FULL_PCT": ("30", "30.0", "40", "40.0", "50", "50.0", "60", "60.0"),
    "MAX_CONCURRENT_POS": ("15", "15.0"),
}


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
    # Retired policies: raw profile score, stacked rolling win/PF/Top2 gates and the hard-threshold stop are
    # no longer qualification controls. Purge old rows so stale databases cannot expose or restore them.
    db.execute(
        "DELETE FROM params WHERE key IN "
        "('MIN_ACTIVE_SCORE','COPY_STOP_ENABLE','STOP_MARGIN_PCT','HARVEST_WEEK_VLM_MAX',"
        "'HARVEST_PNL_VOL_MIN','HARVEST_PNL_VOL_MAX','DAILY_PROFILE_BUDGET',"
        "'FULL_REFRESH_SHARDS','RANDOM_EXPLORATION_RATIO','DISCOVERY_MAX_EXTRA_SHARDS',"
        "'CANDIDATE_MAX_RECHECK_DAYS','WALLET_HWM_FREEZE_DD_PCT',"
        "'WALLET_HWM_REDUCE_DD_PCT','WALLET_HWM_EXIT_DD_PCT',"
        "'WALLET_HWM_RELEASE_DD_PCT','WALLET_HWM_EXIT_COOLDOWN_DAYS',"
        "'WALLET_FORWARD_LOSS_FREEZE_PCT','CORE_COPY_SAMPLE_FLOORS',"
        "'CORE_COPY_WIN_RATE_FLOORS','CORE_COPY_WIN_RATE_LCB','CORE_COPY_RECENT_BODY',"
        "'CORE_COPY_CAMPAIGN_FLOORS','CHALLENGER_MIN_COPY_RETURN_30D',"
        "'CORE_STRONG_COPY_RETURN_30D',"
        "'ROUGH_COPY_MIN_RETURN_30D','ROUGH_COPY_MIN_RETURN_7D','COPY_BT_MIN_NET_PNL',"
        "'CORE_COPY_WIN_RATE_30D_MIN','CORE_RETENTION_WIN_RATE_30D_MIN',"
        "'CORE_COPY_WIN_RATE_LCB_30D_MIN','CORE_RETENTION_WIN_RATE_LCB_30D_MIN',"
        "'COPY_MIN_PROFIT_FACTOR','COPY_MIN_TAIL_RETURN_30D',"
        "'PORTFOLIO_MAX_TURNOVER','PORTFOLIO_MIN_EDGE_BPS',"
        "'CORE_RETENTION_MIN_COPY_RETURN_30D',"
        "'HARVEST_WEEK_ROI_MIN','HARVEST_MONTH_ROI_MIN','HARVEST_ALL_ROI_MIN',"
        "'HARVEST_ROI_WINDOWS_MIN_PASS','MAX_TOTAL_MARGIN_PCT',"
        "'WALLET_MARGIN_CAP_PCT','WALLET_SECTOR_SIDE_CAP_PCT',"
        "'WALLET_CRYPTO_STABLE_SIDE_CAP_PCT','WALLET_CRYPTO_MID_SIDE_CAP_PCT',"
        "'WALLET_CRYPTO_HIGH_SIDE_CAP_PCT','WALLET_STOCK_SIDE_CAP_PCT',"
        "'WALLET_MAX_OPEN_POSITIONS','WALLET_STOCK_SIDE_MAX_POSITIONS',"
        "'CORE_INTRATRADE_DD_MAX','CORE_INTRATRADE_DD_REJECT',"
        "'CORE_DEEP_BAG_MAX_FAILED','CORE_DEEP_BAG_MIN_RECOVERY_RATE',"
        "'CORE_TARGET_MIN_N','CORE_PROMOTION_MIN_HOURS','CORE_SOFT_MIN_TENURE_DAYS',"
        "'CORE_SOFT_FAIL_CONFIRMATIONS','COPY_MIN_EXPECTED_MARGIN_RETURN',"
        "'COPY_STABILITY_FOLD_DAYS','COPY_STABILITY_FOLD_COUNT',"
        "'COPY_STABILITY_MIN_EVALUABLE_FOLDS','COPY_STABILITY_MIN_PROFITABLE_FOLDS',"
        "'COPY_STABILITY_MIN_RETURN','COPY_STABILITY_MAX_LOSS_TO_30D_PROFIT',"
        "'CORE_MIN_COPY_RETURN_30D','CORE_MIN_COPY_RETURN_7D',"
        "'CORE_MIN_AVG_NET_PER_CLOSE_RETURN','COPY_WEEKLY_MIN_CAMPAIGNS_PER_FOLD',"
        "'COPY_WEEKLY_MIN_RETURN','COPY_WEEKLY_SCORE_RETURN_TARGET',"
        "'COPY_WEEKLY_MIN_NET_PER_CLOSED_RETURN','CORE_MIN_COPY_NET_PNL_30D',"
        "'CORE_MIN_COPY_NET_PNL_7D','CORE_COPY_STABILITY',"
        "'CORE_MIN_CAMPAIGN_MARGIN_RETURN_30D','CORE_MIN_CAMPAIGN_MARGIN_RETURN_7D',"
        "'CORE_COPY_CAMPAIGN_FLOOR','CORE_COPY_MIN_CAMPAIGN_WIN_RATE',"
        "'CORE_COPY_MIN_BODY_WIN_RATE','CORE_MIN_FOLLOW_SCORE',"
        "'OFFICIAL_PERP_FOLD_DAYS','OFFICIAL_PERP_FOLD_COUNT',"
        "'OFFICIAL_PERP_MIN_FOLD_RETURN','COPY_CAMPAIGN_ZERO_RETURN_PRIOR')"
    )
    for key, category, level, ptype, effect, default, name, desc in PARAM_SPEC:
        dv = _to_text(default)
        # Approved policy migration: the old hidden heavy-DCA threshold was 20.  This is a deliberate
        # strategy change, not a metadata-default refresh, so existing databases must move with the code.
        if key == "max_single_adds":
            db.execute("UPDATE params SET value=? WHERE key=? AND value='20'", (dv, key))
        # Approved Core-count policy: 16 is an upper bound only. Move the former untouched default of 10
        # to 16 while preserving any genuinely operator-edited limit.
        if key == "CORE_INITIAL_MAX_N":
            db.execute(
                "UPDATE params SET value=? WHERE key=? AND value IN ('10','10.0') "
                "AND default_value IN ('10','10.0') AND value=default_value",
                (dv, key),
            )
        # Approved harvest-policy migration. Move only previously approved default surfaces, including the
        # immediately preceding 15/20/20 + 2k/8k/0 policy,
        # to the new production default. Unrelated operator custom values remain untouched.
        old_values = _HARVEST_PREVIOUS_DEFAULTS.get(key)
        if old_values:
            marks = ",".join("?" for _ in old_values)
            db.execute(
                f"UPDATE params SET value=? WHERE key=? AND value IN ({marks}) "
                f"AND default_value IN ({marks}) AND value=default_value AND default_value<>?",
                (dv, key, *old_values, *old_values, dv),
            )
        old_risk_values = _RISK_PREVIOUS_DEFAULTS.get(key)
        if old_risk_values:
            marks = ",".join("?" for _ in old_risk_values)
            db.execute(
                f"UPDATE params SET value=? WHERE key=? AND value IN ({marks}) "
                f"AND default_value IN ({marks}) AND value=default_value AND default_value<>?",
                (dv, key, *old_risk_values, *old_risk_values, dv),
            )
        db.execute(
            "INSERT OR IGNORE INTO params (key,value,category,level,type,effect,default_value,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (key, dv, category, level, ptype, effect, dv, stamp))
        db.execute(
            "UPDATE params SET category=?,level=?,type=?,effect=?,default_value=? WHERE key=?",
            (category, level, ptype, effect, dv, key))
        if ptype == "display":
            db.execute("UPDATE params SET value=? WHERE key=?", (dv, key))
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
# pct/nullable are stored as UI percent (0.5 == 0.5%) -> engine wants the fraction (÷100).
# Everything else is stored in engine units already, so the rule is purely type-based.
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
    "HARVEST_MIN_ACCT": "min_acct",
    "HARVEST_WEEK_VLM_MIN": "week_vlm_min",
    "HARVEST_WEEK_PNL_MIN": "week_pnl_min",
    "HARVEST_MONTH_PNL_MIN": "month_pnl_min", "HARVEST_ALL_PNL_MIN": "all_pnl_min",
    "HARVEST_PERP_PNL_SHARE_MIN": "perp_pnl_share_min",
    "min_perp": "min_perp", "inactive_days": "inactive_days", "max_daily_eps": "max_daily_eps",
    "grid_max_adds": "grid_max_adds",
    "max_single_adds": "max_single_adds",
    "EXCLUDE_HFT": "exclude_hft", "HFT_MIN_HOLD_MIN": "hft_min_hold_min",
    "max_fills_per_ep": "max_fills_per_ep",
    "COPY_BT_GATE_ENABLE": "copy_bt_gate_enable", "COPY_BT_DAYS": "copy_bt_days",
    "COPY_BT_MIN_CLOSED": "copy_bt_min_closed",
    "MAX_CONCURRENT_POS": "max_concurrent_pos",
    "DAILY_SCAN_TIME_BUDGET_MIN": "daily_scan_time_budget_min",
    "CORE_REFRESH_DEADLINE_MIN": "core_refresh_deadline_min",
    "SCAN_FINALIZE_RESERVE_MIN": "scan_finalize_reserve_min",
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
