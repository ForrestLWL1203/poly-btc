import { fNum, fSign, short } from "../../lib/format.js";
import { InfoIcon } from "../../lib/icons.jsx";

const { useEffect } = React;

const SECTOR_LABEL = { crypto: "加密", stock: "美股/指数" };

const copyWindowRows = (breakdown) => {
  const pnl = breakdown.copyPnl || {};
  const closed = breakdown.closedN || {};
  return [
    ["30 天", pnl["30d"], closed["30d"]],
    ["14 天", pnl["14d"], closed["14d"]],
    ["7 天", pnl["7d"], closed["7d"]],
  ].filter((row) => Number(row[2] || 0) > 0 || Math.abs(Number(row[1] || 0)) > 0);
};

const sectorRows = (policy) => {
  if (!policy) return [];
  return ["crypto", "stock"].map((key) => {
    const item = policy[key] || {};
    if (item.allow == null && !item.status && !item.reason) return null;
    const pnl = item.pnl || {};
    const closed = item.closed || {};
    return {
      key,
      label: SECTOR_LABEL[key] || key,
      allow: !!item.allow,
      status: item.reason || item.status || "无策略",
      pnl14: pnl["14"],
      closed14: closed["14"],
    };
  }).filter(Boolean);
};

export function WalletScoreCell({ wallet, color, onOpen }) {
  return (
    <span className="score-cell">
      <b style={{ color }}>{fNum(wallet.score, 1)}</b>
      <button
        type="button"
        className="score-info-btn"
        aria-label={"查看 " + short(wallet.address) + " 的评分细节"}
        onClick={(e) => { e.stopPropagation(); onOpen(wallet); }}
      >
        <InfoIcon />
      </button>
    </span>
  );
}

export function WalletScoreDetailModal({ wallet, onClose }) {
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!wallet) return null;
  const breakdown = wallet.scoreBreakdown || {};
  const windows = copyWindowRows(breakdown);
  const sectors = sectorRows(breakdown.sectorPolicy || wallet.sectorPolicy);
  const reasons = (breakdown.reasons || []).slice(0, 6);

  return (
    <div className="score-detail-overlay" onClick={onClose}>
      <div className="score-detail-modal" role="dialog" aria-modal="true" aria-label="评分细节" onClick={(e) => e.stopPropagation()}>
        <div className="score-detail-head">
          <div>
            <div className="score-detail-kicker">评分细节</div>
            <h3>{short(wallet.address)}</h3>
          </div>
          <button className="score-detail-close" type="button" onClick={onClose} aria-label="关闭评分细节">×</button>
        </div>

        <div className="score-hero">
          <div>
            <span>最终跟单分</span>
            <b>{fNum(wallet.score, 1)}</b>
          </div>
          <div>
            <span>原始评分</span>
            <b>{fNum(wallet.rawScore ?? breakdown.rawScore, 1)}</b>
          </div>
          <div>
            <span>copy 分</span>
            <b>{breakdown.copyScore != null ? fNum(breakdown.copyScore, 1) : "—"}</b>
          </div>
          <div>
            <span>置信</span>
            <b>{breakdown.confidencePct != null ? fNum(breakdown.confidencePct, 0) + "%" : "—"}</b>
          </div>
        </div>

        {windows.length > 0 && (
          <div className="score-section">
            <div className="score-section-title">copy 回测</div>
            <div className="score-window-grid">
              {windows.map(([label, pnl, n]) => (
                <div className="score-window" key={label}>
                  <span>{label}</span>
                  <b className={(pnl || 0) >= 0 ? "up" : "down"}>{fSign(pnl || 0, 0)}</b>
                  <small>{n || 0} 笔</small>
                </div>
              ))}
            </div>
          </div>
        )}

        {sectors.length > 0 && (
          <div className="score-section">
            <div className="score-section-title">跟单板块</div>
            <div className="score-sector-list">
              {sectors.map((s) => (
                <div className={"score-sector " + (s.allow ? "allow" : "deny")} key={s.key}>
                  <div>
                    <b>{s.label}</b>
                    <span>{s.status}</span>
                  </div>
                  <small>{s.pnl14 != null || s.closed14 != null ? "14天 " + fSign(s.pnl14 || 0, 0) + " / " + (s.closed14 || 0) + " 笔" : "样本不足"}</small>
                </div>
              ))}
            </div>
          </div>
        )}

        {reasons.length > 0 && (
          <div className="score-section">
            <div className="score-section-title">分数依据</div>
            <div className="score-reasons">
              {reasons.map((r, i) => <span key={i}>{r}</span>)}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
