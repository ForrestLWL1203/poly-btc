import { cls, fNum, fPct, fPrice, fSign, fUsd, normalizeCoin, short } from "../../lib/format.js";
import { BanIcon, IC, Ico } from "../../lib/icons.jsx";
import { PositionDetail } from "./PositionDetail.jsx";

export function OpenPositionsTable({
  open,
  openRows,
  openItems,
  expandedId,
  details,
  closing,
  pnlSort,
  cyclePnlSort,
  toggleRow,
  doClose,
  blacklist,
  blacklisting,
  addBlacklist,
  opg,
  opages,
  perPage,
  setOpage,
}) {
  return (
    <React.Fragment>
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
      {openRows.length > perPage && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 12, marginTop: 10 }}>
          <button className="btn" disabled={opg <= 0} onClick={() => setOpage(opg - 1)}>上一页</button>
          <span className="muted mono">第 {opg + 1} / {opages} 页 · 共 {openRows.length} 笔</span>
          <button className="btn" disabled={opg >= opages - 1} onClick={() => setOpage(opg + 1)}>下一页</button>
        </div>
      )}
    </React.Fragment>
  );
}
