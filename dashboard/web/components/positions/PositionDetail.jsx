import { cls, fNum, fPrice, fSign, fTime, fUsd } from "../../lib/format.js";

const ACT_TINT = { 开仓: "tint-green", 加仓: "tint-blue", 减仓: "tint-amber", 平仓: "tint-gray", 尾盈平仓: "tint-green" };

export function PositionDetail({ d }) {
  if (!d) return <div className="muted" style={{ padding: "14px 16px" }}>加载中…</div>;
  const live = d.status === "open";
  const pnl = live ? d.unrealizedPnl : d.realizedPnl;
  return (
    <div className="pos-detail">
      <div className="pos-detail-metrics">
        <div className="pos-detail-stat">
          <span>源加仓</span><b>{d.masterAdds}<small>次</small></b>
        </div>
        <div className="pos-detail-stat">
          <span>我方加仓</span><b>{d.ourAdds}<small>次</small></b>
        </div>
        <div className="pos-detail-stat">
          <span>源成本均价</span><b>{fPrice(d.masterEntry)}<small>{d.masterLeverage != null ? fNum(d.masterLeverage, 0) + "x" : "—x"}</small></b>
        </div>
        <div className="pos-detail-stat">
          <span>我方成本均价</span><b>{fPrice(d.ourEntry)}<small>{fNum(d.ourLeverage, 0)}x</small></b>
        </div>
        <div className="pos-detail-stat">
          <span>占用保证金</span><b>{fUsd(d.ourMargin)}</b>
        </div>
        <div className="pos-detail-stat pnl">
          <span>{live ? "浮动盈亏" : "已实现盈亏"}</span><b className={cls(pnl)}>{fSign(pnl, 1)}</b>
        </div>
      </div>
      <div className="pos-detail-section-head">
        <h4>成交记录</h4>
        <span>{d.fills.length} 笔</span>
      </div>
      <table className="fills-tbl">
        <thead><tr><th>时间</th><th>动作</th><th className="num">价格</th><th className="num">保证金投入/返还</th><th className="num">数量</th><th className="num">盈亏</th></tr></thead>
        <tbody>
          {d.fills.length === 0 && <tr><td colSpan="6" className="muted" style={{ padding: "6px 8px" }}>暂无成交</td></tr>}
          {d.fills.map((f, i) => (
            <tr key={i}>
              <td className="mono muted">{fTime(f.atSec)}</td>
              <td><span className={"tint " + (ACT_TINT[f.actionLabel] || "tint-gray")}>{f.actionLabel}</span>
                {f.fillCount > 1 && <span className="muted" style={{ marginLeft: 4, fontSize: 10 }} title="该订单分多笔成交">×{f.fillCount}</span>}</td>
              <td className="num">{fPrice(f.px)}</td>
              <td className={"num " + (f.capitalKind === "返还" && f.capital < 0 ? "down" : "")}
                title={f.capitalKind === "返还" && f.releasedMargin != null
                  ? `释放保证金 ${fUsd(f.releasedMargin)} + 已实现盈亏 ${fSign(f.pnl, 1)}`
                  : "本次开仓或加仓锁定的保证金"}>
                {fUsd(f.capital)}
                <div className="muted" style={{ fontSize: 10 }}>{f.capitalKind}</div>
              </td>
              <td className="num muted">{fNum(f.qty, 2)}</td>
              <td className={"num " + (f.pnl != null ? cls(f.pnl) : "")}>{f.pnl != null ? fSign(f.pnl, 1) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
