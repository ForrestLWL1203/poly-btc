/* 跟单监控台 — build-free React preview (babel-standalone). Talks to the live dashboard API.
   Pages: Overview / Positions / Wallets (P0). Discovery / Settings are stubbed for now. */
const { useState, useEffect, useRef, useCallback } = React;

const DASH_PW = "mock123";                       // preview auto-login (matches launch.json env)
const TOK_KEY = "hl_dash_token";

/* ----------------------------------------------------------------- api */
const api = {
  token: localStorage.getItem(TOK_KEY) || null,
  async login(pw) {
    const r = await fetch("/api/auth/login", { method: "POST", body: JSON.stringify({ password: pw }) });
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
const fPct = (v, d = 1) => (v == null ? "—" : (v >= 0 ? "+" : "") + Number(v).toFixed(d) + "%");
const fNum = (v, d = 1) => (v == null ? "—" : Number(v).toFixed(d));
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
const SCANNER_LABEL = { rolling: "滚动采集中", scanning: "全量重采中", stopped: "已停止", unknown: "未知" };

/* ----------------------------------------------------------------- icons */
const Ico = ({ d }) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d={d} /></svg>;
const IC = {
  overview: "M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z",
  positions: "M3 3v18h18M7 16l4-4 3 3 5-6",
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
  useEffect(() => { api.get("/api/equity?range=" + range).then(setEq).catch(() => {}); }, [range]);
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

      <div className="card chart-card">
        <div className="section-h" style={{ margin: "0 0 8px" }}>
          <h2>权益曲线</h2>
          <div className="range-tabs">
            {["1d", "7d", "all"].map(x => <button key={x} className={range === x ? "on" : ""} onClick={() => setRange(x)}>{x.toUpperCase()}</button>)}
          </div>
        </div>
        <EquityChart points={eq && eq.points} />
      </div>

      <div className="grid2" style={{ marginTop: 14 }}>
        <div className="card">
          <div className="card-lbl">风险敞口</div>
          <div style={{ display: "flex", gap: 26, margin: "12px 0 14px" }}>
            <div><div className="muted">毛敞口</div><div className="mono" style={{ fontSize: 18 }}>{fUsd(r.gross)}</div></div>
            <div><div className="muted">净敞口</div><div className="mono" style={{ fontSize: 18 }}>{fUsd(r.net)}</div></div>
            <div><div className="muted">净·毛比</div><div className="mono" style={{ fontSize: 18 }}>{fNum(r.netGrossRatioPct, 0)}%</div></div>
          </div>
          <div className="bar-row"><div className="bl">多头</div>
            <div className="bar-track"><div className="bar-fill" style={{ width: r.longPct + "%", background: "var(--green)" }} /></div>
            <div className="bv">{fNum(r.longPct, 0)}%</div></div>
          <div className="bar-row"><div className="bl">空头</div>
            <div className="bar-track"><div className="bar-fill" style={{ width: r.shortPct + "%", background: "var(--red)" }} /></div>
            <div className="bv">{fNum(r.shortPct, 0)}%</div></div>
        </div>
        <div className="card">
          <div className="card-lbl">手续费 / 效率</div>
          <div style={{ display: "flex", gap: 40, marginTop: 14 }}>
            <div><div className="muted">累计手续费</div><div className="mono" style={{ fontSize: 22, marginTop: 6 }}>{fUsd(f.cumulative, 0)}</div></div>
            <div><div className="muted">净利 / 毛成交额</div><div className="mono" style={{ fontSize: 22, marginTop: 6 }}>{fNum(f.netPerGrossBp, 1)} bp</div></div>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ----------------------------------------------------------------- positions */
function Positions({ confirm, toast }) {
  const [open, setOpen] = useState(null);
  const [closed, setClosed] = useState(null);
  const [filter, setFilter] = useState("all");
  const load = useCallback(() => {
    api.get("/api/positions?status=open").then(setOpen).catch(() => {});
    api.get("/api/positions?status=closed").then(setClosed).catch(() => {});
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 6000); return () => clearInterval(t); }, [load]);

  const doClose = (p) => confirm({
    title: "确认平仓", danger: true, ok: "平仓",
    body: `将手动平掉 ${p.coin} ${p.side === "long" ? "多" : "空"}(名义额 ${fUsd(p.notional)})。此操作高危且不可撤销。`,
    onConfirm: async () => { await api.cmd("close_position", { positionId: Number(p.id.replace("pos_", "")) }); toast("已下发平仓指令 " + p.coin); setTimeout(load, 1800); },
  });

  const filt = (rows) => !rows ? [] : rows.filter(p =>
    filter === "all" ? true : filter === "crypto" ? p.marketType === "crypto" :
    filter === "stock" ? p.marketType === "stock" : filter === "long" ? p.side === "long" : p.side === "short");

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
            <th>币种</th><th>方向</th><th className="num">入场/杠杆</th><th className="num">名义额</th><th className="num">σ</th>
            <th className="num">现价</th><th className="num">浮动盈亏</th><th>钱包</th><th className="num">lag</th><th className="num">爆仓距离</th><th></th>
          </tr></thead>
          <tbody>
            {open === null && <tr><td colSpan="11" className="loading">加载中…</td></tr>}
            {open && filt(open.positions).length === 0 && <tr><td colSpan="11" className="empty">无持仓</td></tr>}
            {open && filt(open.positions).map(p => (
              <tr key={p.id}>
                <td><span className="tint tint-gray">{p.marketType === "stock" ? "股" : "币"}</span> <b>{p.coin}</b></td>
                <td><span className={"tint " + (p.side === "long" ? "tint-green" : "tint-red")}>{p.side === "long" ? "多" : "空"}</span></td>
                <td className="num">{fNum(p.entry, 1)} · {fNum(p.leverage, 0)}x</td>
                <td className="num">{fUsd(p.notional)}</td>
                <td className="num">{fNum(p.sigmaPct, 1)}%</td>
                <td className="num">{fNum(p.mark, 1)}</td>
                <td className={"num " + cls(p.unrealizedPnl)}>{fSign(p.unrealizedPnl, 1)}<div className="muted">{fPct(p.unrealizedPctOfMargin, 0)} 保证金</div></td>
                <td className="addr">{short(p.wallet)} <span className="rankbadge">#{p.walletRank}</span></td>
                <td className="num">{fNum(p.lagSec, 1)}s</td>
                <td className={"num " + (p.liqDistancePct > -5 ? "down" : "")}>{fNum(p.liqDistancePct, 1)}%</td>
                <td><button className="btn btn-danger" onClick={() => doClose(p)}>平仓</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="section-h"><h2>已平仓历史</h2></div>
      <div className="tbl-wrap">
        <table>
          <thead><tr><th>币种</th><th>方向</th><th className="num">已实现盈亏</th><th className="num">持仓时长</th><th>结果</th><th>钱包</th></tr></thead>
          <tbody>
            {closed === null && <tr><td colSpan="6" className="loading">加载中…</td></tr>}
            {closed && closed.positions.length === 0 && <tr><td colSpan="6" className="empty">暂无</td></tr>}
            {closed && closed.positions.map(p => (
              <tr key={p.id}>
                <td><b>{p.coin}</b></td>
                <td><span className={"tint " + (p.side === "long" ? "tint-green" : "tint-red")}>{p.side === "long" ? "多" : "空"}</span></td>
                <td className={"num " + cls(p.realizedPnl)}>{fSign(p.realizedPnl, 1)}</td>
                <td className="num">{(p.durationSec / 3600).toFixed(1)}h</td>
                <td><span className={"tint " + (p.result === "win" ? "tint-green" : "tint-red")}>{p.result === "win" ? "赢" : "亏"}</span></td>
                <td className="addr">{short(p.wallet)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ----------------------------------------------------------------- wallets */
function Wallets({ confirm, toast }) {
  const [data, setData] = useState(null);
  const [drawer, setDrawer] = useState(null);
  const load = useCallback(() => { api.get("/api/wallets").then(setData).catch(() => {}); }, []);
  useEffect(() => { load(); const t = setInterval(load, 12000); return () => clearInterval(t); }, [load]);

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
        <h2>被跟名单 {data && <span className="muted">· 跟单线 {fNum(data.followLine, 0)} 分</span>}</h2>
      </div>
      <div className="tbl-wrap">
        <table>
          <thead><tr>
            <th>#</th><th>地址</th><th>市场</th><th className="num">评分</th><th className="num">ROI</th><th className="num">胜率</th>
            <th className="num">网格</th><th className="num">最差亏</th><th>主力</th><th className="num">被跟</th><th>趋势</th><th>启用</th>
          </tr></thead>
          <tbody>
            {data === null && <tr><td colSpan="12" className="loading">加载中…</td></tr>}
            {data && data.wallets.map(w => (
              <tr key={w.address} className={w.enabled ? "" : "row-off"}>
                <td><span className="rankbadge">{w.rank}</span></td>
                <td className="addr" style={{ cursor: "pointer" }} onClick={() => setDrawer(w.address)}>{short(w.address)}</td>
                <td><span className={"tint " + (w.marketType === "crypto" ? "tint-blue" : w.marketType === "stock" ? "tint-amber" : "tint-gray")}>{w.marketType}</span></td>
                <td className="num"><b style={{ color: w.score >= data.followLine ? "var(--green-l)" : "var(--t2)" }}>{fNum(w.score, 1)}</b></td>
                <td className={"num up"}>{fNum(w.roiEqPct, 0)}%</td>
                <td className="num">{fNum(w.winRatePct, 0)}%</td>
                <td className="num">{fNum(w.grid, 2)}</td>
                <td className="num down">{fNum(w.worstSingleLossPct, 0)}%</td>
                <td><b>{w.mainCoin}</b></td>
                <td className="num">{w.followCount}</td>
                <td><Spark data={w.trend} /></td>
                <td><div className={"toggle " + (w.enabled ? "on" : "")} onClick={() => toggle(w)}><div className="knob" /></div></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {drawer && <WalletDrawer address={drawer} onClose={() => setDrawer(null)} />}
    </div>
  );
}

function WalletDrawer({ address, onClose }) {
  const [d, setD] = useState(null);
  useEffect(() => { api.get("/api/wallets/" + address).then(setD).catch(() => {}); }, [address]);
  return (
    <React.Fragment>
      <div className="scrim" onClick={onClose} />
      <div className="drawer">
        <h3>{short(address)}</h3>
        <div className="muted" style={{ marginBottom: 18 }}>排名 #{d ? d.rank : "—"} · {d ? d.marketType : ""}</div>
        {!d ? <div className="loading">加载中…</div> : (
          <React.Fragment>
            <div className="grid2" style={{ gridTemplateColumns: "1fr 1fr 1fr", gap: 10, marginBottom: 18 }}>
              <div className="card"><div className="card-lbl">累计盈亏</div><div className={"mono " + cls(d.cumulativePnl)} style={{ fontSize: 18, marginTop: 6 }}>{fSign(d.cumulativePnl, 0)}</div></div>
              <div className="card"><div className="card-lbl">胜率</div><div className="mono" style={{ fontSize: 18, marginTop: 6 }}>{fNum(d.winRatePct, 0)}%</div></div>
              <div className="card"><div className="card-lbl">评分</div><div className="mono" style={{ fontSize: 18, marginTop: 6 }}>{fNum(d.score, 1)}</div></div>
            </div>
            <div className="card-lbl" style={{ marginBottom: 8 }}>跟单记录</div>
            <div className="tbl-wrap">
              <table><thead><tr><th>币种</th><th>方向</th><th className="num">盈亏</th><th>状态</th></tr></thead>
                <tbody>{d.records.map((r, i) => (
                  <tr key={i}><td><b>{r.coin}</b></td>
                    <td><span className={"tint " + (r.side === "long" ? "tint-green" : "tint-red")}>{r.side === "long" ? "多" : "空"}</span></td>
                    <td className={"num " + cls(r.pnl)}>{fSign(r.pnl, 1)}</td>
                    <td className="muted">{r.status}</td></tr>
                ))}</tbody></table>
            </div>
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
  MAX_TARGETS: { name: "最多跟单钱包数", desc: "同时跟单的钱包上限", range: "10–60", up: "更分散", dn: "更集中" },
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
    body: "将重新拉取排行榜并重建被跟名单(约 20 分钟,期间页面锁定)。占用资源,确认执行?",
    onConfirm: startRescan,
  });

  if (!d) return <div className="content"><div className="loading">加载中…</div></div>;
  const fn = d.funnel, h = d.scoreHistogram, maxBin = Math.max(...h.bins, 1);
  const sc = d.scanner || { mode: "unknown", detail: {} }, det = sc.detail || {};
  const scMode = sc.mode, scColor = scMode === "scanning" ? "var(--amber)" : (scMode === "rolling" && !sc.stale) ? "var(--green-l)" : "var(--red-l)";
  const cyclePct = det.cycle_total ? Math.round(det.cycle_pos / det.cycle_total * 100) : 0;
  return (
    <div className="content">
      <div className="section-h" style={{ marginTop: 6 }}><h2>采集进程 · 实时</h2>
        <button className="btn btn-accent" onClick={doRescan}><Ico d={IC.discovery} /> 触发全量重采</button></div>
      <div className="card">
        <div style={{ display: "flex", alignItems: "center", gap: 20, flexWrap: "wrap" }}>
          <span className="pill" style={{ background: "rgba(255,255,255,.05)", color: scColor }}>
            <span className="dot" style={{ background: scColor, animation: scMode === "rolling" ? "pulse 1.4s infinite" : "none" }} />
            {SCANNER_LABEL[scMode] || scMode}{sc.stale ? " · 心跳超时 ⚠" : ""}</span>
          <div><div className="muted">本轮进度</div><div className="mono" style={{ fontSize: 15 }}>{det.cycle_pos ?? "—"} / {det.cycle_total ?? "—"} <span className="muted">({cyclePct}%)</span></div></div>
          <div><div className="muted">采集节奏</div><div className="mono" style={{ fontSize: 15 }}>每 ~{det.interval_s ?? "—"}s / 个</div></div>
          <div><div className="muted">最近更新</div><div className="mono" style={{ fontSize: 15 }}>{short(det.last_addr)} · {agoText(det.last_at)}</div></div>
          <div><div className="muted">心跳</div><div className="mono" style={{ fontSize: 15, color: sc.stale ? "var(--red-l)" : "var(--green-l)" }}>{agoText(sc.heartbeatAt)}</div></div>
          <div><div className="muted">上次全量重采</div><div className="mono" style={{ fontSize: 15 }}>{agoText(d.lastScanAt)}</div></div>
        </div>
        <div className="bar-track" style={{ marginTop: 14, height: 6 }}>
          <div className="bar-fill" style={{ width: cyclePct + "%", background: "var(--accent-grad)" }} /></div>
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
                <div className="pn"><b>{m.name || p.key}</b><div className="pk">{p.key}</div></div>
                <div className="pd">{m.desc}
                  {m.range && m.range !== "—" && <span style={{ color: "var(--t4)" }}> · 建议 {m.range}</span>}
                  {(m.up || m.dn) && <span onClick={() => setExpanded(expanded === p.key ? null : p.key)}
                    style={{ marginLeft: 8, color: "var(--blue-l)", cursor: "pointer", fontSize: 11 }}>影响 {expanded === p.key ? "▴" : "▾"}</span>}
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
  ["监控", [["overview", "总览", IC.overview], ["positions", "持仓", IC.positions], ["wallets", "跟单钱包", IC.wallets]]],
  ["控制", [["discovery", "采集", IC.discovery], ["settings", "策略参数", IC.settings]]],
];
const TITLES = { overview: "总览 Overview", positions: "持仓 Positions", wallets: "跟单钱包 Wallets", discovery: "采集 Discovery", settings: "策略参数 Settings" };

function Dashboard({ onLogout }) {
  const [page, setPage] = useState("overview");
  const [ov, setOv] = useState(null);
  const [confirmCfg, setConfirmCfg] = useState(null);
  const [toastMsg, setToastMsg] = useState(null);
  const [busy, setBusy] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [scanStatus, setScanStatus] = useState(null);
  const toast = (m) => { setToastMsg(m); setTimeout(() => setToastMsg(null), 2600); };

  const loadOv = useCallback(() => { api.get("/api/overview").then(setOv).catch(() => {}); }, []);
  useEffect(() => { loadOv(); const t = setInterval(loadOv, 7000); return () => clearInterval(t); }, [loadOv]);

  const startRescan = useCallback(async () => { await api.cmd("rescan", {}); setScanning(true); }, []);
  useEffect(() => {                                  // poll scan progress while a rescan runs (mask)
    if (!scanning) return;
    let alive = true, started = Date.now(), seen = false;
    const tick = async () => {
      try {
        const s = await api.get("/api/scan-status");
        if (!alive) return;
        if (s.state === "scanning") { seen = true; setScanStatus(s); }
        else if (seen || Date.now() - started > 8000) {   // grace: wait for the scanner to pick it up
          setScanning(false); setScanStatus(null);
        }
      } catch (_e) {}
    };
    tick(); const t = setInterval(tick, 1200);
    return () => { alive = false; clearInterval(t); };
  }, [scanning]);

  const obs = ov && ov.system ? ov.system.observer : "running";
  const pausing = busy;
  const togglePause = () => {
    if (obs === "running") {
      setConfirmCfg({ title: "暂停跟单", danger: false, ok: "暂停",
        body: "暂停后 Observer 停止开新仓,存量持仓继续跟到平仓。",
        onConfirm: async () => { setBusy(true); await api.cmd("pause", {}); toast("已下发暂停指令"); setTimeout(() => { loadOv(); setBusy(false); }, 2000); } });
    } else {
      (async () => { setBusy(true); await api.cmd("resume", {}); toast("已下发恢复指令"); setTimeout(() => { loadOv(); setBusy(false); }, 2000); })();
    }
  };

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand"><div className="mk">跟</div><div><b>跟单监控台</b><span>COPY-TRADE OPS</span></div></div>
        {NAV.map(([grp, items]) => (
          <div key={grp}>
            <div className="nav-group">{grp}</div>
            {items.map(([k, label, d]) => (
              <div key={k} className={"nav-item" + (page === k ? " active" : "")} onClick={() => setPage(k)}>
                <Ico d={d} />{label}
              </div>
            ))}
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
            <span className="pill pill-paper"><span className="dot" style={{ background: "var(--amber)" }} /> 运行模式 · Paper</span>
            {obs === "paused"
              ? <button className="btn btn-green" onClick={togglePause} disabled={pausing}>{pausing ? <span className="spin" /> : <span className="dot" style={{ width: 7, height: 7, borderRadius: 9, background: "var(--green)" }} />} {pausing ? "恢复中…" : "恢复跟单"}</button>
              : <button className="btn btn-accent" onClick={togglePause} disabled={pausing}>{pausing ? <span className="spin" /> : <span className="dot" style={{ width: 7, height: 7, borderRadius: 9, background: "#fff" }} />} {pausing ? "暂停中…" : "暂停跟单"}</button>}
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
              const color = (sc === "scanning") ? "var(--amber)" : (sc === "rolling" && !stale) ? "var(--green-l)" : "var(--red-l)";
              return <div className="chip"><div className="k">采集</div><div className="v" style={{ fontSize: 13, color }}>{SCANNER_LABEL[sc] || sc}{stale ? " ⚠" : ""}</div></div>; })()}
          </div>
        )}

        {page === "overview" && <Overview ov={ov} />}
        {page === "positions" && <Positions confirm={setConfirmCfg} toast={toast} />}
        {page === "wallets" && <Wallets confirm={setConfirmCfg} toast={toast} />}
        {page === "discovery" && <Discovery scanning={scanning} startRescan={startRescan} confirm={setConfirmCfg} />}
        {page === "settings" && <Settings startRescan={startRescan} confirm={setConfirmCfg} toast={toast} />}
      </main>

      {scanning && <ScanMask status={scanStatus} />}
      <Confirm cfg={confirmCfg} onClose={() => setConfirmCfg(null)} />
      {toastMsg && <div style={{ position: "fixed", top: 18, right: 18, zIndex: 50, background: "rgba(20,20,24,.96)", border: "1px solid var(--glass-border)", padding: "11px 16px", borderRadius: 12, fontSize: 13 }}>{toastMsg}</div>}
    </div>
  );
}

/* ----------------------------------------------------------------- root */
function App() {
  const [authed, setAuthed] = useState(false);
  const [err, setErr] = useState(null);
  const [pw, setPw] = useState(DASH_PW);

  // On mount: validate any existing token; if invalid/missing, auto-login (preview). This survives a
  // server restart that wiped in-memory tokens while a stale token still sits in localStorage.
  useEffect(() => {
    (async () => {
      try {
        if (api.token) { await api.get("/api/overview"); setAuthed(true); return; }
      } catch (_e) { /* stale token -> fall through to login */ }
      try { await api.login(DASH_PW); setAuthed(true); } catch (_e) { setAuthed(false); }
    })();
  }, []);

  const doLogin = async () => {
    try { await api.login(pw); setAuthed(true); setErr(null); }
    catch (_e) { setErr("密码错误"); }
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
        <input type="password" value={pw} onChange={e => setPw(e.target.value)}
          onKeyDown={e => e.key === "Enter" && doLogin()} placeholder="密码" />
        <button className="btn btn-accent" style={{ width: "100%" }} onClick={doLogin}>登录</button>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
