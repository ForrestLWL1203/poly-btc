import { SCANNER_LABEL, agoText, scannerColor } from "../../lib/format.js";
import { IC, Ico } from "../../lib/icons.jsx";

export function ScanControls({ busy, doRescan }) {
  return (
    <div className="section-h" style={{ marginTop: 6 }}><h2>采集进程 · 实时</h2>
      <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
        <button className="btn btn-accent" disabled={busy} onClick={doRescan}><Ico d={IC.discovery} /> {busy ? "采集进行中…" : "触发完整候选重评"}</button>
      </div></div>
  );
}

export function ScanStatusCard({ discovery, scanning }) {
  const fn = discovery.funnel;
  const sc = discovery.scanner || { mode: "unknown" };
  const scMode = sc.mode, scColor = scannerColor(scMode, sc.stale);
  const lastScanH = discovery.lastScanAt ? (Date.now() - new Date(discovery.lastScanAt).getTime()) / 3.6e6 : 1e9;
  const overdue = lastScanH > 26;
  return (
    <div className="card">
      <div style={{ display: "flex", alignItems: "center", gap: 24, flexWrap: "wrap" }}>
        <span className="pill" style={{ background: "rgba(255,255,255,.05)", color: scColor }}>
          <span className="dot" style={{ background: scColor, animation: (scMode === "rolling" || scMode === "scanning" || scanning) ? "pulse 1.4s infinite" : "none" }} />
          {SCANNER_LABEL[scMode] || scMode}{sc.stale && scMode !== "idle" ? " · 心跳超时 ⚠" : ""}</span>
        <div><div className="muted">上次扫描</div><div className="mono" style={{ fontSize: 15, color: overdue ? "var(--red-l)" : undefined }}>{agoText(discovery.lastScanAt)}{overdue ? " ⚠超期" : ""}</div></div>
        <div><div className="muted">采集周期</div><div className="mono" style={{ fontSize: 15 }}>周一/周四完整重评 · 历史增量</div></div>
        <div><div className="muted">被跟名单</div><div className="mono" style={{ fontSize: 15 }}>{fn.watchlist} 钱包</div></div>
        <div><div className="muted">心跳</div><div className="mono" style={{ fontSize: 15, color: (sc.stale && scMode !== "idle") ? "var(--red-l)" : "var(--green-l)" }}>{agoText(sc.heartbeatAt)}</div></div>
      </div>
    </div>
  );
}
