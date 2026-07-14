export const PARAM_META = {
  // follow
  STABLE_MARGIN_MIN_PCT: { name: "稳定档·保证金下限", desc: "组合占用升高后线性缩到的单笔保证金下限", range: "1–3", up: "拥挤时仍开得更重", dn: "拥挤时更轻" },
  STABLE_MARGIN_PCT: { name: "稳定档·保证金上限", desc: "组合占用低时的单笔保证金上限", range: "2–5", up: "低频期每单更重", dn: "低频期每单更轻" },
  STABLE_LEV_CAP: { name: "稳定档·杠杆上限", desc: "σ≤4%的杠杆封顶(绝对上限)", range: "15–20", up: "放开高杠杆", dn: "压低杠杆" },
  STABLE_MIN_NOTIONAL: { name: "稳定档·最低名义额", desc: "BTC/大饼单笔名义额低于此(封顶到主力后)就不开,太小没意义", range: "$3k–8k", up: "过滤更多小单", dn: "连很小的也跟" },
  MID_MARGIN_MIN_PCT: { name: "中档·保证金下限", desc: "组合占用升高后线性缩到的单笔保证金下限", range: "1–3", up: "拥挤时仍开得更重", dn: "拥挤时更轻" },
  MID_MARGIN_PCT: { name: "中档·保证金上限", desc: "组合占用低时的单笔保证金上限", range: "2–5", up: "低频期每单更重", dn: "低频期每单更轻" },
  MID_LEV_CAP: { name: "中档·杠杆上限", desc: "σ 5–9%的杠杆封顶", range: "8–12", up: "放开高杠杆", dn: "压低杠杆" },
  MID_MIN_NOTIONAL: { name: "中档·最低名义额", desc: "ETH/SOL等单笔名义额低于此就不开", range: "$2k–5k", up: "过滤更多小单", dn: "连很小的也跟" },
  HIGH_MARGIN_MIN_PCT: { name: "剧烈档·保证金下限", desc: "组合占用升高后线性缩到的单笔保证金下限", range: "0.5–2", up: "拥挤时仍开得更重", dn: "拥挤时更轻" },
  HIGH_MARGIN_PCT: { name: "剧烈档·保证金上限", desc: "组合占用低时的单笔保证金上限", range: "1–4", up: "低频期每单更重", dn: "低频期每单更轻" },
  HIGH_LEV_CAP: { name: "剧烈档·杠杆上限", desc: "σ≥9%的杠杆封顶", range: "3–5", up: "放开高杠杆", dn: "压低杠杆" },
  HIGH_MIN_NOTIONAL: { name: "剧烈档·最低名义额", desc: "meme/野币单笔名义额低于此就不开(σ高、仓位本就小,门槛设低)", range: "$500–1k", up: "过滤更多小单", dn: "连很小的也跟" },
  DEPLOY_FULL_PCT: { name: "满火力占用线", desc: "组合保证金占用不超过此值时按各档保证金上限开新仓", range: "30–50", up: "更久保持大单", dn: "更早开始缩仓" },
  MAX_DEPLOY_PCT: { name: "组合部署上限", desc: "组合保证金占用达到此值后停开新仓,保留资金给加仓和平仓管理", range: "70–85", up: "允许更多新仓", dn: "更早锁住新仓" },
  MAX_LEV: { name: "最大杠杆", desc: "杠杆上限(σ估计兜底)", range: "10–50", up: "放开高杠杆", dn: "更严格限杠杆" },
  MIN_LEV: { name: "最小杠杆", desc: "杠杆下限(极波动币≈现货)", range: "—" },
  MIN_OPEN_MARGIN_PCT: { name: "单笔最小开仓额", desc: "低于此则跳过该信号(不开尘埃仓)", range: "—" },
  ADD_FRAC: { name: "每次加仓比例", desc: "每次加仓额=首开保证金×此%(50=首开一半;首开3%+3加=满仓7.5%)", range: "30–60", up: "加仓更猛、满仓更重", dn: "加仓更轻" },
  STABLE_MAX_ADDS: { name: "稳定档·最多加仓", desc: "BTC/大饼一笔最多跟几次加仓(波动小,可多摊)", range: "2–4", up: "跟更多加仓", dn: "更早停跟" },
  MID_MAX_ADDS: { name: "中档·最多加仓", desc: "ETH/SOL/HYPE一笔最多跟几次加仓", range: "1–3", up: "跟更多加仓", dn: "更早停跟" },
  HIGH_MAX_ADDS: { name: "剧烈档·最多加仓", desc: "meme/野币/高波股一笔最多跟几次加仓(波动大,少加/设0)", range: "0–2", up: "跟更多加仓", dn: "更早停跟" },
  COPY_STOP_ENABLE: { name: "启用止损", desc: "亏损达到止损保证金比例时自动平仓；回测收益较低，默认关闭", range: "—" },
  STOP_MARGIN_PCT: { name: "止损=亏损保证金%", desc: "亏掉本仓这么多%保证金就平仓(70=亏到70%保证金,爆仓前兜底);带杠杆自动换算逆向价格:5x→14%、3x→23%、7x→10%", range: "50–90", up: "更宽容、离爆仓更近", dn: "砍更早、单笔亏更少但易误杀恢复单" },
  MAX_ENTRY_CHASE_PCT: { name: "追价保护阈值", desc: "开仓价偏离超此%则放弃(空=关闭)", range: "0.3–1", up: "更宽容追价", dn: "更严防滑点" },
  VOL_FAST_DAYS: { name: "波动率快/慢窗口", desc: "σ 计算窗口(只读)", range: "—" },
  VOL_FALLBACK_SIGMA: { name: "默认波动率", desc: "无数据时的兜底σ", range: "—" },
  // scanner
  HARVEST_MIN_ACCT: { name: "钱包最低资金门槛", desc: "账户≥此金额才看", range: "$2k–$10k", up: "只看大资金", dn: "纳入小资金、更杂" },
  HARVEST_WEEK_VLM_MIN: { name: "周成交量范围", desc: "近7天成交额在范围内才看", range: "$300k–$30m" },
  HARVEST_WEEK_VLM_MAX: { name: "周成交量范围", desc: "近7天成交额在范围内才看", range: "$300k–$30m" },
  inactive_days: { name: "最长不活跃天数", desc: "超过此天数没成交则剔除", range: "1–7 天", up: "更宽容沉默", dn: "更快剔除" },
  EXCLUDE_HFT: { name: "过滤高频HFT(开关)", desc: "剔除秒级快炒钱包——他们赚钱但我们延迟太大抄不了;接入高频WS后可关掉", range: "—" },
  CORE_INITIAL_MAX_N: { name: "初始跟单上限", desc: "先整体调参的最高质量Core钱包数;之后只从质量末尾缩减", range: "4–24" },
};

export const UNIT = { usd: "$", pct: "%", x: "×" };
export const AUTO_TUNE_KEY = "AUTO_TUNE_MARGIN_ENABLE";
export const BLACKLIST_KEY = "COIN_BLACKLIST";

export const ADD_KEYS = new Set([
  "FOLLOW_POS_ADD",
  "SMART_ADD",
  "ADD_GAP_K",
  "ADD_GAP_SHRINK_G",
  "ADD_MAX_HARD",
  "ADD_FRAC",
  "STABLE_MAX_ADDS",
  "MID_MAX_ADDS",
  "HIGH_MAX_ADDS",
]);

export const TIER_GROUPS = [
  {
    key: "stable",
    label: "稳定档",
    sub: "σ ≤ 5% · BTC 及更稳的(含低波动股票如GOLD)",
    tint: "tint-green",
    min: "STABLE_MARGIN_MIN_PCT",
    max: "STABLE_MARGIN_PCT",
    lev: "STABLE_LEV_CAP",
    notl: "STABLE_MIN_NOTIONAL",
    cap: "STABLE_COIN_CAP_PCT",
  },
  {
    key: "mid",
    label: "中档",
    sub: "σ 5–9% · ETH / SOL / HYPE 等主流",
    tint: "tint-amber",
    min: "MID_MARGIN_MIN_PCT",
    max: "MID_MARGIN_PCT",
    lev: "MID_LEV_CAP",
    notl: "MID_MIN_NOTIONAL",
    cap: "MID_COIN_CAP_PCT",
  },
  {
    key: "high",
    label: "剧烈档",
    sub: "σ ≥ 9% · ZEC / meme / 野币 / 高波股",
    tint: "tint-red",
    min: "HIGH_MARGIN_MIN_PCT",
    max: "HIGH_MARGIN_PCT",
    lev: "HIGH_LEV_CAP",
    notl: "HIGH_MIN_NOTIONAL",
    cap: "HIGH_COIN_CAP_PCT",
  },
];
