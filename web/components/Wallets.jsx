import { api } from "../lib/api.js";
import { agoText, cls, fNum, fSign, fTime, short } from "../lib/format.js";
import { IC, Ico } from "../lib/icons.jsx";
import { useApiResource } from "../lib/refresh.js";
import { PositionDetail } from "./Positions.jsx";

const { useState, useEffect, useCallback } = React;

const STATUS_LABEL = { open: "在持", closed: "已平", gap_closed: "缺口平", liquidated: "爆仓" };

export function Wallets({ confirm, toast }) {
  const [drawer, setDrawer] = useState(null);
  const [auditOpen, setAuditOpen] = useState({});
  const [audits, setAudits] = useState({});
  const [wpage, setWpage] = useState(0);
  const [tab, setTab] = useState("followed");
  const load = useCallback(() => api.get("/api/wallets?tab=" + tab + "&size=500"), [tab]);
  const { data, reload } = useApiResource(load, { intervalMs: 12000, clearOnLoadChange: true });
  useEffect(() => { setAuditOpen({}); setAudits({}); }, [tab]);
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
    if (b.copyScore != null) lines.push(`copy分 ${fNum(b.copyScore, 1)} · 置信 ${fNum(b.confidencePct, 0)}%`);
    (b.reasons || []).slice(0, 4).forEach(r => lines.push("· " + r));
    return lines.join("\n");
  };

  const auditStage = (s) => ({ profile: "画像", watchlist: "名单", follow_line: "跟单线", auto_tune: "调参" }[s] || s || "—");
  const auditCopyText = (payload) => {
    const c = payload && payload.copyBt;
    if (!c) return null;
    const v30 = c["30dNetPnl"], v14 = c["14dNetPnl"], v7 = c["7dNetPnl"];
    if (v30 == null && v14 == null && v7 == null) return null;
    return `copy 30d ${fSign(v30 || 0, 0)} / 14d ${fSign(v14 || 0, 0)} / 7d ${fSign(v7 || 0, 0)}`;
  };
  const loadAudit = (addr) => {
    const key = (addr || "").toLowerCase();
    setAuditOpen(s => ({ ...s, [key]: !s[key] }));
    if (!audits[key]) {
      setAudits(s => ({ ...s, [key]: { loading: true, events: [] } }));
      api.get("/api/pipeline-audit?addr=" + encodeURIComponent(key) + "&limit=8&compact=1")
        .then(res => setAudits(s => ({ ...s, [key]: { loading: false, events: res.events || [] } })))
        .catch(() => setAudits(s => ({ ...s, [key]: { loading: false, error: true, events: [] } })));
    }
  };
  const auditBox = (addr) => {
    const key = (addr || "").toLowerCase();
    const a = audits[key] || { loading: true, events: [] };
    if (a.loading) return <div className="audit-box muted">加载审计记录…</div>;
    if (a.error) return <div className="audit-box down">审计记录读取失败</div>;
    if (!a.events.length) return <div className="audit-box muted">暂无该钱包的审计记录</div>;
    return (
      <div className="audit-box">
        {a.events.map(e => {
          const copy = auditCopyText(e.payload);
          return (
            <div className="audit-event" key={e.id}>
              <div>
                <span className={"tint " + (e.status === "active" || e.status === "followed" ? "tint-green" : e.status === "below_line" ? "tint-amber" : "tint-red")}>{auditStage(e.stage)}</span>
                <span className="muted" style={{ marginLeft: 8 }}>{e.status || "—"} · {e.reason || "—"}</span>
              </div>
              <div className="audit-meta">
                <span>{e.stamp ? e.stamp.slice(5, 16).replace("T", " ") : "—"}</span>
                {e.rawScore != null && <span>raw {fNum(e.rawScore, 1)}</span>}
                {e.followScore != null && <span>follow {fNum(e.followScore, 1)}</span>}
                {copy && <span>{copy}</span>}
              </div>
            </div>
          );
        })}
      </div>
    );
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
                    <tr className={open ? "row-open" : ""} style={{ cursor: "pointer" }} onClick={() => loadAudit(w.address)}>
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

function WalletDrawer({ address, onClose }) {
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
