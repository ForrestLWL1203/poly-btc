const STAGES_FE = [
  [["scan_leaderboard"], "扫描排行榜"],
  [["fetch_history"], "拉取历史 & 算指标"],
  [["score_filter"], "评分 · 网格/扛单过滤"],
  [["rebuild_watchlist", "prepare_selection_candidates"], "重建被跟名单"],
  [["prefetch_selection_paths", "portfolio_tune", "selection_search", "auto_tune"], "组合回测调参"],
  [["persist"], "写库 & 校验"],
];

// Wallet profiling is only the first, linear part of a scan. Once every wallet is profiled the API's
// scanned/total ratio is 100%, but strict price-path preparation, portfolio search and publication still
// remain. Keep the mask honest instead of showing 100% / 00:00 while those bounded phases are running.
const POST_PROFILE_PROGRESS = {
  rebuild_watchlist: 78,
  prepare_selection_candidates: 82,
  prefetch_selection_paths: 86,
  portfolio_tune: 89,
  selection_search: 91,
  auto_tune: 96,
  persist: 99,
};

const { useState } = React;

export function ScanMask({ status, onStop, stopping = false, stopError = null }) {
  const [confirmStop, setConfirmStop] = useState(false);
  const stage = status && status.stage;
  const curIdx = STAGES_FE.findIndex(([keys]) => keys.includes(stage));
  const pct = POST_PROFILE_PROGRESS[stage] ?? ((status && status.progressPct) || 0);
  const el = (status && status.elapsedSec) || 0;
  const mm = String(Math.floor(el / 60)).padStart(2, "0"), ss = String(el % 60).padStart(2, "0");
  const postProfile = Object.prototype.hasOwnProperty.call(POST_PROFILE_PROGRESS, stage);
  const remain = !postProfile && pct > 3 ? Math.round(el * (100 - pct) / pct) : null;
  const eta = remain != null
    ? `预计还需 ~${String(Math.floor(remain / 60)).padStart(2, "0")}:${String(remain % 60).padStart(2, "0")}`
    : postProfile ? "画像已完成 · 正在执行有限组合计算" : "预计剩余计算中…";
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
        {STAGES_FE.map(([keys, label], i) => {
          const st = curIdx < 0 ? "" : i < curIdx ? "done" : i === curIdx ? "active" : "";
          return <div key={keys[0]} className={"stage-item " + st}>
            <span className="stage-dot">{st === "done" ? "✓" : st === "active" ? "●" : ""}</span>{label}</div>;
        })}
      </div>
      <div className="mask-stop-zone">
        {!confirmStop ? (
          <button className="btn btn-danger mask-stop-btn" onClick={() => setConfirmStop(true)} disabled={stopping}>
            紧急终止采集
          </button>
        ) : (
          <div className="mask-stop-confirm" role="alert">
            <p>终止会丢弃本轮未发布结果，并保留上一次已发布名单。确定继续？</p>
            <div>
              <button className="btn" onClick={() => setConfirmStop(false)} disabled={stopping}>返回等待</button>
              <button className="btn btn-stop" onClick={onStop} disabled={stopping}>
                {stopping ? "正在终止…" : "确认紧急终止"}
              </button>
            </div>
          </div>
        )}
        {stopError && <div className="mask-stop-error">{stopError}</div>}
      </div>
      <div className="mask-lock">⚠ 页面已锁定 · 仅保留紧急终止操作</div>
    </div>
  );
}
