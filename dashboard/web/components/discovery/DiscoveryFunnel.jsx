import { IC, Ico } from "../../lib/icons.jsx";

const numeric = value => {
  if (value == null) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

const formatCount = value => {
  const number = numeric(value);
  return number == null ? "—" : number.toLocaleString("en-US");
};

const formatRate = value => {
  if (!Number.isFinite(value)) return "—";
  if (value > 0 && value < 0.1) return "<0.1%";
  return value.toFixed(value >= 10 ? 1 : 2) + "%";
};

export function DiscoveryFunnel({ funnel }) {
  const visibleStages = [
    { key: "leaderboard", label: "Leaderboard", detail: "全量榜单", count: funnel.leaderboard },
    { key: "perp", label: "Perp周量确认", detail: "活跃门槛", count: funnel.perpPrefilter },
    { key: "profile", label: "有效画像", detail: "完整画像", count: funnel.profileValid ?? funnel.structurePassed },
    { key: "selection", label: "入围名单", detail: "严格候选", count: funnel.selectionPool ?? funnel.top32 },
    { key: "finalCore", label: "最终 Core", detail: "执行名单", count: funnel.finalCore ?? funnel.core },
  ];

  return (
    <section className="discovery-section">
      <div className="discovery-section-head"><h2>筛选漏斗</h2>
        {funnel.funnelPublishedAt && <span>最近完整重评 · {String(funnel.funnelPublishedAt).slice(0, 10)}</span>}
      </div>
      <div className={"discovery-glass funnel-shell" + (funnel.funnelConsistent === false ? " funnel-inconsistent" : "")}>
        <div className="funnel funnel-complete" aria-label="最近一次完整重评候选钱包筛选流程">
          {visibleStages.map((stage, index) => {
            const previous = index > 0 ? numeric(visibleStages[index - 1].count) : null;
            const current = numeric(stage.count);
            const passRate = previous > 0 && current != null ? current / previous * 100 : NaN;
            const dropped = previous != null && current != null ? Math.max(0, previous - current) : null;
            return <React.Fragment key={stage.key}>
              {index > 0 && (
                <div className="funnel-connector" aria-label={`通过率 ${formatRate(passRate)}，流失 ${formatCount(dropped)}`}>
                  <div className="funnel-connector-line"><Ico d={IC.arrowRight} /></div>
                  <strong>{formatRate(passRate)}</strong>
                  <small>流失 {formatCount(dropped)}</small>
                </div>
              )}
              <div className={"funnel-stage" + (stage.key === "finalCore" ? " is-final" : "")}>
                <div className="funnel-stage-head">
                  <span className="funnel-stage-index">{String(index + 1).padStart(2, "0")}</span>
                </div>
                <div className="fn">{formatCount(stage.count)}</div>
                <div className="fl">{stage.label}</div>
                <div className="fd">{stage.detail}</div>
              </div>
            </React.Fragment>;
          })}
        </div>
        {funnel.funnelConsistent === false && <div className="funnel-consistency-note">
          当前完整重评证据不完整，Core 留任数据不参与阶段通过率计算。
        </div>}
      </div>
    </section>
  );
}
