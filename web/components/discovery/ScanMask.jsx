const STAGES_FE = [["scan_leaderboard", "扫描排行榜"], ["fetch_history", "拉取历史 & 算指标"],
  ["score_filter", "评分 · 网格/扛单过滤"], ["rebuild_watchlist", "重建被跟名单"],
  ["auto_tune", "组合回测调参"], ["persist", "写库 & 校验"]];

export function ScanMask({ status }) {
  const stage = status && status.stage;
  const curIdx = STAGES_FE.findIndex(s => s[0] === stage);
  const pct = (status && status.progressPct) || 0;
  const el = (status && status.elapsedSec) || 0;
  const mm = String(Math.floor(el / 60)).padStart(2, "0"), ss = String(el % 60).padStart(2, "0");
  const remain = pct > 3 ? Math.round(el * (100 - pct) / pct) : null;
  const eta = remain != null
    ? `预计还需 ~${String(Math.floor(remain / 60)).padStart(2, "0")}:${String(remain % 60).padStart(2, "0")}`
    : "预计剩余计算中…";
  return (
    <div className="mask">
      <div className="radar" />
      <h2>采集进行中…</h2>
      <div className="sub">{mm}:{ss} 已用 · {eta}</div>
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
