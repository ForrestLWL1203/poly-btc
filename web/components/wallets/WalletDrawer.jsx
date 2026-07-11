import { api } from "../../lib/api.js";
import { agoText, cls, fNum, fSign, short } from "../../lib/format.js";
import { IC, Ico } from "../../lib/icons.jsx";
import { useApiResource } from "../../lib/refresh.js";
import { PositionDetail } from "../positions/PositionDetail.jsx";

const { useState, useEffect, useCallback } = React;

const STATUS_LABEL = { open: "在持", closed: "已平", gap_closed: "缺口平", liquidated: "爆仓" };

const marketLabel = (m) => ({ crypto: "加密", stock: "美股/指数", mixed: "混合" }[m] || m || "—");

const copyWindowRows = (breakdown) => {
  const pnl = breakdown.copyPnl || {};
  const closed = breakdown.closedN || {};
  return [
    ["30 天", pnl["30d"], closed["30d"]],
    ["14 天", pnl["14d"], closed["14d"]],
    ["7 天", pnl["7d"], closed["7d"]],
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
  const copyRows = copyWindowRows(scoreBreakdown);
  const scoreReasons = (scoreBreakdown.reasons || []).slice(0, 6);
  const evidenceTone = !d || d.closedN >= 5 ? "good" : d.closedN > 0 ? "warn" : "muted";
  const riskItems = !d ? [] : [
    losing && ["实盘亏损", fSign(d.netPnl, 1), "danger"],
    d.openUnrealized < -5 && ["在持浮亏", fSign(d.openUnrealized, 1), "danger"],
    d.closedN === 0 && ["无平仓样本", "先观察", "warn"],
    liveWinDelta != null && liveWinDelta < -20 && ["实盘胜率低于历史", fNum(liveWinDelta, 0) + "pt", "warn"],
    d.lossN > d.winN && ["亏损笔数偏多", d.lossN + " 负", "warn"],
  ].filter(Boolean);
  const quietRisk = d && riskItems.length === 0;
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
            <div className={"wallet-decision-hero " + (losing ? "danger" : net > 0 ? "good" : "neutral")}>
              <div>
                <div className="card-lbl">钱包决策</div>
                <div className="wallet-decision-status">{losing ? "需要复核" : net > 0 ? "贡献为正" : "继续观察"}</div>
                <div className="muted">以现有实盘跟单记录评估，不改变跟单逻辑</div>
              </div>
              <div className="wallet-decision-net">
                <span>净盈亏</span>
                <b className={cls(d.netPnl)}>{fSign(d.netPnl, 1)}</b>
              </div>
            </div>

            <div className="wallet-stat-grid">
              <div><span>最终评分</span><b>{fNum(d.score, 1)}</b></div>
              <div><span>历史胜率</span><b>{d.scoredWinRatePct != null ? fNum(d.scoredWinRatePct, 0) + "%" : "—"}</b><em>{d.scoredTrades || 0} 笔</em></div>
              <div><span>实盘胜率</span><b>{d.forwardWinRatePct != null ? fNum(d.forwardWinRatePct, 0) + "%" : "—"}</b><em>{d.closedN} 平仓</em></div>
              <div><span>实盘记录</span><b>{d.recordsTotal}</b><em>{d.openN} 在持</em></div>
            </div>

            <div className="wallet-decision-grid">
              <DecisionCard title="跟单理由" tone={d.score >= 70 ? "good" : ""}>
                <p>评分 {fNum(d.score, 1)}，{d.rank != null ? "当前名单排名 #" + d.rank : "当前未在排名内"}，市场类型为 {marketLabel(d.marketType)}。</p>
                <div className="wallet-mini-row"><span>原始评分</span><b>{scoreBreakdown.rawScore != null ? fNum(scoreBreakdown.rawScore, 1) : "—"}</b></div>
                <div className="wallet-mini-row"><span>copy 分</span><b>{scoreBreakdown.copyScore != null ? fNum(scoreBreakdown.copyScore, 1) : "—"}</b></div>
                <div className="wallet-mini-row"><span>置信度</span><b>{scoreBreakdown.confidencePct != null ? fNum(scoreBreakdown.confidencePct, 0) + "%" : "—"}</b></div>
                <div className="wallet-mini-row"><span>预期保证金收益</span><b className={cls(scoreBreakdown.expectedReturnPct)}>{scoreBreakdown.expectedReturnPct != null ? fSign(scoreBreakdown.expectedReturnPct, 2) + "%" : "—"}</b></div>
                <div className="wallet-mini-row"><span>收益下置信界</span><b className={cls(scoreBreakdown.returnLcbPct)}>{scoreBreakdown.returnLcbPct != null ? fSign(scoreBreakdown.returnLcbPct, 2) + "%" : "—"}</b></div>
                <div className="wallet-mini-row"><span>未来盈利概率</span><b>{scoreBreakdown.positiveProbabilityPct != null ? fNum(scoreBreakdown.positiveProbabilityPct, 1) + "%" : "—"}</b></div>
                <div className="wallet-mini-row"><span>独立证据</span><b>{scoreBreakdown.evidenceDays != null ? scoreBreakdown.evidenceDays + " 天" : "—"}</b></div>
                <div className="wallet-mini-row"><span>历史样本</span><b>{d.scoredTrades || 0} 笔</b></div>
                <div className="wallet-mini-row"><span>历史胜率</span><b>{d.scoredWinRatePct != null ? fNum(d.scoredWinRatePct, 0) + "%" : "—"}</b></div>
                {scoreReasons.length > 0 && (
                  <div className="score-reasons" style={{ marginTop: 9 }}>
                    {scoreReasons.map((r, i) => <span key={i}>{r}</span>)}
                  </div>
                )}
              </DecisionCard>

              <DecisionCard title="历史 copy 回测" tone={copyRows.length ? "good" : "muted"}>
                {copyRows.length ? (
                  <div className="score-window-grid">
                    {copyRows.map(([label, pnl, n]) => (
                      <div className="score-window" key={label}>
                        <span>{label}</span>
                        <b className={(pnl || 0) >= 0 ? "up" : "down"}>{fSign(pnl || 0, 0)}</b>
                        <small>{n || 0} 笔</small>
                      </div>
                    ))}
                  </div>
                ) : <p>暂无可用 copy 回测窗口，先按历史评分和实盘记录观察。</p>}
              </DecisionCard>

              <DecisionCard title="证据质量" tone={evidenceTone}>
                <p>{d.closedN >= 5 ? "已有多笔实盘平仓记录，可用于对照历史评分。" : d.closedN > 0 ? "已有少量实盘记录，但样本仍偏薄，适合继续观察。" : "暂无实盘平仓样本，主要依赖历史评分。"}</p>
                <div className="wallet-mini-row"><span>实盘战绩</span><b><span className="up">{d.winN}胜</span> / <span className="down">{d.lossN}负</span></b></div>
                <div className="wallet-mini-row"><span>已实现</span><b className={cls(d.realizedPnl)}>{fSign(d.realizedPnl, 1)}</b></div>
              </DecisionCard>

              <DecisionCard title="风险信号" tone={losing ? "danger" : riskItems.length ? "warn" : "good"}>
                {quietRisk ? <p>暂无明显红旗，继续看实盘记录是否稳定。</p> : (
                  <div className="wallet-risk-list">
                    {riskItems.map(([label, value, tone]) => (
                      <div className={"wallet-risk " + tone} key={label}>
                        <span>{label}</span><b>{value}</b>
                      </div>
                    ))}
                  </div>
                )}
                <div className="wallet-mini-row"><span>在持浮动</span><b className={cls(d.openUnrealized)}>{fSign(d.openUnrealized, 1)}</b></div>
              </DecisionCard>
            </div>

            <div className="wallet-ledger">
              <div>
                <div className="muted">已实现</div>
                <div className={"mono " + cls(d.realizedPnl)}>{fSign(d.realizedPnl, 1)}</div>
              </div>
              <div>
                <div className="muted">在持({d.openN})浮动</div>
                <div className={"mono " + cls(d.openUnrealized)}>{fSign(d.openUnrealized, 1)}</div>
              </div>
              <div>
                <div className="muted">记录总数</div>
                <div className="mono">{d.recordsTotal}</div>
              </div>
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
