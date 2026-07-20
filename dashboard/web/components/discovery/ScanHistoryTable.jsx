export function ScanHistoryTable({ runs }) {
  return (
    <React.Fragment>
      <div className="section-h"><h2>扫描历史</h2></div>
      <div className="tbl-wrap">
        <table>
          <thead><tr><th>时间</th><th className="num">候选</th><th className="num">画像</th><th className="num">新增</th><th className="num">退役</th><th className="num">拒绝</th><th className="num">在持名单</th></tr></thead>
          <tbody>
            {runs === null && <tr><td colSpan="7" className="loading">加载中…</td></tr>}
            {runs && runs.map((r, i) => (
              <tr key={i} title={r.complete === false ? "扫描不完整，请重试" : r.full ? "全量扫描" : "增量扫描"}>
                <td className="addr">{r.at ? r.at.replace("T", " ").replace("Z", "") : "—"}{r.complete === false ? " ⚠" : ""}</td>
                <td className="num">{r.candidates}</td><td className="num">{r.profiled ?? "—"}{r.failed ? ` / ${r.failed}失败` : ""}</td><td className="num up">+{r.added}</td>
                <td className="num">{r.retired}</td><td className="num">{r.rejected}</td><td className="num">{r.active}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </React.Fragment>
  );
}
