import { api } from "../lib/api.js";
import { fNum, fSign, short } from "../lib/format.js";
import { useApiResource } from "../lib/refresh.js";
import { BanIcon, CopyIcon } from "../lib/icons.jsx";
import { WalletDrawer } from "./wallets/WalletDrawer.jsx";

const { useState, useCallback, useEffect, useRef } = React;

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

const riskBadge = (w) => {
  if (w.operatorIntent === "draining") return ["仅退出中", "tint-amber"];
  if (w.operatorIntent === "requalify") return ["人工退出·等待重评", "tint-gray"];
  return {
    low: ["低风险", "tint-amber"],
    medium: ["中风险", "tint-amber"],
    high: ["高风险", "tint-red"],
    unavailable: ["资金撤出", "tint-red"],
    structural: ["结构不可跟", "tint-red"],
    data_error: ["数据异常", "tint-red"],
  }[w.riskLevel] || null;
};

const copyWalletAddress = async (address) => {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(address);
      return;
    } catch {
      // Fall through for browsers that expose Clipboard API but deny access.
    }
  }
  const input = document.createElement("textarea");
  input.value = address;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.opacity = "0";
  input.style.pointerEvents = "none";
  document.body.appendChild(input);
  input.select();
  input.setSelectionRange(0, input.value.length);
  const copied = document.execCommand("copy");
  input.remove();
  if (!copied) throw new Error("copy_failed");
};

function CopyableAddress({ address, isNew }) {
  const [status, setStatus] = useState("idle");
  const resetTimer = useRef(null);
  useEffect(() => () => window.clearTimeout(resetTimer.current), []);

  const onCopy = async (event) => {
    event.stopPropagation();
    window.clearTimeout(resetTimer.current);
    try {
      await copyWalletAddress(address);
      setStatus("copied");
    } catch {
      setStatus("failed");
    }
    resetTimer.current = window.setTimeout(() => setStatus("idle"), 1400);
  };

  const feedback = status === "copied" ? "已复制 ✓" : "复制失败";
  return (
    <span className="addr-with-new">
      <button type="button"
        className={"addr-copy-btn" + (status === "copied" ? " copied" : status === "failed" ? " failed" : "")}
        aria-label={`复制钱包完整地址 ${address}`}
        title={status === "copied" ? "完整地址已复制" : `点击复制完整地址\n${address}`}
        onClick={onCopy}>
        <span className={"addr-copy-label" + (status !== "idle" ? " hidden" : "")}>{short(address)}</span>
        <span className={"addr-copy-icon" + (status !== "idle" ? " hidden" : "")}><CopyIcon /></span>
        {status !== "idle" && <span className="addr-copy-feedback" aria-live="polite">{feedback}</span>}
      </button>
      {isNew && <span className="new-wallet-badge">NEW</span>}
    </span>
  );
}

export function Wallets({ confirm, onDataChanged = null }) {
  const [drawer, setDrawer] = useState(null);
  const [wpage, setWpage] = useState(0);
  const [tab, setTab] = useState("followed");
  const [starPending, setStarPending] = useState({});
  const [exitPending, setExitPending] = useState({});
  const [starError, setStarError] = useState(null);
  const [exitError, setExitError] = useState(null);
  const load = useCallback(() => api.get("/api/wallets?tab=" + tab + "&size=500"), [tab]);
  const { data, setData, reload } = useApiResource(load, { intervalMs: 12000, clearOnLoadChange: true });
  const explicit = !!(data && data.selectionMode);
  const portfolioReplay = data && data.portfolioReplay;
  const portfolioRoiReplay = portfolioReplay && (portfolioReplay.paperAccount || portfolioReplay);
  const portfolioRelease = data && data.portfolioRelease;
  const allRows = (data && data.wallets) || [];
  const PER = 10, pages = Math.max(1, Math.ceil(allRows.length / PER)), pg = Math.min(wpage, pages - 1);
  const pageRows = allRows.slice(pg * PER, pg * PER + PER);

  const requestExit = (w) => {
    if (exitPending[w.address] || w.operatorIntent === "draining") return;
    const hasPositions = Number(w.exitPositionCount || 0) > 0;
    const act = async () => {
      setExitError(null);
      setExitPending(pending => ({ ...pending, [w.address]: true }));
      try {
        await api.cmdAndWait("wallet_exit_request", { address: w.address });
        await Promise.all([reload(), onDataChanged ? onDataChanged() : Promise.resolve()]);
      } catch (error) {
        if (!error || error.message !== "unauth") setExitError("条件性退榜失败，请稍后重试");
      } finally {
        setExitPending(pending => {
          const next = { ...pending };
          delete next[w.address];
          return next;
        });
      }
    };
    confirm({
      title: hasPositions ? "进入仅退出" : "转入候选",
      danger: true,
      ok: hasPositions ? "仅退出" : "转候选",
      body: hasPositions
        ? `停止 ${short(w.address)} 新开仓和加仓；当前 ${w.exitPositionCount} 笔整批净盈利平仓将自动恢复，亏损或清算后转候选。`
        : `立即将 ${short(w.address)} 转候选，等待每日完整重评恢复。`,
      onConfirm: act,
    });
  };

  const cancelExit = (w) => {
    if (exitPending[w.address] || w.operatorIntent !== "draining") return;
    const act = async () => {
      setExitError(null);
      setExitPending(pending => ({ ...pending, [w.address]: true }));
      try {
        await api.cmdAndWait("wallet_exit_cancel", { address: w.address });
        await Promise.all([reload(), onDataChanged ? onDataChanged() : Promise.resolve()]);
      } catch (error) {
        if (!error || error.message !== "unauth") setExitError("取消仅退出失败，请刷新状态后重试");
      } finally {
        setExitPending(pending => {
          const next = { ...pending };
          delete next[w.address];
          return next;
        });
      }
    };
    confirm({
      title: "取消仅退出",
      ok: "恢复跟单",
      body: `恢复 ${short(w.address)} 的新开仓和加仓权限；当前持仓不会被平掉，将继续按正常跟单规则管理。`,
      onConfirm: act,
    });
  };

  const toggleStar = async (w) => {
    if (starPending[w.address]) return;
    setStarError(null);
    setStarPending(pending => ({ ...pending, [w.address]: true }));
    try {
      const result = await api.cmdAndWait("wallet_star", { address: w.address, starred: !w.starred });
      setData(current => current ? { ...current, wallets: (current.wallets || []).map(row =>
        row.address === w.address
          ? { ...row, starred: !!result.starred, starredAt: result.starredAt || null }
          : row) } : current);
      await reload();
    } catch (error) {
      if (!error || error.message !== "unauth") setStarError("星标更新失败，请稍后重试");
    } finally {
      setStarPending(pending => {
        const next = { ...pending };
        delete next[w.address];
        return next;
      });
    }
  };

  return (
    <div className="content">
      <div className="section-h wallets-section-h" style={{ marginTop: 6 }}>
        <h2>跟踪名单</h2>
        <div className="wallets-head-actions">
          {tab === "followed" && portfolioReplay && (
            <div className="portfolio-replay-kpi" title="ROI 以本次采集时冻结的账户期初权益为分母；30日和最近7日分别使用各自窗口边界的真实浮动权益。">
              <span>严格回测预估收益：</span>
              <b className={(portfolioRoiReplay?.dynamicReturn30d || 0) < 0 ? "down" : "up"}>
                30d {portfolioRoiReplay?.dynamicReturn30d != null ? fSign(portfolioRoiReplay.dynamicReturn30d * 100, 1) + "%" : "—"}
              </b>
              <i>｜</i>
              <i>7d {portfolioRoiReplay?.dynamicReturn7d != null ? fSign(portfolioRoiReplay.dynamicReturn7d * 100, 1) + "%" : "—"}</i>
            </div>
          )}
          <div className="range-tabs">
            <button className={tab === "followed" ? "on" : ""} onClick={() => { setTab("followed"); setWpage(0); }}>跟单中{tab === "followed" && data && data.total != null ? " " + data.total : ""}</button>
            <button className={tab === "challenger" ? "on" : ""} onClick={() => { setTab("challenger"); setWpage(0); }}>候选{tab === "challenger" && data && data.total != null ? " " + data.total : ""}</button>
          </div>
        </div>
      </div>
      {tab === "followed" && portfolioRelease && portfolioRelease.status === "operator_review_degraded" &&
        <div className="wallet-alert" role="status">
          组合经济门槛降级：保留当前有效 Core 与参数，暂停自动晋升和调参，等待人工复核。
        </div>}
      {starError && <div className="wallet-alert" role="alert">{starError}</div>}
      {exitError && <div className="wallet-alert" role="alert">{exitError}</div>}
      <div className="tbl-wrap">
        {explicit ? (
          <table>
            <thead><tr>
              <th>#</th><th>地址</th><th>市场</th><th className="num" title="最终Strict评分：60分资格基线 + 35分盈利能力 + 5分综合可信度；评分只排序，不替代准入门槛">评分</th>
              <th className="num" title="60% × 严格Copy保守30日收益率 + 25% × 最近28天四段7日平均收益 + 15% × 四段最差收益；四段均须有已平回合且盈利">盈利优先</th>
              <th className="num" title="目标钱包自己近7天的新开仓次数 / 已平仓回合数">近7日钱包 开 / 平</th>
              <th className="num" title="仅显示当前 generation 的最终严格 Copy 证据；缺少严格回放时不以粗回放替代。">回放数据</th>
              <th className="num" title="该钱包自开始被跟单以来的实际仓位数与累计净盈亏；包含已平仓已实现盈亏和当前持仓浮动盈亏">实际跟单</th>
              <th>主力</th>
              {tab === "challenger" && <th>未跟原因</th>}<th>操作</th>
            </tr></thead>
            <tbody>
              {data === null && <tr><td colSpan={tab === "challenger" ? 11 : 10} className="loading">加载中…</td></tr>}
              {data && pageRows.length === 0 && <tr><td colSpan={tab === "challenger" ? 11 : 10} className="empty">{tab === "challenger" ? "当前没有待观察钱包" : "当前没有符合实跟条件的钱包"}</td></tr>}
              {data && pageRows.map(w => {
                const warning = dataWarning(w.dataStatus);
                const risk = riskBadge(w);
                return (
                  <tr key={w.address} className={w.operatorIntent === "requalify" ? "row-off" : ""}
                    style={{ cursor: "pointer" }} onClick={() => setDrawer(w.address)}>
                    <td><div className="wallet-rank-cell">
                      {tab === "followed" && <button type="button"
                        className={"btn btn-star" + (w.starred ? " on" : "")}
                        aria-label={w.starred ? "取消星标" : "设为星标钱包"}
                        aria-pressed={!!w.starred}
                        title={w.starred
                          ? "全量严格重评时保留 Core 席位；硬性安全门禁仍可退出，点击取消星标"
                          : "星标后可在全量严格重评中获得 Core 留任保护；硬性安全门禁仍然生效"}
                        disabled={!!starPending[w.address]}
                        onClick={(e) => { e.stopPropagation(); toggleStar(w); }}>
                        {starPending[w.address] ? "…" : w.starred ? "★" : "☆"}
                      </button>}
                      <span className="rankbadge">{w.followPos}</span>
                    </div></td>
                    <td className="addr">
                      <CopyableAddress address={w.address} isNew={w.isNew} />
                      {warning && <span className={"tint " + warning[1]} style={{ marginLeft: 6 }} title="本轮画像数据不完整">{warning[0]}</span>}
                      {risk && <span className={"tint " + risk[1]} style={{ marginLeft: 6 }}
                        title={(w.riskReasons || []).join("；") || risk[0]}>{risk[0]}</span>}
                      {tab === "followed" && (w.retentionStatus === "safety_frozen" || w.retentionStatus === "safety_pending") &&
                        <span className="tint tint-red" style={{ marginLeft: 6 }}>安全冻结</span>}
                    </td>
                    <td><span className={"tint " + (w.marketType === "crypto" ? "tint-blue" : w.marketType === "stock" ? "tint-amber" : "tint-gray")}>{marketLabel(w.marketType)}</span></td>
                    <td className="num"><b style={{ color: "var(--green-l)" }} title={w.scoreProjected ? `按当前V7公式投影；本代审计原分 ${fNum(w.auditScore, 1)}` : ""}>{fNum(w.score, 1)}</b></td>
                    <td className="num">
                      <b className={(w.profitPriorityPct || 0) < 0 ? "down" : "up"}>
                        {w.profitPriorityPct != null ? fSign(w.profitPriorityPct, 1) + "%" : "—"}
                      </b>
                      {w.profitRank != null && <div className="muted" style={{ fontSize: 10 }}>跟单序 #{w.profitRank}</div>}
                    </td>
                    <td className="num mono"><b>{w.openEvents7d ?? "—"}</b> <span className="muted">/</span> {w.closed7d ?? "—"}</td>
                    <td className="num">
                      {w.copyReplayStage === "strict" ? <React.Fragment>
                        <div><span className="muted">30d </span><b className={(w.copyBacktestReturnPct || 0) < 0 ? "down" : "up"}>{w.copyBacktestReturnPct != null ? fSign(w.copyBacktestReturnPct, 1) + "%" : "—"}</b>
                          <span className="muted"> · 7d </span><b className={(w.copyBacktest7dReturnPct || 0) < 0 ? "down" : "up"}>{w.copyBacktest7dReturnPct != null ? fSign(w.copyBacktest7dReturnPct, 1) + "%" : "—"}</b>
                        </div>
                        <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>
                          完整回合 30d {w.copyBacktestClosedN ?? "—"} / 7d {w.copyBacktest7dClosedN ?? "—"}
                        </div>
                        <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
                          {Number(w.copyBacktestUnrealizedPnl || 0) < 0 ? "持仓浮亏 " : Number(w.copyBacktestUnrealizedPnl || 0) > 0 ? "持仓浮盈 " : "持仓浮动 "}
                          {w.copyBacktestUnrealizedPnl != null
                            ? <span className={w.copyBacktestUnrealizedPnl < 0 ? "down" : "up"}>{fSign(w.copyBacktestUnrealizedPnl, 0)}</span>
                            : "—"}
                          <span> · 胜率 {w.winRatePct != null ? fNum(w.winRatePct, 0) + "%" : "—"}</span>
                        </div>
                        {w.largeSingleLiquidation && <div className="muted" style={{ fontSize: 11, marginTop: 2, color: "var(--amber)" }}>
                          较大单次清算 {fNum(w.maxSingleLiquidationLossPct, 1)}%
                        </div>}
                      </React.Fragment> : <span className="muted">严格回放待完成</span>}
                    </td>
                    <td className="num">
                      {w.followCount > 0 ? <React.Fragment>
                        <b style={{ color: (w.forwardNetPnl || 0) < 0 ? "var(--red-l)" : "var(--green-l)" }}>{fSign(w.forwardNetPnl || 0, 0)}</b>
                        <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>共 {w.followCount} 笔</div>
                      </React.Fragment> : <span className="muted">暂无跟单</span>}
                    </td>
                    <td><b>{w.mainCoin || "—"}</b></td>
                    {tab === "challenger" && <td><span className="muted">{w.operatorIntent === "requalify" ? "人工退出·等待每日完整重评" : (w.selectionReasonText || "未满足实跟条件")}</span></td>}
                    <td>
                      {tab === "followed" ? <button type="button"
                        className={"coin-ban-btn" + (w.operatorIntent === "draining" ? " on" : "")}
                        aria-label={w.operatorIntent === "draining" ? "取消该钱包仅退出状态" : "请求该钱包条件性退榜"}
                        aria-pressed={w.operatorIntent === "draining"}
                        title={w.operatorIntent === "draining" ? "仅退出中；点击取消并恢复正常跟单" : "条件性退榜"}
                        disabled={!!exitPending[w.address]}
                        onClick={(e) => { e.stopPropagation(); w.operatorIntent === "draining" ? cancelExit(w) : requestExit(w); }}>
                        {exitPending[w.address] ? <span className="spin" /> : <BanIcon />}
                        <span className="coin-ban-tip">{w.operatorIntent === "draining" ? "取消仅退出" : "条件性退榜"}</span>
                      </button> : <span className="muted">—</span>}
                    </td>
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
              <th className="num">最大亏损</th><th>主力</th><th className="num">被跟</th><th className="num">总体盈亏</th><th>操作</th>
            </tr></thead>
            <tbody>
              {data === null && <tr><td colSpan="12" className="loading">加载中…</td></tr>}
              {data && pageRows.map(w => (
                <tr key={w.address} className={w.enabled ? "" : "row-off"}
                  style={{ cursor: "pointer" }} onClick={() => setDrawer(w.address)}>
                  <td><span className="rankbadge" title={w.followPos != null ? "跟单序号(与脚本一致);全站评分名次 #" + w.rank : "全站评分名次"}>{w.followPos != null ? w.followPos : w.rank}</span></td>
                  <td className="addr"><CopyableAddress address={w.address} isNew={w.isNew} /></td>
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
                  <td><button type="button" className="coin-ban-btn"
                    aria-label="请求该钱包条件性退榜" title="条件性退榜"
                    disabled={!!exitPending[w.address]}
                    onClick={(e) => { e.stopPropagation(); requestExit(w); }}>
                    {exitPending[w.address] ? <span className="spin" /> : <BanIcon />}
                  </button></td>
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
