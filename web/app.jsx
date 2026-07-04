/* 跟单监控台 — build-free React preview (babel-standalone). Talks to the live dashboard API.
   Pages: Overview / Positions / Wallets (P0). Discovery / Settings are stubbed for now. */
const { useState, useEffect, useRef, useCallback } = React;

const DASH_USER = "admin";                       // preview auto-login (matches launch.json env)
const DASH_PW = "mock123";
const TOK_KEY = "hl_dash_token";

/* ----------------------------------------------------------------- api */
const api = {
  token: localStorage.getItem(TOK_KEY) || null,
  async login(username, pw) {
    const r = await fetch("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password: pw }) });
    if (!r.ok) throw new Error("login_failed");
    const d = await r.json();
    api.token = d.token; localStorage.setItem(TOK_KEY, d.token); return d;
  },
  async get(path) {
    const r = await fetch(path, { headers: { Authorization: "Bearer " + api.token } });
    if (r.status === 401) { api.token = null; localStorage.removeItem(TOK_KEY); throw new Error("unauth"); }
    return (await r.json()).data;
  },
  async cmd(type, payload) {
    const r = await fetch("/api/commands", {
      method: "POST", headers: { Authorization: "Bearer " + api.token },
      body: JSON.stringify({ type, payload }) });
    return r.json();
  },
};

/* ----------------------------------------------------------------- format */
const fUsd = (v, d = 0) => (v == null ? "—" : "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
const fSign = (v, d = 0) => (v == null ? "—" : (v >= 0 ? "+" : "") + Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
const fTime = (ep) => (ep == null ? "—" : new Date(ep * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }));  // epoch(UTC) -> UTC+8
const fPct = (v, d = 1) => (v == null ? "—" : (v >= 0 ? "+" : "") + Number(v).toFixed(d) + "%");
const fNum = (v, d = 1) => (v == null ? "—" : Number(v).toFixed(d));
// price formatter with magnitude-adaptive decimals (BTC 60528 vs S 0.02221 vs FARTCOIN 0.1317)
const fPrice = (v) => {
  if (v == null) return "—";
  const a = Math.abs(Number(v));
  if (a === 0) return "0";
  if (a >= 1000) return Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (a >= 1) return Number(v).toFixed(2);
  if (a >= 0.01) return Number(v).toFixed(4);
  if (a >= 0.0001) return Number(v).toFixed(6);
  return Number(v).toPrecision(3);
};
// duration: seconds for scalps, minutes/hours/days as it grows (was always "X.Xh" -> 5s showed "0.0h")
const fDur = (s) => {
  if (s == null) return "—";
  if (s < 60) return Math.round(s) + "s";
  if (s < 3600) return (s / 60).toFixed(s < 600 ? 1 : 0) + "m";
  if (s < 86400) return (s / 3600).toFixed(1) + "h";
  return (s / 86400).toFixed(1) + "d";
};
const short = (a) => (a ? a.slice(0, 6) + "…" + a.slice(-4) : "—");
const cls = (v) => (v == null ? "" : v >= 0 ? "up" : "down");
const agoText = (iso) => {
  if (!iso) return "—";
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return s + "s 前";
  if (s < 3600) return Math.floor(s / 60) + "m 前";
  if (s < 86400) return Math.floor(s / 3600) + "h 前";
  return Math.floor(s / 86400) + "d 前";
};
const SCANNER_LABEL = { rolling: "滚动采集中", scanning: "采集扫描中", idle: "空闲", stopped: "已停止", unknown: "未上报" };
const scannerColor = (mode, stale) => {
  if (mode === "scanning") return "var(--amber)";
  if (mode === "rolling" && !stale) return "var(--green-l)";
  if (mode === "idle" && !stale) return "var(--t2)";        // healthy, waiting for next 6h cycle
  return "var(--red-l)";                                     // stopped / unknown / stale
};

/* ----------------------------------------------------------------- icons */
const Ico = ({ d }) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d={d} /></svg>;
const IC = {
  overview: "M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z",
  positions: "M3 3v18h18M7 16l4-4 3 3 5-6",
  history: "M3 3v5h5M3.05 13A9 9 0 1 0 6 5.3L3 8M12 7v5l4 2",
  wallets: "M3 7h18v12H3zM3 7l2-3h14l2 3M16 13h2",
  discovery: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zm10 2-4.3-4.3",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12a7 7 0 0 0-.1-1l2-1.6-2-3.4-2.4 1a7 7 0 0 0-1.7-1l-.4-2.6H9.6l-.4 2.6a7 7 0 0 0-1.7 1l-2.4-1-2 3.4L5.1 11a7 7 0 0 0 0 2l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 1.7 1l.4 2.6h4.8l.4-2.6a7 7 0 0 0 1.7-1l2.4 1 2-3.4-2-1.6a7 7 0 0 0 .1-1z",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
  bolt: "M13 2 3 14h7l-1 8 10-12h-7z",
  close: "M18 6 6 18M6 6l12 12",
};

/* ----------------------------------------------------------------- equity chart */
function EquityChart({ points }) {
  const W = 920, H = 230, PAD = 8;
  if (!points || points.length < 2) return <div className="loading">暂无权益数据</div>;
  const eqs = points.map(p => p.equity);
  const min = Math.min(...eqs), max = Math.max(...eqs), rng = max - min || 1;
  const X = i => PAD + i / (points.length - 1) * (W - 2 * PAD);
  const Y = v => PAD + (1 - (v - min) / rng) * (H - 2 * PAD);
  let line = "";
  points.forEach((p, i) => { line += (i ? " L" : "M") + X(i).toFixed(1) + " " + Y(p.equity).toFixed(1); });
  const area = line + ` L${X(points.length - 1).toFixed(1)} ${H - PAD} L${X(0).toFixed(1)} ${H - PAD} Z`;
  const last = points[points.length - 1];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" preserveAspectRatio="none" style={{ display: "block" }}>
      <defs>
        <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="rgba(255,106,43,0.35)" />
          <stop offset="100%" stopColor="rgba(255,106,43,0)" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#eqfill)" />
      <path d={line} fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinejoin="round" />
      <circle cx={X(points.length - 1)} cy={Y(last.equity)} r="4" fill="#fff" stroke="var(--accent)" strokeWidth="2" />
    </svg>
  );
}

/* ----------------------------------------------------------------- modal / confirm */
function Confirm({ cfg, onClose }) {
  const [pct, setPct] = useState(100);
  useEffect(() => { if (cfg && cfg.pctPicker) setPct(100); }, [cfg]);   // reset to default (100%) each open
  if (!cfg) return null;
  const pick = cfg.pctPicker;
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <h3>{cfg.title}</h3>
        <p>{cfg.body}</p>
        {pick && <div style={{ margin: "4px 0 2px" }}>
          <div className="close-pop-row">
            {[25, 50, 75, 100].map(v => (
              <button key={v} className={"pct-chip" + (pct === v ? " on" : "")} onClick={() => setPct(v)}>{v}%</button>
            ))}
          </div>
          <p style={{ marginTop: 8 }}>平掉 {pct}% ≈ <b>{fUsd((pick.notional || 0) * pct / 100)}</b> 名义额</p>
        </div>}
        <div className="modal-row">
          <button className="btn" onClick={onClose}>取消</button>
          <button className={"btn " + (cfg.danger ? "btn-danger" : "btn-accent")}
            onClick={() => { cfg.onConfirm(pick ? pct / 100 : undefined); onClose(); }}>
            {pick ? `平仓 ${pct}%` : (cfg.ok || "确认")}</button>
        </div>
      </div>
    </div>
  );
}

/* ----------------------------------------------------------------- overview */
function Overview({ ov }) {
  const [range, setRange] = useState("7d");
  const [eq, setEq] = useState(null);
  const [ins, setIns] = useState(null);
  useEffect(() => { api.get("/api/equity?range=" + range).then(setEq).catch(() => {}); }, [range]);
  useEffect(() => { const f = () => api.get("/api/insights").then(setIns).catch(() => {}); f(); const t = setInterval(f, 15000); return () => clearInterval(t); }, []);
  if (!ov) return <div className="loading">加载中…</div>;
  const r = ov.risk, f = ov.fees;
  return (
    <div className="content">
      <div className="grid4">
        <div className="card">
          <div className="card-lbl">总权益</div>
          <div className="kpi">{fUsd(ov.equity, 0)}</div>
          <div className="kpi-sub"><span className={cls(ov.roiPct)}>ROI {fPct(ov.roiPct)}</span>
            <span className={cls(ov.todayPct)}>今日 {fPct(ov.todayPct)}</span></div>
        </div>
        <div className="card">
          <div className="card-lbl">已实现 / 未实现</div>
          <div className="kpi">{fUsd(ov.realizedPnl, 0)}</div>
          <div className="kpi-sub"><span className={cls(ov.unrealizedPnl)}>浮动 {fSign(ov.unrealizedPnl, 0)}</span></div>
        </div>
        <div className="card">
          <div className="card-lbl">胜率 / 在持</div>
          <div className="kpi">{fNum(ov.winRatePct, 1)}%</div>
          <div className="kpi-sub"><span>{ov.openCount} 笔在持</span></div>
        </div>
        <div className="card">
          <div className="card-lbl">可动用余额</div>
          <div className="kpi">{fUsd(ov.availableBalance, 0)}</div>
          <div className="kpi-sub"><span>占权益 {fNum(ov.availablePctOfEquity, 0)}%</span></div>
        </div>
      </div>

      {/* left: equity curve (half width) | right: merged 持仓敞口 + 手续费 — fits without vertical scroll */}
      <div className="grid2" style={{ marginTop: 14, alignItems: "stretch" }}>
        <div className="card chart-card" style={{ marginTop: 0, display: "flex", flexDirection: "column" }}>
          <div className="section-h" style={{ margin: "0 0 8px" }}>
            <h2>权益曲线</h2>
            <div className="range-tabs">
              {["1d", "7d", "all"].map(x => <button key={x} className={range === x ? "on" : ""} onClick={() => setRange(x)}>{x.toUpperCase()}</button>)}
            </div>
          </div>
          <div style={{ flex: 1, display: "flex", alignItems: "center" }}><EquityChart points={eq && eq.points} /></div>
        </div>

        <div className="card">
          <div className="card-lbl">持仓敞口</div>
          <div style={{ display: "flex", gap: 24, margin: "12px 0 14px", flexWrap: "wrap" }}>
            <div title="所有在持仓位的名义额相加(多+空),衡量你在市场上铺了多大的盘">
              <div className="muted">总持仓规模</div><div className="mono" style={{ fontSize: 18 }}>{fUsd(r.gross)}</div>
              <div className="muted" style={{ fontSize: 10 }}>多+空 名义额</div></div>
            <div title="多头名义额 − 空头名义额。正=整体偏多,负=偏空">
              <div className="muted">净方向</div><div className="mono" style={{ fontSize: 18 }}>{fUsd(r.net)}</div>
              <div className="muted" style={{ fontSize: 10 }}>{r.net > 50 ? "整体偏多" : r.net < -50 ? "整体偏空" : "多空均衡"}</div></div>
            <div title="净敞口 ÷ 总持仓。越接近 0 = 多空越对冲、方向风险越低;越接近 ±100% = 越单边重押">
              <div className="muted">方向性</div><div className="mono" style={{ fontSize: 18 }}>{fNum(r.netGrossRatioPct, 0)}%</div>
              <div className="muted" style={{ fontSize: 10 }}>{Math.abs(r.netGrossRatioPct) < 25 ? "多空基本对冲" : Math.abs(r.netGrossRatioPct) < 60 ? "略偏单边" : "明显单边"}</div></div>
          </div>
          <div className="bar-row"><div className="bl">多头</div>
            <div className="bar-track"><div className="bar-fill" style={{ width: r.longPct + "%", background: "var(--green)" }} /></div>
            <div className="bv">{fNum(r.longPct, 0)}%</div></div>
          <div className="bar-row"><div className="bl">空头</div>
            <div className="bar-track"><div className="bar-fill" style={{ width: r.shortPct + "%", background: "var(--red)" }} /></div>
            <div className="bv">{fNum(r.shortPct, 0)}%</div></div>

          <div style={{ borderTop: "1px solid var(--glass-border)", marginTop: 16, paddingTop: 14 }}>
            <div className="card-lbl">手续费 / 赚钱效率</div>
            <div style={{ display: "flex", gap: 40, marginTop: 12 }}>
              <div title="至今所有跟单成交累计付出的手续费">
                <div className="muted">累计手续费</div><div className="mono" style={{ fontSize: 20, marginTop: 4 }}>{fUsd(f.cumulative, 0)}</div></div>
              <div title="净利润 ÷ 总成交额。bp=基点=万分之一,16.7bp=0.167%,即每成交 $1万 净赚约 $16.7">
                <div className="muted">成交净赚率</div><div className="mono" style={{ fontSize: 20, marginTop: 4 }}>{fNum(f.netPerGrossBp, 1)} bp</div>
                <div className="muted" style={{ fontSize: 10 }}>≈每 $1万 成交净赚 ${fNum(f.netPerGrossBp, 1)}</div></div>
            </div>
          </div>
        </div>
      </div>

      {/* forward-performance breakdowns: which followed wallets / coins actually earn us money */}
      <div className="grid2" style={{ marginTop: 14, alignItems: "stretch" }}>
        <div className="card">
          <div className="card-lbl" style={{ marginBottom: 8 }}>跟单钱包贡献榜 <span className="muted">· 实盘净盈亏(已实现+浮动)</span></div>
          {!ins ? <div className="loading">加载中…</div> : ins.walletContrib.length === 0 ? <div className="empty">暂无</div> : (
            <div className="tbl-wrap"><table>
              <thead><tr><th>#</th><th>地址</th><th className="num">净盈亏</th><th className="num">实盘胜率</th><th className="num">笔数</th></tr></thead>
              <tbody>{ins.walletContrib.map(w => (
                <tr key={w.address}>
                  <td>{w.rank != null ? <span className="rankbadge">{w.rank}</span> : <span className="tint tint-gray">脱榜</span>}</td>
                  <td className="addr">{short(w.address)}</td>
                  <td className={"num " + cls(w.netPnl)}>{fSign(w.netPnl, 1)}</td>
                  <td className="num">{w.winRatePct != null ? fNum(w.winRatePct, 0) + "%" : "—"}</td>
                  <td className="num">{w.closedN}</td>
                </tr>))}</tbody>
            </table></div>)}
        </div>
        <div className="card">
          <div className="card-lbl" style={{ marginBottom: 8 }}>币种盈亏 <span className="muted">· 实盘净盈亏</span></div>
          {!ins ? <div className="loading">加载中…</div> : ins.coinPnl.length === 0 ? <div className="empty">暂无</div> : (
            <div className="tbl-wrap"><table>
              <thead><tr><th>币种</th><th className="num">净盈亏</th><th className="num">笔数</th></tr></thead>
              <tbody>{ins.coinPnl.map(c => (
                <tr key={c.coin}>
                  <td><b>{c.coin}</b></td>
                  <td className={"num " + cls(c.netPnl)}>{fSign(c.netPnl, 1)}</td>
                  <td className="num">{c.n}</td>
                </tr>))}</tbody>
            </table></div>)}
        </div>
      </div>
    </div>
  );
}

/* ----------------------------------------------------------------- positions */
function Positions({ confirm, toast, streamOpen }) {
  const [polledOpen, setPolledOpen] = useState(null);
  const [closing, setClosing] = useState({});        // positionId -> true while its 平仓 command is in flight
  const [filter, setFilter] = useState("all");
  const [opage, setOpage] = useState(0);             // open positions page (20/page)
  const [pnlSort, setPnlSort] = useState(null);      // null = 默认(新开在前) | "asc" 浮亏在前 | "desc" 浮盈在前
  const [expandedId, setExpandedId] = useState(null); // pos_id expanded to its detail
  const [details, setDetails] = useState({});
  const toggleRow = (rowId) => {
    const pid = Number(String(rowId).replace("pos_", ""));
    if (expandedId === pid) { setExpandedId(null); return; }
    setExpandedId(pid);
    if (!details[pid]) api.get(`/api/positions/${pid}`).then(d => setDetails(m => ({ ...m, [pid]: d }))).catch(() => {});
  };
  const open = streamOpen || polledOpen;             // prefer the SSE stream for open positions
  // 浮动盈亏 表头点击循环:默认(新开在前) → 浮亏在前 → 浮盈在前 → 默认
  const cyclePnlSort = () => { setPnlSort(d => d === null ? "asc" : d === "asc" ? "desc" : null); setOpage(0); };
  const loadOpen = useCallback(() => { api.get("/api/positions?status=open").then(setPolledOpen).catch(() => {}); }, []);
  const load = loadOpen;                              // doClose refreshes the open list after a manual close
  // open positions come from the SSE stream; fallback-poll only when the stream isn't delivering.
  useEffect(() => {
    if (!streamOpen) { loadOpen(); var to = setInterval(loadOpen, 6000); }
    return () => { if (to) clearInterval(to); };
  }, [loadOpen, streamOpen]);

  const doClose = (p) => confirm({
    title: "手动平仓", danger: true,
    body: `平掉 ${p.coin} ${p.side === "long" ? "多" : "空"}(当前名义额 ${fUsd(p.notional)})。选择平仓比例(默认100%),不可撤销。`,
    pctPicker: { notional: p.notional },
    onConfirm: async (frac = 1) => {                  // button shows inline loading until done — no toast
      const pid = Number(p.id.replace("pos_", ""));
      setClosing(c => ({ ...c, [pid]: true }));
      try { await api.cmd("close_position", { positionId: pid, fraction: frac }); } catch (_e) {}
      await new Promise(r => setTimeout(r, 1800));
      load();
      setClosing(c => { const m = { ...c }; delete m[pid]; return m; });
    },
  });

  const filt = (rows) => !rows ? [] : rows.filter(p =>
    filter === "all" ? true : filter === "crypto" ? p.marketType === "crypto" :
    filter === "stock" ? p.marketType === "stock" : filter === "long" ? p.side === "long" : p.side === "short");

  const OPER = 20;
  let openRows = open ? filt(open.positions) : [];   // API delivers newest-first; that's the default order
  if (pnlSort) openRows = [...openRows].sort((a, b) =>
    pnlSort === "asc" ? (a.unrealizedPnl - b.unrealizedPnl) : (b.unrealizedPnl - a.unrealizedPnl));
  const opages = Math.max(1, Math.ceil(openRows.length / OPER));
  const opg = Math.min(opage, opages - 1);
  const openItems = openRows.slice(opg * OPER, opg * OPER + OPER);

  return (
    <div className="content">
      <div className="section-h" style={{ marginTop: 6 }}>
        <h2>当前持仓 {open && <span className="muted">· 浮动 <span className={cls(open.summary.floatingPnl)}>{fSign(open.summary.floatingPnl, 1)}</span> · {open.summary.openCount} 笔</span>}</h2>
        <div className="range-tabs">
          {[["all", "全部"], ["crypto", "Crypto"], ["stock", "股票"], ["long", "多"], ["short", "空"]].map(([k, l]) =>
            <button key={k} className={filter === k ? "on" : ""} onClick={() => setFilter(k)}>{l}</button>)}
        </div>
      </div>
      <div className="tbl-wrap">
        <table>
          <thead><tr>
            <th>币种</th><th>方向</th><th className="num">入场/杠杆</th><th className="num">名义额</th>
            <th className="num">现价</th>
            <th className="num sortable" onClick={cyclePnlSort} title="点击按浮动盈亏排序(浮亏在前 / 浮盈在前 / 默认新开在前)">
              浮动盈亏 <span className={"sort-ind" + (pnlSort ? " active" : "")}>{pnlSort === "asc" ? "▲" : pnlSort === "desc" ? "▼" : "⇅"}</span>
            </th>
            <th>钱包</th><th className="num">lag</th><th className="num">爆仓价</th><th></th>
          </tr></thead>
          <tbody>
            {open === null && <tr><td colSpan="10" className="loading">加载中…</td></tr>}
            {open && openRows.length === 0 && <tr><td colSpan="10" className="empty">无持仓</td></tr>}
            {openItems.map(p => { const pid = Number(String(p.id).replace("pos_", "")); const isOpen = expandedId === pid;
              return <React.Fragment key={p.id}>
              <tr onClick={() => toggleRow(p.id)} style={{ cursor: "pointer" }} className={isOpen ? "row-open" : ""}>
                <td><span className="row-caret" style={{ transform: isOpen ? "rotate(90deg)" : "none" }}>▸</span> <span className="tint tint-gray">{p.marketType === "stock" ? "股" : "币"}</span> <b>{p.coin}</b>
                  <a className="ext-link" href={"https://app.hyperliquid.xyz/trade/" + p.coin}
                     target="_blank" rel="noopener noreferrer" title="在 Hyperliquid 看K线" onClick={e => e.stopPropagation()}>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" /><polyline points="15 3 21 3 21 9" /><line x1="10" y1="14" x2="21" y2="3" /></svg></a>
                  {p.addCount > 0 && <span className="tint tint-gray" style={{ marginLeft: 8 }} title="目标加仓、我们跟进的次数(上限2)">加仓{p.addCount}</span>}</td>
                <td><span className={"tint " + (p.side === "long" ? "tint-green" : "tint-red")}>{p.side === "long" ? "多" : "空"}</span></td>
                <td className="num">{fPrice(p.entry)} · {fNum(p.leverage, 0)}x
                  <div className="muted" title="源(目标钱包)的加权均价(随其加仓更新)· 杠杆">源 {fPrice(p.masterEntry)} · {fNum(p.masterLeverage, 0)}x</div></td>
                <td className="num">{fUsd(p.notional)}
                  <div className="muted" title="源(目标钱包)这一单的名义额(我们 ≤ 它)">源 {fUsd(p.masterNotional)}</div></td>
                <td className="num">{fPrice(p.mark)}</td>
                <td className={"num " + cls(p.unrealizedPnl)}>{fSign(p.unrealizedPnl, 1)}<div className="muted">{fPct(p.unrealizedPctOfMargin, 0)} 保证金</div></td>
                <td className="addr">{short(p.wallet)} {p.followPos != null
                  ? <span className="rankbadge" title={"跟单序号" + (p.walletRank ? " · 全站评分#" + p.walletRank : "")}>#{p.followPos}</span>
                  : <span className="tint tint-gray" title="当前不在跟单集(仅平仓)">脱榜</span>}</td>
                <td className="num" title="跟单延迟:目标开仓 → 我们检测并跟开的秒数(旧仓未记录显示 —)">{p.lagSec != null ? fNum(p.lagSec, 1) + "s" : "—"}</td>
                <td className={"num " + (p.liqDistancePct != null && p.liqDistancePct > -8 ? "down" : "")} title="距现价多少就触发强平">{fPrice(p.liqPx)}
                  {p.liqDistancePct != null && <div className="muted">差 {fNum(Math.abs(p.liqDistancePct), 1)}%</div>}</td>
                <td>{(() => { const busy = closing[Number(p.id.replace("pos_", ""))];
                  return <button className="btn btn-stop btn-sm" disabled={busy} onClick={e => { e.stopPropagation(); doClose(p); }}>
                    {busy ? <><span className="spin" />平仓中</> : <><Ico d={IC.close} />平仓</>}</button>; })()}</td>
              </tr>
              {isOpen && <tr className="detail-row"><td colSpan="10"><PositionDetail d={details[pid]} /></td></tr>}
              </React.Fragment>; })}
          </tbody>
        </table>
      </div>
      {openRows.length > OPER && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 12, marginTop: 10 }}>
          <button className="btn" disabled={opg <= 0} onClick={() => setOpage(opg - 1)}>上一页</button>
          <span className="muted mono">第 {opg + 1} / {opages} 页 · 共 {openRows.length} 笔</span>
          <button className="btn" disabled={opg >= opages - 1} onClick={() => setOpage(opg + 1)}>下一页</button>
        </div>
      )}
    </div>
  );
}

/* ----------------------------------------------------------------- history (closed positions + stats) */
const CLOSE_TYPE = { mirror: { label: "镜像", tint: "tint-blue" }, stop: { label: "止损", tint: "tint-amber" }, liq: { label: "爆仓", tint: "tint-red" } };
const ACT_TINT = { 开仓: "tint-green", 加仓: "tint-blue", 减仓: "tint-amber", 平仓: "tint-gray" };
function PositionDetail({ d }) {
  if (!d) return <div className="muted" style={{ padding: "14px 16px" }}>加载中…</div>;
  const live = d.status === "open";
  const pnl = live ? d.unrealizedPnl : d.realizedPnl;
  return (
    <div className="pos-detail">
      <div className="pos-detail-sum">
        <span>目标加仓 <b>{d.masterAdds}</b> 次 · 我们跟 <b>{d.ourAdds}</b> 次</span>
        <span>目标成本均价 <b>{fPrice(d.masterEntry)}</b></span>
        <span>我方成本均价 <b>{fPrice(d.ourEntry)}</b> · {fNum(d.ourLeverage, 0)}x</span>
        <span>我方投入保证金 <b>{fUsd(d.ourMargin)}</b></span>
        <span>{live ? "浮动" : "已实现"}盈亏 <b className={cls(pnl)}>{fSign(pnl, 1)}</b></span>
      </div>
      <div className="muted" style={{ fontSize: 11, margin: "2px 0 5px" }}>我们的成交记录:</div>
      <table className="fills-tbl">
        <thead><tr><th>时间</th><th>动作</th><th className="num">价格</th><th className="num">本金</th><th className="num">数量</th><th className="num">盈亏</th></tr></thead>
        <tbody>
          {d.fills.length === 0 && <tr><td colSpan="6" className="muted" style={{ padding: "6px 8px" }}>暂无成交</td></tr>}
          {d.fills.map((f, i) => (
            <tr key={i}>
              <td className="mono muted">{fTime(f.atSec)}</td>
              <td><span className={"tint " + (ACT_TINT[f.actionLabel] || "tint-gray")}>{f.actionLabel}</span>
                {f.fillCount > 1 && <span className="muted" style={{ marginLeft: 4, fontSize: 10 }} title="该订单分多笔成交">×{f.fillCount}</span>}</td>
              <td className="num">{fPrice(f.px)}</td>
              <td className="num">{fUsd(f.margin)}</td>
              <td className="num muted">{fNum(f.qty, 2)}</td>
              <td className={"num " + (f.pnl != null ? cls(f.pnl) : "")}>{f.pnl != null ? fSign(f.pnl, 1) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function History() {
  const [data, setData] = useState(null);
  const [filter, setFilter] = useState("all");        // all | win | loss
  const [ctype, setCtype] = useState("all");          // all | mirror | stop | liq
  const [page, setPage] = useState(0);                // 25/page
  const [expandedId, setExpandedId] = useState(null); // pos_id of the row expanded to fill-by-fill detail
  const [details, setDetails] = useState({});         // pos_id -> detail payload (lazy-fetched, cached)
  const toggleRow = (rowId) => {
    const pid = Number(String(rowId).replace("cls_", ""));
    if (expandedId === pid) { setExpandedId(null); return; }
    setExpandedId(pid);
    if (!details[pid]) api.get(`/api/positions/${pid}`).then(d => setDetails(m => ({ ...m, [pid]: d }))).catch(() => {});
  };
  useEffect(() => {
    const load = () => api.get("/api/positions?status=closed").then(setData).catch(() => {});
    load(); const t = setInterval(load, 15000); return () => clearInterval(t);
  }, []);
  const PER = 25;
  const st = data && data.stats;
  const all = (data && data.positions) || [];
  const rows = all.filter(p => (filter === "all" || p.result === filter) && (ctype === "all" || p.closeType === ctype));
  const pages = Math.max(1, Math.ceil(rows.length / PER));
  const pg = Math.min(page, pages - 1);
  const items = rows.slice(pg * PER, pg * PER + PER);
  return (
    <div className="content">
      <div className="section-h" style={{ marginTop: 6 }}>
        <h2>历史持仓 {st && <span className="muted">· 累计 {st.total} 笔已平仓</span>}</h2>
      </div>
      {data === null ? <div className="loading">加载中…</div> : st && st.total === 0 ? <div className="empty">暂无已平仓记录</div> : (
        <React.Fragment>
          <div className="grid4">
            <div className="card">
              <div className="card-lbl">胜率</div>
              <div className="kpi">{fNum(st.winRatePct, 1)}%</div>
              <div className="kpi-sub"><span className="up">{st.wins} 胜</span><span className="down">{st.losses} 负</span></div>
            </div>
            <div className="card">
              <div className="card-lbl">累计已实现盈亏</div>
              <div className={"kpi " + cls(st.totalPnl)}>{fSign(st.totalPnl, 0)}</div>
              <div className="kpi-sub"><span>平均每笔 <span className={cls(st.avgPnl)}>{fSign(st.avgPnl, 1)}</span></span></div>
            </div>
            <div className="card">
              <div className="card-lbl">盈利因子</div>
              <div className="kpi">{st.profitFactor == null ? "∞" : fNum(st.profitFactor, 2)}</div>
              <div className="kpi-sub"><span>总盈 ÷ 总亏(&gt;1 为正期望)</span></div>
            </div>
            <div className="card">
              <div className="card-lbl">平均持仓时长</div>
              <div className="kpi">{fDur(st.avgHoldSec)}</div>
              <div className="kpi-sub"><span>平均盈 <span className="up">{fSign(st.avgWin, 0)}</span> · 亏 <span className="down">{fSign(st.avgLoss, 0)}</span></span></div>
            </div>
          </div>
          <div className="section-h" style={{ marginTop: 16 }}>
            <h2>明细 <span className="muted">· 最近 {all.length} 笔 · 最佳 <span className="up">{fSign(st.bestPnl, 0)}</span> / 最差 <span className="down">{fSign(st.worstPnl, 0)}</span></span></h2>
            <div className="hfilters">
              <select className="fdrop" value={filter} onChange={e => { setFilter(e.target.value); setPage(0); }}>
                <option value="all">盈亏 · 全部</option><option value="win">仅盈利</option><option value="loss">仅亏损</option>
              </select>
              <select className="fdrop" value={ctype} onChange={e => { setCtype(e.target.value); setPage(0); }}>
                <option value="all">平仓类型 · 全部</option><option value="mirror">镜像跟随</option><option value="stop">主动止损</option><option value="liq">爆仓</option>
              </select>
            </div>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead><tr><th>币种</th><th>方向</th><th className="num">源入场/杠杆</th><th className="num">入场/杠杆</th><th>结算类型</th><th className="num">名义额</th><th className="num">已实现盈亏</th><th className="num">持仓时长</th><th>平仓时间</th><th>钱包</th></tr></thead>
              <tbody>
                {rows.length === 0 && <tr><td colSpan="10" className="empty">暂无</td></tr>}
                {items.map(p => { const pid = Number(String(p.id).replace("cls_", "")); const isOpen = expandedId === pid;
                  return <React.Fragment key={p.id}>
                  <tr onClick={() => toggleRow(p.id)} style={{ cursor: "pointer" }} className={isOpen ? "row-open" : ""}>
                    <td><span className="row-caret" style={{ transform: isOpen ? "rotate(90deg)" : "none" }}>▸</span> <b>{p.coin}</b>
                      {p.addCount > 0 && <span className="tint tint-gray" style={{ marginLeft: 8 }} title="目标加仓、我们跟进的次数">加仓{p.addCount}</span>}</td>
                    <td><span className={"tint " + (p.side === "long" ? "tint-green" : "tint-red")}>{p.side === "long" ? "多" : "空"}</span></td>
                    <td className="num muted" title="源(目标钱包)的加权均价(随其加仓更新)· 杠杆">{fPrice(p.masterEntry)} · {fNum(p.masterLeverage, 0)}x</td>
                    <td className="num" title="我们的加权均价 · 杠杆">{fPrice(p.entry)} · {fNum(p.leverage, 0)}x</td>
                    <td>{(() => { const t = CLOSE_TYPE[p.closeType] || CLOSE_TYPE.mirror; return <span className={"tint " + t.tint}>{t.label}</span>; })()}
                      <div className="muted" style={{ fontSize: 11 }} title="我们实际平仓价(止损=被砍价 / 镜像=跟随目标平仓价)">@ {fPrice(p.closePx)}</div></td>
                    <td className="num">{fUsd(p.notional)}
                      <div className="muted" title="源(目标钱包)这一单的名义额">源 {fUsd(p.masterNotional)}</div></td>
                    <td className={"num " + cls(p.realizedPnl)}>{fSign(p.realizedPnl, 1)}</td>
                    <td className="num">{fDur(p.durationSec)}</td>
                    <td className="mono" style={{ color: "var(--t2)", fontSize: 12 }}>{fTime(p.closedAt)}</td>
                    <td className="addr">{short(p.wallet)} {p.followPos != null
                      ? <span className="rankbadge" title={"跟单序号" + (p.walletRank ? " · 全站评分#" + p.walletRank : "")}>#{p.followPos}</span>
                      : <span className="tint tint-gray" title="当时/现在不在跟单集">脱榜</span>}</td>
                  </tr>
                  {isOpen && <tr className="detail-row"><td colSpan="10">
                    {details[pid] ? <PositionDetail d={details[pid]} /> : <div className="muted" style={{ padding: "14px 16px" }}>加载中…</div>}
                  </td></tr>}
                  </React.Fragment>; })}
              </tbody>
            </table>
          </div>
          {rows.length > PER && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 12, marginTop: 10 }}>
              <button className="btn" disabled={pg <= 0} onClick={() => setPage(pg - 1)}>上一页</button>
              <span className="muted mono">第 {pg + 1} / {pages} 页</span>
              <button className="btn" disabled={pg >= pages - 1} onClick={() => setPage(pg + 1)}>下一页</button>
            </div>
          )}
        </React.Fragment>
      )}
    </div>
  );
}

/* ----------------------------------------------------------------- wallets */
function Wallets({ confirm, toast }) {
  const [data, setData] = useState(null);
  const [drawer, setDrawer] = useState(null);
  const [wpage, setWpage] = useState(0);             // 10/page
  const [tab, setTab] = useState("followed");        // followed(实跟) | observing(样本观察) | dropped(降级)
  const load = useCallback(() => { api.get("/api/wallets?tab=" + tab + "&page=" + wpage + "&size=10").then(setData).catch(() => {}); }, [wpage, tab]);
  useEffect(() => { load(); const t = setInterval(load, 12000); return () => clearInterval(t); }, [load]);
  const dropped = tab === "dropped";

  const toggle = (w) => {
    const next = !w.enabled;
    const act = () => api.cmd("wallet_toggle", { address: w.address, enabled: next })
      .then(() => { toast((next ? "启用" : "停用") + " " + short(w.address)); setTimeout(load, 1800); });
    if (next) act(); else confirm({ title: "停用钱包", danger: true, ok: "停用",
      body: `停用后不再对 ${short(w.address)} 开新仓,存量持仓继续跟到平仓。`, onConfirm: act });
  };

  return (
    <div className="content">
      <div className="section-h" style={{ marginTop: 6 }}>
        <h2>跟踪名单 {data && <span className="muted">· 跟单线 {fNum(data.followLine, 0)} 分 · {
          tab === "followed" ? "实跟 " + data.total + " 个(与跟单脚本一致)"
          : tab === "observing" ? "样本观察 " + data.total + " 个(达标但样本不足,暂不跟)"
          : "降级 " + data.total + " 个"}</span>}</h2>
        <div className="range-tabs">
          <button className={tab === "followed" ? "on" : ""} onClick={() => { setTab("followed"); setWpage(0); }}>跟单中{data && data.followed != null ? " " + data.followed : ""}</button>
          <button className={tab === "observing" ? "on" : ""} onClick={() => { setTab("observing"); setWpage(0); }}>样本观察{data && data.observing != null ? " " + data.observing : ""}</button>
          <button className={tab === "dropped" ? "on" : ""} onClick={() => { setTab("dropped"); setWpage(0); }}>降级</button>
        </div>
      </div>
      <div className="tbl-wrap">
        {dropped ? (
          <table>
            <thead><tr>
              <th>地址</th><th>市场</th><th className="num">当前分</th><th className="num">曾在线</th><th className="num">ROI</th>
              <th className="num">胜率</th><th>主力</th><th>降级原因</th><th>降级时间</th>
            </tr></thead>
            <tbody>
              {data === null && <tr><td colSpan="9" className="loading">加载中…</td></tr>}
              {data && data.wallets.length === 0 && <tr><td colSpan="9" className="empty">暂无降级钱包 —— 都在跟单中 👍</td></tr>}
              {data && data.wallets.map(w => (
                <tr key={w.address} style={{ cursor: "pointer" }} onClick={() => setDrawer(w.address)}>
                  <td className="addr">{short(w.address)}</td>
                  <td><span className={"tint " + (w.marketType === "crypto" ? "tint-blue" : w.marketType === "stock" ? "tint-amber" : "tint-gray")}>{w.marketType}</span></td>
                  <td className="num"><b style={{ color: "var(--t2)" }}>{fNum(w.score, 1)}</b></td>
                  <td className="num muted">{fNum(w.lastFollowedScore, 1)}</td>
                  <td className="num up">{fNum(w.roiEqPct, 0)}%</td>
                  <td className="num">{fNum(w.winRatePct, 0)}%</td>
                  <td><b>{w.mainCoin}</b></td>
                  <td><span className="tint tint-red">{w.dropReason}</span></td>
                  <td className="mono" style={{ color: "var(--t2)", fontSize: 12 }}>{fTime(w.lastFollowedAt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <table>
            <thead><tr>
              <th>#</th><th>地址</th><th>市场</th><th className="num">评分</th><th className="num">ROI</th><th className="num">胜率</th>
              <th className="num" title="目标钱包自己最近7天平掉的回合数(活跃度)">近7天</th>
              <th className="num">最大亏损</th><th>主力</th><th className="num">被跟</th><th className="num">总体盈亏</th><th>启用</th>
            </tr></thead>
            <tbody>
              {data === null && <tr><td colSpan="12" className="loading">加载中…</td></tr>}
              {data && data.wallets.map(w => (
                <tr key={w.address} className={w.enabled ? "" : "row-off"}
                  style={{ cursor: "pointer" }} onClick={() => setDrawer(w.address)}>
                  <td><span className="rankbadge" title={w.followPos != null ? "跟单序号(与脚本一致);全站评分名次 #" + w.rank : "全站评分名次"}>{w.followPos != null ? w.followPos : w.rank}</span></td>
                  <td className="addr">{short(w.address)}</td>
                  <td><span className={"tint " + (w.marketType === "crypto" ? "tint-blue" : w.marketType === "stock" ? "tint-amber" : "tint-gray")}>{w.marketType}</span></td>
                  <td className="num"><b style={{ color: w.evidenceHeld ? "var(--t2)" : (w.score >= data.followLine ? "var(--green-l)" : "var(--t2)") }}>{fNum(w.score, 1)}</b>
                    {w.evidenceHeld && <div style={{ fontSize: 10, color: "var(--amber-l, #e0a23a)" }} title="评分达标,但成交笔数/活跃天数未到证据门槛,仅观察不跟单">样本观察</div>}
                  </td>
                  <td className={"num up"}>{fNum(w.roiEqPct, 0)}%</td>
                  <td className="num">{fNum(w.winRatePct, 0)}%</td>
                  <td className="num">{w.closed7d != null ? w.closed7d : "—"}</td>
                  <td className="num down">{fNum(w.worstSingleLossPct, 0)}%</td>
                  <td><b>{w.mainCoin}</b></td>
                  <td className="num">{w.followCount}</td>
                  <td className="num">{(w.closedN > 0 || (w.forwardNetPnl || 0) !== 0)
                    ? <b style={{ color: (w.forwardNetPnl || 0) < 0 ? "var(--red-l)" : "var(--green-l)" }}>
                        {fSign(w.forwardNetPnl || 0, 0)}{(w.forwardNetPnl || 0) < -5 ? " ⚠" : ""}</b>
                    : <span className="muted">—</span>}</td>
                  <td><div className={"toggle " + (w.enabled ? "on" : "")} onClick={(e) => { e.stopPropagation(); toggle(w); }}><div className="knob" /></div></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {data && data.total > data.size && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 12, marginTop: 10 }}>
          <button className="btn" disabled={wpage <= 0} onClick={() => setWpage(wpage - 1)}>上一页</button>
          <span className="muted mono">第 {data.page + 1} / {Math.ceil(data.total / data.size)} 页</span>
          <button className="btn" disabled={(wpage + 1) * data.size >= data.total} onClick={() => setWpage(wpage + 1)}>下一页</button>
        </div>
      )}
      {drawer && <WalletDrawer address={drawer} onClose={() => setDrawer(null)} />}
    </div>
  );
}

const STATUS_LABEL = { open: "在持", closed: "已平", gap_closed: "缺口平", liquidated: "爆仓" };

function WalletDrawer({ address, onClose }) {
  const [d, setD] = useState(null);
  const [recPage, setRecPage] = useState(0);
  const [exp, setExp] = useState({});
  useEffect(() => { setRecPage(0); setExp({}); }, [address]);
  useEffect(() => { api.get(`/api/wallets/${address}?recPage=${recPage}&recSize=20`).then(setD).catch(() => {}); }, [address, recPage]);
  const net = d && (d.netPnl || 0);
  const losing = d && net < -5;          // ⚠ only when we're actually losing money on it (not low win%)
  const recPages = d ? Math.max(1, Math.ceil(d.recordsTotal / d.recSize)) : 1;
  return (
    <React.Fragment>
      <div className="scrim" onClick={onClose} />
      <div className="drawer">
        <h3>{short(address)}</h3>
        <div className="muted" style={{ marginBottom: 18 }}>排名 #{d ? (d.rank != null ? d.rank : "—") : "—"} · {d ? d.marketType : ""}</div>
        {!d ? <div className="loading">加载中…</div> : (
          <React.Fragment>
            <div className="card" style={{ marginBottom: 14 }}>
              <div className="card-lbl" style={{ marginBottom: 10 }}>历史评分 vs 实盘对账 {losing && <span style={{ color: "var(--red-l)" }}>· ⚠ 实盘亏损</span>}</div>
              <div style={{ display: "flex", gap: 22, flexWrap: "wrap" }}>
                <div><div className="muted">评分</div><div className="mono" style={{ fontSize: 18 }}>{fNum(d.score, 1)}</div></div>
                <div><div className="muted">历史胜率</div><div className="mono" style={{ fontSize: 18 }}>{d.scoredWinRatePct != null ? fNum(d.scoredWinRatePct, 0) + "%" : "—"}<span className="muted" style={{ fontSize: 11 }}> /{d.scoredTrades || 0}笔</span></div></div>
                <div><div className="muted">实盘胜率</div><div className="mono" style={{ fontSize: 18, color: "var(--t1)" }}>{d.forwardWinRatePct != null ? fNum(d.forwardWinRatePct, 0) + "%" : "—"}<span className="muted" style={{ fontSize: 11 }}> /{d.closedN}笔</span></div></div>
              </div>
              <div style={{ display: "flex", gap: 22, flexWrap: "wrap", marginTop: 14, borderTop: "1px solid var(--glass-border)", paddingTop: 12 }}>
                <div><div className="muted">实盘战绩</div><div className="mono" style={{ fontSize: 15 }}><span className="up">{d.winN}胜</span> / <span className="down">{d.lossN}负</span></div></div>
                <div><div className="muted">已实现</div><div className={"mono " + cls(d.realizedPnl)} style={{ fontSize: 15 }}>{fSign(d.realizedPnl, 1)}</div></div>
                <div><div className="muted">在持({d.openN})浮动</div><div className={"mono " + cls(d.openUnrealized)} style={{ fontSize: 15 }}>{fSign(d.openUnrealized, 1)}</div></div>
                <div><div className="muted">净盈亏</div><div className={"mono " + cls(d.netPnl)} style={{ fontSize: 16, fontWeight: 700 }}>{fSign(d.netPnl, 1)}</div></div>
              </div>
            </div>

            <div className="card-lbl" style={{ marginBottom: 8 }}>跟单记录 <span className="muted">· 共 {d.recordsTotal} 笔(点击展开)</span></div>
            <div className="tbl-wrap">
              <table><thead><tr><th>币种</th><th>方向</th><th className="num">盈亏</th><th className="num">时间</th><th>状态</th></tr></thead>
                <tbody>{d.records.map(r => (
                  <React.Fragment key={r.id}>
                    <tr style={{ cursor: "pointer" }} onClick={() => setExp(e => ({ ...e, [r.id]: !e[r.id] }))}>
                      <td><b>{r.coin}</b> <span className="muted" style={{ fontSize: 10 }}>{exp[r.id] ? "▴" : "▾"}</span></td>
                      <td><span className={"tint " + (r.side === "long" ? "tint-green" : "tint-red")}>{r.side === "long" ? "多" : "空"}</span></td>
                      <td className={"num " + cls(r.pnl)}>{fSign(r.pnl, 1)}{r.status === "open" ? <span className="muted" style={{ fontSize: 10 }}> 浮</span> : ""}</td>
                      <td className="num muted">{agoText(r.openedAt)}</td>
                      <td className="muted">{STATUS_LABEL[r.status] || r.status}</td>
                    </tr>
                    {exp[r.id] && (
                      <tr><td colSpan="5" style={{ background: "rgba(255,255,255,.02)", fontFamily: "var(--mono)", fontSize: 11.5, lineHeight: 1.9, color: "var(--t2)" }}>
                        开仓价 <b>{fPrice(r.entry)}</b> → {r.status === "open" ? "现价" : "平仓价"} <b>{fPrice(r.exit)}</b>
                        　杠杆 {fNum(r.leverage, 0)}x　保证金 {fUsd(r.margin)}　名义额 {fUsd(r.notional)}<br />
                        主力开仓价 {fPrice(r.masterEntry)}{r.addCount ? `　加仓 ${r.addCount} 次` : ""}
                        {r.closedAt ? `　平于 ${agoText(r.closedAt)}` : ""}
                      </td></tr>
                    )}
                  </React.Fragment>
                ))}</tbody></table>
            </div>
            {d.recordsTotal > d.recSize && (
              <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 12, marginTop: 10 }}>
                <button className="btn" disabled={recPage <= 0} onClick={() => setRecPage(recPage - 1)}>上一页</button>
                <span className="muted mono">第 {recPage + 1} / {recPages} 页</span>
                <button className="btn" disabled={recPage >= recPages - 1} onClick={() => setRecPage(recPage + 1)}>下一页</button>
              </div>
            )}
          </React.Fragment>
        )}
      </div>
    </React.Fragment>
  );
}

/* ----------------------------------------------------------------- param metadata (UI-side) */
const PARAM_META = {
  // follow
  MIN_FOLLOW_SCORE: { name: "跟单评分线", desc: "watchlist 里评分≥此线的钱包才实际跟单(0–100 标准化分,见下方实时达标数)", range: "—", up: "更严、跟更少精英", dn: "更宽、纳入更多" },
  FOLLOW_MIN_TRADES: { name: "跟单·最低成交笔数", desc: "证据门槛:近30天平掉回合数<此=样本太薄,留在名单观察但不跟单", range: "5–10", up: "只跟履历厚的、更稳", dn: "放进薄样本、信号更多" },
  FOLLOW_MIN_ACTIVE_DAYS: { name: "跟单·最低活跃天数", desc: "证据门槛:活跃天数<此=履历太短,留在名单观察但不跟单", range: "3–5", up: "只跟交易天数多的", dn: "放进新钱包" },
  STABLE_MARGIN_PCT: { name: "稳定档·保证金", desc: "σ≤4%(BTC等)单笔投入(占可用%)", range: "8–12", up: "每单更重", dn: "每单更轻" },
  STABLE_LEV_CAP: { name: "稳定档·杠杆上限", desc: "σ≤4%的杠杆封顶(绝对上限)", range: "15–20", up: "放开高杠杆", dn: "压低杠杆" },
  STABLE_MIN_NOTIONAL: { name: "稳定档·最低名义额", desc: "BTC/大饼单笔名义额低于此(封顶到主力后)就不开,太小没意义", range: "$3k–8k", up: "过滤更多小单", dn: "连很小的也跟" },
  MID_MARGIN_PCT: { name: "中档·保证金", desc: "σ 4–10%(ETH/SOL/HYPE)单笔投入(占可用%)", range: "6–10", up: "每单更重", dn: "每单更轻" },
  MID_LEV_CAP: { name: "中档·杠杆上限", desc: "σ 4–10%的杠杆封顶", range: "8–12", up: "放开高杠杆", dn: "压低杠杆" },
  MID_MIN_NOTIONAL: { name: "中档·最低名义额", desc: "ETH/SOL等单笔名义额低于此就不开", range: "$2k–5k", up: "过滤更多小单", dn: "连很小的也跟" },
  HIGH_MARGIN_PCT: { name: "剧烈档·保证金", desc: "σ≥10%(meme/野币)单笔投入(占可用%)", range: "4–8", up: "每单更重", dn: "每单更轻" },
  HIGH_LEV_CAP: { name: "剧烈档·杠杆上限", desc: "σ≥10%的杠杆封顶", range: "3–5", up: "放开高杠杆", dn: "压低杠杆" },
  HIGH_MIN_NOTIONAL: { name: "剧烈档·最低名义额", desc: "meme/野币单笔名义额低于此就不开(σ高、仓位本就小,门槛设低)", range: "$500–1k", up: "过滤更多小单", dn: "连很小的也跟" },
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
const STAGES_FE = [["scan_leaderboard", "扫描排行榜"], ["fetch_history", "拉取历史 & 算指标"],
  ["score_filter", "评分 · 网格/扛单过滤"], ["rebuild_watchlist", "重建被跟名单"], ["persist", "写库 & 校验"]];

/* ----------------------------------------------------------------- scan mask */
function ScanMask({ status }) {
  const stage = status && status.stage;
  const curIdx = STAGES_FE.findIndex(s => s[0] === stage);
  const pct = (status && status.progressPct) || 0;
  const el = (status && status.elapsedSec) || 0;
  const mm = String(Math.floor(el / 60)).padStart(2, "0"), ss = String(el % 60).padStart(2, "0");
  return (
    <div className="mask">
      <div className="radar" />
      <h2>采集进行中…</h2>
      <div className="sub">{mm}:{ss} 已用 · 预计 ~20:00</div>
      <div className="mask-prog"><div className="pf" style={{ width: pct + "%" }} /></div>
      <div className="mask-meta">
        <span>{pct}%</span>
        <span>已扫描 {(status && status.candidatesScanned) || 0} / {(status && status.candidatesTotal) || "—"}</span>
      </div>
      <div className="stage-list">
        {STAGES_FE.map(([k, label], i) => {
          const st = curIdx < 0 ? "" : i < curIdx ? "done" : i === curIdx ? "active" : "";
          return <div key={k} className={"stage-item " + st}>
            <span className="stage-dot">{st === "done" ? "✓" : st === "active" ? "●" : ""}</span>{label}</div>;
        })}
      </div>
      <div className="mask-lock">⚠ 页面已锁定 · 重采期间禁止操作</div>
    </div>
  );
}

/* observer 进程控制过渡遮罩 — 启动/停止/暂停/恢复期间锁页面,直到引擎真正到达目标状态(running/
   stopped/paused)才消失。进程级启停要 ~5-10s(supervisor 轮询→systemctl→boot),软暂停略快。 */
function ObsMask({ label }) {
  return (
    <div className="mask">
      <span className="spin" style={{ width: 34, height: 34, borderWidth: 3 }} />
      <h2 style={{ marginTop: 22 }}>{label}</h2>
      <div className="sub">正在等待引擎确认…</div>
      <div className="mask-lock">⚠ 页面已锁定 · 操作进行中</div>
    </div>
  );
}

/* ----------------------------------------------------------------- discovery */
function Discovery({ scanning, startRescan, confirm }) {
  const [d, setD] = useState(null);
  const [runs, setRuns] = useState(null);
  const [fullScan, setFullScan] = useState(false);   // 采集模式:勾选=全量(重采所有候选),默认=增量(仅活跃+新)
  const load = useCallback(() => {
    api.get("/api/discovery").then(setD).catch(() => {});
    api.get("/api/scan-runs?limit=8").then(r => setRuns(r.runs)).catch(() => {});
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 4000); return () => clearInterval(t); }, [load]);  // live
  useEffect(() => { if (!scanning) load(); }, [scanning, load]);  // refresh after a rescan finishes

  const doRescan = () => confirm({
    title: fullScan ? "触发全量采集" : "触发增量采集",
    danger: fullScan, ok: fullScan ? "开始全量" : "开始增量",
    body: fullScan
      ? "全量:重拉排行榜 + 重采所有候选,让每个 profile 都到最新评分标准(改过评分逻辑后必须跑一次)。无跟单时全速约 30–90 分钟,有跟单则自动慢采让速。期间按钮锁定。确认?"
      : "增量:只重采活跃+新候选(快,几分钟),旧的 rejected 长尾不动。日常刷新用这个。确认?",
    onConfirm: () => startRescan(fullScan),
  });

  if (!d) return <div className="content"><div className="loading">加载中…</div></div>;
  const fn = d.funnel, h = d.scoreHistogram, maxBin = Math.max(...h.bins, 1);
  const sc = d.scanner || { mode: "unknown", detail: {} }, det = sc.detail || {};
  const scMode = sc.mode, scColor = scannerColor(scMode, sc.stale);
  const rolling = det.cycle_total != null;                 // preview sim populates a rolling sweep
  const cyclePct = det.cycle_total ? Math.round(det.cycle_pos / det.cycle_total * 100) : 0;
  const busy = scMode === "scanning" || scanning;          // a scan (manual OR 24h auto) is running -> lock the button
  const lastScanH = d.lastScanAt ? (Date.now() - new Date(d.lastScanAt).getTime()) / 3.6e6 : 1e9;
  const overdue = lastScanH > 26;                          // auto cadence is 24h -> >26h means the daily scan is stuck
  return (
    <div className="content">
      <div className="section-h" style={{ marginTop: 6 }}><h2>采集进程 · 实时</h2>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <label title="勾选=全量(重拉排行榜+重采所有候选,改过评分后必跑);默认=增量(仅活跃+新,快)"
            style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, cursor: busy ? "default" : "pointer", opacity: busy ? .5 : 1 }}>
            <input type="checkbox" checked={fullScan} disabled={busy} onChange={e => setFullScan(e.target.checked)} />
            全量采集
          </label>
          <button className="btn btn-accent" disabled={busy} onClick={doRescan}><Ico d={IC.discovery} /> {busy ? "采集进行中…" : (fullScan ? "触发全量采集" : "触发增量采集")}</button>
        </div></div>
      <div className="card">
        <div style={{ display: "flex", alignItems: "center", gap: 24, flexWrap: "wrap" }}>
          <span className="pill" style={{ background: "rgba(255,255,255,.05)", color: scColor }}>
            <span className="dot" style={{ background: scColor, animation: (scMode === "rolling" || scMode === "scanning") ? "pulse 1.4s infinite" : "none" }} />
            {SCANNER_LABEL[scMode] || scMode}{sc.stale && scMode !== "idle" ? " · 心跳超时 ⚠" : ""}</span>
          {rolling && <div><div className="muted">本轮进度</div><div className="mono" style={{ fontSize: 15 }}>{det.cycle_pos} / {det.cycle_total} <span className="muted">({cyclePct}%)</span></div></div>}
          {rolling && <div><div className="muted">采集节奏</div><div className="mono" style={{ fontSize: 15 }}>每 ~{det.interval_s ?? "—"}s / 个</div></div>}
          {rolling && <div><div className="muted">最近更新</div><div className="mono" style={{ fontSize: 15 }}>{short(det.last_addr)} · {agoText(det.last_at)}</div></div>}
          <div><div className="muted">上次扫描</div><div className="mono" style={{ fontSize: 15, color: overdue ? "var(--red-l)" : undefined }}>{agoText(d.lastScanAt)}{overdue ? " ⚠超期" : ""}</div></div>
          {!rolling && <div><div className="muted">采集周期</div><div className="mono" style={{ fontSize: 15 }}>每 24h 自动</div></div>}
          <div><div className="muted">被跟名单</div><div className="mono" style={{ fontSize: 15 }}>{fn.watchlist} 钱包</div></div>
          <div><div className="muted">心跳</div><div className="mono" style={{ fontSize: 15, color: (sc.stale && scMode !== "idle") ? "var(--red-l)" : "var(--green-l)" }}>{agoText(sc.heartbeatAt)}</div></div>
        </div>
        {rolling && <div className="bar-track" style={{ marginTop: 14, height: 6 }}>
          <div className="bar-fill" style={{ width: cyclePct + "%", background: "var(--accent-grad)" }} /></div>}
      </div>

      <div className="section-h"><h2>筛选漏斗</h2></div>
      <div className="card">
        <div className="funnel">
          <div className="funnel-stage"><div className="fn">{fn.candidates}</div><div className="fl">候选 candidates</div></div>
          <div className="funnel-arrow">→</div>
          <div className="funnel-stage"><div className="fn" style={{ color: "var(--blue-l)" }}>{fn.active}</div><div className="fl">active</div></div>
          <div className="funnel-arrow">→</div>
          <div className="funnel-stage"><div className="fn" style={{ color: "var(--green-l)" }}>{fn.watchlist}</div><div className="fl">跟单线以上 watchlist</div></div>
        </div>
      </div>

      <div className="grid2" style={{ marginTop: 14 }}>
        <div className="card">
          <div className="card-lbl">拒绝原因占比</div>
          <div style={{ marginTop: 12 }}>
            {d.rejectReasons.map((r, i) => (
              <div className="bar-row" key={i}><div className="bl" style={{ width: 120 }}>{r.label}</div>
                <div className="bar-track"><div className="bar-fill" style={{ width: r.pct + "%", background: "var(--accent-grad)" }} /></div>
                <div className="bv">{r.pct}%</div></div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-lbl">评分分布(标出跟单线)</div>
          <div className="histo">
            {h.bins.map((b, i) => (
              <div key={i} className={"hb" + (i < h.followLineBinIndex ? " below" : "")} style={{ height: (b / maxBin * 100) + "%" }} />
            ))}
            <div className="histo-line" style={{ left: (h.followLineBinIndex / h.bins.length * 100) + "%" }}>
              <span className="lbl">跟单线</span></div>
          </div>
        </div>
      </div>

      <div className="section-h"><h2>扫描历史</h2></div>
      <div className="tbl-wrap">
        <table>
          <thead><tr><th>时间</th><th className="num">候选</th><th className="num">新增</th><th className="num">退役</th><th className="num">拒绝</th><th className="num">在持名单</th></tr></thead>
          <tbody>
            {runs === null && <tr><td colSpan="6" className="loading">加载中…</td></tr>}
            {runs && runs.map((r, i) => (
              <tr key={i}><td className="addr">{r.at ? r.at.replace("T", " ").replace("Z", "") : "—"}</td>
                <td className="num">{r.candidates}</td><td className="num up">+{r.added}</td>
                <td className="num">{r.retired}</td><td className="num">{r.rejected}</td><td className="num">{r.active}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ----------------------------------------------------------------- settings */
/* 下单沙盘 — 用真实 v10 公式实时换算:杠杆 = 档位上限(floor,clip MIN/MAX_LEV);止损 = 亏SM%保证金 逆向。
   只读展示(无可调目标杠杆),紧凑液态玻璃风。σ 为实采日内最高-最低振幅均值。读自正在编辑的 vals。 */
function SizingPreview({ vals }) {
  const [bal, setBal] = React.useState(10000);
  const n = (k, d) => { const v = Number(vals[k]); return isFinite(v) && v > 0 ? v : d; };
  const stMax = n("STABLE_SIGMA_MAX", 4), hiMin = n("HIGH_SIGMA_MIN", 10);
  const MAXL = n("MAX_LEV", 20), MINL = Math.max(1, n("MIN_LEV", 1));
  const SM = n("STOP_MARGIN_PCT", 70);
  const stopOn = vals["COPY_STOP_ENABLE"] !== false;
  const tier = s => s <= stMax ? "stable" : (s >= hiMin ? "high" : "mid");
  const TM = { stable: ["STABLE_MARGIN_PCT", "STABLE_LEV_CAP"], mid: ["MID_MARGIN_PCT", "MID_LEV_CAP"], high: ["HIGH_MARGIN_PCT", "HIGH_LEV_CAP"] };
  const DOT = { stable: "var(--green)", mid: "var(--amber)", high: "var(--red)" };
  const dft = { STABLE_MARGIN_PCT: 10, STABLE_LEV_CAP: 20, MID_MARGIN_PCT: 8, MID_LEV_CAP: 10, HIGH_MARGIN_PCT: 6, HIGH_LEV_CAP: 5 };
  const usd = x => x >= 1000 ? "$" + (x / 1000).toFixed(x >= 10000 ? 0 : 1) + "k" : "$" + Math.round(x);
  const calc = s0 => {
    const s = Math.max(0.1, s0), t = tier(s);
    const mPct = n(TM[t][0], dft[TM[t][0]]) / 100, cap = n(TM[t][1], dft[TM[t][1]]);
    const lev = Math.max(MINL, Math.floor(Math.min(cap, MAXL)));   // v10: 杠杆 = 档位上限(再被目标杠杆+股票上限封顶)
    const margin = bal * mPct;
    const stopLoss = Math.min(SM / 100, 1), stopDist = stopLoss / lev * 100;  // 硬亏=SM%保证金(固定),逆向价格=SM%÷杠杆
    return { t, margin, lev, notl: margin * lev, stopDist, stopLoss };
  };
  const COINS = [["BTC", 3.9], ["ETH", 5.3], ["ZEC", 14.6]];   /* 每档一个代表:稳定 / 中 / 剧烈 */
  return (
    <div className="sz">
      <div className="sz-hd">
        <div className="sz-ttl">下单沙盘<span>· 按当前参数实时换算</span></div>
        <div className="sz-bal"><label>可用余额</label>
          <input type="number" value={bal} onChange={e => setBal(Number(e.target.value) || 0)} /></div>
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
        杠杆 = <b>σ 档位上限</b>(σ 定档,再被目标杠杆+股票上限封顶)· 保证金 = 可用 × 档位%{stopOn
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
  if (disabled) return <span className="ev ev-ro">{value == null ? "—" : value}{unit && <i className="ev-u">{unit}</i>}</span>;
  if (editing) return (
    <input ref={ref} className="ev-input" type={ptype === "nullable" ? "text" : "number"} value={draft == null ? "" : draft}
      placeholder={ptype === "nullable" ? "关闭" : ""}
      onChange={e => setDraft(e.target.value)} onBlur={commit}
      onKeyDown={e => { if (e.key === "Enter") commit(); else if (e.key === "Escape") { setDraft(value); setEditing(false); } }} />
  );
  return (
    <span className="ev" title="点击编辑" onClick={() => { setDraft(value); setEditing(true); }}>
      {value == null ? <span className="ev-empty">关闭</span> : value}{value != null && unit && <i className="ev-u">{unit}</i>}
    </span>
  );
}

function Settings({ startRescan, confirm, toast }) {
  const [params, setParams] = useState(null);
  const [tab, setTab] = useState("scanner");
  const [vals, setVals] = useState({});
  const [dirty, setDirty] = useState({});
  const [expanded, setExpanded] = useState(null);
  const [saving, setSaving] = useState(false);                    // 保存时的短暂全页 loading(替代右上角 toast)
  const [openTiers, setOpenTiers] = useState({});                 // 档位折叠(默认全部收起)
  const [scoreDist, setScoreDist] = useState(null);               // watchlist 全体显示分(0-100),供跟单线实时计数

  useEffect(() => {
    api.get("/api/params").then(p => {
      setParams(p);
      const v = {}; [...p.scanner, ...p.follow].forEach(x => { v[x.key] = x.value; });
      setVals(v);
    }).catch(() => {});
    api.get("/api/score-dist").then(setScoreDist).catch(() => {});
  }, []);

  if (!params) return <div className="content"><div className="loading">加载中…</div></div>;
  const ADD_KEYS = new Set(["FOLLOW_POS_ADD", "SMART_ADD", "ADD_GAP_K", "ADD_GAP_SHRINK_G", "ADD_MAX_HARD",
    "ADD_FRAC", "STABLE_MAX_ADDS", "MID_MAX_ADDS", "HIGH_MAX_ADDS"]);   // 归入独立「加仓策略」tab
  //  (单币上限 STABLE/MID/HIGH_COIN_CAP_PCT 已挪回「跟单策略 · σ分档」—— 它是全局灾难闸,管开仓+加仓,不是加仓专属)
  const list = tab === "add" ? params.follow.filter(p => ADD_KEYS.has(p.key)) : params[tab];
  const editable = (p) => !(p.type === "display" || p.level === "black");
  const set = (key, val) => { setVals(v => ({ ...v, [key]: val })); setDirty(dd => ({ ...dd, [key]: true })); };
  const tabDirty = list.filter(p => dirty[p.key]);

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
    { key: "stable", label: "稳定档", sub: "σ ≤ 5% · BTC 及更稳的(含低波动股票如GOLD)", tint: "tint-green", keys: ["STABLE_MARGIN_PCT", "STABLE_LEV_CAP", "STABLE_MIN_NOTIONAL", "STABLE_COIN_CAP_PCT"] },
    { key: "mid", label: "中档", sub: "σ 5–10% · ETH / SOL / HYPE 等主流", tint: "tint-amber", keys: ["MID_MARGIN_PCT", "MID_LEV_CAP", "MID_MIN_NOTIONAL", "MID_COIN_CAP_PCT"] },
    { key: "high", label: "剧烈档", sub: "σ ≥ 10% · ZEC / meme / 野币 / 高波股", tint: "tint-red", keys: ["HIGH_MARGIN_PCT", "HIGH_LEV_CAP", "HIGH_MIN_NOTIONAL", "HIGH_COIN_CAP_PCT"] },
  ];
  const tierKeys = new Set(TIER_GROUPS.flatMap(g => g.keys));

  const apply = async () => {
    const body = {}; tabDirty.forEach(p => { body[p.key] = vals[p.key]; });
    const doIt = async () => {
      setSaving(true);                                  // 短暂全页 loading 代替右上角 tooltip
      const t0 = Date.now();
      const cat = tab === "add" ? "follow" : tab;              // 加仓参数在后端属 follow 类
      try { await fetch("/api/params/" + cat, { method: "PATCH", headers: { Authorization: "Bearer " + api.token }, body: JSON.stringify(body) }); } catch (_e) {}
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

  return (
    <div className="content">
      {saving && <div className="mask"><span className="spin" style={{ width: 34, height: 34, borderWidth: 3 }} /><h2 style={{ marginTop: 22 }}>保存中…</h2></div>}
      <div className="tabs">
        <div className={"tab" + (tab === "scanner" ? " on" : "")} onClick={() => setTab("scanner")}>钱包采集参数</div>
        <div className={"tab" + (tab === "follow" ? " on" : "")} onClick={() => setTab("follow")}>跟单策略参数</div>
        <div className={"tab" + (tab === "add" ? " on" : "")} onClick={() => setTab("add")}>加仓策略</div>
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
        {tab !== "add" && list.filter(p => !(tab === "follow" && (tierKeys.has(p.key) || ADD_KEYS.has(p.key)))).map(p => {
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
              </React.Fragment>
            );
          }
          return Prow(p);
        })}
        {tab === "follow" && <div className="psec-h">保证金与杠杆 · 按波动率 σ 分档
          <span>杠杆 = σ 所在档位的上限(σ 定档),这里设各档的单笔保证金% 与杠杆上限</span></div>}
        {tab === "follow" && TIER_GROUPS.map(g => {
          const open = openTiers[g.key];
          const rows = g.keys.map(k => list.find(p => p.key === k)).filter(Boolean);
          return (
            <div key={g.key}>
              <div className={"expand-head" + (open ? " open" : "")} onClick={() => setOpenTiers(o => ({ ...o, [g.key]: !o[g.key] }))}>
                <span style={{ color: "var(--t3)", width: 12 }}>{open ? "▾" : "▸"}</span>
                <span className={"pill " + g.tint}>{g.label}</span>
                <span className="muted" style={{ fontSize: 12 }}>{g.sub}</span>
                {!open && <span className="muted" style={{ marginLeft: "auto", fontSize: 11 }}>
                  保证金 {vals[g.keys[0]]}% · 杠杆 ≤{vals[g.keys[1]]}x · 最低 ${vals[g.keys[2]]} · 单币上限 {vals[g.keys[3]]}%</span>}
              </div>
              {open && <div className="expand-body">{rows.map(Prow)}</div>}
            </div>
          );
        })}
      </div>

      {tabDirty.length > 0 && (
        <div className="apply-bar">
          <div className="ab-l">{tabDirty.length} 项未应用改动{tab === "scanner" ? "(需重采生效)" : "(即时生效)"}</div>
          <div style={{ display: "flex", gap: 10 }}>
            <button className="btn" onClick={() => { setVals(v => { const nv = { ...v }; const o = {}; [...params.scanner, ...params.follow].forEach(x => o[x.key] = x.value); tabDirty.forEach(p => nv[p.key] = o[p.key]); return nv; }); setDirty({}); }}>放弃</button>
            <button className="btn btn-accent" onClick={apply}>{tab === "scanner" ? "应用并重采" : "保存(即时生效)"}</button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ----------------------------------------------------------------- shell */
function ShadowCompare() {
  const [d, setD] = useState(null);
  useEffect(() => {
    const f = () => api.get("/api/shadow").then(setD).catch(() => {});
    f(); const t = setInterval(f, 10000); return () => clearInterval(t);
  }, []);
  if (!d) return <div className="content"><div className="loading">加载中…</div></div>;
  const roi = b => (b.equity / 10000 - 1) * 100;
  const Acct = ({ b, name, tint }) => (
    <div className="card" style={{ flex: 1 }}>
      <div className="card-lbl">{name}</div>
      <div className="kpi" style={{ color: tint }}>{fUsd(b.equity)}</div>
      <div className="muted" style={{ fontSize: 12, lineHeight: 1.7 }}>
        ROI <b className={cls(roi(b))}>{fSign(roi(b), 1)}%</b> · 已实现 <span className={cls(b.realized)}>{fSign(b.realized, 0)}</span> · 浮动 <span className={cls(b.unrealized)}>{fSign(b.unrealized, 0)}</span><br />
        {b.openN} 持仓 · {b.closedN} 平仓 · 胜率 {fNum(b.winRatePct, 0)}%
      </div>
    </div>
  );
  const diff = d.maker.equity - d.taker.equity;
  return (
    <div className="content">
      <h2>影子对比 · Maker vs Taker <span className="muted">· 同一套策略,只差执行方式</span></h2>
      {!d.enabled && <div className="muted" style={{ marginTop: 8 }}>⚠ 影子引擎未启用</div>}
      <div style={{ display: "flex", gap: 14, marginTop: 12 }}>
        <Acct b={d.taker} name="Taker 账(实盘执行)" tint="var(--t1)" />
        <Acct b={d.maker} name="Maker 影子账(挂单执行)" tint="var(--accent)" />
      </div>
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-lbl">Maker − Taker 权益差</div>
        <div className="kpi" style={{ color: diff >= 0 ? "var(--green-l)" : "var(--red-l)" }}>{fSign(diff, 1)}</div>
        <div className="muted" style={{ fontSize: 12 }}>正 = maker 执行更优(省手续费 + 更好入场价,但成交率更低)。两账从同一 $10k 起点、同策略跑,差异纯来自执行。</div>
      </div>
      <h3 style={{ marginTop: 18 }}>Maker 账当前持仓 <span className="muted">· {d.makerPositions.length} 笔</span></h3>
      <table><thead><tr><th>币</th><th>方向</th><th className="num">入场/杠杆</th><th className="num">保证金</th><th className="num">现价</th><th className="num">浮动</th><th>钱包</th></tr></thead>
        <tbody>
          {d.makerPositions.length === 0 && <tr><td colSpan="7" className="empty">影子账暂无持仓(等目标 maker 成交后建仓)</td></tr>}
          {d.makerPositions.map((p, i) => (
            <tr key={i}>
              <td><b>{p.coin}</b>{p.addN > 0 && <span className="pill" style={{ marginLeft: 6 }}>加{p.addN}</span>}</td>
              <td><span className={"tint " + (p.side === "long" ? "tint-green" : "tint-red")}>{p.side === "long" ? "多" : "空"}</span></td>
              <td className="num">{fPrice(p.entry)} · {fNum(p.lev, 0)}x</td>
              <td className="num">{fUsd(p.margin)}</td>
              <td className="num">{fPrice(p.mark)}</td>
              <td className={"num " + cls(p.upnl)}>{fSign(p.upnl, 1)}</td>
              <td className="addr">{short(p.addr)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const NAV = [
  ["监控", [["overview", "总览", IC.overview], ["positions", "持仓中", IC.positions], ["history", "历史持仓", IC.history], ["shadow", "影子对比", IC.overview], ["wallets", "跟踪钱包", IC.wallets]]],
  ["控制", [["discovery", "采集", IC.discovery], ["settings", "策略参数", IC.settings]]],
];
const TITLES = { overview: "总览 Overview", positions: "持仓中 Positions", history: "历史持仓 History", shadow: "影子对比 Maker Shadow", wallets: "跟踪钱包 Wallets", discovery: "采集 Discovery", settings: "策略参数 Settings" };

function Dashboard({ onLogout }) {
  const [page, setPage] = useState("overview");
  const [polledOv, setPolledOv] = useState(null);
  const [live, setLive] = useState(null);            // SSE fast bundle {overview, positions, serverTime}
  const [streamOk, setStreamOk] = useState(false);
  const [confirmCfg, setConfirmCfg] = useState(null);
  const [busy, setBusy] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [scanStatus, setScanStatus] = useState(null);
  const [obsPending, setObsPending] = useState(null);   // observer 控制过渡 {label, target} → 显示遮罩
  const [stopChecked, setStopChecked] = useState(false); // 运行态按钮内「彻底停止」复选框(勾选才升级为杀进程)
  const toast = () => {};   // 右上角 tooltip 已废弃 — 各动作改用整页/按钮内联 loading 反馈

  const ov = (streamOk && live && live.overview) || polledOv;    // prefer live stream; fall back to polled

  // SSE live stream (replaces polling when connected). EventSource auto-reconnects; on error we flip
  // streamOk off so the polling fallback below resumes until the stream recovers.
  useEffect(() => {
    if (!api.token || typeof EventSource === "undefined") return;
    let es;
    try {
      es = new EventSource("/api/stream?token=" + encodeURIComponent(api.token));
      es.onmessage = (e) => { try { setLive(JSON.parse(e.data)); setStreamOk(true); } catch (_e) {} };
      es.onerror = () => setStreamOk(false);
    } catch (_e) { setStreamOk(false); }
    return () => { if (es) es.close(); };
  }, []);

  // Polling fallback for overview: only while the stream is NOT delivering. One immediate load on mount
  // paints before the stream's first push.
  const loadOv = useCallback(() => { api.get("/api/overview").then(setPolledOv).catch(() => {}); }, []);
  useEffect(() => {
    loadOv();
    if (streamOk) return;
    const t = setInterval(loadOv, 7000);
    return () => clearInterval(t);
  }, [loadOv, streamOk]);

  const startRescan = useCallback(async (full = false) => { await api.cmd("rescan", { full: !!full }); setScanning(true); }, []);
  // The SERVER is the source of truth for "a full scan is running" (scan_progress / process_status,
  // surfaced as system.scanner). Driving the mask off this — not just a click in this tab — means the
  // mask survives a page refresh/reopen AND catches the 24h auto-scan, so you can't refresh past it and
  // double-click 重采. (Backend is already single-executor + absorbs duplicate rescans; this closes the UX gap.)
  const serverScanning = !!(ov && ov.system && ov.system.scanner === "scanning");
  // ONLY a MANUAL (dashboard-triggered) scan locks the page. The 24h AUTO scan runs SILENTLY in the
  // background — it MUST be slow (the observer owns the rate budget) so it takes a long time, and locking
  // the dashboard for its whole duration is unacceptable. So when the server reports a scan running, we
  // check scan-status.manual and only raise the mask for a manual one.
  useEffect(() => {
    if (!serverScanning) return;
    api.get("/api/scan-status").then((s) => {
      if (s && s.state === "scanning" && s.manual) { setScanning(true); setScanStatus(s); }
    }).catch(() => {});
  }, [serverScanning]);
  // one-shot on mount: if a MANUAL scan is already in flight, raise the mask IMMEDIATELY (survives a
  // refresh mid-manual-scan). An auto scan in flight is ignored — dashboard stays usable.
  useEffect(() => {
    api.get("/api/scan-status").then((s) => {
      if (s && s.state === "scanning" && s.manual) { setScanning(true); setScanStatus(s); }
    }).catch(() => {});
  }, []);
  useEffect(() => {                                  // poll scan progress while the mask is up
    if (!scanning) return;
    let alive = true, started = Date.now(), seen = false;
    const tick = async () => {
      try {
        const s = await api.get("/api/scan-status");
        if (!alive) return;
        if (s.state === "scanning") { seen = true; setScanStatus(s); }
        // clear ONLY when BOTH the progress poll AND the overview agree the scan is done (avoids a
        // premature un-mask from API timing skew); 8s grace covers the click→daemon-pickup window.
        else if ((seen || Date.now() - started > 8000) && !serverScanning) {
          setScanning(false); setScanStatus(null);
        }
      } catch (_e) {}
    };
    tick(); const t = setInterval(tick, 1200);
    return () => { alive = false; clearInterval(t); };
  }, [scanning, serverScanning]);

  // observer-control transition: poll overview while the mask is up, clear it when the engine reaches the
  // target state (running/stopped/paused) — start/stop take ~5-10s (supervisor+systemctl+boot). 30s safety.
  useEffect(() => {
    if (!obsPending) return;
    let alive = true, started = Date.now();
    const tick = async () => {
      try {
        const o = await api.get("/api/overview");
        if (!alive) return;
        setPolledOv(o);
        const st = o && o.system ? o.system.observer : null;
        if (st === obsPending.target || Date.now() - started > 30000) setObsPending(null);
      } catch (_e) {}
    };
    tick(); const t = setInterval(tick, 1500);
    return () => { alive = false; clearInterval(t); };
  }, [obsPending]);

  const obs = ov && ov.system ? ov.system.observer : "stopped";   // stopped | running | paused
  const obsUp = obs === "running" || obs === "paused";            // process is alive (vs not started)
  const pausing = busy || !!obsPending;
  // fire an observer-control command + raise the transition mask until the engine reaches `target`
  // (start/stop go through the supervisor + systemctl ~5-10s; pause/resume apply in the observer loop).
  const ctl = (type, label, target) => { api.cmd(type, {}); setObsPending({ label, target }); };
  // SMART start (shown when not actively opening): process alive but paused → just resume opening new
  // orders; process gone/hung (stopped) → restart the whole observer via the supervisor.
  const smartStart = () => obs === "paused"
    ? ctl("resume", "正在恢复开单…", "running")
    : ctl("observer_start", "正在启动跟单…", "running");
  // RUNNING control: default = soft pause (停开新仓、存量继续管); arm the in-button checkbox to ESCALATE to
  // a full process stop. One button, FIXED size — only label/color change between 暂停开单 ↔ 彻底停止跟单.
  const pauseOrStop = () => {
    if (stopChecked) {
      setConfirmCfg({ title: "彻底停止跟单", danger: true, ok: "彻底停止整个进程",
        body: "将停止整个 Observer 进程:不再开新仓,且存量持仓也不再被管理(下次启动会自动重新接管)。只想停开新仓、让存量继续跟到平仓的话,取消勾选即可。",
        onConfirm: () => ctl("observer_stop", "正在停止跟单…", "stopped") });
    } else {
      ctl("pause", "正在暂停开单…", "paused");
    }
  };
  useEffect(() => { if (obs !== "running") setStopChecked(false); }, [obs]);  // reset escalation off-running

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand"><div className="mk">跟</div><div><b>跟单监控台</b><span>COPY-TRADE OPS</span></div></div>
        {NAV.map(([grp, items]) => (
          <div key={grp}>
            <div className="nav-group">{grp}</div>
            {items.map(([k, label, d]) => {
              const cnt = (ov && ov.system)
                ? (k === "positions" ? ov.openCount : k === "wallets" ? ov.system.watchlistCount : null)
                : null;
              return (
                <div key={k} className={"nav-item" + (page === k ? " active" : "")} onClick={() => setPage(k)}>
                  <Ico d={d} />{label}
                  {cnt != null && <span className="nav-count">{cnt}</span>}
                </div>
              );
            })}
          </div>
        ))}
        <div className="spacer" />
        <div className="logout" onClick={onLogout}><Ico d={IC.logout} /> 退出登录</div>
      </aside>

      <main className="main">
        <div className="topbar">
          <div>
            <div className="crumb">{TITLES[page] && TITLES[page].split(" ")[1]} · 模拟盘</div>
            <div className="title">{TITLES[page]}</div>
          </div>
          <div className="topbar-right">
            <span className="pill" style={{ background: "rgba(255,255,255,.05)", color: streamOk ? "var(--green-l)" : "var(--t3)" }}
              title={streamOk ? "SSE 实时推送已连接" : "轮询兜底(SSE 未连接)"}>
              <span className="dot" style={{ background: streamOk ? "var(--green)" : "var(--gray)", animation: streamOk ? "pulse 1.6s infinite" : "none" }} />
              {streamOk ? "实时" : "轮询"}</span>
            <span className="pill pill-paper"><span className="dot" style={{ background: "var(--amber)" }} /> 运行模式 · Paper</span>
            {/* fixed-width control (minWidth 150, centered) so changing the label never resizes the button.
                stopped → 启动跟单(restart) · paused → 恢复开单(resume) · running → 暂停开单 + in-button
                「彻底停止」checkbox that escalates to a full process stop (red). */}
            {!(ov && ov.system) ? null : obs === "stopped"
              ? <button className="btn btn-go" style={{ minWidth: 150, justifyContent: "center" }} onClick={smartStart} disabled={pausing}>
                  <span className="dot" style={{ width: 7, height: 7, borderRadius: 9, background: "#fff" }} /> 启动跟单</button>
              : obs === "paused"
              ? <button className="btn btn-go" style={{ minWidth: 150, justifyContent: "center" }} onClick={smartStart} disabled={pausing}>
                  <span className="dot" style={{ width: 7, height: 7, borderRadius: 9, background: "#fff" }} /> 恢复开单</button>
              : <button className={"btn " + (stopChecked ? "btn-stop" : "btn-accent")} style={{ minWidth: 150, justifyContent: "center" }} disabled={pausing} onClick={pauseOrStop}>
                  <span onClick={(e) => { e.stopPropagation(); setStopChecked(v => !v); }} title="勾选后升级为彻底停止整个进程"
                    style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 15, height: 15, borderRadius: 4, border: "1.5px solid rgba(255,255,255,.75)", fontSize: 10, lineHeight: 1, cursor: "pointer", flexShrink: 0 }}>
                    {stopChecked ? "✓" : ""}</span>
                  {stopChecked ? "彻底停止跟单" : "暂停开单"}</button>}
          </div>
        </div>

        {ov && ov.system && (
          <div className="ticker">
            <div className="chip"><div className="k">权益</div><div className="v">{fUsd(ov.equity)}</div></div>
            <div className="chip"><div className="k">ROI</div><div className={"v " + cls(ov.roiPct)}>{fPct(ov.roiPct)}</div></div>
            <div className="chip"><div className="k">今日</div><div className={"v " + cls(ov.todayPct)}>{fPct(ov.todayPct)}</div></div>
            <div className="chip"><div className="k">在持</div><div className="v">{ov.openCount}</div></div>
            <div className="chip"><div className="k">可用</div><div className="v">{fUsd(ov.availableBalance)}</div></div>
            <div className="chip"><div className="k">被跟</div><div className="v">{ov.system.watchlistCount}</div></div>
            <div className="chip"><div className="k">浮动</div><div className={"v " + cls(ov.unrealizedPnl)}>{fSign(ov.unrealizedPnl)}</div></div>
            <div className="chip"><div className="k">Observer</div><div className="v" style={{ fontSize: 13, color: obs === "stopped" ? "var(--t3)" : obs === "paused" ? "var(--amber)" : "var(--green-l)" }}>{obs === "stopped" ? "已停止" : obs === "paused" ? "已暂停" : "运行中"}</div></div>
            {(() => { const sc = ov.system.scanner, stale = ov.system.scannerStale;
              return <div className="chip"><div className="k">采集</div><div className="v" style={{ fontSize: 13, color: scannerColor(sc, stale) }}>{SCANNER_LABEL[sc] || sc}{stale && sc !== "idle" ? " ⚠" : ""}</div></div>; })()}
          </div>
        )}

        {page === "overview" && <Overview ov={ov} />}
        {page === "positions" && <Positions confirm={setConfirmCfg} toast={toast} streamOpen={streamOk ? (live && live.positions) : null} />}
        {page === "history" && <History />}
        {page === "shadow" && <ShadowCompare />}
        {page === "wallets" && <Wallets confirm={setConfirmCfg} toast={toast} />}
        {page === "discovery" && <Discovery scanning={scanning} startRescan={startRescan} confirm={setConfirmCfg} />}
        {page === "settings" && <Settings startRescan={startRescan} confirm={setConfirmCfg} toast={toast} />}
      </main>

      {scanning && <ScanMask status={scanStatus} />}{/* scanning = MANUAL scan only; 24h auto runs silent */}
      {obsPending && <ObsMask label={obsPending.label} />}
      <Confirm cfg={confirmCfg} onClose={() => setConfirmCfg(null)} />
    </div>
  );
}

/* ----------------------------------------------------------------- root */
function App() {
  const [authed, setAuthed] = useState(false);
  const [err, setErr] = useState(null);
  const [user, setUser] = useState("admin");
  const [pw, setPw] = useState("");          // empty by default (auto-login tries preview creds for local)

  // On mount: validate any existing token; if invalid/missing, auto-login (local preview creds only —
  // harmless on prod where the password differs). Survives a server restart that wiped in-memory tokens.
  useEffect(() => {
    (async () => {
      try {
        if (api.token) { await api.get("/api/overview"); setAuthed(true); return; }
      } catch (_e) { /* stale token -> fall through to login */ }
      try { await api.login(DASH_USER, DASH_PW); setAuthed(true); } catch (_e) { setAuthed(false); }
    })();
  }, []);

  const doLogin = async () => {
    try { await api.login(user, pw); setAuthed(true); setErr(null); }
    catch (_e) { setErr("账号或密码错误"); }
  };
  const logout = () => { api.token = null; localStorage.removeItem(TOK_KEY); setAuthed(false); };

  if (authed) return <Dashboard onLogout={logout} />;
  return (
    <div className="login-shell">
      <div className="login-card">
        <div className="login-mark">跟</div>
        <h1>跟单监控台</h1>
        <p>COPY-TRADE OPS · 登录</p>
        {err && <p className="err">{err}</p>}
        <input type="text" value={user} onChange={e => setUser(e.target.value)}
          onKeyDown={e => e.key === "Enter" && doLogin()} placeholder="账号" autoComplete="username" />
        <input type="password" value={pw} onChange={e => setPw(e.target.value)}
          onKeyDown={e => e.key === "Enter" && doLogin()} placeholder="密码" autoComplete="current-password" />
        <button className="btn btn-accent" style={{ width: "100%" }} onClick={doLogin}>登录</button>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
