import { api, AUTH_EXPIRED_EVENT } from "./lib/api.js";
import { activateLiveAndStart, friendlyExecutionError } from "./lib/execution.js";
import { Confirm } from "./components/Confirm.jsx";
import { Discovery, ScanMask, scanStageLabel } from "./components/Discovery.jsx";
import { ExecutionStatusRings } from "./components/ExecutionStatusRings.jsx";
import { History } from "./components/History.jsx";
import { ObsMask } from "./components/ObsMask.jsx";
import { Overview } from "./components/Overview.jsx";
import { Positions } from "./components/Positions.jsx";
import { Settings } from "./components/Settings.jsx";
import { Wallets } from "./components/Wallets.jsx";
import {
  SCANNER_LABEL,
  cls,
  fNum,
  fPct,
  fSign,
  fUsd,
  scannerColor,
} from "./lib/format.js";
import { IC, Ico, PauseIcon, PlayIcon, StopIcon } from "./lib/icons.jsx";
import { useDashboardRefresh } from "./lib/refresh.js";

/* HyperEcho — precompiled React dashboard. Talks to the live dashboard API. */
const { useState, useEffect, useRef, useCallback } = React;

/* ----------------------------------------------------------------- shell */
const NAV = [
  ["监控", [["overview", "总览", IC.overview], ["positions", "持仓中", IC.positions], ["history", "历史持仓", IC.history], ["wallets", "跟踪钱包", IC.wallets]]],
  ["控制", [["discovery", "采集", IC.discovery], ["settings", "策略参数", IC.settings]]],
];
const TITLES = { overview: "总览", positions: "持仓中", history: "历史持仓", wallets: "跟踪钱包", discovery: "采集 Discovery", settings: "策略参数 Settings" };
const ACCENT_TITLE_PAGES = new Set(["overview", "positions", "history", "wallets"]);
const fStorage = bytes => {
  const value = Number(bytes || 0);
  if (Math.abs(value) >= 1e9) return (value / 1e9).toFixed(2) + " GB";
  if (Math.abs(value) >= 1e6) return (value / 1e6).toFixed(1) + " MB";
  return Math.round(value / 1e3) + " KB";
};

function ObserverControl({ status, executionState, busy, onStart, onPause, onStop, live = false }) {
  const stopped = status === "stopped";
  const paused = status === "paused";
  const draining = live && executionState === "draining";
  const reconcileRequired = live && executionState === "reconcile_required";
  const transportLocked = draining || reconcileRequired;
  const showPlay = stopped || paused;
  const transportLabel = stopped ? "启动跟单" : paused ? "恢复开仓" : "暂停新开仓";
  const stopLabel = live ? "排空并停止实盘" : "彻底停止跟单";

  return (
    <div className="observer-controls" role="group" aria-label="跟单运行控制">
      <button className={"btn observer-control-btn " + (showPlay ? "observer-play-btn" : "observer-pause-btn")}
        onClick={showPlay ? onStart : onPause} disabled={busy || transportLocked}
        aria-label={transportLabel} title={transportLocked ? (draining ? "实盘正在排空" : "需要先完成账户核对") : transportLabel}
        aria-busy={busy || undefined}>
        {showPlay ? <PlayIcon /> : <PauseIcon />}
      </button>
      <button className="btn observer-control-btn observer-stop-btn" onClick={onStop}
        disabled={busy || stopped || draining} aria-label={stopLabel} title={stopLabel}
        aria-busy={busy || undefined}>
        <StopIcon />
      </button>
    </div>
  );
}

function Dashboard({ onLogout }) {
  const [page, setPage] = useState("overview");
  const { ov, execution, livePositions, refreshModeData, scanning, setScanning, scanStatus, obsPending, setObsPending } = useDashboardRefresh(api);
  const [confirmCfg, setConfirmCfg] = useState(null);
  const [scanStopping, setScanStopping] = useState(false);
  const [scanStopError, setScanStopError] = useState(null);
  const [settingsTab, setSettingsTab] = useState(null);
  const [liveStarting, setLiveStarting] = useState(false);
  const mobileNavRef = useRef(null);

  const startRescan = useCallback(async (full = false) => { await api.cmd("rescan", { full: !!full }); setScanning(true); }, []);
  const stopRescan = useCallback(async () => {
    setScanStopping(true);
    setScanStopError(null);
    try {
      const result = await api.cmd("scan_stop", {});
      if (!result || result.error || result.status === "error") throw new Error("scan_stop_failed");
      setScanning(false);
    } catch (_e) {
      setScanStopping(false);
      setScanStopError("终止失败，扫描仍可能在运行，请稍后重试");
    }
  }, []);
  useEffect(() => {
    if (!scanning) {
      setScanStopping(false);
      setScanStopError(null);
    }
  }, [scanning]);
  const obs = ov && ov.system ? ov.system.observer : "stopped";   // stopped | running | paused
  const storageGuard = ov && ov.system && ov.system.storageGuard ? ov.system.storageGuard : {};
  const storageAlert = storageGuard.status === "warning" || storageGuard.status === "critical";
  const pausing = !!obsPending || liveStarting;
  const liveMode = execution?.selectedMode === "live" || ov?.system?.mode === "live";
  const executionState = execution?.state;
  // fire an observer-control command + raise the transition mask until the engine reaches `target`
  // (start/stop go through the supervisor + systemctl ~5-10s; pause/resume apply in the observer loop).
  const ctl = (type, label, target) => { api.cmd(type, {}); setObsPending({ label, target }); };
  // SMART start (shown when not actively opening): process alive but paused → just resume opening new
  // orders; process gone/hung (stopped) → restart the whole observer via the supervisor.
  const openAccountSettings = () => { setSettingsTab("account"); setPage("settings"); };
  const launchLive = async () => {
    setLiveStarting(true);
    try {
      await activateLiveAndStart(api);
      setObsPending({ label: "实盘检查通过，正在启动跟单…", target: "running" });
    } catch (error) {
      setObsPending(null);
      setConfirmCfg({
        title: "实盘启动未完成",
        body: friendlyExecutionError(error),
        ok: "查看账户信息",
        danger: false,
        onConfirm: openAccountSettings,
      });
    } finally {
      setLiveStarting(false);
    }
  };
  const requestLiveStart = () => {
    if (execution?.credentials?.mainnet?.status !== "verified") {
      openAccountSettings();
      return;
    }
    setConfirmCfg({
      title: "启动实盘跟单",
      danger: true,
      ok: "确认启动实盘",
      body: "将自动检查真实资金、Unified、Core、市场、REST/WS、仓位和挂单；全部通过后按当前实盘权益与策略参数创建会话并启动 Observer。",
      onConfirm: launchLive,
    });
  };
  const smartStart = () => {
    if (obs === "paused") return ctl("resume", "正在恢复开单…", "running");
    if (liveMode && !execution?.activeSessionId) return requestLiveStart();
    return ctl("observer_start", "正在启动跟单…", "running");
  };
  const pauseOpening = () => ctl("pause", "正在暂停新开仓…", "paused");
  const stopObserver = () => setConfirmCfg(liveMode ? {
    title: "排空并停止实盘", danger: true, ok: "进入 Draining",
    body: "系统会立即禁止开仓和加仓，但继续跟随目标减仓/平仓。真实仓位与系统订单全部归零后，Observer 才会停止；不会留下无人管理的实盘仓位。",
    onConfirm: () => ctl("drain", "正在进入实盘排空…", ov?.openCount ? "paused" : "stopped"),
  } : {
    title: "彻底停止跟单", danger: true, ok: "彻底停止整个进程",
    body: "将停止整个 Observer 进程：不再开新仓，存量持仓也不再由跟单进程管理。只想停止新开仓、让存量继续跟到平仓，请使用“暂停新开仓”。",
    onConfirm: () => ctl("observer_stop", "正在停止跟单…", "stopped"),
  });
  useEffect(() => {
    if (typeof window !== "undefined" && window.matchMedia && !window.matchMedia("(max-width: 860px)").matches) return;
    const raf = requestAnimationFrame(() => {
      const nav = mobileNavRef.current;
      const active = nav && nav.querySelector(".mobile-nav-item.active");
      if (!nav || !active) return;
      const left = active.offsetLeft - (nav.clientWidth - active.offsetWidth) / 2;
      nav.scrollLeft = Math.max(0, left);
    });
    return () => cancelAnimationFrame(raf);
  }, [page]);

  const mobileNavItems = NAV.flatMap(([, items]) => items);

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">
          <img className="brand-mark" src="/hyper-echo-mark.svg" alt="" />
          <div className="brand-copy">
            <b><span>HYPER</span><em>ECHO</em></b>
          </div>
        </div>
        {NAV.map(([grp, items]) => (
          <div key={grp}>
            <div className="nav-group">{grp}</div>
            {items.map(([k, label, d]) => {
              const cnt = (ov && ov.system)
                ? (k === "positions" ? ov.openCount : k === "history" ? ov.closedCount
                  : k === "wallets" ? ov.system.watchlistCount : null)
                : null;
              return (
                <div key={k} className={"nav-item" + (page === k ? " active" : "")} onClick={() => setPage(k)}>
                  <Ico d={d} />{label}
                  {cnt != null && <span className="nav-count">{cnt}</span>}
                </div>
              );
            })}
          </div>
        ))}
        <div className="spacer" />
        <div className="logout" onClick={onLogout}><Ico d={IC.logout} /> 退出登录</div>
      </aside>

      <main className="main">
        <div className="topbar">
          <div>
            {!ACCENT_TITLE_PAGES.has(page) && <div className={"crumb " + (liveMode ? "crumb-live" : "")}>{TITLES[page] && TITLES[page].split(" ")[1]} · {liveMode ? "实盘" : "模拟盘"}</div>}
            <div className={"title" + (ACCENT_TITLE_PAGES.has(page) ? " title-accent" : "")}>{TITLES[page]}</div>
          </div>
          <div className="topbar-right">
            <ExecutionStatusRings status={obs} executionState={executionState} live={liveMode} />
            {storageAlert && <span className={"pill " + (storageGuard.status === "critical" ? "tint-red" : "tint-amber")}
              title={`磁盘 ${Number(storageGuard.diskUsedPct || 0).toFixed(1)}% · DB日增 ${fStorage(storageGuard.dbGrowth24hBytes)} · WAL ${fStorage(storageGuard.dbWalBytes)}`}>
              <span className="dot" style={{ background: storageGuard.status === "critical" ? "var(--red)" : "var(--amber)", animation: "pulse 1.6s infinite" }} />
              {storageGuard.status === "critical" ? "磁盘高危" : "磁盘预警"} {Number(storageGuard.diskUsedPct || 0).toFixed(0)}%
            </span>}
            {ov && ov.system && <ObserverControl status={obs} executionState={executionState} busy={pausing}
              onStart={smartStart} onPause={pauseOpening} onStop={stopObserver} live={liveMode} />}
          </div>
        </div>

        {ov && ov.system && (
          <div className="system-strip" aria-label="系统状态摘要">
            <div className="strip-item"><span>权益</span><b>{fUsd(ov.equity)}</b></div>
            <div className="strip-item"><span>ROI</span><b className={cls(ov.roiPct)}>{fPct(ov.roiPct)}</b></div>
            <div className="strip-item"><span>今日</span><b className={cls(ov.todayPct)}>{fPct(ov.todayPct)}</b></div>
            <div className="strip-item"><span>在持</span><b>{ov.openCount}</b></div>
            <div className="strip-item"><span>可用</span><b>{fUsd(ov.availableBalance)}</b></div>
            <div className="strip-item"><span>浮动</span><b className={cls(ov.unrealizedPnl)}>{fSign(ov.unrealizedPnl)}</b></div>
            {(() => { const sc = ov.system.scanner, stale = ov.system.scannerStale;
              const active = sc === "scanning" && !stale;
              const stage = active ? scanStageLabel(ov.system.scannerStage) : (SCANNER_LABEL[sc] || sc);
              return <div className="strip-item strip-scanner">
                <span className="strip-scanner-title"><i className="dot" style={{ background: stale ? "var(--red)" : active ? "var(--green)" : "var(--gray)", animation: active || stale ? "pulse 1.6s infinite" : "none" }} />采集运行状态</span>
                <div className="strip-scanner-values"><b title={stage} style={{ color: scannerColor(sc, stale) }}>{stage}{stale && sc !== "idle" ? " ⚠" : ""}</b><small>{active ? "采集中" : stale ? "心跳超时" : "等待任务"}</small></div>
                <div className={"strip-scanner-line" + (active ? " active" : stale ? " stale" : "")} />
              </div>; })()}
          </div>
        )}

        {page === "overview" && <Overview ov={ov} />}
        {page === "positions" && <Positions confirm={setConfirmCfg} streamOpen={livePositions} />}
        {page === "history" && <History />}
        {page === "wallets" && <Wallets confirm={setConfirmCfg} onDataChanged={refreshModeData} />}
        {page === "discovery" && <Discovery scanning={scanning} startRescan={startRescan} confirm={setConfirmCfg} />}
        {page === "settings" && <Settings confirm={setConfirmCfg} initialTab={settingsTab}
          observerState={obs} onModeDataChanged={refreshModeData} />}
      </main>

      <nav className="mobile-nav" aria-label="移动端导航" ref={mobileNavRef}>
        {mobileNavItems.map(([k, label, d]) => {
          const cnt = (ov && ov.system)
            ? (k === "positions" ? ov.openCount : k === "history" ? ov.closedCount
              : k === "wallets" ? ov.system.watchlistCount : null)
            : null;
          return (
            <button key={k} className={"mobile-nav-item" + (page === k ? " active" : "")} onClick={() => setPage(k)} type="button">
              <Ico d={d} />
              <span>{label}</span>
              {cnt != null && <b>{cnt}</b>}
            </button>
          );
        })}
        <button className="mobile-nav-item mobile-logout" onClick={onLogout} type="button">
          <Ico d={IC.logout} />
          <span>退出</span>
        </button>
      </nav>

      {scanning && <ScanMask status={scanStatus} onStop={stopRescan} stopping={scanStopping}
        stopError={scanStopError} />}{/* Manual scans lock the page; scheduled scans stay non-blocking. */}
      {liveStarting && <ObsMask label="正在完成实盘启动检查…" />}
      {!liveStarting && obsPending && <ObsMask label={obsPending.label} />}
      <Confirm cfg={confirmCfg} onClose={() => setConfirmCfg(null)} />
    </div>
  );
}

/* ----------------------------------------------------------------- root */
function App() {
  const [authed, setAuthed] = useState(false);
  const [err, setErr] = useState(null);
  const [checkingSession, setCheckingSession] = useState(!!api.token);
  const [loggingIn, setLoggingIn] = useState(false);

  useEffect(() => {
    const expired = () => {
      setAuthed(false);
      setCheckingSession(false);
      setErr("登录已失效，请重新登录");
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
  }, []);

  // Validate an existing session only. Preview and production both require an explicit login when no
  // valid token exists; this avoids a stale preview-credential request racing the user's first submit.
  useEffect(() => {
    let cancelled = false;
    if (!api.token) {
      setCheckingSession(false);
      return () => { cancelled = true; };
    }
    (async () => {
      try {
        await api.get("/api/overview");
        if (!cancelled) setAuthed(true);
      } catch (_e) {
        if (!cancelled) setAuthed(false);
      } finally {
        if (!cancelled) setCheckingSession(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const doLogin = async event => {
    event.preventDefault();
    if (loggingIn || checkingSession) return;
    const values = new FormData(event.currentTarget);
    const username = String(values.get("username") || "").trim();
    const password = String(values.get("password") || "");
    setLoggingIn(true);
    setErr(null);
    try {
      await api.login(username, password);
      setAuthed(true);
    } catch (_e) {
      setErr("账号或密码错误");
    } finally {
      setLoggingIn(false);
    }
  };
  const logout = () => { api.logout(); setAuthed(false); };

  if (authed) return <Dashboard onLogout={logout} />;
  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={doLogin}>
        <div className="login-brand">
          <img src="/hyper-echo-mark.svg" alt="" />
          <div><b>HYPER <em>ECHO</em></b></div>
        </div>
        {err && <p className="err">{err}</p>}
        <input type="text" name="username" defaultValue="admin" required disabled={checkingSession || loggingIn}
          placeholder="账号" autoComplete="username" />
        <input type="password" name="password" required disabled={checkingSession || loggingIn}
          placeholder="密码" autoComplete="current-password" />
        <button type="submit" className="btn btn-accent" style={{ width: "100%" }}
          disabled={checkingSession || loggingIn} aria-busy={loggingIn || undefined}>
          {checkingSession ? "验证会话…" : loggingIn ? "登录中…" : "登录"}
        </button>
      </form>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
