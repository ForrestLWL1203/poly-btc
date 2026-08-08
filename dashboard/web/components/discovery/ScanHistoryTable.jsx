import { IC, Ico } from "../../lib/icons.jsx";
import { fShanghaiDateTime } from "../../lib/format.js";

const runType = run => run.kind === "challenger_refresh" ? "轻量重评" : "全量重评";

const runReason = reason => ({
  shared_economics_operator_review: "经济门槛未通过 · 未发布晋升/调参",
  challenger_daily_hard_safety_removal: "安全/可用性退出 · 未发布晋升",
  daily_proposal_would_remove_core: "成员变更未发布 · 保留原 Core",
  retuned_proposal_not_strict_superset: "调参后不再是严格晋升 · 保留原 Core",
}[reason] || reason || "");

const runTooltip = run => [
  run.complete === false ? `未发布${run.reason ? `：${run.reason}` : ""}` : runType(run),
  run.complete && run.reason ? `结果 ${runReason(run.reason)}` : "",
  `API ${run.apiRequests || 0} 次 / 权重 ${run.apiWeight || 0}`,
  `Core +${run.coreAdded || 0}/-${run.coreRemoved || 0}`,
  `观察 ${run.coreProbation || 0} · 恢复 ${run.coreRecovered || 0} · 确认降级 ${run.coreConfirmedDemotion || 0} · 安全退出 ${run.coreSafetyExit || 0}`,
  run.replacementBlocked ? "显著增益不足，替换已阻止" : "",
  `数据源 ${run.effectiveSource === "quicknode" ? "QuickNode" : "Hyperliquid"}`,
  run.sourceFallbackReason ? `本轮由 QuickNode 回退：${run.sourceFallbackReason}` : "",
].filter(Boolean).join(" · ");

export function ScanHistoryTable({ runs }) {
  return (
    <section className="discovery-section scan-history-section">
      <div className="discovery-section-head"><h2>扫描历史</h2><span>最近 5 次</span></div>
      <div className="discovery-glass scan-history-wrap">
        <table className="scan-history-table">
          <thead><tr><th className="scan-run-state"><span className="sr-only">状态</span></th><th>时间</th><th>类型</th><th className="num">候选</th><th className="num">画像</th><th className="num">新增</th><th className="num">移除</th><th className="num">拒绝</th><th className="num">在持名单</th></tr></thead>
          <tbody>
            {runs === null && <tr><td colSpan="9" className="loading">加载中…</td></tr>}
            {runs && runs.map((r, i) => {
              const failed = r.complete === false;
              return <tr key={i} className={failed ? "scan-run-failed" : "scan-run-complete"} title={runTooltip(r)}>
                <td className="scan-run-state"><span aria-label={failed ? "本轮失败" : "本轮完成"} title={failed ? "未发布" : "已完成"}>
                  <Ico d={failed ? IC.xCircle : IC.checkCircle} />
                </span></td>
                <td className="addr">{fShanghaiDateTime(r.at)}</td>
                <td><span className={"scan-run-kind " + (r.kind === "challenger_refresh" ? "is-light" : "is-full")}>{runType(r)}</span>
                  {r.complete && r.reason && <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>{runReason(r.reason)}</div>}
                </td>
                <td className="num">{r.candidates}</td>
                <td className="num">{r.profiled ?? "—"}{r.failed ? ` / ${r.failed}失败` : ""}</td>
                <td className="num up">+{r.added}</td>
                <td className="num">{r.retired}</td>
                <td className="num">{r.rejected}</td>
                <td className="num">{r.active}</td>
              </tr>;
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
