import { cls, fNum, fPrice, fSign, fTime, fUsd } from "../../lib/format.js";

const ACT_TINT = { 开仓: "tint-green", 加仓: "tint-blue", 减仓: "tint-amber", 平仓: "tint-gray" };

export function PositionDetail({ d }) {
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
