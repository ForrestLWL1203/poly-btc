export function DiscoveryFunnel({ funnel }) {
  const visibleStages = [
    { key: "leaderboard", label: "Leaderboard", count: funnel.leaderboard },
    { key: "coarse", label: "低成本召回", count: funnel.candidates },
    { key: "perp", label: "Perp周量确认", count: funnel.perpPrefilter },
    { key: "structure", label: "结构可跟", count: funnel.structurePassed },
    { key: "rough", label: "粗Copy完成", count: funnel.roughCompleted },
    { key: "activity", label: "跨周活跃", count: funnel.persistentActivity },
    { key: "pf", label: "PF/非彩票", count: funnel.pfLotteryPassed },
    { key: "primary", label: "Primary", count: funnel.primary },
    { key: "reserve", label: "Reserve", count: funnel.reserve },
    { key: "top32", label: "Strict Top32", count: funnel.top32 },
    { key: "strict", label: "Strict通过", count: funnel.strict },
    { key: "challenger", label: "Challenger", count: funnel.challenger },
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
