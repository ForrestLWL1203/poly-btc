const { useState, useEffect, useCallback } = React;

export function usePolling(load, intervalMs, enabled = true) {
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const tick = () => { if (!cancelled) load(); };
    tick();
    const t = setInterval(tick, intervalMs);
    return () => { cancelled = true; clearInterval(t); };
  }, [load, intervalMs, enabled]);
}

function useDashboardStream(token) {
  const [live, setLive] = useState(null);
  const [streamOk, setStreamOk] = useState(false);

  useEffect(() => {
    if (!token || typeof EventSource === "undefined") return;
    let es;
    try {
      es = new EventSource("/api/stream?token=" + encodeURIComponent(token));
      es.onmessage = (e) => { try { setLive(JSON.parse(e.data)); setStreamOk(true); } catch (_e) {} };
      es.onerror = () => setStreamOk(false);
    } catch (_e) { setStreamOk(false); }
    return () => { if (es) es.close(); };
  }, [token]);

  return { live, streamOk };
}

function useOverviewRefresh(api, live, streamOk) {
  const [polledOverview, setPolledOverview] = useState(null);
  const loadOverview = useCallback(() => { api.get("/api/overview").then(setPolledOverview).catch(() => {}); }, [api]);

  usePolling(loadOverview, 7000, !streamOk);

  return {
    overview: (streamOk && live && live.overview) || polledOverview,
    setPolledOverview,
  };
}

function useManualScanProgress(api, serverScanning) {
  const [scanning, setScanning] = useState(false);
  const [scanStatus, setScanStatus] = useState(null);

  const checkManualScan = useCallback(() => {
    api.get("/api/scan-status").then((s) => {
      if (s && s.state === "scanning" && s.manual) { setScanning(true); setScanStatus(s); }
    }).catch(() => {});
  }, [api]);

  useEffect(() => { if (serverScanning) checkManualScan(); }, [serverScanning, checkManualScan]);
  useEffect(() => { checkManualScan(); }, [checkManualScan]);

  useEffect(() => {
    if (!scanning) return;
    let alive = true, started = Date.now(), seen = false;
    const tick = async () => {
      try {
        const s = await api.get("/api/scan-status");
        if (!alive) return;
        if (s.state === "scanning") { seen = true; setScanStatus(s); }
        else if ((seen || Date.now() - started > 8000) && !serverScanning) {
          setScanning(false); setScanStatus(null);
        }
      } catch (_e) {}
    };
    tick(); const t = setInterval(tick, 1200);
    return () => { alive = false; clearInterval(t); };
  }, [api, scanning, serverScanning]);

  return { scanning, setScanning, scanStatus };
}

function useObserverTransition(api, setOverview) {
  const [obsPending, setObsPending] = useState(null);

  useEffect(() => {
    if (!obsPending) return;
    let alive = true, started = Date.now();
    const tick = async () => {
      try {
        const o = await api.get("/api/overview");
        if (!alive) return;
        setOverview(o);
        const st = o && o.system ? o.system.observer : null;
        if (st === obsPending.target || Date.now() - started > 30000) setObsPending(null);
      } catch (_e) {}
    };
    tick(); const t = setInterval(tick, 1500);
    return () => { alive = false; clearInterval(t); };
  }, [api, obsPending, setOverview]);

  return { obsPending, setObsPending };
}

export function useDashboardRefresh(api) {
  const { live, streamOk } = useDashboardStream(api.token);
  const { overview, setPolledOverview } = useOverviewRefresh(api, live, streamOk);
  const serverScanning = !!(overview && overview.system && overview.system.scanner === "scanning");
  const scan = useManualScanProgress(api, serverScanning);
  const observer = useObserverTransition(api, setPolledOverview);

  return {
    ov: overview,
    livePositions: streamOk ? (live && live.positions) : null,
    streamOk,
    ...scan,
    ...observer,
  };
}
