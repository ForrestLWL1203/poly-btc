import { api } from "../lib/api.js";
import { fParam, formatCoinList, normalizeCoin, parseCoinList } from "../lib/format.js";
import { IC, Ico } from "../lib/icons.jsx";

const { useState, useEffect, useRef } = React;

/* ----------------------------------------------------------------- param metadata (UI-side) */
const PARAM_META = {
  // follow
  MIN_FOLLOW_SCORE: { name: "跟单评分线", desc: "watchlist 里评分≥此线的钱包才实际跟单(0–100 标准化分,见下方实时达标数)", range: "—", up: "更严、跟更少精英", dn: "更宽、纳入更多" },
  STABLE_MARGIN_MIN_PCT: { name: "稳定档·保证金下限", desc: "组合占用升高后线性缩到的单笔保证金下限", range: "1–3", up: "拥挤时仍开得更重", dn: "拥挤时更轻" },
  STABLE_MARGIN_PCT: { name: "稳定档·保证金上限", desc: "组合占用低时的单笔保证金上限", range: "2–5", up: "低频期每单更重", dn: "低频期每单更轻" },
  STABLE_LEV_CAP: { name: "稳定档·杠杆上限", desc: "σ≤4%的杠杆封顶(绝对上限)", range: "15–20", up: "放开高杠杆", dn: "压低杠杆" },
  STABLE_MIN_NOTIONAL: { name: "稳定档·最低名义额", desc: "BTC/大饼单笔名义额低于此(封顶到主力后)就不开,太小没意义", range: "$3k–8k", up: "过滤更多小单", dn: "连很小的也跟" },
  MID_MARGIN_MIN_PCT: { name: "中档·保证金下限", desc: "组合占用升高后线性缩到的单笔保证金下限", range: "1–3", up: "拥挤时仍开得更重", dn: "拥挤时更轻" },
  MID_MARGIN_PCT: { name: "中档·保证金上限", desc: "组合占用低时的单笔保证金上限", range: "2–5", up: "低频期每单更重", dn: "低频期每单更轻" },
  MID_LEV_CAP: { name: "中档·杠杆上限", desc: "σ 4–10%的杠杆封顶", range: "8–12", up: "放开高杠杆", dn: "压低杠杆" },
  MID_MIN_NOTIONAL: { name: "中档·最低名义额", desc: "ETH/SOL等单笔名义额低于此就不开", range: "$2k–5k", up: "过滤更多小单", dn: "连很小的也跟" },
  HIGH_MARGIN_MIN_PCT: { name: "剧烈档·保证金下限", desc: "组合占用升高后线性缩到的单笔保证金下限", range: "0.5–2", up: "拥挤时仍开得更重", dn: "拥挤时更轻" },
  HIGH_MARGIN_PCT: { name: "剧烈档·保证金上限", desc: "组合占用低时的单笔保证金上限", range: "1–4", up: "低频期每单更重", dn: "低频期每单更轻" },
  HIGH_LEV_CAP: { name: "剧烈档·杠杆上限", desc: "σ≥10%的杠杆封顶", range: "3–5", up: "放开高杠杆", dn: "压低杠杆" },
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
  COPY_STOP_ENABLE: { name: "启用止损", desc: "总开关:逆向超过该币波动率自动平仓(默认开)", range: "—" },
  STOP_MARGIN_PCT: { name: "止损=亏损保证金%", desc: "亏掉本仓这么多%保证金就平仓(70=亏到70%保证金,爆仓前兜底);带杠杆自动换算逆向价格:5x→14%、3x→23%、7x→10%", range: "50–90", up: "更宽容、离爆仓更近", dn: "砍更早、单笔亏更少但易误杀恢复单" },
  MAX_ENTRY_CHASE_PCT: { name: "追价保护阈值", desc: "开仓价偏离超此%则放弃(空=关闭)", range: "0.3–1", up: "更宽容追价", dn: "更严防滑点" },
  EXEC_MAKER_MIRROR: { name: "镜像挂单模式", desc: "暂不开放", range: "—" },
  VOL_FAST_DAYS: { name: "波动率快/慢窗口", desc: "σ 计算窗口(只读)", range: "—" },
  VOL_FALLBACK_SIGMA: { name: "默认波动率", desc: "无数据时的兜底σ", range: "—" },
  // scanner
  HARVEST_MIN_ACCT: { name: "钱包最低资金门槛", desc: "账户≥此金额才看", range: "$2k–$10k", up: "只看大资金", dn: "纳入小资金、更杂" },
  HARVEST_MAX_TURNOVER: { name: "最高日换手率", desc: "高于此判为做市商,排除", range: "5–20", up: "放进更高频", dn: "更严留低频" },
  HARVEST_WEEK_VLM_MIN: { name: "近7天最低成交量", desc: "一周太冷清不要", range: "$25k–$200k", up: "只要近周活跃", dn: "纳入更安静" },
  HARVEST_MON_ROI_MIN: { name: "近30天最低收益率", desc: "月收益下限", range: "5%–20%", up: "只要高收益", dn: "纳入低收益" },
  HARVEST_MON_ROI_MAX: { name: "近30天最高收益率", desc: "反赌徒上限", range: "100%–500%", up: "放进更猛的", dn: "更严防赌徒" },
  HARVEST_WEEK_ROI_MIN: { name: "近7天最低收益率", desc: "近周也要在赚", range: "0%–5%", up: "更严", dn: "更宽" },
  min_perp: { name: "合约交易占比下限", desc: "合约占比太低不可跟", range: "—" },
  inactive_days: { name: "最长不活跃天数", desc: "超过此天数没成交则剔除", range: "1–7 天", up: "更宽容沉默", dn: "更快剔除" },
  max_daily_eps: { name: "每日最多交易次数", desc: "反机器人上限", range: "—" },
  min_activity: { name: "最低活跃度", desc: "≈活跃天/14", range: "—" },
  grid_max_adds: { name: "单笔最多加仓次数", desc: "反网格", range: "—" },
  EXCLUDE_HFT: { name: "过滤高频HFT(开关)", desc: "剔除秒级快炒钱包——他们赚钱但我们延迟太大抄不了;接入高频WS后可关掉", range: "—" },
  HFT_MIN_HOLD_MIN: { name: "HFT最短中位持仓", desc: "开关开启时,中位持仓低于此分钟数判为HFT剔除", range: "2–5 分钟" },
  SCORE_W_WIN: { name: "评分·胜率权重", desc: "综合评分里胜率的占比(三权重相对生效,无需凑100)", range: "—", up: "更看重持续胜率", dn: "更看重收益/稳定" },
  SCORE_W_ROI: { name: "评分·收益权重", desc: "综合评分里风险调整收益的占比", range: "—", up: "更看重赚得多", dn: "更看重胜率/稳定" },
  SCORE_W_ACT: { name: "评分·活跃度权重", desc: "综合评分里活跃度(成交数+活跃天数)的占比", range: "—", up: "更看重高频活跃", dn: "更看重胜率/收益" },
  SCORE_STRETCH: { name: "评分·标度拉伸", desc: "线性拉伸使最强钱包≈100、平滑下滑,便于设跟单线", range: "1.0–1.3", up: "top更贴近100", dn: "整体压低" },
  UW_TOL: { name: "浮亏容忍线 / 危险线", desc: "只读展示", range: "—" },
};
const UNIT = { usd: "$", pct: "%", x: "×" };

/* ----------------------------------------------------------------- settings */
/* 下单沙盘 — 用真实 v10 公式实时换算:杠杆 = 档位上限(floor,clip MIN/MAX_LEV);止损 = 亏SM%保证金 逆向。
   只读展示(无可调目标杠杆),紧凑液态玻璃风。σ 为实采日内最高-最低振幅均值。读自正在编辑的 vals。 */
function SizingPreview({ vals }) {
  const [bal, setBal] = React.useState(10000);
  const [deploy, setDeploy] = React.useState(20);
  const n = (k, d) => { const v = Number(vals[k]); return isFinite(v) && v > 0 ? v : d; };
  const stMax = n("STABLE_SIGMA_MAX", 4), hiMin = n("HIGH_SIGMA_MIN", 10);
  const MINL = Math.max(1, n("MIN_LEV", 1));
  const SM = n("STOP_MARGIN_PCT", 70);
  const stopOn = vals["COPY_STOP_ENABLE"] !== false;
  const tier = s => s <= stMax ? "stable" : (s >= hiMin ? "high" : "mid");
  const TM = { stable: ["STABLE_MARGIN_MIN_PCT", "STABLE_MARGIN_PCT", "STABLE_LEV_CAP"],
    mid: ["MID_MARGIN_MIN_PCT", "MID_MARGIN_PCT", "MID_LEV_CAP"], high: ["HIGH_MARGIN_MIN_PCT", "HIGH_MARGIN_PCT", "HIGH_LEV_CAP"] };
  const DOT = { stable: "var(--green)", mid: "var(--amber)", high: "var(--red)" };
  const dft = { STABLE_MARGIN_MIN_PCT: 2, STABLE_MARGIN_PCT: 3.5, STABLE_LEV_CAP: 25,
    MID_MARGIN_MIN_PCT: 2, MID_MARGIN_PCT: 3, MID_LEV_CAP: 10, HIGH_MARGIN_MIN_PCT: 1.2, HIGH_MARGIN_PCT: 2, HIGH_LEV_CAP: 4 };
  const usd = x => x >= 1000 ? "$" + (x / 1000).toFixed(x >= 10000 ? 0 : 1) + "k" : "$" + Math.round(x);
  const marginPct = t => {
    const lo = Math.min(n(TM[t][0], dft[TM[t][0]]), n(TM[t][1], dft[TM[t][1]])) / 100;
    const hi = Math.max(n(TM[t][0], dft[TM[t][0]]), n(TM[t][1], dft[TM[t][1]])) / 100;
    const full = n("DEPLOY_FULL_PCT", 40) / 100, lock = n("MAX_DEPLOY_PCT", 80) / 100;
    const d = Math.max(0, deploy / 100);
    if (d <= full) return hi;
    if (d >= lock || lock <= full) return lo;
    return lo + (hi - lo) * (lock - d) / (lock - full);
  };
  const calc = s0 => {
    const s = Math.max(0.1, s0), t = tier(s);
    const mPct = marginPct(t), cap = n(TM[t][2], dft[TM[t][2]]);
    const lev = Math.max(MINL, Math.floor(cap));   // v10: 杠杆 = 档位上限(再被目标杠杆+股票上限封顶)
    const margin = bal * mPct;
    const stopLoss = Math.min(SM / 100, 1), stopDist = stopLoss / lev * 100;  // 硬亏=SM%保证金(固定),逆向价格=SM%÷杠杆
    return { t, margin, lev, notl: margin * lev, stopDist, stopLoss };
  };
  const COINS = [["BTC", 3.9], ["ETH", 5.3], ["ZEC", 14.6]];   /* 每档一个代表:稳定 / 中 / 剧烈 */
  return (
    <div className="sz">
      <div className="sz-hd">
        <div className="sz-ttl">下单沙盘<span>· 按当前参数实时换算</span></div>
        <div className="sz-bal"><label>账户权益</label>
          <input type="number" value={bal} onChange={e => setBal(Number(e.target.value) || 0)} /></div>
        <div className="sz-bal"><label>已占用%</label>
          <input type="number" value={deploy} onChange={e => setDeploy(Number(e.target.value) || 0)} /></div>
      </div>
      <div className="sz-grid">
        <div className="sz-hdr">币种</div><div className="sz-hdr sz-num">σ</div>
        <div className="sz-hdr sz-num">杠杆</div><div className="sz-hdr sz-num">保证金 / 名义</div>
        <div className="sz-hdr sz-num">止损 / 硬亏</div>
        {COINS.map(([sym, sig]) => {
          const r = calc(sig);
          return (
            <div className="sz-row" key={sym}>
              <div className="sz-cell sz-coin"><span className="sz-dot" style={{ color: DOT[r.t] }} />{sym}</div>
              <div className="sz-cell sz-num">{sig.toFixed(1)}%</div>
              <div className="sz-cell sz-lev">{r.lev}x</div>
              <div className="sz-cell sz-num">{usd(r.margin)}<span className="sz-sub"> / {usd(r.notl)}</span></div>
              <div className="sz-cell sz-num">{stopOn
                ? <React.Fragment>−{r.stopDist.toFixed(1)}%<span className="sz-sub"> / 亏{Math.round(r.stopLoss * 100)}%</span></React.Fragment>
                : <span className="sz-sub">已关</span>}</div>
            </div>
          );
        })}
      </div>
      <div className="sz-foot">
        杠杆 = <b>σ 档位上限</b>(σ 定档,再被目标杠杆+股票上限封顶)· 保证金 = 权益 × 动态区间%{stopOn
          ? <React.Fragment> · 止损 = 亏到 <b>{Math.round(SM)}%</b> 保证金就平(与币种无关的硬亏),换算逆向价格 = <b>{Math.round(SM)}%÷杠杆</b></React.Fragment>
          : <React.Fragment> · <b>止损已关闭</b>,仅靠强平兜底</React.Fragment>}
      </div>
    </div>
  );
}

/* 行内编辑值:平时是一段带轻微底色的文本(值+单位),点击变成输入框,失焦/回车提交并复原成文本。
   提交只更新暂存(vals/dirty),实际落库仍由底部 apply-bar(确认/重采)。Esc 取消。 */
function EditableValue({ value, unit, ptype, disabled, onCommit }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const ref = useRef(null);
  useEffect(() => { setDraft(value); }, [value]);                       // 外部值变化(保存后)时同步
  useEffect(() => { if (editing && ref.current) { ref.current.focus(); ref.current.select(); } }, [editing]);
  const commit = () => {
    setEditing(false);
    const v = draft === "" || draft == null ? null : Number(draft);
    if (v !== value && !(v == null && value == null)) onCommit(v);
  };
  if (disabled) return <span className="ev ev-ro">{value == null ? "—" : fParam(value, ptype)}{unit && <i className="ev-u">{unit}</i>}</span>;
  if (editing) return (
    <input ref={ref} className="ev-input" type={ptype === "nullable" ? "text" : "number"} value={draft == null ? "" : draft}
      placeholder={ptype === "nullable" ? "关闭" : ""}
      onChange={e => setDraft(e.target.value)} onBlur={commit}
      onKeyDown={e => { if (e.key === "Enter") commit(); else if (e.key === "Escape") { setDraft(value); setEditing(false); } }} />
  );
  return (
    <span className="ev" title="点击编辑" onClick={() => { setDraft(value); setEditing(true); }}>
      {value == null ? <span className="ev-empty">关闭</span> : fParam(value, ptype)}{value != null && unit && <i className="ev-u">{unit}</i>}
    </span>
  );
}

function CoinBlacklistEditor({ param, value, dirty, disabled, onCommit }) {
  const [draft, setDraft] = useState("");
  const coins = parseCoinList(value);
  const commitCoins = (next) => onCommit(formatCoinList(next));
  const add = () => {
    const c = normalizeCoin(draft);
    if (!c || coins.includes(c)) { setDraft(""); return; }
    commitCoins([...coins, c]);
    setDraft("");
  };
  return (
    <div className={"prow coin-blacklist-row" + (dirty ? " dirty" : "")}>
      <span className="lvl-dot lvl-green" />
      <div className="pn"><b>{param.name}</b></div>
      <div className="pd">{param.desc}</div>
      <div className="pctl coin-blacklist-ctl">
        <div className="coin-tags">
          {coins.length === 0 && <span className="coin-empty">暂无黑名单</span>}
          {coins.map(c => (
            <button key={c} className="coin-tag" disabled={disabled} title="从黑名单删除"
              onClick={() => commitCoins(coins.filter(x => x !== c))}>
              <span>{c}</span><b>×</b>
            </button>
          ))}
        </div>
        <div className="coin-add">
          <input value={draft} disabled={disabled} placeholder="XYZ:SHKX"
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") add(); else if (e.key === "Escape") setDraft(""); }} />
          <button className="btn btn-sm" disabled={disabled || !normalizeCoin(draft)}
            title="添加币种" onClick={add}><Ico d={IC.plus} /></button>
        </div>
      </div>
    </div>
  );
}

export function Settings({ startRescan, confirm, toast }) {
  const [params, setParams] = useState(null);
  const [tab, setTab] = useState("scanner");
  const [vals, setVals] = useState({});
  const [dirty, setDirty] = useState({});
  const [expanded, setExpanded] = useState(null);
  const [saving, setSaving] = useState(false);                    // 保存时的短暂全页 loading(替代右上角 toast)
  const [openTiers, setOpenTiers] = useState({});                 // 档位折叠(默认全部收起)
  const [scoreDist, setScoreDist] = useState(null);               // watchlist 全体显示分(0-100),供跟单线实时计数

  const loadParams = async () => {
    const p = await api.get("/api/params?includeScoreDist=1");
    setParams(p);
    const v = {};
    [...p.scanner, ...p.follow].forEach(x => { v[x.key] = x.value; });
    setVals(v);
    if (p.scoreDist) setScoreDist(p.scoreDist);
  };

  useEffect(() => {
    loadParams().catch(() => {
      api.get("/api/params").then(p => {
        setParams(p);
        const v = {}; [...p.scanner, ...p.follow].forEach(x => { v[x.key] = x.value; });
        setVals(v);
      }).catch(() => {});
      api.get("/api/score-dist").then(setScoreDist).catch(() => {});
    });
  }, []);

  if (!params) return <div className="content"><div className="loading">加载中…</div></div>;
  const ADD_KEYS = new Set(["FOLLOW_POS_ADD", "SMART_ADD", "ADD_GAP_K", "ADD_GAP_SHRINK_G", "ADD_MAX_HARD",
    "ADD_FRAC", "STABLE_MAX_ADDS", "MID_MAX_ADDS", "HIGH_MAX_ADDS"]);   // 归入独立「加仓策略」tab
  const AUTO_TUNE_KEY = "AUTO_TUNE_MARGIN_ENABLE";
  const BLACKLIST_KEY = "COIN_BLACKLIST";
  //  (单币上限 STABLE/MID/HIGH_COIN_CAP_PCT 已挪回「跟单策略 · σ分档」—— 它是全局灾难闸,管开仓+加仓,不是加仓专属)
  const list = tab === "add" ? params.follow.filter(p => ADD_KEYS.has(p.key)) : params[tab];
  const editable = (p) => !(p.type === "display" || p.level === "black");
  const set = (key, val) => { setVals(v => ({ ...v, [key]: val })); setDirty(dd => ({ ...dd, [key]: true })); };
  const tabDirty = list.filter(p => dirty[p.key]);
  const autoTuneParam = tab === "follow" ? list.find(p => p.key === AUTO_TUNE_KEY) : null;
  const blacklistParam = tab === "follow" ? list.find(p => p.key === BLACKLIST_KEY) : null;
  const byKey = k => list.find(p => p.key === k);

  const Prow = (p) => {
    const m = PARAM_META[p.key] || {}; const ed = editable(p); const lvl = p.level;
    return (
      <div key={p.key}>
        <div className={"prow" + (dirty[p.key] ? " dirty" : "")}>
          <span className="lvl-dot lvl-green" />
          <div className="pn"><b>{p.name || m.name || p.key}</b></div>
          <div className="pd">{p.desc || m.desc}{m.range && m.range !== "—" && <span style={{ color: "var(--t4)" }}> · 建议 {m.range}</span>}</div>
          <div className="pctl">
            {p.type === "bool" ? (
              <div className={"toggle " + (vals[p.key] ? "on" : "")} onClick={() => ed && set(p.key, !vals[p.key])} style={{ opacity: ed ? 1 : .5 }}><div className="knob" /></div>
            ) : p.type === "display" ? (
              <span className="mono" style={{ color: "var(--t2)", fontSize: 12 }}>{p.value}</span>
            ) : (
              <EditableValue value={vals[p.key]} unit={UNIT[p.type] || ""} ptype={p.type}
                disabled={!ed} onCommit={v => set(p.key, v)} />
            )}
            {(lvl === "black" || p.type === "display") && <span className="plock">只读</span>}
          </div>
        </div>
        {expanded === p.key && (m.up || m.dn) && (
          <div className="peffect">
            {m.up && <span><span className="eff-up">调高↑</span> {m.up}　</span>}
            {m.dn && <span><span className="eff-dn">调低↓</span> {m.dn}</span>}
          </div>
        )}
      </div>
    );
  };
  /* v8 三档保证金/杠杆折叠分组(否则页面太高) */
  const TIER_GROUPS = [
    { key: "stable", label: "稳定档", sub: "σ ≤ 5% · BTC 及更稳的(含低波动股票如GOLD)", tint: "tint-green",
      min: "STABLE_MARGIN_MIN_PCT", max: "STABLE_MARGIN_PCT", lev: "STABLE_LEV_CAP", notl: "STABLE_MIN_NOTIONAL", cap: "STABLE_COIN_CAP_PCT" },
    { key: "mid", label: "中档", sub: "σ 5–10% · ETH / SOL / HYPE 等主流", tint: "tint-amber",
      min: "MID_MARGIN_MIN_PCT", max: "MID_MARGIN_PCT", lev: "MID_LEV_CAP", notl: "MID_MIN_NOTIONAL", cap: "MID_COIN_CAP_PCT" },
    { key: "high", label: "剧烈档", sub: "σ ≥ 10% · ZEC / meme / 野币 / 高波股", tint: "tint-red",
      min: "HIGH_MARGIN_MIN_PCT", max: "HIGH_MARGIN_PCT", lev: "HIGH_LEV_CAP", notl: "HIGH_MIN_NOTIONAL", cap: "HIGH_COIN_CAP_PCT" },
  ];
  const tierKeys = new Set(TIER_GROUPS.flatMap(g => [g.min, g.max, g.lev, g.notl, g.cap]));
  const deployKeys = new Set(["DEPLOY_FULL_PCT", "MAX_DEPLOY_PCT"]);
  const validationBadKeys = new Set();
  const validationErrors = [];
  const numVal = k => Number(vals[k]);
  const markErr = (msg, keys) => {
    validationErrors.push(msg);
    (keys || []).forEach(k => validationBadKeys.add(k));
  };
  const validatePct = (label, key) => {
    const v = numVal(key);
    if (!Number.isFinite(v)) { markErr(`${label} 必须是数字`, [key]); return false; }
    if (v < 0 || v > 100) { markErr(`${label} 必须在 0–100% 之间`, [key]); return false; }
    return true;
  };
  if (tab === "follow") {
    TIER_GROUPS.forEach(g => {
      const okMin = validatePct(`${g.label}保证金下限`, g.min);
      const okMax = validatePct(`${g.label}保证金上限`, g.max);
      if (okMin && okMax && numVal(g.min) > numVal(g.max)) {
        markErr(`${g.label}保证金下限不能高于上限`, [g.min, g.max]);
      }
    });
    const okFull = validatePct("满火力占用线", "DEPLOY_FULL_PCT");
    const okLock = validatePct("组合部署上限", "MAX_DEPLOY_PCT");
    if (okFull && okLock && numVal("DEPLOY_FULL_PCT") >= numVal("MAX_DEPLOY_PCT")) {
      markErr("满火力占用线必须低于组合部署上限", ["DEPLOY_FULL_PCT", "MAX_DEPLOY_PCT"]);
    }
  }

  const RangeRow = (g) => {
    const pMin = byKey(g.min), pMax = byKey(g.max);
    if (!pMin || !pMax) return null;
    return (
      <div className={"prow range-row" + (dirty[g.min] || dirty[g.max] ? " dirty" : "") + (validationBadKeys.has(g.min) || validationBadKeys.has(g.max) ? " invalid" : "")}>
        <span className="lvl-dot lvl-green" />
        <div className="pn"><b>{g.label}·保证金区间</b></div>
        <div className="pd">低占用用上限,拥挤时线性缩到下限。自动调参只改上限</div>
        <div className="range-ctl">
          <EditableValue value={vals[g.min]} unit="%" ptype="pct" disabled={!editable(pMin)} onCommit={v => set(g.min, v)} />
          <span>至</span>
          <EditableValue value={vals[g.max]} unit="%" ptype="pct" disabled={!editable(pMax)} onCommit={v => set(g.max, v)} />
        </div>
      </div>
    );
  };

  const DeployRangeRow = () => {
    const pFull = byKey("DEPLOY_FULL_PCT"), pLock = byKey("MAX_DEPLOY_PCT");
    if (!pFull || !pLock) return null;
    return (
      <div className={"prow range-row deploy-row" + (dirty.DEPLOY_FULL_PCT || dirty.MAX_DEPLOY_PCT ? " dirty" : "") + (validationBadKeys.has("DEPLOY_FULL_PCT") || validationBadKeys.has("MAX_DEPLOY_PCT") ? " invalid" : "")}>
        <span className="lvl-dot lvl-green" />
        <div className="pn"><b>组合火力区间</b></div>
        <div className="pd">占用≤左值满火力;左值到右值线性缩仓;≥右值停开新仓,保留资金给加仓/平仓管理</div>
        <div className="range-ctl">
          <EditableValue value={vals.DEPLOY_FULL_PCT} unit="%" ptype="pct" disabled={!editable(pFull)} onCommit={v => set("DEPLOY_FULL_PCT", v)} />
          <span>至</span>
          <EditableValue value={vals.MAX_DEPLOY_PCT} unit="%" ptype="pct" disabled={!editable(pLock)} onCommit={v => set("MAX_DEPLOY_PCT", v)} />
        </div>
      </div>
    );
  };

  const apply = async () => {
    if (validationErrors.length) return;
    const body = {}; tabDirty.forEach(p => { body[p.key] = vals[p.key]; });
    const doIt = async () => {
      setSaving(true);                                  // 短暂全页 loading 代替右上角 tooltip
      const t0 = Date.now();
      const cat = tab === "add" ? "follow" : tab;              // 加仓参数在后端属 follow 类
      try { await api.patchParams(cat, body); } catch (_e) {}
      setDirty({});
      if (tab === "follow" || tab === "add") { try { await api.cmd("reload_params", {}); } catch (_e) {} }  // observer ~1.5s 内生效
      await new Promise(r => setTimeout(r, Math.max(0, 450 - (Date.now() - t0))));   // 让 loading 可感知
      setSaving(false);
      if (tab === "scanner") startRescan();             // 重采有自己的整页遮罩接管
    };
    if (tab === "scanner") confirm({ title: "应用并重采", danger: false, ok: "应用并重采", body: "采集参数改动需重采才生效,将立即触发全量重采。", onConfirm: doIt });
    else if (tabDirty.some(p => p.level === "yellow")) confirm({ title: "保存跟单参数", danger: false, ok: "保存",
      body: "包含谨慎级参数(影响每一笔新仓),确认即时生效?", onConfirm: doIt });
    else doIt();
  };

  // 恢复默认配置:把当前页所属类别(scanner / follow — add 属 follow)全部参数强制写回代码默认值,覆盖操作员修改。
  const resetDefaults = () => {
    const cat = tab === "add" ? "follow" : tab;
    const label = cat === "scanner" ? "钱包采集" : "跟单策略(含加仓)";
    confirm({
      title: "恢复默认配置", danger: true, ok: "恢复默认",
      body: `将把「${label}」全部参数强制恢复为代码默认值,覆盖你在此页的所有修改。不可撤销。`,
      onConfirm: async () => {
        setSaving(true);
        const t0 = Date.now();
        try { await fetch("/api/params/" + cat + "/reset", { method: "POST", headers: { Authorization: "Bearer " + api.token } }); } catch (_e) {}
        try { await loadParams(); setDirty({}); } catch (_e) {}   // 重取,把重置后的值刷回界面
        if (cat === "follow") { try { await api.cmd("reload_params", {}); } catch (_e) {} }   // observer ~1.5s 内生效
        await new Promise(r => setTimeout(r, Math.max(0, 450 - (Date.now() - t0))));
        setSaving(false);
        if (cat === "scanner") startRescan();                     // 采集默认值需重采才生效(重采有自己的整页遮罩)
      },
    });
  };

  return (
    <div className="content">
      {saving && <div className="mask"><span className="spin" style={{ width: 34, height: 34, borderWidth: 3 }} /><h2 style={{ marginTop: 22 }}>保存中…</h2></div>}
      <div className="tabs">
        <div className={"tab" + (tab === "scanner" ? " on" : "")} onClick={() => setTab("scanner")}>钱包采集参数</div>
        <div className={"tab" + (tab === "follow" ? " on" : "")} onClick={() => setTab("follow")}>跟单策略参数</div>
        <div className={"tab" + (tab === "add" ? " on" : "")} onClick={() => setTab("add")}>加仓策略</div>
        <button className="btn" title="把本页参数强制恢复为代码默认值" onClick={resetDefaults}
          style={{ marginLeft: "auto", alignSelf: "center", fontSize: 12, padding: "4px 12px" }}>↺ 恢复默认</button>
      </div>

      {tab === "follow" && <SizingPreview vals={vals} />}

      <div className="tbl-wrap">
        {tab === "add" && (() => {
          const bk = k => list.find(p => p.key === k);
          const smart = !!vals.SMART_ADD, bOpen = openTiers.B === undefined ? true : openTiers.B;
          const secLbl = t => <div className="muted" style={{ fontSize: 11, padding: "8px 0 2px", fontWeight: 600, color: "var(--t2)" }}>{t}</div>;
          return <React.Fragment>
            <div className="psec-h">加仓策略 · 独立于跟单/采集<span>目标加仓时:我们是否跟、跟多少、跟几次。逆向摊低是重点。</span></div>
            <div>
              <div className={"expand-head" + (openTiers.A ? " open" : "")} onClick={() => setOpenTiers(o => ({ ...o, A: !o.A }))}>
                <span style={{ color: "var(--t3)", width: 12 }}>{openTiers.A ? "▾" : "▸"}</span>
                <span className="pill tint-green">A · 正向加仓</span>
                <span className="muted" style={{ fontSize: 12 }}>盈利单顺势加仓、拉高成本追更大利润</span>
                {!openTiers.A && <span className="muted" style={{ marginLeft: "auto", fontSize: 11 }}>{vals.FOLLOW_POS_ADD ? "跟随" : "不跟(默认)"}</span>}
              </div>
              {openTiers.A && <div className="expand-body">
                {[bk("FOLLOW_POS_ADD")].filter(Boolean).map(Prow)}
                <div className="muted" style={{ fontSize: 11, padding: "2px 0 6px" }}>正向较简单:开启后按「比例镜像 + 硬顶 + 三档预算」跟,不用波动闸。</div>
              </div>}
            </div>
            <div>
              <div className={"expand-head" + (bOpen ? " open" : "")} onClick={() => setOpenTiers(o => ({ ...o, B: !(o.B === undefined ? true : o.B) }))}>
                <span style={{ color: "var(--t3)", width: 12 }}>{bOpen ? "▾" : "▸"}</span>
                <span className="pill tint-red">B · 逆向加仓(摊低)</span>
                <span className="muted" style={{ fontSize: 12 }}>目标逆势摊低成本 —— 我们如何跟(二选一)</span>
                {!bOpen && <span className="muted" style={{ marginLeft: "auto", fontSize: 11 }}>{smart ? "② 智能动态" : "① 分档硬cap"}</span>}
              </div>
              {bOpen && <div className="expand-body">
                {[bk("SMART_ADD")].filter(Boolean).map(Prow)}
                {smart ? <React.Fragment>
                  {secLbl("② 智能动态(σ波动闸 + 比例镜像)")}
                  {["ADD_GAP_K", "ADD_GAP_SHRINK_G", "ADD_MAX_HARD"].map(bk).filter(Boolean).map(Prow)}
                  <div className="muted" style={{ fontSize: 11, padding: "4px 0 6px" }}>加仓额封顶到该币「单币上限」剩余预算 —— 该上限是全局灾难闸,在「跟单策略参数 · 保证金与杠杆 σ分档」里调。</div>
                </React.Fragment> : <React.Fragment>
                  {secLbl("① 分档硬cap(固定次数 + 固定比例)")}
                  {["ADD_FRAC", "STABLE_MAX_ADDS", "MID_MAX_ADDS", "HIGH_MAX_ADDS"].map(bk).filter(Boolean).map(Prow)}
                </React.Fragment>}
              </div>}
            </div>
          </React.Fragment>;
        })()}
        {tab !== "add" && list.filter(p => !(tab === "follow" && (tierKeys.has(p.key) || deployKeys.has(p.key) || ADD_KEYS.has(p.key) || p.key === AUTO_TUNE_KEY || p.key === BLACKLIST_KEY))).map(p => {
          if (tab === "follow" && p.key === "MIN_FOLLOW_SCORE") {
            const v = Number(vals.MIN_FOLLOW_SCORE);
            const n = scoreDist ? scoreDist.scores.filter(s => s >= v).length : null;
            return (
              <React.Fragment key={p.key}>
                {Prow(p)}
                <div className="score-hint">
                  {n == null ? "加载钱包分布…" : <React.Fragment>
                    评分 ≥ <b>{isFinite(v) ? v : "—"}</b> 时,当前 watchlist 有 <b style={{ color: "var(--accent)" }}>{n}</b> 个钱包达标会被跟单
                    <span className="muted"> / 共 {scoreDist.total} 个候选</span></React.Fragment>}
                </div>
                {blacklistParam && <CoinBlacklistEditor key={blacklistParam.key} param={blacklistParam}
                  value={vals[BLACKLIST_KEY]} dirty={!!dirty[BLACKLIST_KEY]} disabled={!editable(blacklistParam)}
                  onCommit={v => set(BLACKLIST_KEY, v)} />}
              </React.Fragment>
            );
          }
          return Prow(p);
        })}
        {tab === "follow" && <div className="psec-h psec-h-row">
          <div className="psec-title-block">保证金与杠杆 · 按波动率 σ 分档
            <span>杠杆 = σ 所在档位的上限(σ 定档),这里设各档的单笔保证金% 与杠杆上限</span></div>
          {autoTuneParam && <div className={"psec-switch" + (dirty[AUTO_TUNE_KEY] ? " dirty" : "")} title={autoTuneParam.desc}>
            <span>自动调保证金</span>
            <div className={"toggle " + (vals[AUTO_TUNE_KEY] ? "on" : "")}
              onClick={() => editable(autoTuneParam) && set(AUTO_TUNE_KEY, !vals[AUTO_TUNE_KEY])}
              style={{ opacity: editable(autoTuneParam) ? 1 : .5 }}><div className="knob" /></div>
          </div>}
        </div>}
        {tab === "follow" && DeployRangeRow()}
        {tab === "follow" && validationErrors.length > 0 && (
          <div className="param-errors">
            {validationErrors.map((e, i) => <div key={i}>{e}</div>)}
          </div>
        )}
        {tab === "follow" && TIER_GROUPS.map(g => {
          const open = openTiers[g.key];
          const rows = [g.lev, g.notl, g.cap].map(byKey).filter(Boolean);
          return (
            <div key={g.key}>
              <div className={"expand-head" + (open ? " open" : "")} onClick={() => setOpenTiers(o => ({ ...o, [g.key]: !o[g.key] }))}>
                <span style={{ color: "var(--t3)", width: 12 }}>{open ? "▾" : "▸"}</span>
                <span className={"pill " + g.tint}>{g.label}</span>
                <span className="muted" style={{ fontSize: 12 }}>{g.sub}</span>
                {!open && <span className="muted" style={{ marginLeft: "auto", fontSize: 11 }}>
                  保证金 {fParam(vals[g.min], "pct")}–{fParam(vals[g.max], "pct")}% · 杠杆 ≤{fParam(vals[g.lev], "x")}x · 最低 ${fParam(vals[g.notl], "usd")} · 单币上限 {fParam(vals[g.cap], "pct")}%</span>}
              </div>
              {open && <div className="expand-body">
                {RangeRow(g)}
                {rows.map(Prow)}
              </div>}
            </div>
          );
        })}
      </div>

      {tabDirty.length > 0 && (
        <div className="apply-bar">
          <div className="ab-l">{tabDirty.length} 项未应用改动{tab === "scanner" ? "(需重采生效)" : "(即时生效)"}</div>
          <div style={{ display: "flex", gap: 10 }}>
            <button className="btn" onClick={() => { setVals(v => { const nv = { ...v }; const o = {}; [...params.scanner, ...params.follow].forEach(x => o[x.key] = x.value); tabDirty.forEach(p => nv[p.key] = o[p.key]); return nv; }); setDirty({}); }}>放弃</button>
            <button className="btn btn-accent" disabled={validationErrors.length > 0} onClick={apply}>{tab === "scanner" ? "应用并重采" : "保存(即时生效)"}</button>
          </div>
        </div>
      )}
    </div>
  );
}
