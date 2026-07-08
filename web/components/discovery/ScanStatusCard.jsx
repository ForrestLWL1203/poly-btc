import { SCANNER_LABEL, agoText, scannerColor, short } from "../../lib/format.js";
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
  const sc = discovery.scanner || { mode: "unknown", detail: {} }, det = sc.detail || {};
  const scMode = sc.mode, scColor = scannerColor(scMode, sc.stale);
  const rolling = det.cycle_total != null;
  const cyclePct = det.cycle_total ? Math.round(det.cycle_pos / det.cycle_total * 100) : 0;
  const lastScanH = discovery.lastScanAt ? (Date.now() - new Date(discovery.lastScanAt).getTime()) / 3.6e6 : 1e9;
  const overdue = lastScanH > 26;
  return (
    <div className="card">
      <div style={{ display: "flex", alignItems: "center", gap: 24, flexWrap: "wrap" }}>
        <span className="pill" style={{ background: "rgba(255,255,255,.05)", color: scColor }}>
          <span className="dot" style={{ background: scColor, animation: (scMode === "rolling" || scMode === "scanning" || scanning) ? "pulse 1.4s infinite" : "none" }} />
          {SCANNER_LABEL[scMode] || scMode}{sc.stale && scMode !== "idle" ? " · 心跳超时 ⚠" : ""}</span>
        {rolling && <div><div className="muted">本轮进度</div><div className="mono" style={{ fontSize: 15 }}>{det.cycle_pos} / {det.cycle_total} <span className="muted">({cyclePct}%)</span></div></div>}
        {rolling && <div><div className="muted">采集节奏</div><div className="mono" style={{ fontSize: 15 }}>每 ~{det.interval_s ?? "—"}s / 个</div></div>}
        {rolling && <div><div className="muted">最近更新</div><div className="mono" style={{ fontSize: 15 }}>{short(det.last_addr)} · {agoText(det.last_at)}</div></div>}
        <div><div className="muted">上次扫描</div><div className="mono" style={{ fontSize: 15, color: overdue ? "var(--red-l)" : undefined }}>{agoText(discovery.lastScanAt)}{overdue ? " ⚠超期" : ""}</div></div>
        {!rolling && <div><div className="muted">采集周期</div><div className="mono" style={{ fontSize: 15 }}>每 24h 自动</div></div>}
        <div><div className="muted">被跟名单</div><div className="mono" style={{ fontSize: 15 }}>{fn.watchlist} 钱包</div></div>
        <div><div className="muted">心跳</div><div className="mono" style={{ fontSize: 15, color: (sc.stale && scMode !== "idle") ? "var(--red-l)" : "var(--green-l)" }}>{agoText(sc.heartbeatAt)}</div></div>
      </div>
      {rolling && <div className="bar-track" style={{ marginTop: 14, height: 6 }}>
        <div className="bar-fill" style={{ width: cyclePct + "%", background: "var(--accent-grad)" }} /></div>}
    </div>
  );
}
