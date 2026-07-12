import { SCANNER_LABEL, agoText, scannerColor } from "../../lib/format.js";
import { IC, Ico } from "../../lib/icons.jsx";

export function ScanControls({ fullScan, setFullScan, busy, doRescan }) {
  return (
    <div className="section-h" style={{ marginTop: 6 }}><h2>采集进程 · 实时</h2>
      <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
        <label title="勾选=全量(重拉排行榜+重采所有候选,改过评分后必跑);默认=增量(仅活跃+新,快)"
          style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, cursor: busy ? "default" : "pointer", opacity: busy ? .5 : 1 }}>
          <input type="checkbox" checked={fullScan} disabled={busy} onChange={e => setFullScan(e.target.checked)} />
          全量采集
        </label>
        <button className="btn btn-accent" disabled={busy} onClick={doRescan}><Ico d={IC.discovery} /> {busy ? "采集进行中…" : (fullScan ? "触发全量采集" : "触发增量采集")}</button>
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
        <div><div className="muted">采集周期</div><div className="mono" style={{ fontSize: 15 }}>每日增量 · 每周全量</div></div>
        <div><div className="muted">被跟名单</div><div className="mono" style={{ fontSize: 15 }}>{fn.watchlist} 钱包</div></div>
        <div><div className="muted">心跳</div><div className="mono" style={{ fontSize: 15, color: (sc.stale && scMode !== "idle") ? "var(--red-l)" : "var(--green-l)" }}>{agoText(sc.heartbeatAt)}</div></div>
      </div>
    </div>
  );
}
