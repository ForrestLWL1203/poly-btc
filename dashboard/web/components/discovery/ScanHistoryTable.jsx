export function ScanHistoryTable({ runs }) {
  return (
    <React.Fragment>
      <div className="section-h"><h2>扫描历史</h2></div>
      <div className="tbl-wrap">
        <table>
          <thead><tr><th>时间</th><th>类型</th><th className="num">候选</th><th className="num">画像</th><th className="num">新增</th><th className="num">移除</th><th className="num">拒绝</th><th className="num">在持名单</th></tr></thead>
          <tbody>
            {runs === null && <tr><td colSpan="8" className="loading">加载中…</td></tr>}
            {runs && runs.map((r, i) => (
              <tr key={i} title={[
                r.complete === false ? `未发布${r.reason ? `：${r.reason}` : ""}` : (r.kind === "challenger_refresh" ? "Challenger 日更" : "完整候选重评"),
                `API ${r.apiRequests || 0} 次 / 权重 ${r.apiWeight || 0}`,
                `Core +${r.coreAdded || 0}/-${r.coreRemoved || 0}`,
                `观察 ${r.coreProbation || 0} · 恢复 ${r.coreRecovered || 0} · 确认降级 ${r.coreConfirmedDemotion || 0} · 安全退出 ${r.coreSafetyExit || 0}`,
                r.replacementBlocked ? "显著增益不足，替换已阻止" : "",
              ].filter(Boolean).join(" · ")}>
                <td className="addr">{r.at ? r.at.replace("T", " ").replace("Z", "") : "—"}{r.complete === false ? " ⚠" : ""}</td>
                <td>{r.kind === "challenger_refresh" ? "Challenger 日更" : "全量重评"}</td>
                <td className="num">{r.candidates}</td><td className="num">{r.profiled ?? "—"}{r.failed ? ` / ${r.failed}失败` : ""}</td><td className="num up">+{r.added}</td>
                <td className="num">{r.retired}</td><td className="num">{r.rejected}</td><td className="num">
                  {r.active}
                  {(r.coreProbation > 0 || r.coreSafetyExit > 0) && <div className="muted" style={{ fontSize: 10 }}>
                    观察 {r.coreProbation || 0} / 安退 {r.coreSafetyExit || 0}
                  </div>}
                </td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </React.Fragment>
  );
}
