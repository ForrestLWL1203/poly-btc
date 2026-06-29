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
};

/* ----------------------------------------------------------------- sparkline */
function Spark({ data, w = 78, h = 22 }) {
  if (!data || data.length < 2) return <span className="muted">—</span>;
  const min = Math.min(...data), max = Math.max(...data), rng = max - min || 1;
  const pts = data.map((v, i) => [i / (data.length - 1) * w, h - ((v - min) / rng) * (h - 4) - 2]);
  const up = data[data.length - 1] >= data[0];
  return <svg className="spark" width={w} height={h}><polyline fill="none" stroke={up ? "var(--green)" : "var(--red)"} strokeWidth="1.6"
    points={pts.map(p => p.join(",")).join(" ")} /></svg>;
}

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
  if (!cfg) return null;
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <h3>{cfg.title}</h3>
        <p>{cfg.body}</p>
        <div className="modal-row">
          <button className="btn" onClick={onClose}>取消</button>
          <button className={"btn " + (cfg.danger ? "btn-danger" : "btn-accent")}
            onClick={() => { cfg.onConfirm(); onClose(); }}>{cfg.ok || "确认"}</button>
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
  const [filter, setFilter] = useState("all");
  const [opage, setOpage] = useState(0);             // open positions page (20/page)
  const [pnlSort, setPnlSort] = useState(null);      // null = 默认(新开在前) | "asc" 浮亏在前 | "desc" 浮盈在前
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
    title: "确认平仓", danger: true, ok: "平仓",
    body: `将手动平掉 ${p.coin} ${p.side === "long" ? "多" : "空"}(名义额 ${fUsd(p.notional)})。此操作高危且不可撤销。`,
    onConfirm: async () => { await api.cmd("close_position", { positionId: Number(p.id.replace("pos_", "")) }); toast("已下发平仓指令 " + p.coin); setTimeout(load, 1800); },
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
            {openItems.map(p => (
              <tr key={p.id}>
                <td><span className="tint tint-gray">{p.marketType === "stock" ? "股" : "币"}</span> <b>{p.coin}</b>
                  <a className="ext-link" href={"https://app.hyperliquid.xyz/trade/" + p.coin}
                     target="_blank" rel="noopener noreferrer" title="在 Hyperliquid 看K线" onClick={e => e.stopPropagation()}>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" /><polyline points="15 3 21 3 21 9" /><line x1="10" y1="14" x2="21" y2="3" /></svg></a>
                  {p.addCount > 0 && <span className="tint tint-gray" style={{ marginLeft: 8 }} title="目标加仓、我们跟进的次数(上限2)">加仓{p.addCount}</span>}</td>
                <td><span className={"tint " + (p.side === "long" ? "tint-green" : "tint-red")}>{p.side === "long" ? "多" : "空"}</span></td>
                <td className="num">{fPrice(p.entry)} · {fNum(p.leverage, 0)}x
                  <div className="muted" title="源(目标钱包)的开仓价 · 杠杆">源 {fPrice(p.masterEntry)} · {fNum(p.masterLeverage, 0)}x</div></td>
                <td className="num">{fUsd(p.notional)}
                  <div className="muted" title="源(目标钱包)这一单的名义额(我们 ≤ 它)">源 {fUsd(p.masterNotional)}</div></td>
                <td className="num">{fPrice(p.mark)}</td>
                <td className={"num " + cls(p.unrealizedPnl)}>{fSign(p.unrealizedPnl, 1)}<div className="muted">{fPct(p.unrealizedPctOfMargin, 0)} 保证金</div></td>
                <td className="addr">{short(p.wallet)} <span className="rankbadge">#{p.walletRank}</span></td>
                <td className="num" title="跟单延迟:目标开仓 → 我们检测并跟开的秒数(旧仓未记录显示 —)">{p.lagSec != null ? fNum(p.lagSec, 1) + "s" : "—"}</td>
                <td className={"num " + (p.liqDistancePct != null && p.liqDistancePct > -8 ? "down" : "")} title="距现价多少就触发强平">{fPrice(p.liqPx)}
                  {p.liqDistancePct != null && <div className="muted">差 {fNum(Math.abs(p.liqDistancePct), 1)}%</div>}</td>
                <td><button className="btn btn-danger" onClick={() => doClose(p)}>平仓</button></td>
              </tr>
            ))}
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
function History() {
  const [data, setData] = useState(null);
  const [filter, setFilter] = useState("all");        // all | win | loss
  const [page, setPage] = useState(0);                // 25/page
  useEffect(() => {
    const load = () => api.get("/api/positions?status=closed").then(setData).catch(() => {});
    load(); const t = setInterval(load, 15000); return () => clearInterval(t);
  }, []);
  const PER = 25;
  const st = data && data.stats;
  const all = (data && data.positions) || [];
  const rows = all.filter(p => filter === "all" ? true : filter === "win" ? p.result === "win" : p.result === "loss");
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
            <div className="range-tabs">
              {[["all", "全部"], ["win", "盈利"], ["loss", "亏损"]].map(([k, l]) =>
                <button key={k} className={filter === k ? "on" : ""} onClick={() => { setFilter(k); setPage(0); }}>{l}</button>)}
            </div>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead><tr><th>币种</th><th>方向</th><th className="num">入场/杠杆</th><th className="num">平仓价</th><th className="num">名义额</th><th className="num">已实现盈亏</th><th className="num">持仓时长</th><th>平仓时间</th><th>结果</th><th>钱包</th></tr></thead>
              <tbody>
                {rows.length === 0 && <tr><td colSpan="10" className="empty">暂无</td></tr>}
                {items.map(p => (
                  <tr key={p.id}>
                    <td><b>{p.coin}</b></td>
                    <td><span className={"tint " + (p.side === "long" ? "tint-green" : "tint-red")}>{p.side === "long" ? "多" : "空"}</span></td>
                    <td className="num">{fPrice(p.entry)} · {fNum(p.leverage, 0)}x
                      <div className="muted" title="源(目标钱包)的开仓价 · 杠杆">源 {fPrice(p.masterEntry)} · {fNum(p.masterLeverage, 0)}x</div></td>
                    <td className="num" title="我们的平仓均价(按已实现盈亏反推)">{fPrice(p.closePx)}</td>
                    <td className="num">{fUsd(p.notional)}
                      <div className="muted" title="源(目标钱包)这一单的名义额">源 {fUsd(p.masterNotional)}</div></td>
                    <td className={"num " + cls(p.realizedPnl)}>{fSign(p.realizedPnl, 1)}</td>
                    <td className="num">{fDur(p.durationSec)}</td>
                    <td className="mono" style={{ color: "var(--t2)", fontSize: 12 }}>{fTime(p.closedAt)}</td>
                    <td><span className={"tint " + (p.result === "win" ? "tint-green" : "tint-red")}>{p.result === "win" ? "赢" : "亏"}</span></td>
                    <td className="addr">{short(p.wallet)} {p.walletRank != null
                      ? <span className="rankbadge">#{p.walletRank}</span>
                      : <span className="tint tint-gray">已脱榜</span>}</td>
                  </tr>
                ))}
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
  const [wpage, setWpage] = useState(0);             // 30/page
  const [tab, setTab] = useState("followed");        // followed(在跟) | dropped(已掉线)
  const load = useCallback(() => { api.get("/api/wallets?tab=" + tab + "&page=" + wpage + "&size=30").then(setData).catch(() => {}); }, [wpage, tab]);
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
        <h2>跟踪名单 {data && <span className="muted">· 跟单线 {fNum(data.followLine, 0)} 分 · {dropped ? "降级" : "跟单中"} {data.total} 个</span>}</h2>
        <div className="range-tabs">
          <button className={!dropped ? "on" : ""} onClick={() => { setTab("followed"); setWpage(0); }}>跟单中</button>
          <button className={dropped ? "on" : ""} onClick={() => { setTab("dropped"); setWpage(0); }}>降级</button>
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
              <th className="num">网格</th><th className="num">最差亏</th><th>主力</th><th className="num">被跟</th><th>趋势</th><th>启用</th>
            </tr></thead>
            <tbody>
              {data === null && <tr><td colSpan="13" className="loading">加载中…</td></tr>}
              {data && data.wallets.map(w => (
                <tr key={w.address} className={w.enabled ? "" : "row-off"}
                  style={{ cursor: "pointer" }} onClick={() => setDrawer(w.address)}>
                  <td><span className="rankbadge">{w.rank}</span></td>
                  <td className="addr">{short(w.address)}</td>
                  <td><span className={"tint " + (w.marketType === "crypto" ? "tint-blue" : w.marketType === "stock" ? "tint-amber" : "tint-gray")}>{w.marketType}</span></td>
                  <td className="num"><b style={{ color: w.score >= data.followLine ? "var(--green-l)" : "var(--t2)" }}>{fNum(w.score, 1)}</b></td>
                  <td className={"num up"}>{fNum(w.roiEqPct, 0)}%</td>
                  <td className="num">{fNum(w.winRatePct, 0)}%
                    {(w.closedN > 0 || (w.forwardNetPnl || 0) !== 0) && (() => {
                      const net = w.forwardNetPnl || 0;
                      return <div style={{ fontSize: 10, color: net < 0 ? "var(--red-l)" : "var(--green-l)" }}>
                        实盘 {fSign(net, 0)}{net < -5 ? " ⚠" : ""}</div>;
                    })()}
                  </td>
                  <td className="num">{w.closed7d != null ? w.closed7d : "—"}</td>
                  <td className="num">{fNum(w.grid, 2)}</td>
                  <td className="num down">{fNum(w.worstSingleLossPct, 0)}%</td>
                  <td><b>{w.mainCoin}</b></td>
                  <td className="num">{w.followCount}</td>
                  <td><Spark data={w.trend} /></td>
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
  MIN_FOLLOW_SCORE: { name: "跟单评分线", desc: "评分≥此线才跟单(最常调)", range: "27–67", up: "更严、跟更少精英", dn: "更宽、纳入更多" },
  MIN_TIMES_ACTIVE: { name: "最少验证轮次", desc: "钱包需在≥N轮扫描中合格才跟(剔除单轮运气;1=关闭)", range: "1–5", up: "只跟久经验证的", dn: "纳入新发现" },
  MAX_TARGETS: { name: "最多跟单钱包数", desc: "同时跟单的钱包上限", range: "10–60", up: "更分散", dn: "更集中" },
  LEV_LOWVOL_X: { name: "稳定币最高杠杆", range: "10–25" },
  LEV_HIGHVOL_X: { name: "Meme最高杠杆", range: "1.5–4" },
  RISK_K: { name: "风险保守度", desc: "爆仓安全垫(影响每一笔)", range: "3–6", up: "更保守、仓更小", dn: "更激进、更易爆" },
  RF_MIN: { name: "单仓最小下注比例", desc: "低信心仓位的下注下限", range: "—", up: "底仓更大", dn: "底仓更小" },
  RF_MAX: { name: "单仓最大下注比例", desc: "全押钱包的下注上限", range: "—", up: "重仓更大", dn: "封顶更小" },
  MAX_LEV: { name: "最大杠杆", desc: "杠杆上限(σ估计兜底)", range: "10–50", up: "放开高杠杆", dn: "更严格限杠杆" },
  MIN_LEV: { name: "最小杠杆", desc: "杠杆下限(极波动币≈现货)", range: "—" },
  MIN_OPEN_MARGIN_PCT: { name: "单笔最小开仓额", desc: "低于此则跳过该信号(不开尘埃仓)", range: "—" },
  ADD_MARGIN_PCT: { name: "每次加仓比例", desc: "跟随加仓每次投入(占可用)", range: "—", up: "加仓更猛", dn: "加仓更轻" },
  MAX_ADDS: { name: "最多加仓次数", desc: "跟随主力加仓的次数上限", range: "—", up: "跟更多加仓", dn: "更早停跟加仓" },
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
  max_single_loss: { name: "单笔最大亏损容忍", desc: "单笔亏超此%判扛单", range: "5%–15%", up: "更宽容大亏", dn: "更严筛扛单" },
  EXCLUDE_HFT: { name: "过滤高频HFT(开关)", desc: "剔除秒级快炒钱包——他们赚钱但我们延迟太大抄不了;接入高频WS后可关掉", range: "—" },
  HFT_MIN_HOLD_MIN: { name: "HFT最短中位持仓", desc: "开关开启时,中位持仓低于此分钟数判为HFT剔除", range: "2–5 分钟" },
  SCORE_SHRINK_K: { name: "样本不足惩罚强度", desc: "低样本收益打折", range: "—" },
  SCORE_RAR_CAP: { name: "收益评分上限", desc: "风险调整收益封顶", range: "—" },
  SCORE_K: { name: "评分置信度参数", desc: "日序列置信", range: "—" },
  SCORE_GAMMA: { name: "稳定性严格度", desc: "日一致性指数", range: "—" },
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
      <h2>全量重采进行中…</h2>
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

/* ----------------------------------------------------------------- discovery */
function Discovery({ scanning, startRescan, confirm }) {
  const [d, setD] = useState(null);
  const [runs, setRuns] = useState(null);
  const load = useCallback(() => {
    api.get("/api/discovery").then(setD).catch(() => {});
    api.get("/api/scan-runs?limit=8").then(r => setRuns(r.runs)).catch(() => {});
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 4000); return () => clearInterval(t); }, [load]);  // live
  useEffect(() => { if (!scanning) load(); }, [scanning, load]);  // refresh after a rescan finishes

  const doRescan = () => confirm({
    title: "触发全量重采", danger: true, ok: "开始重采",
    body: "将重新拉取排行榜并重建被跟名单(慢速约 2 小时,期间按钮锁定)。全程让跟单优先、不抢其速率。确认执行?",
    onConfirm: startRescan,
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
        <button className="btn btn-accent" disabled={busy} onClick={doRescan}><Ico d={IC.discovery} /> {busy ? "采集进行中…" : "触发全量重采"}</button></div>
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
function Settings({ startRescan, confirm, toast }) {
  const [params, setParams] = useState(null);
  const [tab, setTab] = useState("scanner");
  const [dev, setDev] = useState(false);
  const [vals, setVals] = useState({});
  const [dirty, setDirty] = useState({});
  const [expanded, setExpanded] = useState(null);

  useEffect(() => {
    api.get("/api/params").then(p => {
      setParams(p);
      const v = {}; [...p.scanner, ...p.follow].forEach(x => { v[x.key] = x.value; });
      setVals(v);
    }).catch(() => {});
  }, []);

  if (!params) return <div className="content"><div className="loading">加载中…</div></div>;
  const list = params[tab];
  const editable = (p) => !(p.type === "display" || p.level === "black") &&
    (p.level !== "blue" || dev);
  const set = (key, val) => { setVals(v => ({ ...v, [key]: val })); setDirty(dd => ({ ...dd, [key]: true })); };
  const tabDirty = list.filter(p => dirty[p.key]);

  const apply = async () => {
    const body = {}; tabDirty.forEach(p => { body[p.key] = vals[p.key]; });
    const doIt = async () => {
      await fetch("/api/params/" + tab, { method: "PATCH", headers: { Authorization: "Bearer " + api.token }, body: JSON.stringify(body) });
      setDirty({});
      if (tab === "scanner") { toast("已保存,触发重采以生效"); startRescan(); }
      else toast("已保存,即时生效");
    };
    if (tab === "scanner") confirm({ title: "应用并重采", danger: false, ok: "应用并重采", body: "采集参数改动需重采才生效,将立即触发全量重采。", onConfirm: doIt });
    else if (tabDirty.some(p => p.level === "yellow")) confirm({ title: "保存跟单参数", danger: false, ok: "保存",
      body: "包含谨慎级参数(影响每一笔新仓),确认即时生效?", onConfirm: doIt });
    else doIt();
  };

  return (
    <div className="content">
      <div className="tabs">
        <div className={"tab" + (tab === "scanner" ? " on" : "")} onClick={() => setTab("scanner")}>采集 watchlist 参数</div>
        <div className={"tab" + (tab === "follow" ? " on" : "")} onClick={() => setTab("follow")}>跟单策略参数</div>
        <label className="devmode"><input type="checkbox" checked={dev} onChange={e => setDev(e.target.checked)} /> 开发者模式(解锁进阶)</label>
      </div>

      <div className="tbl-wrap">
        {list.map(p => {
          const m = PARAM_META[p.key] || {};
          const ed = editable(p);
          const lvl = p.level;
          return (
            <div key={p.key}>
              <div className={"prow" + (dirty[p.key] ? " dirty" : "")}>
                <span className={"lvl-dot lvl-" + lvl} title={lvl} />
                <div className="pn"><b>{p.name || m.name || p.key}</b><div className="pk">{p.key}</div></div>
                <div className="pd">{p.desc || m.desc}
                  {m.range && m.range !== "—" && <span style={{ color: "var(--t4)" }}> · 建议 {m.range}</span>}
                </div>
                <div className="pctl">
                  {p.type === "bool" ? (
                    <div className={"toggle " + (vals[p.key] ? "on" : "")} onClick={() => ed && set(p.key, !vals[p.key])} style={{ opacity: ed ? 1 : .5 }}><div className="knob" /></div>
                  ) : p.type === "display" ? (
                    <span className="mono" style={{ color: "var(--t2)", fontSize: 12 }}>{p.value}</span>
                  ) : (
                    <React.Fragment>
                      <input className="pinput" type={p.type === "nullable" ? "text" : "number"} disabled={!ed}
                        value={vals[p.key] == null ? "" : vals[p.key]}
                        placeholder={p.type === "nullable" ? "关闭" : ""}
                        onChange={e => set(p.key, e.target.value === "" ? null : Number(e.target.value))} />
                      <span className="punit">{UNIT[p.type] || ""}</span>
                    </React.Fragment>
                  )}
                  {!ed && lvl === "blue" && <span className="plock" title="开发者模式解锁">🔒</span>}
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
const NAV = [
  ["监控", [["overview", "总览", IC.overview], ["positions", "持仓中", IC.positions], ["history", "历史持仓", IC.history], ["wallets", "跟踪钱包", IC.wallets]]],
  ["控制", [["discovery", "采集", IC.discovery], ["settings", "策略参数", IC.settings]]],
];
const TITLES = { overview: "总览 Overview", positions: "持仓中 Positions", history: "历史持仓 History", wallets: "跟踪钱包 Wallets", discovery: "采集 Discovery", settings: "策略参数 Settings" };

function Dashboard({ onLogout }) {
  const [page, setPage] = useState("overview");
  const [polledOv, setPolledOv] = useState(null);
  const [live, setLive] = useState(null);            // SSE fast bundle {overview, positions, serverTime}
  const [streamOk, setStreamOk] = useState(false);
  const [confirmCfg, setConfirmCfg] = useState(null);
  const [toastMsg, setToastMsg] = useState(null);
  const [busy, setBusy] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [scanStatus, setScanStatus] = useState(null);
  const toast = (m) => { setToastMsg(m); setTimeout(() => setToastMsg(null), 2600); };

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

  const startRescan = useCallback(async () => { await api.cmd("rescan", {}); setScanning(true); }, []);
  // The SERVER is the source of truth for "a full scan is running" (scan_progress / process_status,
  // surfaced as system.scanner). Driving the mask off this — not just a click in this tab — means the
  // mask survives a page refresh/reopen AND catches the 24h auto-scan, so you can't refresh past it and
  // double-click 重采. (Backend is already single-executor + absorbs duplicate rescans; this closes the UX gap.)
  const serverScanning = !!(ov && ov.system && ov.system.scanner === "scanning");
  useEffect(() => { if (serverScanning) setScanning(true); }, [serverScanning]);
  // one-shot on mount: if a scan is already in flight, raise the mask IMMEDIATELY (before the first
  // overview/stream push arrives), so a refresh during a scan never briefly exposes the page.
  useEffect(() => {
    api.get("/api/scan-status").then((s) => {
      if (s && s.state === "scanning") { setScanning(true); setScanStatus(s); }
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

  const obs = ov && ov.system ? ov.system.observer : "stopped";   // stopped | running | paused
  const obsUp = obs === "running" || obs === "paused";            // process is alive (vs not started)
  const pausing = busy;
  const cmdThen = (type, msg) => { setBusy(true); api.cmd(type, {}).then(() => { toast(msg); setTimeout(() => { loadOv(); setBusy(false); }, 2000); }); };
  // PROCESS lifecycle (启动/停止整个 observer 进程) — routed through the scan-trigger supervisor via systemctl.
  const toggleObserver = () => {
    if (!obsUp) {                                   // not running -> start the whole process
      cmdThen("observer_start", "已下发启动跟单指令(进程启动中…)");
    } else {                                        // running -> stop the whole process (positions unmanaged)
      setConfirmCfg({ title: "停止跟单", danger: true, ok: "停止整个进程",
        body: "将停止整个 Observer 进程:不再开新仓,且存量持仓也不再被管理(进程重启后会自动重新接管)。若只想停开新仓、让存量继续跟到平仓,请改用「暂停跟单」。",
        onConfirm: () => cmdThen("observer_stop", "已下发停止跟单指令") });
    }
  };
  // SOFT pause (停开新仓、存量跟到平仓,进程保持运行) — only meaningful while the process is up.
  const togglePause = () => {
    if (obs === "running") {
      setConfirmCfg({ title: "暂停跟单", danger: false, ok: "暂停",
        body: "暂停后 Observer 停止开新仓,存量持仓继续跟到平仓(进程保持运行)。",
        onConfirm: () => cmdThen("pause", "已下发暂停指令") });
    } else if (obs === "paused") {
      cmdThen("resume", "已下发恢复指令");
    }
  };

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
            {!(ov && ov.system) ? null : !obsUp
              ? /* 进程未运行 → 只有「启动跟单」(绿) */
                <button className="btn btn-go" onClick={toggleObserver} disabled={pausing}>{pausing ? <span className="spin" /> : <span className="dot" style={{ width: 7, height: 7, borderRadius: 9, background: "#fff" }} />} {pausing ? "启动中…" : "启动跟单"}</button>
              : /* 运行中 → 软暂停/恢复(绿/珊瑚) + 停止整个进程(红) */
                <>
                  {obs === "paused"
                    ? <button className="btn btn-go" onClick={togglePause} disabled={pausing}>{pausing ? <span className="spin" /> : <span className="dot" style={{ width: 7, height: 7, borderRadius: 9, background: "#fff" }} />} {pausing ? "恢复中…" : "恢复跟单"}</button>
                    : <button className="btn btn-accent" onClick={togglePause} disabled={pausing}>{pausing ? <span className="spin" /> : <span className="dot" style={{ width: 7, height: 7, borderRadius: 9, background: "#fff" }} />} {pausing ? "暂停中…" : "暂停跟单"}</button>}
                  <button className="btn btn-danger" onClick={toggleObserver} disabled={pausing} title="停止整个 Observer 进程">停止跟单</button>
                </>}
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
            <div className="chip"><div className="k">Observer</div><div className="v" style={{ fontSize: 13, color: obs === "paused" ? "var(--red-l)" : "var(--green-l)" }}>{obs === "paused" ? "已暂停" : "运行中"}{ov.system.observerStale ? " ⚠" : ""}</div></div>
            {(() => { const sc = ov.system.scanner, stale = ov.system.scannerStale;
              return <div className="chip"><div className="k">采集</div><div className="v" style={{ fontSize: 13, color: scannerColor(sc, stale) }}>{SCANNER_LABEL[sc] || sc}{stale && sc !== "idle" ? " ⚠" : ""}</div></div>; })()}
          </div>
        )}

        {page === "overview" && <Overview ov={ov} />}
        {page === "positions" && <Positions confirm={setConfirmCfg} toast={toast} streamOpen={streamOk ? (live && live.positions) : null} />}
        {page === "history" && <History />}
        {page === "wallets" && <Wallets confirm={setConfirmCfg} toast={toast} />}
        {page === "discovery" && <Discovery scanning={scanning} startRescan={startRescan} confirm={setConfirmCfg} />}
        {page === "settings" && <Settings startRescan={startRescan} confirm={setConfirmCfg} toast={toast} />}
      </main>

      {(scanning || serverScanning) && <ScanMask status={scanStatus} />}
      <Confirm cfg={confirmCfg} onClose={() => setConfirmCfg(null)} />
      {toastMsg && <div style={{ position: "fixed", top: 18, right: 18, zIndex: 50, background: "rgba(20,20,24,.96)", border: "1px solid var(--glass-border)", padding: "11px 16px", borderRadius: 12, fontSize: 13 }}>{toastMsg}</div>}
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
