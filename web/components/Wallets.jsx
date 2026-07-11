import { api } from "../lib/api.js";
import { fNum, fSign, fTime, short } from "../lib/format.js";
import { useApiResource } from "../lib/refresh.js";
import { useWalletAudit } from "./wallets/WalletAudit.jsx";
import { WalletDrawer } from "./wallets/WalletDrawer.jsx";

const { useState, useEffect, useCallback } = React;

const dropBatchLabel = (source) => ({
  scan: "每日重采", scan_post_tune: "重采后调参", regate: "重新门控",
  regate_post_tune: "门控后调参", watchlist: "名单重建",
}[source] || source || "历史记录");

const marketLabel = (market) => ({ crypto: "加密", stock: "美股/指数", mixed: "混合" }[market] || market || "—");

const dataWarning = (status) => {
  if (!status || status === "valid") return null;
  if (["stale", "deferred_data_error"].includes(status)) return ["数据延迟", "tint-amber"];
  return ["数据异常", "tint-red"];
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
  const explicit = !!(data && data.selectionMode);
  const allRows = (data && data.wallets) || [];
  const PER = 10, pages = Math.max(1, Math.ceil(allRows.length / PER)), pg = Math.min(wpage, pages - 1);
  const pageRows = allRows.slice(pg * PER, pg * PER + PER);

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
        <h2>跟踪名单</h2>
        <div className="range-tabs">
          <button className={tab === "followed" ? "on" : ""} onClick={() => { setTab("followed"); setWpage(0); }}>Core{tab === "followed" && data && data.total != null ? " " + data.total : ""}</button>
          <button className={tab === "challenger" ? "on" : ""} onClick={() => { setTab("challenger"); setWpage(0); }}>Challenger{tab === "challenger" && data && data.total != null ? " " + data.total : ""}</button>
          <button className={tab === "dropped" ? "on" : ""} onClick={() => { setTab("dropped"); setWpage(0); }}>降级</button>
        </div>
      </div>
      <div className="tbl-wrap">
        {dropped ? (
          <table>
            <thead><tr>
              <th>地址</th><th>市场</th><th className="num">当前分</th><th className="num">曾在线</th><th className="num">ROI</th>
              <th className="num">胜率</th><th>主力</th><th>降级原因</th><th>退榜批次</th>
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
                      <td className="num"><b style={{ color: "var(--t2)" }}>{fNum(w.score, 1)}</b></td>
                      <td className="num muted">{fNum(w.lastFollowedScore, 1)}</td>
                      <td className="num up">{fNum(w.roiEqPct, 0)}%</td>
                      <td className="num">{fNum(w.winRatePct, 0)}%</td>
                      <td><b>{w.mainCoin}</b></td>
                      <td><span className="tint tint-red">{w.dropReason}</span></td>
                      <td title={w.dropDecidedAt ? "本批次完成判定于 " + fTime(w.dropDecidedAt) : ""}>
                        <div className="mono" style={{ color: "var(--t2)", fontSize: 12 }}>{fTime(w.dropAt || w.lastFollowedAt)}</div>
                        <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>{dropBatchLabel(w.dropSource)}</div>
                      </td>
                    </tr>
                    {open && <tr className="detail-row"><td colSpan="9">{auditBox(w.address)}</td></tr>}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        ) : explicit ? (
          <table>
            <thead><tr>
              <th>#</th><th>地址</th><th>市场</th><th className="num">评分</th>
              <th className="num" title="目标钱包自己近7天的新开仓次数 / 已平仓回合数">近7日钱包 开 / 平</th>
              <th>最近开仓</th><th className="num" title="按当前跟单策略回放，已扣手续费；同时展示长期与近期结果">Copy回放</th>
              <th className="num">胜率</th><th>主力</th>
              {tab === "challenger" && <th>未跟原因</th>}<th>启用</th>
            </tr></thead>
            <tbody>
              {data === null && <tr><td colSpan={tab === "challenger" ? 11 : 10} className="loading">加载中…</td></tr>}
              {data && pageRows.length === 0 && <tr><td colSpan={tab === "challenger" ? 11 : 10} className="empty">{tab === "challenger" ? "当前没有待观察钱包" : "当前没有符合实跟条件的钱包"}</td></tr>}
              {data && pageRows.map(w => {
                const warning = dataWarning(w.dataStatus);
                return (
                  <tr key={w.address} className={w.enabled ? "" : "row-off"}
                    style={{ cursor: "pointer" }} onClick={() => setDrawer(w.address)}>
                    <td><span className="rankbadge">{w.followPos}</span></td>
                    <td className="addr">
                      <span className="addr-with-new">{short(w.address)}{w.isNew && <span className="new-wallet-badge">NEW</span>}</span>
                      {warning && <span className={"tint " + warning[1]} style={{ marginLeft: 6 }} title="本轮画像数据不完整">{warning[0]}</span>}
                    </td>
                    <td><span className={"tint " + (w.marketType === "crypto" ? "tint-blue" : w.marketType === "stock" ? "tint-amber" : "tint-gray")}>{marketLabel(w.marketType)}</span></td>
                    <td className="num"><b style={{ color: "var(--green-l)" }}>{fNum(w.score, 1)}</b></td>
                    <td className="num mono"><b>{w.openEvents7d ?? "—"}</b> <span className="muted">/</span> {w.closed7d ?? "—"}</td>
                    <td className="mono" style={{ color: "var(--t2)", fontSize: 12 }}>{w.lastActionableOpenAt ? fTime(w.lastActionableOpenAt) : "—"}</td>
                    <td className="num">
                      <b style={{ color: (w.copyBacktestNetPnl || 0) < 0 ? "var(--red-l)" : "var(--green-l)" }}>{w.copyBacktestNetPnl != null ? fSign(w.copyBacktestNetPnl, 0) : "—"}</b>
                      <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>30日 · {w.copyBacktestClosedN || 0}笔</div>
                      <div style={{ fontSize: 11, marginTop: 2, color: (w.copyBacktest7dNetPnl || 0) < 0 ? "var(--red-l)" : "var(--t2)" }}>
                        7日 {w.copyBacktest7dNetPnl != null ? fSign(w.copyBacktest7dNetPnl, 0) : "—"} · {w.copyBacktest7dClosedN || 0}笔
                      </div>
                    </td>
                    <td className="num">{w.winRatePct != null ? fNum(w.winRatePct, 0) + "%" : "—"}</td>
                    <td><b>{w.mainCoin || "—"}</b></td>
                    {tab === "challenger" && <td><span className="muted">{w.selectionReasonText || "未满足实跟条件"}</span></td>}
                    <td><div className={"toggle " + (w.enabled ? "on" : "")} onClick={(e) => { e.stopPropagation(); toggle(w); }}><div className="knob" /></div></td>
                  </tr>
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
                  <td className="addr"><span className="addr-with-new">{short(w.address)}{w.isNew && <span className="new-wallet-badge">NEW</span>}</span></td>
                  <td><span className={"tint " + (w.marketType === "crypto" ? "tint-blue" : w.marketType === "stock" ? "tint-amber" : "tint-gray")}>{w.marketType}</span></td>
                  <td className="num"><b style={{ color: w.score >= data.followLine ? "var(--green-l)" : "var(--t2)" }}>{fNum(w.score, 1)}</b></td>
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
