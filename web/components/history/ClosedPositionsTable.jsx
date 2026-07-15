import { cls, fDur, fNum, fPrice, fSign, fTime, fUsd, short } from "../../lib/format.js";
import { PositionDetail } from "../positions/PositionDetail.jsx";

const CLOSE_TYPE = {
  mirror: { label: "镜像", tint: "tint-blue" },
  stop: { label: "止损", tint: "tint-amber" },
  liq: { label: "爆仓", tint: "tint-red" },
  tail: { label: "尾盈", tint: "tint-green" },
};

export function ClosedPositionsTable({
  rows,
  items,
  expandedId,
  details,
  toggleRow,
  pg,
  pages,
  setPage,
  perPage,
}) {
  return (
    <React.Fragment>
      <div className="tbl-wrap">
        <table>
          <thead><tr><th>币种</th><th>方向</th><th className="num">源入场/杠杆</th><th className="num">入场/杠杆</th><th>结算类型</th><th className="num">名义额</th><th className="num">已实现盈亏</th><th className="num">持仓时长</th><th>平仓时间</th><th>钱包</th></tr></thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan="10" className="empty">暂无</td></tr>}
            {items.map(p => { const pid = Number(String(p.id).replace("cls_", "")); const isOpen = expandedId === pid;
              return <React.Fragment key={p.id}>
              <tr onClick={() => toggleRow(p.id)} style={{ cursor: "pointer" }} className={isOpen ? "row-open" : ""}>
                <td><span className="row-caret" style={{ transform: isOpen ? "rotate(90deg)" : "none" }}>▸</span> <b>{p.coin}</b>
                  {p.addCount > 0 && <span className="tint tint-gray" style={{ marginLeft: 8 }} title="目标加仓、我们跟进的次数">加仓{p.addCount}</span>}
                  {p.shadowRisk?.wouldBlock && <span className="tint tint-ai" title={`开仓时风险分 ${p.shadowRisk.riskScore?.toFixed(1) || "—"}`}>Shadow · AI拟拦截</span>}
                  {p.shadowRisk?.outcome === "avoided_loss" && <span className="tint tint-green" style={{ marginLeft: 6 }}>本可避免亏损</span>}
                  {p.shadowRisk?.outcome === "missed_profit" && <span className="tint tint-red" style={{ marginLeft: 6 }}>会错过盈利</span>}</td>
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
      {rows.length > perPage && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 12, marginTop: 10 }}>
          <button className="btn" disabled={pg <= 0} onClick={() => setPage(pg - 1)}>上一页</button>
          <span className="muted mono">第 {pg + 1} / {pages} 页</span>
          <button className="btn" disabled={pg >= pages - 1} onClick={() => setPage(pg + 1)}>下一页</button>
        </div>
      )}
    </React.Fragment>
  );
}
