const CATEGORY_LABELS = {
  data_error: "数据错误",
  business_reject: "业务拒绝",
  soft_retention_failure: "软保留失败",
  hard_risk_exit: "硬风险退出",
};

export function DiscoveryFunnel({ funnel, stages, failureCategories, scoreHistogram, rejectReasons }) {
  const h = scoreHistogram;
  const maxBin = Math.max(...h.bins, 1);
  const visibleStages = stages || [
    { key: "leaderboard", label: "Leaderboard", count: funnel.leaderboard },
    { key: "coarse", label: "粗筛", count: funnel.candidates },
    { key: "perp", label: "Perp预筛", count: funnel.perpPrefilter },
    { key: "structure", label: "结构过滤", count: funnel.structureFilter },
    { key: "research", label: "Research", count: funnel.research },
    { key: "challenger", label: "Challenger", count: funnel.challengerEvidence ?? funnel.challenger },
    { key: "personalCore", label: "个人Core", count: funnel.personalCore },
    { key: "finalCore", label: "最终Core", count: funnel.finalCore ?? funnel.core },
  ];
  const reasonStages = visibleStages.filter((stage) => (stage.topReasons || []).length);
  return (
    <React.Fragment>
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
        <div className="failure-legend">
          {(failureCategories || []).map((item) => (
            <span className={"failure-tag " + item.category} key={item.category}>
              {CATEGORY_LABELS[item.category] || item.category} · {item.count}
            </span>
          ))}
        </div>
        {reasonStages.length > 0 && (
          <div className="funnel-reasons">
            {reasonStages.map((stage) => (
              <div className="funnel-reason-stage" key={stage.key}>
                <b>{stage.label}</b>
                {(stage.topReasons || []).slice(0, 3).map((reason) => (
                  <span key={reason.reason} title={reason.reason}>
                    <i className={"reason-dot " + reason.category} />
                    {reason.reason} · {reason.count}
                  </span>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="grid2" style={{ marginTop: 14 }}>
        <div className="card">
          <div className="card-lbl">拒绝原因占比</div>
          <div style={{ marginTop: 12 }}>
            {rejectReasons.map((r, i) => (
              <div className="bar-row" key={i}><div className="bl" style={{ width: 120 }}>{r.label}</div>
                <div className="bar-track"><div className="bar-fill" style={{ width: r.pct + "%", background: "var(--accent-grad)" }} /></div>
                <div className="bv">{r.pct}%</div></div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-lbl">Qualified候选评分分布 <span className="muted">· 仅用于排序解释</span></div>
          <div className="histo">
            {h.bins.map((b, i) => (
              <div key={i} className="hb" style={{ height: (b / maxBin * 100) + "%" }} />
            ))}
          </div>
        </div>
      </div>
    </React.Fragment>
  );
}
