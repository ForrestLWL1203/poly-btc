import { fUsd } from "../lib/format.js";

const { useState, useEffect } = React;

export function Confirm({ cfg, onClose }) {
  const [pct, setPct] = useState(100);
  useEffect(() => { if (cfg && cfg.pctPicker) setPct(100); }, [cfg]);
  if (!cfg) return null;
  const pick = cfg.pctPicker;
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <h3>{cfg.title}</h3>
        <p>{cfg.body}</p>
        {pick && <div style={{ margin: "4px 0 2px" }}>
          <div className="close-pop-row">
            {[25, 50, 75, 100].map(v => (
              <button key={v} className={"pct-chip" + (pct === v ? " on" : "")} onClick={() => setPct(v)}>{v}%</button>
            ))}
          </div>
          <p style={{ marginTop: 8 }}>平掉 {pct}% ≈ <b>{fUsd((pick.notional || 0) * pct / 100)}</b> 名义额</p>
        </div>}
        <div className="modal-row">
          <button className="btn" onClick={onClose}>取消</button>
          <button className={"btn " + (cfg.danger ? "btn-danger" : "btn-accent")}
            onClick={() => { cfg.onConfirm(pick ? pct / 100 : undefined); onClose(); }}>
            {pick ? `平仓 ${pct}%` : (cfg.ok || "确认")}</button>
        </div>
      </div>
    </div>
  );
}
