export function DiscoveryFunnel({ funnel, scoreHistogram, rejectReasons }) {
  const h = scoreHistogram;
  const maxBin = Math.max(...h.bins, 1);
  return (
    <React.Fragment>
      <div className="section-h"><h2>筛选漏斗</h2></div>
      <div className="card">
        <div className="funnel">
          <div className="funnel-stage"><div className="fn">{funnel.leaderboard ?? "—"}</div><div className="fl">Leaderboard</div></div>
          <div className="funnel-arrow">→</div>
          <div className="funnel-stage"><div className="fn">{funnel.candidates}</div><div className="fl">候选 candidates</div></div>
          <div className="funnel-arrow">→</div>
          <div className="funnel-stage"><div className="fn" style={{ color: "var(--blue-l)" }}>{funnel.qualified ?? funnel.active}</div><div className="fl">Qualified</div></div>
          <div className="funnel-arrow">→</div>
          <div className="funnel-stage"><div className="fn" style={{ color: "var(--amber)" }}>{funnel.challenger ?? 0}</div><div className="fl">Challenger</div></div>
          <div className="funnel-arrow">→</div>
          <div className="funnel-stage"><div className="fn" style={{ color: "var(--green-l)" }}>{funnel.core ?? funnel.watchlist}</div><div className="fl">Core · 实际开仓</div></div>
        </div>
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
