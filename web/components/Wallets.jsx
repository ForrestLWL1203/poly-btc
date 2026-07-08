import { api } from "../lib/api.js";
import { fNum, fSign, fTime, short } from "../lib/format.js";
import { useApiResource } from "../lib/refresh.js";
import { useWalletAudit } from "./wallets/WalletAudit.jsx";
import { WalletDrawer } from "./wallets/WalletDrawer.jsx";

const { useState, useEffect, useCallback } = React;

const SECTOR_LABEL = { crypto: "加密", stock: "美股/指数" };

const sectorTitleLines = (policy) => {
  if (!policy) return [];
  return ["crypto", "stock"].map(k => {
    const item = policy[k] || {};
    if (item.allow == null && !item.status && !item.reason) return null;
    const mark = item.allow ? "✓" : "×";
    const status = item.reason || item.status || "无策略";
    const pnl = item.pnl || {};
    const closed = item.closed || {};
    const p14 = pnl["14"], n14 = closed["14"];
    const sample = (p14 != null || n14 != null) ? ` · 14天 ${fSign(p14 || 0, 0)} / ${n14 || 0}笔` : "";
    return `${SECTOR_LABEL[k] || k} ${mark} ${status}${sample}`;
  }).filter(Boolean);
};

export function Wallets({ confirm, toast }) {
  const [drawer, setDrawer] = useState(null);
  const [wpage, setWpage] = useState(0);
  const [tab, setTab] = useState("followed");
  const load = useCallback(() => api.get("/api/wallets?tab=" + tab + "&size=500"), [tab]);
  const { data, reload } = useApiResource(load, { intervalMs: 12000, clearOnLoadChange: true });
  const { auditOpen, resetAudits, toggleAudit, auditBox } = useWalletAudit();
  useEffect(resetAudits, [tab]);
  const dropped = tab === "dropped";
  const allRows = (data && data.wallets) || [];
  const PER = 10, pages = Math.max(1, Math.ceil(allRows.length / PER)), pg = Math.min(wpage, pages - 1);
  const pageRows = allRows.slice(pg * PER, pg * PER + PER);
  const scoreTitle = (w) => {
    const b = w.scoreBreakdown || {};
    const pnl = b.copyPnl || {};
    const n = b.closedN || {};
    const lines = [
      `最终跟单分 ${fNum(w.score, 1)}`,
      `原始评分 ${fNum(w.rawScore, 1)}`,
    ];
    if (pnl["30d"] != null || pnl["14d"] != null || pnl["7d"] != null) {
      lines.push(`copy回测 30天 ${fSign(pnl["30d"] || 0, 0)} / ${n["30d"] || 0}笔`);
      lines.push(`copy回测 14天 ${fSign(pnl["14d"] || 0, 0)} / ${n["14d"] || 0}笔`);
      lines.push(`copy回测 7天 ${fSign(pnl["7d"] || 0, 0)} / ${n["7d"] || 0}笔`);
    }
    const sectors = sectorTitleLines(b.sectorPolicy || w.sectorPolicy);
    if (sectors.length) {
      lines.push("跟单板块");
      sectors.forEach(s => lines.push("· " + s));
    }
    if (b.copyScore != null) lines.push(`copy分 ${fNum(b.copyScore, 1)} · 置信 ${fNum(b.confidencePct, 0)}%`);
    (b.reasons || []).slice(0, 4).forEach(r => lines.push("· " + r));
    return lines.join("\n");
  };

  const toggle = (w) => {
    const next = !w.enabled;
    const act = () => api.cmd("wallet_toggle", { address: w.address, enabled: next })
      .then(() => { toast((next ? "启用" : "停用") + " " + short(w.address)); setTimeout(reload, 1800); });
    if (next) act(); else confirm({ title: "停用钱包", danger: true, ok: "停用",
      body: `停用后不再对 ${short(w.address)} 开新仓,存量持仓继续跟到平仓。`, onConfirm: act });
  };

  return (
    <div className="content">
      <div className="section-h" style={{ marginTop: 6 }}>
        <h2>跟踪名单 {data && <span className="muted">· 跟单线 {fNum(data.followLine, 0)} 分 · {
          tab === "followed" ? "实跟 " + data.total + " 个(与跟单脚本一致)"
          : "降级 " + data.total + " 个"}</span>}</h2>
        <div className="range-tabs">
          <button className={tab === "followed" ? "on" : ""} onClick={() => { setTab("followed"); setWpage(0); }}>跟单中{data && data.followed != null ? " " + data.followed : ""}</button>
          <button className={tab === "dropped" ? "on" : ""} onClick={() => { setTab("dropped"); setWpage(0); }}>降级</button>
        </div>
      </div>
      <div className="tbl-wrap">
        {dropped ? (
          <table>
            <thead><tr>
              <th>地址</th><th>市场</th><th className="num">当前分</th><th className="num">曾在线</th><th className="num">ROI</th>
              <th className="num">胜率</th><th>主力</th><th>降级原因</th><th>降级时间</th>
            </tr></thead>
            <tbody>
              {data === null && <tr><td colSpan="9" className="loading">加载中…</td></tr>}
              {data && data.wallets.length === 0 && <tr><td colSpan="9" className="empty">暂无降级钱包 —— 都在跟单中 👍</td></tr>}
              {data && pageRows.map(w => {
                const key = (w.address || "").toLowerCase();
                const open = !!auditOpen[key];
                return (
                  <React.Fragment key={w.address}>
                    <tr className={open ? "row-open" : ""} style={{ cursor: "pointer" }} onClick={() => toggleAudit(w.address)}>
                      <td className="addr"><span className="row-caret">{open ? "▴" : "▾"}</span>{short(w.address)}</td>
                      <td><span className={"tint " + (w.marketType === "crypto" ? "tint-blue" : w.marketType === "stock" ? "tint-amber" : "tint-gray")}>{w.marketType}</span></td>
                      <td className="num" title={scoreTitle(w)}><b style={{ color: "var(--t2)" }}>{fNum(w.score, 1)}</b></td>
                      <td className="num muted">{fNum(w.lastFollowedScore, 1)}</td>
                      <td className="num up">{fNum(w.roiEqPct, 0)}%</td>
                      <td className="num">{fNum(w.winRatePct, 0)}%</td>
                      <td><b>{w.mainCoin}</b></td>
                      <td><span className="tint tint-red">{w.dropReason}</span></td>
                      <td className="mono" style={{ color: "var(--t2)", fontSize: 12 }}>{fTime(w.lastFollowedAt)}</td>
                    </tr>
                    {open && <tr className="detail-row"><td colSpan="9">{auditBox(w.address)}</td></tr>}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        ) : (
          <table>
            <thead><tr>
              <th>#</th><th>地址</th><th>市场</th><th className="num">评分</th><th className="num">ROI</th><th className="num">胜率</th>
              <th className="num" title="目标钱包自己最近7天平掉的回合数(活跃度)">近7天</th>
              <th className="num">最大亏损</th><th>主力</th><th className="num">被跟</th><th className="num">总体盈亏</th><th>启用</th>
            </tr></thead>
            <tbody>
              {data === null && <tr><td colSpan="12" className="loading">加载中…</td></tr>}
              {data && pageRows.map(w => (
                <tr key={w.address} className={w.enabled ? "" : "row-off"}
                  style={{ cursor: "pointer" }} onClick={() => setDrawer(w.address)}>
                  <td><span className="rankbadge" title={w.followPos != null ? "跟单序号(与脚本一致);全站评分名次 #" + w.rank : "全站评分名次"}>{w.followPos != null ? w.followPos : w.rank}</span></td>
                  <td className="addr">{short(w.address)}</td>
                  <td><span className={"tint " + (w.marketType === "crypto" ? "tint-blue" : w.marketType === "stock" ? "tint-amber" : "tint-gray")}>{w.marketType}</span></td>
                  <td className="num" title={scoreTitle(w)}><b style={{ color: w.score >= data.followLine ? "var(--green-l)" : "var(--t2)" }}>{fNum(w.score, 1)}</b></td>
                  <td className={"num up"}>{fNum(w.roiEqPct, 0)}%</td>
                  <td className="num">{fNum(w.winRatePct, 0)}%</td>
                  <td className="num">{w.closed7d != null ? w.closed7d : "—"}</td>
                  <td className="num down">{fNum(w.worstSingleLossPct, 0)}%</td>
                  <td><b>{w.mainCoin}</b></td>
                  <td className="num">{w.followCount}</td>
                  <td className="num">{(w.closedN > 0 || (w.forwardNetPnl || 0) !== 0)
                    ? <b style={{ color: (w.forwardNetPnl || 0) < 0 ? "var(--red-l)" : "var(--green-l)" }}>
                        {fSign(w.forwardNetPnl || 0, 0)}{(w.forwardNetPnl || 0) < -5 ? " ⚠" : ""}</b>
                    : <span className="muted">—</span>}</td>
                  <td><div className={"toggle " + (w.enabled ? "on" : "")} onClick={(e) => { e.stopPropagation(); toggle(w); }}><div className="knob" /></div></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {allRows.length > PER && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 12, marginTop: 10 }}>
          <button className="btn" disabled={pg <= 0} onClick={() => setWpage(pg - 1)}>上一页</button>
          <span className="muted mono">第 {pg + 1} / {pages} 页 · 共 {allRows.length}</span>
          <button className="btn" disabled={pg >= pages - 1} onClick={() => setWpage(pg + 1)}>下一页</button>
        </div>
      )}
      {drawer && <WalletDrawer address={drawer} onClose={() => setDrawer(null)} />}
    </div>
  );
}
