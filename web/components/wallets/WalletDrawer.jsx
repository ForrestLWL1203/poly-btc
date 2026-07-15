import { api } from "../../lib/api.js";
import { agoText, cls, fNum, fSign, short } from "../../lib/format.js";
import { IC, Ico } from "../../lib/icons.jsx";
import { useApiResource } from "../../lib/refresh.js";
import { PositionDetail } from "../positions/PositionDetail.jsx";

const { useState, useEffect, useCallback } = React;

const STATUS_LABEL = { open: "在持", closed: "已平", gap_closed: "缺口平", liquidated: "爆仓", tail_closed: "尾盈平" };

const marketLabel = (m) => ({ crypto: "加密", stock: "美股/指数", mixed: "混合" }[m] || m || "—");

const copyWindowRows = (breakdown) => {
  const pnl = breakdown.copyPnl || {};
  const open = breakdown.copyUnrealizedPnl || {};
  const closed = breakdown.closedN || {};
  return [
    ["30 天", pnl["30d"], closed["30d"], open["30d"]],
    ["14 天", pnl["14d"], closed["14d"], open["14d"]],
    ["7 天", pnl["7d"], closed["7d"], open["7d"]],
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
              <div><span>30日回放</span><b className={copy30 ? cls(copy30[1]) : ""}>{copy30 ? fSign(copy30[1] || 0, 0) : "—"}</b><em>{copy30 ? (copy30[2] || 0) + " 笔" : "暂无数据"}</em></div>
            </div>

            <div className="wallet-decision-grid">
              <DecisionCard title="当前参数回放" tone={copyRows.length ? "good" : "muted"}>
                {copyRows.length ? (
                  <div className="score-window-grid">
                    {copyRows.map(([label, pnl, n, openPnl]) => (
                      <div className="score-window" key={label}>
                        <span>{label}</span>
                        <b className={(pnl || 0) >= 0 ? "up" : "down"}>{fSign(pnl || 0, 0)}</b>
                        <small>{n || 0} 笔{openPnl != null && Math.abs(openPnl) >= 0.5 ? ` · ${openPnl < 0 ? "持仓亏损" : "持仓盈利"} ${fSign(openPnl, 0)}` : ""}</small>
                      </div>
                    ))}
                  </div>
                ) : <p>暂无可用 copy 回测窗口，先按历史评分和实盘记录观察。</p>}
              </DecisionCard>

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
