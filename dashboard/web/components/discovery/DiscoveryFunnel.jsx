export function DiscoveryFunnel({ funnel }) {
  const visibleStages = [
    { key: "leaderboard", label: "Leaderboard", count: funnel.leaderboard },
    { key: "coarse", label: "粗筛", count: funnel.candidates },
    { key: "perp", label: "Perp预筛", count: funnel.perpPrefilter },
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
