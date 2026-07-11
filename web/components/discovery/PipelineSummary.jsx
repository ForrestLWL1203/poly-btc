import { fSign } from "../../lib/format.js";

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
  const prof = p.profile || {}, fl = p.followLine || {}, sel = p.selection || {}, tune = p.autoTune || {};
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
        <span className={"tint " + (sel.generation ? "tint-green" : "tint-amber")}>{sel.generation ? `Selection · ${sel.action || "keep"}` : (fl.reason || fl.status || "尚无显式 Selection")}</span>
      </div>
      <div className="pipeline-grid">
        <div className="pipe-metric">
          <span>画像结果</span>
          <b>{prof.active || 0}<em> / {prof.total || 0}</em></b>
          <small>{prof.retired || 0} 退役 · {prof.rejected || 0} 拒绝</small>
        </div>
        <div className="pipe-metric">
          <span>显式 Selection</span>
          <b>{sel.core || 0}<em> Core</em></b>
          <small>{sel.challenger || 0} Challenger · {sel.exitOnly || 0} Exit-only</small>
        </div>
        <div className="pipe-metric">
          <span>组合动作</span>
          <b>{sel.action || "keep"}</b>
          <small>{sel.generation || "等待首个完整 generation"}</small>
        </div>
        <div className="pipe-metric">
          <span>自动调参</span>
          <b className={tuneChanged ? "up" : ""}>{tuneChanged ? "已调整" : (tune.mode ? `${tune.mode} · ${tune.status || "proposal"}` : tune.status || "—")}</b>
          <small>{tune.eligibleToApply ? "满足Apply门槛" : ((tune.validation && tune.validation.reasons || []).slice(0, 2).join(" · ") || "等待样本外验证")}</small>
        </div>
      </div>
      <div className="pipeline-detail">
        <div>
          <div className="muted">Selection / 旧评分线参考</div>
          <div className="pipeline-line">
            {sel.generation ? <React.Fragment>{sel.generation} · Core {sel.core || 0} · Challenger {sel.challenger || 0}</React.Fragment>
              : selected.n ? <React.Fragment>旧 top {selected.n} · 14d {fSign(pnl14 || 0, 0)} · 7d {fSign(pnl7 || 0, 0)}</React.Fragment>
              : (fl.reason || "等待首个 selection generation")}
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
