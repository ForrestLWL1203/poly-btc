import { api } from "../lib/api.js";
import { fSign } from "../lib/format.js";
import { useApiResource } from "../lib/refresh.js";
import { ClosedPositionsTable } from "./history/ClosedPositionsTable.jsx";
import { HistoryStats } from "./history/HistoryStats.jsx";

const { useState, useCallback } = React;

export function History() {
  const [filter, setFilter] = useState("all");
  const [ctype, setCtype] = useState("all");
  const [page, setPage] = useState(0);
  const [expandedId, setExpandedId] = useState(null);
  const [details, setDetails] = useState({});
  const toggleRow = (rowId) => {
    const pid = Number(String(rowId).replace("cls_", ""));
    if (expandedId === pid) { setExpandedId(null); return; }
    setExpandedId(pid);
    if (!details[pid]) api.get(`/api/positions/${pid}`).then(d => setDetails(m => ({ ...m, [pid]: d }))).catch(() => {});
  };
  const loadClosed = useCallback(() => api.get("/api/positions?status=closed"), []);
  const { data } = useApiResource(loadClosed, { intervalMs: 15000 });
  const PER = 25;
  const st = data && data.stats;
  const all = (data && data.positions) || [];
  const rows = all.filter(p => (filter === "all" || p.result === filter) && (ctype === "all" || p.closeType === ctype));
  const pages = Math.max(1, Math.ceil(rows.length / PER));
  const pg = Math.min(page, pages - 1);
  const items = rows.slice(pg * PER, pg * PER + PER);
  return (
    <div className="content">
      <div className="section-h" style={{ marginTop: 6 }}>
        <h2>历史持仓 {st && <span className="muted">· 累计 {st.total} 笔已平仓</span>}</h2>
      </div>
      {data === null ? <div className="loading">加载中…</div> : st && st.total === 0 ? <div className="empty">暂无已平仓记录</div> : (
        <React.Fragment>
          <HistoryStats stats={st} />
          <div className="section-h" style={{ marginTop: 16 }}>
            <h2>明细 <span className="muted">· 最近 {all.length} 笔 · 最佳 <span className="up">{fSign(st.bestPnl, 0)}</span> / 最差 <span className="down">{fSign(st.worstPnl, 0)}</span></span></h2>
            <div className="hfilters">
              <select className="fdrop" value={filter} onChange={e => { setFilter(e.target.value); setPage(0); }}>
                <option value="all">盈亏 · 全部</option><option value="win">仅盈利</option><option value="loss">仅亏损</option>
              </select>
              <select className="fdrop" value={ctype} onChange={e => { setCtype(e.target.value); setPage(0); }}>
                <option value="all">平仓类型 · 全部</option><option value="mirror">镜像跟随</option><option value="stop">主动止损</option><option value="liq">爆仓</option>
              </select>
            </div>
          </div>
          <ClosedPositionsTable
            rows={rows}
            items={items}
            expandedId={expandedId}
            details={details}
            toggleRow={toggleRow}
            pg={pg}
            pages={pages}
            setPage={setPage}
            perPage={PER}
          />
        </React.Fragment>
      )}
    </div>
  );
}
