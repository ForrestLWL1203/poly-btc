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
  const gen = discovery.generation, perf = (gen && gen.performance) || {};
  const first = (...keys) => keys.map(k => perf[k]).find(v => v != null);
  const cacheHit = first("fillCacheHitRate", "fill_cache_hit_rate", "replayCacheHitRate", "replay_cache_hit_rate");
  const observerP95 = first("observerPollP95Ms", "observer_poll_p95_ms", "observerP95OpenLatencyMs", "observer_p95_open_latency_ms");
  const restWeight = first("restWeight", "rest_weight", "estimated_weight", "apiWeight", "api_weight");
  const requests = first("requests", "requestCount", "request_count");
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
        {!rolling && <div><div className="muted">采集周期</div><div className="mono" style={{ fontSize: 15 }}>每日增量 · 每周全量</div></div>}
        <div><div className="muted">被跟名单</div><div className="mono" style={{ fontSize: 15 }}>{fn.watchlist} 钱包</div></div>
        <div><div className="muted">心跳</div><div className="mono" style={{ fontSize: 15, color: (sc.stale && scMode !== "idle") ? "var(--red-l)" : "var(--green-l)" }}>{agoText(sc.heartbeatAt)}</div></div>
      </div>
      {rolling && <div className="bar-track" style={{ marginTop: 14, height: 6 }}>
        <div className="bar-fill" style={{ width: cyclePct + "%", background: "var(--accent-grad)" }} /></div>}
      {gen && <div style={{ display: "flex", alignItems: "center", gap: 24, flexWrap: "wrap", marginTop: 14, paddingTop: 14, borderTop: "1px solid var(--line)" }}>
        <div><div className="muted">已发布 Generation</div><div className="mono" style={{ fontSize: 13, color: "var(--green-l)" }}>{gen.generation}</div></div>
        <div><div className="muted">工作集 / Fills</div><div className="mono" style={{ fontSize: 13 }}>{gen.worksetMode || "—"} · {gen.fillMode || "—"}</div></div>
        <div><div className="muted">画像 / 延期</div><div className="mono" style={{ fontSize: 13 }}>{gen.profiled ?? "—"} / {gen.deferred ?? 0}</div></div>
        <div><div className="muted">Leaderboard / Profile</div><div className="mono" style={{ fontSize: 13, color: gen.leaderboardValid && gen.profileComplete ? "var(--green-l)" : "var(--red-l)" }}>{gen.leaderboardValid ? "valid" : "invalid"} · {gen.profileComplete ? "complete" : "incomplete"}</div></div>
        {restWeight != null && <div><div className="muted">REST权重</div><div className="mono" style={{ fontSize: 13 }}>{restWeight}</div></div>}
        {requests != null && <div><div className="muted">网络请求</div><div className="mono" style={{ fontSize: 13 }}>{requests}</div></div>}
        {cacheHit != null && <div><div className="muted">缓存命中</div><div className="mono" style={{ fontSize: 13 }}>{Math.round(cacheHit * (cacheHit <= 1 ? 100 : 1))}%</div></div>}
        {observerP95 != null && <div><div className="muted">Observer P95</div><div className="mono" style={{ fontSize: 13 }}>{Math.round(observerP95)}ms</div></div>}
      </div>}
    </div>
  );
}
