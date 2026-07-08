import { fNum, fSign } from "../../lib/format.js";

const REASON_LABELS = {
  copy_backtest_loss: "copy 回测亏损",
  edge_decayed: "边际衰减",
  thin_edge: "优势太薄",
  low_quality: "评分不足",
  too_many_concurrent: "并发过高",
  grid_dca: "网格/DCA过重",
  inactive: "不活跃",
};

export function PipelineSummary({ p }) {
  if (!p || !p.stamp) return null;
  const prof = p.profile || {}, wl = p.watchlist || {}, fl = p.followLine || {}, tune = p.autoTune || {};
  const selected = fl.selected || {};
  const win = selected.windows || {};
  const pnl14 = win["14"] && win["14"].copy_net_pnl;
  const pnl7 = win["7"] && win["7"].copy_net_pnl;
  const tuneChanged = tune.appliedSizing || tune.appliedAdd;
  const reasonRows = (prof.reasonCounts || []).slice(0, 5);
  const reasonLabel = (r) => REASON_LABELS[r] || r || "—";
  return (
    <div className="pipeline-card card">
      <div className="pipeline-head">
        <div>
          <div className="card-lbl">最近一轮流水线审计</div>
          <div className="pipeline-stamp">{p.stamp.replace("T", " ").replace("Z", "")} · {p.source || "scan"}</div>
        </div>
        <span className={"tint " + (fl.status === "ok" ? "tint-green" : "tint-amber")}>{fl.reason || fl.status || "无跟单线记录"}</span>
      </div>
      <div className="pipeline-grid">
        <div className="pipe-metric">
          <span>画像结果</span>
          <b>{prof.active || 0}<em> / {prof.total || 0}</em></b>
          <small>{prof.retired || 0} 退役 · {prof.rejected || 0} 拒绝</small>
        </div>
        <div className="pipe-metric">
          <span>跟单线</span>
          <b>{fl.score != null ? fNum(fl.score, 1) : "—"}<em> 分</em></b>
          <small>{fl.count != null ? `选择 ${fl.count} 个` : "未自动选择"}{fl.targetN ? ` · 目标 ${fl.targetN}` : ""}</small>
        </div>
        <div className="pipe-metric">
          <span>最终名单</span>
          <b>{wl.followed || 0}<em> 个</em></b>
          <small>{wl.belowLine || 0} 在线下 · {wl.disabled || 0} 停用</small>
        </div>
        <div className="pipe-metric">
          <span>自动调参</span>
          <b className={tuneChanged ? "up" : ""}>{tuneChanged ? "已调整" : (tune.status || "—")}</b>
          <small>{tune.followedN != null ? `${tune.followedN} 钱包参与` : "无调参记录"}{tune.selectedMult ? ` · ${fNum(tune.selectedMult, 2)}x` : ""}</small>
        </div>
      </div>
      <div className="pipeline-detail">
        <div>
          <div className="muted">自动选线依据</div>
          <div className="pipeline-line">
            {selected.n ? <React.Fragment>top {selected.n} · 总分 {fSign(selected.score || 0, 0)} · 14d {fSign(pnl14 || 0, 0)} · 7d {fSign(pnl7 || 0, 0)}</React.Fragment>
              : (fl.reason || "暂无 top-N 组合回测摘要")}
          </div>
        </div>
        <div>
          <div className="muted">主要出局原因</div>
          <div className="pipe-reasons">
            {reasonRows.length === 0 && <span className="muted">暂无退役/拒绝记录</span>}
            {reasonRows.map((r, i) => (
              <span key={i} className="pipe-reason"><b>{r.count}</b>{reasonLabel(r.reason)}</span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
