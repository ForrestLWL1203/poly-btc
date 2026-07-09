import { api } from "../lib/api.js";
import {
  cls,
  fSign,
  fUsd,
  formatCoinList,
  normalizeCoin,
  parseCoinList,
} from "../lib/format.js";
import { IC, Ico } from "../lib/icons.jsx";
import { useApiResource } from "../lib/refresh.js";
import { OpenPositionsTable } from "./positions/OpenPositionsTable.jsx";

const { useState, useEffect, useCallback } = React;

export function Positions({ confirm, toast, streamOpen }) {
  const [closing, setClosing] = useState({});
  const [closingAll, setClosingAll] = useState(false);
  const [blacklist, setBlacklist] = useState([]);
  const [blacklisting, setBlacklisting] = useState({});
  const [filter, setFilter] = useState("all");
  const [opage, setOpage] = useState(0);
  const [pnlSort, setPnlSort] = useState(null);
  const [expandedId, setExpandedId] = useState(null);
  const [details, setDetails] = useState({});
  const toggleRow = (rowId) => {
    const pid = Number(String(rowId).replace("pos_", ""));
    if (expandedId === pid) { setExpandedId(null); return; }
    setExpandedId(pid);
    if (!details[pid]) api.get(`/api/positions/${pid}`).then(d => setDetails(m => ({ ...m, [pid]: d }))).catch(() => {});
  };
  const openLoader = useCallback(() => api.get("/api/positions?status=open"), []);
  const { data: polledOpen, reload: loadOpen } = useApiResource(openLoader, { intervalMs: 6000, enabled: !streamOpen });
  const open = streamOpen || polledOpen;
  const cyclePnlSort = () => { setPnlSort(d => d === null ? "asc" : d === "asc" ? "desc" : null); setOpage(0); };
  const load = loadOpen;
  const loadBlacklist = useCallback(() => {
    api.get("/api/params").then(p => {
      const row = (p.follow || []).find(x => x.key === "COIN_BLACKLIST");
      setBlacklist(parseCoinList(row ? row.value : ""));
    }).catch(() => {});
  }, []);
  useEffect(() => { loadBlacklist(); }, [loadBlacklist]);

  const doClose = (p) => confirm({
    title: "手动平仓", danger: true,
    body: `平掉 ${p.coin} ${p.side === "long" ? "多" : "空"}(当前名义额 ${fUsd(p.notional)})。选择平仓比例(默认100%),不可撤销。`,
    pctPicker: { notional: p.notional },
    onConfirm: async (frac = 1) => {
      const pid = Number(p.id.replace("pos_", ""));
      setClosing(c => ({ ...c, [pid]: true }));
      try { await api.cmd("close_position", { positionId: pid, fraction: frac }); } catch (_e) {}
      await new Promise(r => setTimeout(r, 1800));
      load();
      setClosing(c => { const m = { ...c }; delete m[pid]; return m; });
    },
  });
  const doCloseAll = () => {
    const positions = open ? (open.positions || []) : [];
    const summary = (open && open.summary) || {};
    const count = summary.openCount || positions.length;
    if (!count || closingAll) return;
    confirm({
      title: "一键平仓",
      danger: true,
      ok: "全部平仓",
      body: `以 taker 方式平掉当前全部 ${count} 笔持仓。当前浮动盈亏 ${fSign(summary.floatingPnl, 1)}，提交后不可撤销。`,
      onConfirm: async () => {
        const ids = positions.map(p => Number(String(p.id).replace("pos_", ""))).filter(Number.isFinite);
        setClosingAll(true);
        setClosing(Object.fromEntries(ids.map(pid => [pid, true])));
        try {
          await api.cmd("close_all", {});
          if (toast) toast(`已提交一键平仓 · ${count} 笔`);
        } catch (_e) {
          if (toast) toast("一键平仓提交失败");
        }
        await new Promise(r => setTimeout(r, 2500));
        load();
        setClosing({});
        setClosingAll(false);
      },
    });
  };
  const addBlacklist = async (coin) => {
    const normalized = normalizeCoin(coin);
    if (!normalized) return;
    setBlacklisting(m => ({ ...m, [normalized]: true }));
    try {
      const p = await api.get("/api/params");
      const row = (p.follow || []).find(x => x.key === "COIN_BLACKLIST");
      const current = parseCoinList(row ? row.value : "");
      if (!current.includes(normalized)) {
        const next = formatCoinList([...current, normalized]);
        await api.patchParams("follow", { COIN_BLACKLIST: next });
        await api.cmd("reload_params", {});
        setBlacklist(parseCoinList(next));
      } else {
        setBlacklist(current);
      }
    } catch (_e) {
      loadBlacklist();
    } finally {
      setBlacklisting(m => { const n = { ...m }; delete n[normalized]; return n; });
    }
  };

  const filt = (rows) => !rows ? [] : rows.filter(p =>
    filter === "all" ? true : filter === "crypto" ? p.marketType === "crypto" :
    filter === "stock" ? p.marketType === "stock" : filter === "long" ? p.side === "long" : p.side === "short");

  const OPER = 20;
  let openRows = open ? filt(open.positions) : [];
  if (pnlSort) openRows = [...openRows].sort((a, b) =>
    pnlSort === "asc" ? (a.unrealizedPnl - b.unrealizedPnl) : (b.unrealizedPnl - a.unrealizedPnl));
  const opages = Math.max(1, Math.ceil(openRows.length / OPER));
  const opg = Math.min(opage, opages - 1);
  const openItems = openRows.slice(opg * OPER, opg * OPER + OPER);

  return (
    <div className="content">
      <div className="section-h" style={{ marginTop: 6 }}>
        <div className="positions-title-row">
          <h2>当前持仓 {open && <span className="muted">· 浮动 <span className={cls(open.summary.floatingPnl)}>{fSign(open.summary.floatingPnl, 1)}</span> · {open.summary.openCount} 笔</span>}</h2>
          <button className="btn btn-danger btn-close-all" disabled={!(open && open.summary && open.summary.openCount) || closingAll}
            title="以 taker 方式平掉当前全部持仓" onClick={doCloseAll}>
            {closingAll ? <span className="spin" /> : <Ico d={IC.close} />}
            {closingAll ? "平仓中" : "一键平仓"}
          </button>
        </div>
        <div className="range-tabs">
          {[["all", "全部"], ["crypto", "Crypto"], ["stock", "股票"], ["long", "多"], ["short", "空"]].map(([k, l]) =>
            <button key={k} className={filter === k ? "on" : ""} onClick={() => setFilter(k)}>{l}</button>)}
        </div>
      </div>
      <OpenPositionsTable
        open={open}
        openRows={openRows}
        openItems={openItems}
        expandedId={expandedId}
        details={details}
        closing={closing}
        pnlSort={pnlSort}
        cyclePnlSort={cyclePnlSort}
        toggleRow={toggleRow}
        doClose={doClose}
        blacklist={blacklist}
        blacklisting={blacklisting}
        addBlacklist={addBlacklist}
        opg={opg}
        opages={opages}
        perPage={OPER}
        setOpage={setOpage}
      />
    </div>
  );
}
