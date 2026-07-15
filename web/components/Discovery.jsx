import { api } from "../lib/api.js";
import { useApiResource } from "../lib/refresh.js";
import { DiscoveryFunnel } from "./discovery/DiscoveryFunnel.jsx";
import { ScanControls, ScanStatusCard } from "./discovery/ScanStatusCard.jsx";
import { ScanHistoryTable } from "./discovery/ScanHistoryTable.jsx";

export { ScanMask } from "./discovery/ScanMask.jsx";
export { scanStageLabel } from "./discovery/ScanMask.jsx";

const { useState, useEffect, useCallback, useRef } = React;

export function Discovery({ scanning, startRescan, confirm }) {
  const [fullScan, setFullScan] = useState(false);
  const load = useCallback(async () => {
    const [discovery, scanRuns] = await Promise.all([
      api.get("/api/discovery"),
      api.get("/api/scan-runs?limit=8"),
    ]);
    return { discovery, runs: scanRuns.runs };
  }, []);
  const { data, reload } = useApiResource(load, { intervalMs: 4000 });
  const wasScanning = useRef(scanning);
  useEffect(() => {
    if (wasScanning.current && !scanning) reload();
    wasScanning.current = scanning;
  }, [scanning, reload]);
  const d = data && data.discovery;
  const runs = data && data.runs;

  const doRescan = () => confirm({
    title: fullScan ? "触发全量采集" : "触发增量采集",
    danger: fullScan, ok: fullScan ? "开始全量" : "开始增量",
    body: fullScan
      ? "全量:重拉排行榜 + 重采所有候选,让每个 profile 都到最新评分标准(改过评分逻辑后必须跑一次)。无跟单时全速约 30–90 分钟,有跟单则自动慢采让速。期间按钮锁定。确认?"
      : "增量:只重采活跃+新候选(快,几分钟),旧的 rejected 长尾不动。日常刷新用这个。确认?",
    onConfirm: () => startRescan(fullScan),
  });

  if (!d) return <div className="content"><div className="loading">加载中…</div></div>;
  const busy = ((d.scanner || {}).mode === "scanning") || scanning;
  return (
    <div className="content">
      <ScanControls fullScan={fullScan} setFullScan={setFullScan} busy={busy} doRescan={doRescan} />
      <ScanStatusCard discovery={d} scanning={scanning} />
      <DiscoveryFunnel funnel={d.funnel} scoreHistogram={d.scoreHistogram} rejectReasons={d.rejectReasons} />

      <ScanHistoryTable runs={runs} />
    </div>
  );
}
