import { api } from "../lib/api.js";
import { cls, fDur, fNum, fPrice, fSign, fTime, fUsd, short } from "../lib/format.js";
import { usePolling } from "../lib/refresh.js";
import { PositionDetail } from "./Positions.jsx";

const { useState, useCallback } = React;

const CLOSE_TYPE = {
  mirror: { label: "镜像", tint: "tint-blue" },
  stop: { label: "止损", tint: "tint-amber" },
  liq: { label: "爆仓", tint: "tint-red" },
};

export function History() {
  const [data, setData] = useState(null);
  const [filter, setFilter] = useState("all");
  const [ctype, setCtype] = useState("all");
  const [page, setPage] = useState(0);
  const [expandedId, setExpandedId] = useState(null);
  const [details, setDetails] = useState({});
  const toggleRow = (rowId) => {
    const pid = Number(String(rowId).replace("cls_", ""));
    if (expandedId === pid) { setExpandedId(null); return; }
    setExpandedId(pid);
    if (!details[pid]) api.get(`/api/positions/${pid}`).then(d => setDetails(m => ({ ...m, [pid]: d }))).catch(() => {});
  };
  const loadClosed = useCallback(() => { api.get("/api/positions?status=closed").then(setData).catch(() => {}); }, []);
  usePolling(loadClosed, 15000);
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
