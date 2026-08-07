import { SCANNER_LABEL, agoText, fShanghaiDateTime, scannerColor } from "../../lib/format.js";
import { IC, Ico } from "../../lib/icons.jsx";
import { scanStageLabel } from "./ScanMask.jsx";

const PIPELINE_LABELS = ["排行榜", "Perp 确认", "有效画像", "入围名单", "Core"];

const sourceLabel = source => source === "quicknode" ? "QuickNode" : "Hyperliquid";

export function ScanStatusCard({ discovery, scanning, scanStatus, busy, doRescan,
  collectionSource, sourceChanging, sourceError, onSourceChange, onQuickNodeSetup }) {
  const sc = discovery.scanner || { mode: "unknown", detail: {} };
  const detail = sc.detail || {};
  const scMode = sc.mode;
  const activeScan = scMode === "scanning" || scanning;
  const stage = (scanStatus && scanStatus.stage) || detail.stage;
  const scanned = Number((scanStatus && scanStatus.candidatesScanned) ?? detail.scanned ?? 0);
  const total = Number((scanStatus && scanStatus.candidatesTotal) ?? detail.total ?? 0);
  const derivedPct = total > 0 ? Math.round(scanned / total * 100) : 0;
  const progressPct = Math.max(0, Math.min(100,
    Number((scanStatus && scanStatus.progressPct) ?? derivedPct) || 0));
  const startedAt = (scanStatus && scanStatus.startedAt) || detail.startedAt;
  const stale = Boolean(sc.stale && scMode !== "idle");
  const statusColor = scannerColor(scMode, sc.stale);
  const stateLabel = activeScan ? "采集扫描中" : (SCANNER_LABEL[scMode] || scMode || "等待采集");
  const stageLabel = activeScan
    ? (stage === "score_filter" ? "深度画像 / 评分筛选" : scanStageLabel(stage))
    : "等待下一轮完整重评";
  const selectedSource = collectionSource?.selectedSource || "official";
  const effectiveSource = scanStatus?.effectiveSource || detail.effectiveSource
    || collectionSource?.effectiveSource || null;
  const taskSelectedSource = scanStatus?.selectedSource || detail.selectedSource
    || collectionSource?.currentTaskSelectedSource || selectedSource;
  const fallbackReason = scanStatus?.sourceFallbackReason || detail.sourceFallbackReason
    || collectionSource?.fallbackReason;
  const quicknodeReady = collectionSource?.quicknode?.configured
    && Boolean(collectionSource?.quicknode?.verifiedAt);
  const sourceLocked = busy || collectionSource?.switchLocked || sourceChanging;

  return (
    <section className="discovery-glass scan-command" aria-label="实时采集进度">
      <div className="scan-command-state">
        <div className="scan-command-glyph" aria-hidden="true"><Ico d={IC.discovery} /></div>
        <div className="scan-command-copy">
          <span>当前状态</span>
          <strong style={{ color: statusColor }}>{stateLabel}<i className="scan-live-dot" /></strong>
          <small>{activeScan ? "启动时间" : "上次完成"}</small>
          <b className="mono">{activeScan ? fShanghaiDateTime(startedAt) : agoText(discovery.lastScanAt)}</b>
        </div>
      </div>

      <div className="scan-command-stage">
        <span>当前阶段</span>
        <strong>{stageLabel}</strong>
        <small>心跳监测</small>
        <b className={"mono" + (stale ? " scan-heartbeat-stale" : "")}>
          {stale ? "心跳超时" : agoText(sc.heartbeatAt)}
        </b>
      </div>

      <div className="scan-command-progress">
        <div className="scan-source-control">
          <div className="scan-source-copy">
            <span>采集数据源</span>
            <small>首选 <b>{sourceLabel(selectedSource)}</b>{activeScan && <React.Fragment> · 本轮 <b>{sourceLabel(effectiveSource || taskSelectedSource)}</b></React.Fragment>}</small>
          </div>
          <div className="scan-source-segment" role="group" aria-label="选择采集数据源">
            <button type="button" className={selectedSource === "quicknode" ? "on" : ""}
              disabled={sourceLocked || !quicknodeReady} onClick={() => onSourceChange("quicknode")}
              title={!quicknodeReady ? "请先到账户信息保存并验证 QuickNode Endpoint" : "下次采集使用 QuickNode"}>QuickNode</button>
            <button type="button" className={selectedSource === "official" ? "on" : ""}
              disabled={sourceLocked} onClick={() => onSourceChange("official")}>Hyperliquid</button>
          </div>
        </div>
        {!quicknodeReady && selectedSource !== "quicknode" && <div className="scan-source-hint">
          QuickNode 尚未验证 · <button type="button" onClick={onQuickNodeSetup}>前往账户信息配置</button>
        </div>}
        {activeScan && effectiveSource && effectiveSource !== taskSelectedSource && <div className="scan-source-fallback">
          已回退 Hyperliquid{fallbackReason ? ` · ${fallbackReason}` : ""}
        </div>}
        {sourceError && <div className="scan-source-error">{sourceError}</div>}
        <div className="scan-command-progress-head">
          <div><span>实时进度</span><strong className="mono">
            {total > 0 ? <React.Fragment><em>{scanned.toLocaleString("en-US")}</em> / {total.toLocaleString("en-US")} · <em>{progressPct}%</em></React.Fragment> : "等待数据"}
          </strong></div>
          <button className="btn btn-accent scan-command-action" disabled={busy} onClick={doRescan}>
            <Ico d={IC.discovery} /> {busy ? "采集进行中…" : "触发完整候选重评"}
          </button>
        </div>
        <div className="scan-progress-track" role="progressbar" aria-label="采集进度"
          aria-valuemin="0" aria-valuemax="100" aria-valuenow={progressPct}>
          <span style={{ width: progressPct + "%" }} />
        </div>
        <div className="scan-progress-stages">
          {PIPELINE_LABELS.map((label, index) => <span key={label} className={index === 0 && activeScan ? "active" : ""}>{label}</span>)}
        </div>
      </div>
    </section>
  );
}
