import { api } from "../lib/api.js";
import { fNum, fSign, short } from "../lib/format.js";
import { useApiResource } from "../lib/refresh.js";
import { WalletDrawer } from "./wallets/WalletDrawer.jsx";

const { useState, useCallback } = React;

const marketLabel = (market) => ({ crypto: "加密", stock: "美股/指数", mixed: "混合" }[market] || market || "—");

const dataWarning = (status) => {
  if (!status || status === "valid") return null;
  if (status === "stale") return ["数据延迟", "tint-amber"];
  if (status === "deferred_data_error") return ["数据延迟", "tint-amber"];
  if (["replay_error", "invalid", "copy_data_error", "quarantine"].includes(status)) {
    return ["数据异常", "tint-red"];
  }
  return null;
};

export function Wallets({ confirm, toast }) {
  const [drawer, setDrawer] = useState(null);
  const [wpage, setWpage] = useState(0);
  const [tab, setTab] = useState("followed");
  const load = useCallback(() => api.get("/api/wallets?tab=" + tab + "&size=500"), [tab]);
  const { data, reload } = useApiResource(load, { intervalMs: 12000, clearOnLoadChange: true });
  const explicit = !!(data && data.selectionMode);
  const portfolioReplay = data && data.portfolioReplay;
  const replayLevs = portfolioReplay && portfolioReplay.effectiveParams && portfolioReplay.effectiveParams.leverageCaps;
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
      <div className="section-h wallets-section-h" style={{ marginTop: 6 }}>
        <h2>跟踪名单</h2>
        <div className="wallets-head-actions">
          {tab === "followed" && portfolioReplay && (
            <div className="portfolio-replay-kpi" title="当前 Core 使用 Observer 已生效参数在共享账户中做严格30日回放。复刻率同时考虑开仓捕获、加仓捕获以及是否存活到目标自然退出；爆仓为成交OHLC代理的保守压力值。">
              <span>当前Core · 生效参数 · 严格30d：</span>
              <b className={(portfolioReplay.netPnl30Worst || portfolioReplay.netPnl30 || 0) < 0 ? "down" : "up"}>{fSign(portfolioReplay.netPnl30Worst || portfolioReplay.netPnl30 || 0, 0)}</b>
              <i>爆仓≤{portfolioReplay.liquidations30Worst == null ? "—" : portfolioReplay.liquidations30Worst}</i>
              {portfolioReplay.behaviorReplication30Worst != null && <i>复刻≈{fNum(portfolioReplay.behaviorReplication30Worst * 100, 0)}%</i>}
              {replayLevs && <i>{fNum(replayLevs.STABLE_LEV_CAP, 0)}/{fNum(replayLevs.MID_LEV_CAP, 0)}/{fNum(replayLevs.HIGH_LEV_CAP, 0)}x</i>}
            </div>
          )}
          <div className="range-tabs">
            <button className={tab === "followed" ? "on" : ""} onClick={() => { setTab("followed"); setWpage(0); }}>跟单中{tab === "followed" && data && data.total != null ? " " + data.total : ""}</button>
            <button className={tab === "challenger" ? "on" : ""} onClick={() => { setTab("challenger"); setWpage(0); }}>候选{tab === "challenger" && data && data.total != null ? " " + data.total : ""}</button>
          </div>
        </div>
      </div>
      <div className="tbl-wrap">
        {explicit ? (
          <table>
            <thead><tr>
              <th>#</th><th>地址</th><th>市场</th><th className="num">评分</th>
              <th className="num" title="目标钱包自己近7天的新开仓次数 / 已平仓回合数">近7日钱包 开 / 平</th>
              <th className="num" title="按当前已生效的调参结果回放，已扣手续费；同时展示长期与近期结果">当前参数回放</th>
              <th className="num" title="该钱包自开始被跟单以来的实际仓位数与累计净盈亏；包含已平仓已实现盈亏和当前持仓浮动盈亏">实际跟单</th>
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
                    <td className="num">
                      <b style={{ color: (w.copyBacktestNetPnl || 0) < 0 ? "var(--red-l)" : "var(--green-l)" }}>{w.copyBacktestNetPnl != null ? fSign(w.copyBacktestNetPnl, 0) : "—"}</b>
                      <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>
                        30日已平 {w.copyBacktestNetPnl != null ? fSign((w.copyBacktestNetPnl || 0) - (w.copyBacktestUnrealizedPnl || 0), 0) : "—"}
                        {Math.abs(w.copyBacktestUnrealizedPnl || 0) >= 0.5 && <React.Fragment> · {(w.copyBacktestUnrealizedPnl || 0) < 0 ? "持仓亏损" : "持仓盈利"} <span style={{ color: (w.copyBacktestUnrealizedPnl || 0) < 0 ? "var(--red-l)" : "var(--green-l)" }}>{fSign(w.copyBacktestUnrealizedPnl, 0)}</span></React.Fragment>}
                      </div>
                      <div style={{ fontSize: 11, marginTop: 2, color: (w.copyBacktest7dNetPnl || 0) < 0 ? "var(--red-l)" : "var(--t2)" }}>
                        7日合计 {w.copyBacktest7dNetPnl != null ? fSign(w.copyBacktest7dNetPnl, 0) : "—"} · {w.copyBacktest7dClosedN || 0}笔
                      </div>
                      {w.copyBacktestValuationStatus && w.copyBacktestValuationStatus !== "complete" && <div className="muted" style={{ fontSize: 10, marginTop: 2 }}>持仓估值待确认</div>}
                    </td>
                    <td className="num">
                      {w.followCount > 0 ? <React.Fragment>
                        <b style={{ color: (w.forwardNetPnl || 0) < 0 ? "var(--red-l)" : "var(--green-l)" }}>{fSign(w.forwardNetPnl || 0, 0)}</b>
                        <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>共 {w.followCount} 笔</div>
                      </React.Fragment> : <span className="muted">暂无跟单</span>}
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
                  <td className="num"><b style={{ color: "var(--green-l)" }}>{fNum(w.score, 1)}</b></td>
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
