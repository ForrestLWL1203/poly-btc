import { api } from "../lib/api.js";
import { cls, fNum, fPct, fSign, fUsd, short } from "../lib/format.js";
import { useApiResource } from "../lib/refresh.js";

const { useState, useCallback } = React;

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

export function Overview({ ov }) {
  const [range, setRange] = useState("7d");
  const loadEquity = useCallback(() => api.get("/api/equity?range=" + range), [range]);
  const loadInsights = useCallback(() => api.get("/api/insights"), []);
  const { data: eq } = useApiResource(loadEquity);
  const { data: ins } = useApiResource(loadInsights, { intervalMs: 15000 });
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
