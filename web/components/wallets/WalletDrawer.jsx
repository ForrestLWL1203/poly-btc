import { api } from "../../lib/api.js";
import { agoText, cls, fNum, fSign, short } from "../../lib/format.js";
import { IC, Ico } from "../../lib/icons.jsx";
import { useApiResource } from "../../lib/refresh.js";
import { PositionDetail } from "../positions/PositionDetail.jsx";

const { useState, useEffect, useCallback } = React;

const STATUS_LABEL = { open: "在持", closed: "已平", gap_closed: "缺口平", liquidated: "爆仓" };

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
  return (
    <React.Fragment>
      <div className="scrim" onClick={onClose} />
      <div className="drawer">
        <div className="drawer-head">
          <div>
            <h3>{short(address)}</h3>
            <div className="muted">排名 #{d ? (d.rank != null ? d.rank : "—") : "—"} · {d ? d.marketType : ""}</div>
          </div>
          <button className="drawer-close" type="button" onClick={onClose} aria-label="关闭跟单记录" title="关闭">
            <Ico d={IC.close} />
          </button>
        </div>
        {!d ? <div className="loading">加载中…</div> : (
          <React.Fragment>
            <div className="card" style={{ marginBottom: 14 }}>
              <div className="card-lbl" style={{ marginBottom: 10 }}>历史评分 vs 实盘对账 {losing && <span style={{ color: "var(--red-l)" }}>· ⚠ 实盘亏损</span>}</div>
              <div style={{ display: "flex", gap: 22, flexWrap: "wrap" }}>
                <div><div className="muted">评分</div><div className="mono" style={{ fontSize: 18 }}>{fNum(d.score, 1)}</div></div>
                <div><div className="muted">历史胜率</div><div className="mono" style={{ fontSize: 18 }}>{d.scoredWinRatePct != null ? fNum(d.scoredWinRatePct, 0) + "%" : "—"}<span className="muted" style={{ fontSize: 11 }}> /{d.scoredTrades || 0}笔</span></div></div>
                <div><div className="muted">实盘胜率</div><div className="mono" style={{ fontSize: 18, color: "var(--t1)" }}>{d.forwardWinRatePct != null ? fNum(d.forwardWinRatePct, 0) + "%" : "—"}<span className="muted" style={{ fontSize: 11 }}> /{d.closedN}笔</span></div></div>
              </div>
              <div style={{ display: "flex", gap: 22, flexWrap: "wrap", marginTop: 14, borderTop: "1px solid var(--glass-border)", paddingTop: 12 }}>
                <div><div className="muted">实盘战绩</div><div className="mono" style={{ fontSize: 15 }}><span className="up">{d.winN}胜</span> / <span className="down">{d.lossN}负</span></div></div>
                <div><div className="muted">已实现</div><div className={"mono " + cls(d.realizedPnl)} style={{ fontSize: 15 }}>{fSign(d.realizedPnl, 1)}</div></div>
                <div><div className="muted">在持({d.openN})浮动</div><div className={"mono " + cls(d.openUnrealized)} style={{ fontSize: 15 }}>{fSign(d.openUnrealized, 1)}</div></div>
                <div><div className="muted">净盈亏</div><div className={"mono " + cls(d.netPnl)} style={{ fontSize: 16, fontWeight: 700 }}>{fSign(d.netPnl, 1)}</div></div>
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
