import { api } from "../lib/api.js";
import { SCANNER_LABEL, agoText, fNum, fSign, scannerColor, short } from "../lib/format.js";
import { IC, Ico } from "../lib/icons.jsx";
import { useApiResource } from "../lib/refresh.js";

const { useState, useEffect, useCallback, useRef } = React;

const STAGES_FE = [["scan_leaderboard", "扫描排行榜"], ["fetch_history", "拉取历史 & 算指标"],
  ["score_filter", "评分 · 网格/扛单过滤"], ["rebuild_watchlist", "重建被跟名单"],
  ["auto_tune", "组合回测调参"], ["persist", "写库 & 校验"]];

export function ScanMask({ status }) {
  const stage = status && status.stage;
  const curIdx = STAGES_FE.findIndex(s => s[0] === stage);
  const pct = (status && status.progressPct) || 0;
  const el = (status && status.elapsedSec) || 0;
  const mm = String(Math.floor(el / 60)).padStart(2, "0"), ss = String(el % 60).padStart(2, "0");
  const remain = pct > 3 ? Math.round(el * (100 - pct) / pct) : null;
  const eta = remain != null
    ? `预计还需 ~${String(Math.floor(remain / 60)).padStart(2, "0")}:${String(remain % 60).padStart(2, "0")}`
    : "预计剩余计算中…";
  return (
    <div className="mask">
      <div className="radar" />
      <h2>采集进行中…</h2>
      <div className="sub">{mm}:{ss} 已用 · {eta}</div>
      <div className="mask-prog"><div className="pf" style={{ width: pct + "%" }} /></div>
      <div className="mask-meta">
        <span>{pct}%</span>
        <span>已扫描 {(status && status.candidatesScanned) || 0} / {(status && status.candidatesTotal) || "—"}</span>
      </div>
      <div className="stage-list">
        {STAGES_FE.map(([k, label], i) => {
          const st = curIdx < 0 ? "" : i < curIdx ? "done" : i === curIdx ? "active" : "";
          return <div key={k} className={"stage-item " + st}>
            <span className="stage-dot">{st === "done" ? "✓" : st === "active" ? "●" : ""}</span>{label}</div>;
        })}
      </div>
      <div className="mask-lock">⚠ 页面已锁定 · 重采期间禁止操作</div>
    </div>
  );
}

function PipelineSummary({ p }) {
  if (!p || !p.stamp) return null;
  const prof = p.profile || {}, wl = p.watchlist || {}, fl = p.followLine || {}, tune = p.autoTune || {};
  const selected = fl.selected || {};
  const win = selected.windows || {};
  const pnl14 = win["14"] && win["14"].copy_net_pnl;
  const pnl7 = win["7"] && win["7"].copy_net_pnl;
  const tuneChanged = tune.appliedSizing || tune.appliedAdd;
  const reasonRows = (prof.reasonCounts || []).slice(0, 5);
  const reasonLabel = (r) => ({
    copy_backtest_loss: "copy 回测亏损",
    edge_decayed: "边际衰减",
    thin_edge: "优势太薄",
    low_quality: "评分不足",
    too_many_concurrent: "并发过高",
    grid_dca: "网格/DCA过重",
    inactive: "不活跃",
  }[r] || r || "—");
  return (
    <div className="pipeline-card card">
      <div className="pipeline-head">
        <div>
          <div className="card-lbl">最近一轮流水线审计</div>
          <div className="pipeline-stamp">{p.stamp.replace("T", " ").replace("Z", "")} · {p.source || "scan"}</div>
        </div>
        <span className={"tint " + (fl.status === "ok" ? "tint-green" : "tint-amber")}>{fl.reason || fl.status || "无跟单线记录"}</span>
      </div>
      <div className="pipeline-grid">
        <div className="pipe-metric">
          <span>画像结果</span>
          <b>{prof.active || 0}<em> / {prof.total || 0}</em></b>
          <small>{prof.retired || 0} 退役 · {prof.rejected || 0} 拒绝</small>
        </div>
        <div className="pipe-metric">
          <span>跟单线</span>
          <b>{fl.score != null ? fNum(fl.score, 1) : "—"}<em> 分</em></b>
          <small>{fl.count != null ? `选择 ${fl.count} 个` : "未自动选择"}{fl.targetN ? ` · 目标 ${fl.targetN}` : ""}</small>
        </div>
        <div className="pipe-metric">
          <span>最终名单</span>
          <b>{wl.followed || 0}<em> 个</em></b>
          <small>{wl.belowLine || 0} 在线下 · {wl.disabled || 0} 停用</small>
        </div>
        <div className="pipe-metric">
          <span>自动调参</span>
          <b className={tuneChanged ? "up" : ""}>{tuneChanged ? "已调整" : (tune.status || "—")}</b>
          <small>{tune.followedN != null ? `${tune.followedN} 钱包参与` : "无调参记录"}{tune.selectedMult ? ` · ${fNum(tune.selectedMult, 2)}x` : ""}</small>
        </div>
      </div>
      <div className="pipeline-detail">
        <div>
          <div className="muted">自动选线依据</div>
          <div className="pipeline-line">
            {selected.n ? <React.Fragment>top {selected.n} · 总分 {fSign(selected.score || 0, 0)} · 14d {fSign(pnl14 || 0, 0)} · 7d {fSign(pnl7 || 0, 0)}</React.Fragment>
              : (fl.reason || "暂无 top-N 组合回测摘要")}
          </div>
        </div>
        <div>
          <div className="muted">主要出局原因</div>
          <div className="pipe-reasons">
            {reasonRows.length === 0 && <span className="muted">暂无退役/拒绝记录</span>}
            {reasonRows.map((r, i) => (
              <span key={i} className="pipe-reason"><b>{r.count}</b>{reasonLabel(r.reason)}</span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export function Discovery({ scanning, startRescan, confirm }) {
  const [fullScan, setFullScan] = useState(false);
  const load = useCallback(async () => {
    const [discovery, scanRuns, pipeline] = await Promise.all([
      api.get("/api/discovery"),
      api.get("/api/scan-runs?limit=8"),
      api.get("/api/pipeline-summary"),
    ]);
    return { discovery, runs: scanRuns.runs, pipeline };
  }, []);
  const { data, reload } = useApiResource(load, { intervalMs: 4000 });
  const wasScanning = useRef(scanning);
  useEffect(() => {
    if (wasScanning.current && !scanning) reload();
    wasScanning.current = scanning;
  }, [scanning, reload]);
  const d = data && data.discovery;
  const runs = data && data.runs;
  const pipeline = data && data.pipeline;

  const doRescan = () => confirm({
    title: fullScan ? "触发全量采集" : "触发增量采集",
    danger: fullScan, ok: fullScan ? "开始全量" : "开始增量",
    body: fullScan
      ? "全量:重拉排行榜 + 重采所有候选,让每个 profile 都到最新评分标准(改过评分逻辑后必须跑一次)。无跟单时全速约 30–90 分钟,有跟单则自动慢采让速。期间按钮锁定。确认?"
      : "增量:只重采活跃+新候选(快,几分钟),旧的 rejected 长尾不动。日常刷新用这个。确认?",
    onConfirm: () => startRescan(fullScan),
  });

  if (!d) return <div className="content"><div className="loading">加载中…</div></div>;
  const fn = d.funnel, h = d.scoreHistogram, maxBin = Math.max(...h.bins, 1);
  const sc = d.scanner || { mode: "unknown", detail: {} }, det = sc.detail || {};
  const scMode = sc.mode, scColor = scannerColor(scMode, sc.stale);
  const rolling = det.cycle_total != null;
  const cyclePct = det.cycle_total ? Math.round(det.cycle_pos / det.cycle_total * 100) : 0;
  const busy = scMode === "scanning" || scanning;
  const lastScanH = d.lastScanAt ? (Date.now() - new Date(d.lastScanAt).getTime()) / 3.6e6 : 1e9;
  const overdue = lastScanH > 26;
  return (
    <div className="content">
      <div className="section-h" style={{ marginTop: 6 }}><h2>采集进程 · 实时</h2>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <label title="勾选=全量(重拉排行榜+重采所有候选,改过评分后必跑);默认=增量(仅活跃+新,快)"
            style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, cursor: busy ? "default" : "pointer", opacity: busy ? .5 : 1 }}>
            <input type="checkbox" checked={fullScan} disabled={busy} onChange={e => setFullScan(e.target.checked)} />
            全量采集
          </label>
          <button className="btn btn-accent" disabled={busy} onClick={doRescan}><Ico d={IC.discovery} /> {busy ? "采集进行中…" : (fullScan ? "触发全量采集" : "触发增量采集")}</button>
        </div></div>
      <div className="card">
        <div style={{ display: "flex", alignItems: "center", gap: 24, flexWrap: "wrap" }}>
          <span className="pill" style={{ background: "rgba(255,255,255,.05)", color: scColor }}>
            <span className="dot" style={{ background: scColor, animation: (scMode === "rolling" || scMode === "scanning") ? "pulse 1.4s infinite" : "none" }} />
            {SCANNER_LABEL[scMode] || scMode}{sc.stale && scMode !== "idle" ? " · 心跳超时 ⚠" : ""}</span>
          {rolling && <div><div className="muted">本轮进度</div><div className="mono" style={{ fontSize: 15 }}>{det.cycle_pos} / {det.cycle_total} <span className="muted">({cyclePct}%)</span></div></div>}
          {rolling && <div><div className="muted">采集节奏</div><div className="mono" style={{ fontSize: 15 }}>每 ~{det.interval_s ?? "—"}s / 个</div></div>}
          {rolling && <div><div className="muted">最近更新</div><div className="mono" style={{ fontSize: 15 }}>{short(det.last_addr)} · {agoText(det.last_at)}</div></div>}
          <div><div className="muted">上次扫描</div><div className="mono" style={{ fontSize: 15, color: overdue ? "var(--red-l)" : undefined }}>{agoText(d.lastScanAt)}{overdue ? " ⚠超期" : ""}</div></div>
          {!rolling && <div><div className="muted">采集周期</div><div className="mono" style={{ fontSize: 15 }}>每 24h 自动</div></div>}
          <div><div className="muted">被跟名单</div><div className="mono" style={{ fontSize: 15 }}>{fn.watchlist} 钱包</div></div>
          <div><div className="muted">心跳</div><div className="mono" style={{ fontSize: 15, color: (sc.stale && scMode !== "idle") ? "var(--red-l)" : "var(--green-l)" }}>{agoText(sc.heartbeatAt)}</div></div>
        </div>
        {rolling && <div className="bar-track" style={{ marginTop: 14, height: 6 }}>
          <div className="bar-fill" style={{ width: cyclePct + "%", background: "var(--accent-grad)" }} /></div>}
      </div>

      <div className="section-h"><h2>筛选漏斗</h2></div>
      <div className="card">
        <div className="funnel">
          <div className="funnel-stage"><div className="fn">{fn.candidates}</div><div className="fl">候选 candidates</div></div>
          <div className="funnel-arrow">→</div>
          <div className="funnel-stage"><div className="fn" style={{ color: "var(--blue-l)" }}>{fn.active}</div><div className="fl">active</div></div>
          <div className="funnel-arrow">→</div>
          <div className="funnel-stage"><div className="fn" style={{ color: "var(--green-l)" }}>{fn.watchlist}</div><div className="fl">跟单线以上 watchlist</div></div>
        </div>
      </div>

      <PipelineSummary p={pipeline} />

      <div className="grid2" style={{ marginTop: 14 }}>
        <div className="card">
          <div className="card-lbl">拒绝原因占比</div>
          <div style={{ marginTop: 12 }}>
            {d.rejectReasons.map((r, i) => (
              <div className="bar-row" key={i}><div className="bl" style={{ width: 120 }}>{r.label}</div>
                <div className="bar-track"><div className="bar-fill" style={{ width: r.pct + "%", background: "var(--accent-grad)" }} /></div>
                <div className="bv">{r.pct}%</div></div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-lbl">评分分布(标出跟单线)</div>
          <div className="histo">
            {h.bins.map((b, i) => (
              <div key={i} className={"hb" + (i < h.followLineBinIndex ? " below" : "")} style={{ height: (b / maxBin * 100) + "%" }} />
            ))}
            <div className="histo-line" style={{ left: (h.followLineBinIndex / h.bins.length * 100) + "%" }}>
              <span className="lbl">跟单线</span></div>
          </div>
        </div>
      </div>

      <div className="section-h"><h2>扫描历史</h2></div>
      <div className="tbl-wrap">
        <table>
          <thead><tr><th>时间</th><th className="num">候选</th><th className="num">新增</th><th className="num">退役</th><th className="num">拒绝</th><th className="num">在持名单</th></tr></thead>
          <tbody>
            {runs === null && <tr><td colSpan="6" className="loading">加载中…</td></tr>}
            {runs && runs.map((r, i) => (
              <tr key={i}><td className="addr">{r.at ? r.at.replace("T", " ").replace("Z", "") : "—"}</td>
                <td className="num">{r.candidates}</td><td className="num up">+{r.added}</td>
                <td className="num">{r.retired}</td><td className="num">{r.rejected}</td><td className="num">{r.active}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
