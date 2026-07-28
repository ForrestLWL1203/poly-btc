import { api } from "../../lib/api.js";
import { agoText, cls, fNum, fSign, short } from "../../lib/format.js";
import { IC, Ico } from "../../lib/icons.jsx";
import { useApiResource } from "../../lib/refresh.js";
import { PositionDetail } from "../positions/PositionDetail.jsx";

const { useState, useEffect, useCallback } = React;

const STATUS_LABEL = { open: "在持", closed: "已平", gap_closed: "缺口平", liquidated: "爆仓", tail_closed: "尾盈平" };

const marketLabel = (m) => ({ crypto: "加密", stock: "美股/指数", mixed: "混合" }[m] || m || "—");
const tierLabel = (tier) => ({ stable: "稳定档", mid: "中档", high: "剧烈档" }[tier] || tier || "未分档");
const openSkipLabel = (reason) => ({
  skip_small_notl: "低于最低经济名义额",
  skip_coin_full: "单币容量已满",
  skip_deploy_cap: "组合部署上限",
  skip_no_cash: "可用保证金不足",
  skip_margin_too_small: "可用开仓保证金过小",
  skip_wallet_position_cap: "并发仓位上限",
  skip_coin_blacklist: "策略禁用市场",
}[reason] || reason || "未执行");

const copyWindowRows = (breakdown, profitability) => {
  const pnl = breakdown.copyPnl || {};
  const closed = breakdown.closedN || {};
  const returns = breakdown.economicReturnsPct || {};
  const equities = breakdown.windowStartEquity || {};
  const economics = profitability || breakdown.copyEconomics || {};
  const e30 = economics["30d"] || {};
  const e7 = economics["7d"] || {};
  return [
    ["30 天", e30.qualificationPnl ?? pnl["30d"], closed["30d"], e30.qualificationReturn != null ? e30.qualificationReturn * 100 : returns["30d"], equities["30d"], e30],
    ["7 天", e7.qualificationPnl ?? pnl["7d"], closed["7d"], e7.qualificationReturn != null ? e7.qualificationReturn * 100 : returns["7d"], equities["7d"], e7],
  ].filter((row) => Number(row[2] || 0) > 0 || Math.abs(Number(row[1] || 0)) > 0);
};

function DecisionCard({ title, tone = "", children }) {
  return (
    <div className={"wallet-decision-card " + tone}>
      <div className="wallet-decision-title">{title}</div>
      {children}
    </div>
  );
}

export function WalletDrawer({ address, onClose }) {
  const [recPage, setRecPage] = useState(0);
  const [exp, setExp] = useState({});
  const [details, setDetails] = useState({});
  useEffect(() => { setRecPage(0); setExp({}); setDetails({}); }, [address]);
  const loadDrawer = useCallback(() => api.get(`/api/wallets/${address}?recPage=${recPage}&recSize=20`), [address, recPage]);
  const { data: d } = useApiResource(loadDrawer, { clearOnLoadChange: true });
  const toggleRecord = (id) => {
    const next = !exp[id];
    setExp(e => ({ ...e, [id]: next }));
    if (next && !details[id]) {
      api.get(`/api/positions/${id}`).then(payload => setDetails(m => ({ ...m, [id]: payload }))).catch(() => {});
    }
  };
  const net = d && (d.netPnl || 0);
  const losing = d && net < -5;
  const recPages = d ? Math.max(1, Math.ceil(d.recordsTotal / d.recSize)) : 1;
  const liveWinDelta = d && d.forwardWinRatePct != null && d.scoredWinRatePct != null
    ? d.forwardWinRatePct - d.scoredWinRatePct
    : null;
  const scoreBreakdown = (d && d.scoreBreakdown) || {};
  const scoreComponents = scoreBreakdown.components || {};
  const sourceQuality = (d && d.sourceQuality) || {};
  const copyExecution = (d && d.copyExecution) || {};
  const historicalSkipDetails = (copyExecution.historicalAudit && copyExecution.historicalAudit.skipDetails) || [];
  const officialEvidence = (((d && d.officialPerpEvidence) || {}).windows || {}).officialPerp30d || {};
  const officialIsShort = officialEvidence.historyTier === "short_history_7d";
  const officialFundedDays = officialEvidence.fundedCoverageDays ?? officialEvidence.positiveCoverageDays;
  const officialRoiLabel = officialIsShort
    ? `官方 Perp 短历史 ROI（累计${fNum(officialFundedDays, 0)}日有资金运行）`
    : `官方 Perp ${officialEvidence.windowDays != null ? fNum(officialEvidence.windowDays, 0) : "约30"}日 ROI`;
  const copyRows = copyWindowRows(scoreBreakdown, d && d.copyProfitability);
  const copy30 = copyRows.find(([label]) => label === "30 天");
  const roleView = !d ? null : d.role === "core"
    ? { label: "跟单中", detail: "已通过钱包质量筛选与组合回放，当前允许新开仓。", tone: "good" }
    : d.role === "challenger"
      ? { label: "候选", detail: d.selectionReasonText || "当前未进入跟单列表。", tone: "neutral" }
      : d.role === "exit_only"
        ? { label: "只平不开", detail: "不再复制新开仓，已有仓位继续管理至退出。", tone: "warn" }
        : { label: "未跟单", detail: d.selectionReasonText || "当前不在跟单列表。", tone: "neutral" };
  const riskItems = !d ? [] : [
    losing && ["实盘亏损", fSign(d.netPnl, 1), "danger"],
    d.openUnrealized < -5 && ["在持浮亏", fSign(d.openUnrealized, 1), "danger"],
    d.closedN === 0 && ["暂无实跟平仓", "0 笔", "warn"],
    liveWinDelta != null && liveWinDelta < -20 && ["实盘胜率低于历史", fNum(liveWinDelta, 0) + "pt", "warn"],
    d.lossN > d.winN && ["亏损笔数偏多", d.lossN + " 负", "warn"],
  ].filter(Boolean);
  return (
    <React.Fragment>
      <div className="scrim" onClick={onClose} />
      <div className="drawer wallet-drawer">
        <div className="drawer-head">
          <div>
            <h3>{short(address)}</h3>
            <div className="muted">排名 #{d ? (d.rank != null ? d.rank : "—") : "—"} · {d ? marketLabel(d.marketType) : ""}</div>
          </div>
          <button className="drawer-close" type="button" onClick={onClose} aria-label="关闭跟单记录" title="关闭">
            <Ico d={IC.close} />
          </button>
        </div>
        {!d ? <div className="loading">加载中…</div> : (
          <React.Fragment>
            <div className={"wallet-decision-hero " + roleView.tone}>
              <div>
                <div className="card-lbl">名单状态</div>
                <div className="wallet-decision-status">{roleView.label}</div>
                <div className="muted">{roleView.detail}</div>
              </div>
            </div>

            <div className="wallet-stat-grid">
              <div><span>实际盈亏</span><b className={cls(d.netPnl)}>{fSign(d.netPnl, 1)}</b><em>含在持浮动</em></div>
              <div><span>实际跟单</span><b>{d.recordsTotal}</b><em>{d.closedN} 已平 · {d.openN} 在持</em></div>
              <div><span>实盘胜率</span><b>{d.forwardWinRatePct != null ? fNum(d.forwardWinRatePct, 0) + "%" : "—"}</b><em>{d.closedN} 平仓</em></div>
              <div><span>30日保守收益</span><b className={copy30 ? cls(copy30[1]) : ""}>{copy30 ? fSign(copy30[1] || 0, 0) : "—"}</b><em>{copy30 ? (copy30[2] || 0) + " 个已平回合" : "暂无数据"}</em></div>
              <div><span>盈利优先值</span><b className={(d.profitPriorityPct || 0) < 0 ? "down" : "up"}>{d.profitPriorityPct != null ? fSign(d.profitPriorityPct, 1) + "%" : "—"}</b><em>70%×30日 + 30%×7日{d.profitRank != null ? ` · #${d.profitRank}` : ""}</em></div>
            </div>

            <div className="wallet-decision-grid">
              <DecisionCard title={d.copyReplayStage === "strict" ? "最终严格 Copy · 保守资格" : "粗略 fills-only Copy · 保守资格"} tone={copyRows.length ? "good" : "muted"}>
                {copyRows.length ? (
                  <div className="score-window-grid">
                    {copyRows.map(([label, pnl, n, returnPct, startEquity, economics]) => (
                      <div className="score-window" key={label}>
                        <span>{label}</span>
                        <b className={(pnl || 0) >= 0 ? "up" : "down"}>{fSign(pnl || 0, 0)}</b>
                        <small>{returnPct != null ? `${fSign(returnPct, 1)}% · ` : ""}{n || 0} 回合{startEquity != null ? ` · 期初 $${fNum(startEquity, 0)}` : ""}</small>
                        {economics && <small>已平 {fSign(economics.closedPnl, 0)} · 浮亏 {fSign(-Number(economics.openLoss || 0), 0)}{Number(economics.openProfitReference || 0) > 0 ? ` · 浮盈参考 ${fSign(economics.openProfitReference, 0)}` : ""}</small>}
                      </div>
                    ))}
                  </div>
                ) : <p>暂无可用 copy 回测窗口，先按历史评分和实盘记录观察。</p>}
              </DecisionCard>

              {sourceQuality.episodeN30d != null && (
                <DecisionCard title="源钱包质量" tone={(sourceQuality.winRate30dPct || 0) >= 70 ? "good" : "warn"}>
                  <div className="wallet-risk-list">
                    <div className="wallet-risk"><span>{officialRoiLabel}</span><b>{d.officialPerpReturnPct != null ? fNum(d.officialPerpReturnPct, 1) + "%" : "—"}</b></div>
                    <div className="wallet-risk"><span>30日完整回合 / 胜率</span><b>{sourceQuality.episodeN30d} / {sourceQuality.winRate30dPct != null ? fNum(sourceQuality.winRate30dPct, 1) + "%" : "—"}</b></div>
                    <div className="wallet-risk"><span>最近7日回合 / 胜率</span><b>{sourceQuality.episodeN7d ?? "—"} / {sourceQuality.winRate7dPct != null ? fNum(sourceQuality.winRate7dPct, 1) + "%" : "—"}</b></div>
                    <div className="wallet-risk"><span>源钱包30日 / 7日净利</span><b>{fSign(sourceQuality.netPnl30d, 0)} / {fSign(sourceQuality.netPnl7d, 0)}</b></div>
                    <div className="wallet-risk"><span>当前浮盈参考 / 当前浮亏</span><b>{fSign((sourceQuality.economics30d || {}).openProfitReference, 0)} / {fSign(-Number((sourceQuality.economics30d || {}).openLoss || 0), 0)}</b></div>
                    <div className="wallet-risk"><span>浮亏占30日已平利润 / 保守资格利润</span><b>{(sourceQuality.economics30d || {}).openLossRatio != null ? fNum(sourceQuality.economics30d.openLossRatio * 100, 1) + "%" : "—"} / {fSign((sourceQuality.economics30d || {}).qualificationPnl, 0)}</b></div>
                    <div className="wallet-risk"><span>前三大赢家占毛利</span><b>{sourceQuality.top3ProfitSharePct != null ? fNum(sourceQuality.top3ProfitSharePct, 1) + "%" : "—"}</b></div>
                    {sourceQuality.top3ProfitSharePct >= 70 && <div className="wallet-risk"><span>去前三后胜率 / 净利</span><b>{sourceQuality.bodyAfterTop3WinRatePct != null ? fNum(sourceQuality.bodyAfterTop3WinRatePct, 1) + "%" : "—"} / {fSign(sourceQuality.bodyAfterTop3NetPnl, 0)}</b></div>}
                  </div>
                </DecisionCard>
              )}

              {copyExecution.effectiveFollowRatePct != null && (
                <DecisionCard title="开仓执行审计" tone="neutral">
                  <div className="wallet-risk-list">
                    <div className="wallet-risk"><span>历史有效跟随率</span><b>{fNum(copyExecution.effectiveFollowRatePct, 1)}%</b></div>
                    <div className="wallet-risk"><span>实际开仓 / 有效目标开仓</span><b>{copyExecution.openedN ?? "—"} / {copyExecution.effectiveTargetOpenN ?? "—"}</b></div>
                    <div className="wallet-risk"><span>原始目标开仓 / 原始捕获率</span><b>{copyExecution.rawTargetOpenN ?? "—"} / {copyExecution.rawCaptureRatePct != null ? fNum(copyExecution.rawCaptureRatePct, 1) + "%" : "—"}</b></div>
                    <div className="wallet-risk"><span>经济小单排除</span><b>{copyExecution.smallOpenExcludedN || 0}</b></div>
                    <div className="wallet-risk"><span>近30日实时流动性跳过</span><b>{copyExecution.liveLiquiditySkipN30d || 0}{(copyExecution.liveLiquiditySkipCoins30d || []).length ? " · " + copyExecution.liveLiquiditySkipCoins30d.join(" / ") : ""}</b></div>
                    {historicalSkipDetails.map((item, index) => (
                      <div className="wallet-risk" key={`${item.coin}-${item.reason}-${index}`}>
                        <span>{item.coin || "未知市场"} · {tierLabel(item.tier)} · {openSkipLabel(item.reason)}</span>
                        <b>{item.count || 0} 次{item.copyNotionalMax != null ? ` · $${fNum(item.copyNotionalMax, 0)}` : ""}{item.minimumNotional ? ` / 门槛 $${fNum(item.minimumNotional, 0)}` : ""}</b>
                      </div>
                    ))}
                  </div>
                </DecisionCard>
              )}

              {Object.keys(scoreComponents).length > 0 && (
                <DecisionCard title="综合评分构成" tone="neutral">
                  <div className="wallet-risk-list">
                    <div className="wallet-risk"><span>Copy保守30日 / Copy保守7日</span><b>{fNum(scoreComponents.copy30d, 1)} / {fNum(scoreComponents.copy7d, 1)}</b></div>
                    <div className="wallet-risk"><span>源胜率 / Copy胜率</span><b>{fNum(scoreComponents.sourceWinRate, 1)} / {fNum(scoreComponents.copyWinRate, 1)}</b></div>
                    <div className="wallet-risk"><span>开仓跟随 / 行为复制</span><b>{fNum(scoreComponents.openFollowRate, 1)} / {fNum(scoreComponents.behaviorReplication, 1)}</b></div>
                    <div className="wallet-risk"><span>活跃度 / 独立开仓</span><b>{fNum(scoreComponents.activityRecency, 1)} / {fNum(scoreComponents.independentOpens, 1)}</b></div>
                  </div>
                </DecisionCard>
              )}

              {riskItems.length > 0 && (
                <DecisionCard title="需要留意" tone={losing ? "danger" : "warn"}>
                  <div className="wallet-risk-list">
                    {riskItems.map(([label, value, tone]) => (
                      <div className={"wallet-risk " + tone} key={label}>
                        <span>{label}</span><b>{value}</b>
                      </div>
                    ))}
                  </div>
                </DecisionCard>
              )}
            </div>

            <div className="card-lbl" style={{ marginBottom: 8 }}>跟单记录 <span className="muted">· 共 {d.recordsTotal} 笔(点击展开)</span></div>
            <div className="tbl-wrap">
              <table><thead><tr><th>币种</th><th>方向</th><th className="num">盈亏</th><th className="num">时间</th><th>状态</th></tr></thead>
                <tbody>{d.records.map(r => (
                  <React.Fragment key={r.id}>
                    <tr style={{ cursor: "pointer" }} onClick={() => toggleRecord(r.id)}>
                      <td><b>{r.coin}</b> <span className="muted" style={{ fontSize: 10 }}>{exp[r.id] ? "▴" : "▾"}</span></td>
                      <td><span className={"tint " + (r.side === "long" ? "tint-green" : "tint-red")}>{r.side === "long" ? "多" : "空"}</span></td>
                      <td className={"num " + cls(r.pnl)}>{fSign(r.pnl, 1)}{r.status === "open" ? <span className="muted" style={{ fontSize: 10 }}> 浮</span> : ""}</td>
                      <td className="num muted">{agoText(r.openedAt)}</td>
                      <td className="muted">{STATUS_LABEL[r.status] || r.status}</td>
                    </tr>
                    {exp[r.id] && (
                      <tr className="detail-row"><td colSpan="5">
                        <PositionDetail d={details[r.id]} />
                      </td></tr>
                    )}
                  </React.Fragment>
                ))}</tbody></table>
            </div>
            {d.recordsTotal > d.recSize && (
              <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 12, marginTop: 10 }}>
                <button className="btn" disabled={recPage <= 0} onClick={() => setRecPage(recPage - 1)}>上一页</button>
                <span className="muted mono">第 {recPage + 1} / {recPages} 页</span>
                <button className="btn" disabled={recPage >= recPages - 1} onClick={() => setRecPage(recPage + 1)}>下一页</button>
              </div>
            )}
          </React.Fragment>
        )}
      </div>
    </React.Fragment>
  );
}
