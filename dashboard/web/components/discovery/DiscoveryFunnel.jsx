export function DiscoveryFunnel({ funnel }) {
  const visibleStages = [
    { key: "leaderboard", label: "Leaderboard", count: funnel.leaderboard },
    { key: "perp", label: "Perp周量确认", count: funnel.perpPrefilter },
    { key: "pf", label: "PF/非彩票", count: funnel.pfLotteryPassed },
    { key: "strict", label: "Strict通过", count: funnel.strict },
    { key: "finalCore", label: "最终Core", count: funnel.finalCore ?? funnel.core },
  ];

  return (
    <>
      <div className="section-h"><h2>筛选漏斗</h2></div>
      <div className="card">
        <div className="funnel funnel-complete">
          {visibleStages.map((stage, index) => (
            <React.Fragment key={stage.key}>
              {index > 0 && <div className="funnel-arrow">→</div>}
              <div className="funnel-stage">
                <div className="fn" style={stage.key === "finalCore" ? { color: "var(--green-l)" } : undefined}>
                  {stage.count ?? "—"}
                </div>
                <div className="fl">{stage.label}</div>
              </div>
            </React.Fragment>
          ))}
        </div>
      </div>
    </>
  );
}
