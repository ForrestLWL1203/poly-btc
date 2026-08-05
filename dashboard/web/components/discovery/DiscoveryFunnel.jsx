export function DiscoveryFunnel({ funnel }) {
  const visibleStages = [
    { key: "leaderboard", label: "Leaderboard", detail: "全量榜单", count: funnel.leaderboard },
    { key: "perp", label: "Perp周量确认", detail: "活跃门槛", count: funnel.perpPrefilter },
    { key: "pf", label: "PF / 非彩票", detail: "质量预筛", count: funnel.pfLotteryPassed },
    { key: "strict", label: "Strict通过", detail: "严格回放", count: funnel.strict },
    { key: "finalCore", label: "最终Core", detail: "执行名单", count: funnel.finalCore ?? funnel.core },
  ];

  const formatCount = value => {
    if (value == null) return "—";
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString("en-US") : value;
  };

  return (
    <>
      <div className="section-h"><h2>筛选漏斗</h2></div>
      <div className="card funnel-shell">
        <div className="funnel funnel-complete" aria-label="候选钱包筛选流程">
          {visibleStages.map((stage, index) => (
            <React.Fragment key={stage.key}>
              {index > 0 && (
                <div className="funnel-connector" aria-hidden="true">
                  <span className="funnel-connector-arrow"><i /><i /><i /></span>
                </div>
              )}
              <div className={`funnel-stage${stage.key === "finalCore" ? " is-final" : ""}`}>
                <div className="funnel-stage-head">
                  <span className="funnel-stage-index">{String(index + 1).padStart(2, "0")}</span>
                  <span className="funnel-stage-dot" />
                </div>
                <div className="fn">
                  {formatCount(stage.count)}
                </div>
                <div className="fl">{stage.label}</div>
                <div className="fd">{stage.detail}</div>
              </div>
            </React.Fragment>
          ))}
        </div>
      </div>
    </>
  );
}
