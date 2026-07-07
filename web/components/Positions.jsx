import { api } from "../lib/api.js";
import {
  cls,
  fNum,
  fPct,
  fPrice,
  fSign,
  fTime,
  fUsd,
  formatCoinList,
  normalizeCoin,
  parseCoinList,
  short,
} from "../lib/format.js";
import { BanIcon, IC, Ico } from "../lib/icons.jsx";
import { usePolling } from "../lib/refresh.js";

const { useState, useEffect, useCallback } = React;

const ACT_TINT = { 开仓: "tint-green", 加仓: "tint-blue", 减仓: "tint-amber", 平仓: "tint-gray" };

export function PositionDetail({ d }) {
  if (!d) return <div className="muted" style={{ padding: "14px 16px" }}>加载中…</div>;
  const live = d.status === "open";
  const pnl = live ? d.unrealizedPnl : d.realizedPnl;
  return (
    <div className="pos-detail">
      <div className="pos-detail-sum">
        <span>目标加仓 <b>{d.masterAdds}</b> 次 · 我们跟 <b>{d.ourAdds}</b> 次</span>
        <span>目标成本均价 <b>{fPrice(d.masterEntry)}</b></span>
        <span>我方成本均价 <b>{fPrice(d.ourEntry)}</b> · {fNum(d.ourLeverage, 0)}x</span>
        <span>我方投入保证金 <b>{fUsd(d.ourMargin)}</b></span>
        <span>{live ? "浮动" : "已实现"}盈亏 <b className={cls(pnl)}>{fSign(pnl, 1)}</b></span>
      </div>
      <div className="muted" style={{ fontSize: 11, margin: "2px 0 5px" }}>我们的成交记录:</div>
      <table className="fills-tbl">
        <thead><tr><th>时间</th><th>动作</th><th className="num">价格</th><th className="num">本金</th><th className="num">数量</th><th className="num">盈亏</th></tr></thead>
        <tbody>
          {d.fills.length === 0 && <tr><td colSpan="6" className="muted" style={{ padding: "6px 8px" }}>暂无成交</td></tr>}
          {d.fills.map((f, i) => (
            <tr key={i}>
              <td className="mono muted">{fTime(f.atSec)}</td>
              <td><span className={"tint " + (ACT_TINT[f.actionLabel] || "tint-gray")}>{f.actionLabel}</span>
                {f.fillCount > 1 && <span className="muted" style={{ marginLeft: 4, fontSize: 10 }} title="该订单分多笔成交">×{f.fillCount}</span>}</td>
              <td className="num">{fPrice(f.px)}</td>
              <td className="num">{fUsd(f.margin)}</td>
              <td className="num muted">{fNum(f.qty, 2)}</td>
              <td className={"num " + (f.pnl != null ? cls(f.pnl) : "")}>{f.pnl != null ? fSign(f.pnl, 1) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Positions({ confirm, toast, streamOpen }) {
  const [polledOpen, setPolledOpen] = useState(null);
  const [closing, setClosing] = useState({});
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
  const open = streamOpen || polledOpen;
  const cyclePnlSort = () => { setPnlSort(d => d === null ? "asc" : d === "asc" ? "desc" : null); setOpage(0); };
  const loadOpen = useCallback(() => { api.get("/api/positions?status=open").then(setPolledOpen).catch(() => {}); }, []);
  const load = loadOpen;
  const loadBlacklist = useCallback(() => {
    api.get("/api/params").then(p => {
      const row = (p.follow || []).find(x => x.key === "COIN_BLACKLIST");
      setBlacklist(parseCoinList(row ? row.value : ""));
    }).catch(() => {});
  }, []);
  usePolling(loadOpen, 6000, !streamOpen);
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
        <h2>当前持仓 {open && <span className="muted">· 浮动 <span className={cls(open.summary.floatingPnl)}>{fSign(open.summary.floatingPnl, 1)}</span> · {open.summary.openCount} 笔</span>}</h2>
        <div className="range-tabs">
          {[["all", "全部"], ["crypto", "Crypto"], ["stock", "股票"], ["long", "多"], ["short", "空"]].map(([k, l]) =>
            <button key={k} className={filter === k ? "on" : ""} onClick={() => setFilter(k)}>{l}</button>)}
        </div>
      </div>
      <div className="tbl-wrap">
        <table>
          <thead><tr>
            <th>币种</th><th>方向</th><th className="num">入场/杠杆</th><th className="num">名义额</th>
            <th className="num">现价</th>
            <th className="num sortable" onClick={cyclePnlSort} title="点击按浮动盈亏排序(浮亏在前 / 浮盈在前 / 默认新开在前)">
              浮动盈亏 <span className={"sort-ind" + (pnlSort ? " active" : "")}>{pnlSort === "asc" ? "▲" : pnlSort === "desc" ? "▼" : "⇅"}</span>
            </th>
            <th>钱包</th><th className="num">lag</th><th className="num">爆仓价</th><th></th>
          </tr></thead>
          <tbody>
            {open === null && <tr><td colSpan="10" className="loading">加载中…</td></tr>}
            {open && openRows.length === 0 && <tr><td colSpan="10" className="empty">无持仓</td></tr>}
            {openItems.map(p => { const pid = Number(String(p.id).replace("pos_", "")); const isOpen = expandedId === pid;
              return <React.Fragment key={p.id}>
              <tr onClick={() => toggleRow(p.id)} style={{ cursor: "pointer" }} className={isOpen ? "row-open" : ""}>
                <td><span className="row-caret" style={{ transform: isOpen ? "rotate(90deg)" : "none" }}>▸</span> <span className="tint tint-gray">{p.marketType === "stock" ? "股" : "币"}</span> <b>{p.coin}</b>
                  <a className="ext-link" href={"https://app.hyperliquid.xyz/trade/" + p.coin}
                     target="_blank" rel="noopener noreferrer" title="在 Hyperliquid 看K线" onClick={e => e.stopPropagation()}>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" /><polyline points="15 3 21 3 21 9" /><line x1="10" y1="14" x2="21" y2="3" /></svg></a>
                  {(() => { const c = normalizeCoin(p.coin), banned = blacklist.includes(c), busy = blacklisting[c];
                    return <button className={"coin-ban-btn" + (banned ? " on" : "")} disabled={busy}
                      aria-label={banned ? "已在币种黑名单" : "加入币种黑名单"}
                      onClick={e => { e.stopPropagation(); if (!banned) addBlacklist(p.coin); }}>
                      {busy ? <span className="spin" /> : <BanIcon />}
                      <span className="coin-ban-tip">{banned ? "已在币种黑名单" : "加入币种黑名单"}</span>
                    </button>;
                  })()}
                  {p.addCount > 0 && <span className="tint tint-gray" style={{ marginLeft: 8 }} title="目标加仓、我们跟进的次数(上限2)">加仓{p.addCount}</span>}</td>
                <td><span className={"tint " + (p.side === "long" ? "tint-green" : "tint-red")}>{p.side === "long" ? "多" : "空"}</span></td>
                <td className="num">{fPrice(p.entry)} · {fNum(p.leverage, 0)}x
                  <div className="muted" title="源(目标钱包)的加权均价(随其加仓更新)· 杠杆">源 {fPrice(p.masterEntry)} · {fNum(p.masterLeverage, 0)}x</div></td>
                <td className="num">{fUsd(p.notional)}
                  <div className="muted" title="源(目标钱包)这一单的名义额(我们 ≤ 它)">源 {fUsd(p.masterNotional)}</div></td>
                <td className="num">{fPrice(p.mark)}</td>
                <td className={"num " + cls(p.unrealizedPnl)}>{fSign(p.unrealizedPnl, 1)}<div className="muted">{fPct(p.unrealizedPctOfMargin, 0)} 保证金</div></td>
                <td className="addr">{short(p.wallet)} {p.followPos != null
                  ? <span className="rankbadge" title={"跟单序号" + (p.walletRank ? " · 全站评分#" + p.walletRank : "")}>#{p.followPos}</span>
                  : <span className="tint tint-gray" title="当前不在跟单集(仅平仓)">脱榜</span>}</td>
                <td className="num" title="跟单延迟:目标开仓 → 我们检测并跟开的秒数(旧仓未记录显示 —)">{p.lagSec != null ? fNum(p.lagSec, 1) + "s" : "—"}</td>
                <td className={"num " + (p.liqDistancePct != null && p.liqDistancePct > -8 ? "down" : "")} title="距现价多少就触发强平">{fPrice(p.liqPx)}
                  {p.liqDistancePct != null && <div className="muted">差 {fNum(Math.abs(p.liqDistancePct), 1)}%</div>}</td>
                <td>{(() => { const busy = closing[Number(p.id.replace("pos_", ""))];
                  return <button className="btn btn-stop btn-sm" disabled={busy} onClick={e => { e.stopPropagation(); doClose(p); }}>
                    {busy ? <><span className="spin" />平仓中</> : <><Ico d={IC.close} />平仓</>}</button>; })()}</td>
              </tr>
              {isOpen && <tr className="detail-row"><td colSpan="10"><PositionDetail d={details[pid]} /></td></tr>}
              </React.Fragment>; })}
          </tbody>
        </table>
      </div>
      {openRows.length > OPER && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 12, marginTop: 10 }}>
          <button className="btn" disabled={opg <= 0} onClick={() => setOpage(opg - 1)}>上一页</button>
          <span className="muted mono">第 {opg + 1} / {opages} 页 · 共 {openRows.length} 笔</span>
          <button className="btn" disabled={opg >= opages - 1} onClick={() => setOpage(opg + 1)}>下一页</button>
        </div>
      )}
    </div>
  );
}
