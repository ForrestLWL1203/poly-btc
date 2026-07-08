import { cls, fDur, fNum, fSign } from "../../lib/format.js";

export function HistoryStats({ stats }) {
  return (
    <div className="grid4">
      <div className="card">
        <div className="card-lbl">胜率</div>
        <div className="kpi">{fNum(stats.winRatePct, 1)}%</div>
        <div className="kpi-sub"><span className="up">{stats.wins} 胜</span><span className="down">{stats.losses} 负</span></div>
      </div>
      <div className="card">
        <div className="card-lbl">累计已实现盈亏</div>
        <div className={"kpi " + cls(stats.totalPnl)}>{fSign(stats.totalPnl, 0)}</div>
        <div className="kpi-sub"><span>平均每笔 <span className={cls(stats.avgPnl)}>{fSign(stats.avgPnl, 1)}</span></span></div>
      </div>
      <div className="card">
        <div className="card-lbl">盈利因子</div>
        <div className="kpi">{stats.profitFactor == null ? "∞" : fNum(stats.profitFactor, 2)}</div>
        <div className="kpi-sub"><span>总盈 ÷ 总亏(&gt;1 为正期望)</span></div>
      </div>
      <div className="card">
        <div className="card-lbl">平均持仓时长</div>
        <div className="kpi">{fDur(stats.avgHoldSec)}</div>
        <div className="kpi-sub"><span>平均盈 <span className="up">{fSign(stats.avgWin, 0)}</span> · 亏 <span className="down">{fSign(stats.avgLoss, 0)}</span></span></div>
      </div>
    </div>
  );
}
