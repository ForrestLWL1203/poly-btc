import { api } from "../lib/api.js";
import { friendlyExecutionError } from "../lib/execution.js";
import { useApiResource } from "../lib/refresh.js";
import { DiscoveryFunnel } from "./discovery/DiscoveryFunnel.jsx";
import { ScanStatusCard } from "./discovery/ScanStatusCard.jsx";
import { ScanHistoryTable } from "./discovery/ScanHistoryTable.jsx";

export { ScanMask } from "./discovery/ScanMask.jsx";
export { scanStageLabel } from "./discovery/ScanMask.jsx";

const { useEffect, useCallback, useRef, useState } = React;

export function Discovery({ scanning, scanStatus, startRescan, confirm, openAccountSettings }) {
  const load = useCallback(async () => {
    const [discovery, scanRuns, collectionSource] = await Promise.all([
      api.get("/api/discovery"),
      api.get("/api/scan-runs?limit=5"),
      api.get("/api/collection-source"),
    ]);
    return { discovery, runs: scanRuns.runs, collectionSource };
  }, []);
  const { data, reload } = useApiResource(load, { intervalMs: 4000 });
  const [sourceChanging, setSourceChanging] = useState(false);
  const [sourceError, setSourceError] = useState(null);
  const wasScanning = useRef(scanning);
  useEffect(() => {
    if (wasScanning.current && !scanning) reload();
    wasScanning.current = scanning;
  }, [scanning, reload]);
  const d = data && data.discovery;
  const runs = data && data.runs;
  const collectionSource = data && data.collectionSource;

  const changeSource = async source => {
    if (!collectionSource || source === collectionSource.selectedSource) return;
    setSourceChanging(true);
    setSourceError(null);
    try {
      await api.patchCollectionSource(source);
      await reload();
    } catch (error) {
      setSourceError(friendlyExecutionError(error));
    } finally {
      setSourceChanging(false);
    }
  };

  const doRescan = () => confirm({
    title: "触发完整候选重评",
    danger: false, ok: "开始重评",
    body: "重新拉取完整 Leaderboard，执行官方 Perp 长/短历史收益预检和全部深度评分。已有完整历史只拉增量，新钱包才初始化 37 天。确认?",
    onConfirm: () => startRescan(true),
  });

  if (!d) return <div className="content"><div className="loading">加载中…</div></div>;
  const busy = ((d.scanner || {}).mode === "scanning") || scanning;
  return (
    <div className="content discovery-page">
      <ScanStatusCard discovery={d} scanning={scanning} scanStatus={scanStatus}
        busy={busy} doRescan={doRescan} collectionSource={collectionSource}
        sourceChanging={sourceChanging} sourceError={sourceError} onSourceChange={changeSource}
        onQuickNodeSetup={openAccountSettings} />
      <DiscoveryFunnel funnel={d.funnel} />

      <ScanHistoryTable runs={runs} />
    </div>
  );
}
