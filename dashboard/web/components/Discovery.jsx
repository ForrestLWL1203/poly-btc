import { api } from "../lib/api.js";
import { useApiResource } from "../lib/refresh.js";
import { DiscoveryFunnel } from "./discovery/DiscoveryFunnel.jsx";
import { ScanControls, ScanStatusCard } from "./discovery/ScanStatusCard.jsx";
import { ScanHistoryTable } from "./discovery/ScanHistoryTable.jsx";

export { ScanMask } from "./discovery/ScanMask.jsx";
export { scanStageLabel } from "./discovery/ScanMask.jsx";

const { useEffect, useCallback, useRef } = React;

export function Discovery({ scanning, startRescan, confirm }) {
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
    title: "触发每日完整候选重评",
    danger: false, ok: "开始重评",
    body: "重新拉取完整 Leaderboard，执行官方 ROI、Perp 预检和全部深度评分。已有完整历史只拉增量，新钱包才初始化 37 天。确认?",
    onConfirm: () => startRescan(true),
  });

  if (!d) return <div className="content"><div className="loading">加载中…</div></div>;
  const busy = ((d.scanner || {}).mode === "scanning") || scanning;
  return (
    <div className="content">
      <ScanControls busy={busy} doRescan={doRescan} />
      <ScanStatusCard discovery={d} scanning={scanning} />
      <DiscoveryFunnel funnel={d.funnel} />

      <ScanHistoryTable runs={runs} />
    </div>
  );
}
