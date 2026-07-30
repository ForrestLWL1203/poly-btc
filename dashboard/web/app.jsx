import { api, AUTH_EXPIRED_EVENT } from "./lib/api.js";
import { Confirm } from "./components/Confirm.jsx";
import { Discovery, ScanMask, scanStageLabel } from "./components/Discovery.jsx";
import { History } from "./components/History.jsx";
import { ObsMask } from "./components/ObsMask.jsx";
import { Overview } from "./components/Overview.jsx";
import { Positions } from "./components/Positions.jsx";
import { RiskRadar } from "./components/RiskRadar.jsx";
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
import { IC, Ico } from "./lib/icons.jsx";
import { useDashboardRefresh } from "./lib/refresh.js";

/* 跟单监控台 — precompiled React dashboard. Talks to the live dashboard API. */
const { useState, useEffect, useRef, useCallback } = React;

const DASH_USER = "admin";                       // preview auto-login (matches launch.json env)
const DASH_PW = "mock123";

/* ----------------------------------------------------------------- shell */
const NAV = [
  ["监控", [["overview", "总览", IC.overview], ["positions", "持仓中", IC.positions], ["history", "历史持仓", IC.history], ["wallets", "跟踪钱包", IC.wallets], ["risk", "风险雷达", IC.risk]]],
  ["控制", [["discovery", "采集", IC.discovery], ["settings", "策略参数", IC.settings]]],
];
const TITLES = { overview: "总览 Overview", positions: "持仓中 Positions", history: "历史持仓 History", wallets: "跟踪钱包 Wallets", risk: "风险雷达 Risk Radar", discovery: "采集 Discovery", settings: "策略参数 Settings" };

function ObserverControl({ status, busy, onStart, onPause, onStop }) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef(null);
  const paused = status === "paused";

  useEffect(() => {
    if (!open) return;
    const closeOutside = e => {
      if (!menuRef.current?.contains(e.target)) setOpen(false);
    };
    const closeOnEscape = e => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  useEffect(() => setOpen(false), [status, busy]);

  if (status === "stopped") {
    return (
      <button className="btn btn-go" onClick={onStart} disabled={busy}>
        <span className="dot observer-control-dot" /> 启动跟单
      </button>
    );
  }

  const act = action => {
    setOpen(false);
    action();
  };

  return (
    <div className="command-menu-wrap" ref={menuRef}>
      <button className={"btn " + (paused ? "btn-go" : "btn-accent")}
        onClick={() => setOpen(v => !v)} disabled={busy}
        aria-haspopup="menu" aria-expanded={open}>
        {paused ? "恢复 / 停止" : "暂停 / 停止"}
        <span className={"command-caret" + (open ? " open" : "")}>⌄</span>
      </button>
      {open && (
        <div className="command-menu" role="menu" aria-label="跟单运行控制">
          <div className="command-menu-title">{paused ? "继续运行" : "温和停止"}</div>
          <button className={"btn " + (paused ? "btn-go" : "btn-accent")} role="menuitem"
            onClick={() => act(paused ? onStart : onPause)}>
            {paused ? "恢复开单" : "暂停新开仓"}
          </button>
          <p>{paused ? "恢复跟随目标钱包的新开仓" : "不再开新仓，存量仓位继续自动管理至退出"}</p>
          <div className="command-menu-separator" />
          <div className="command-menu-title">彻底停止</div>
          <button className="btn btn-danger" role="menuitem" onClick={() => act(onStop)}>
            彻底停止跟单
          </button>
          <p>停止 Observer 进程，存量仓位将不再自动管理</p>
        </div>
      )}
    </div>
  );
}

function Dashboard({ onLogout }) {
  const [page, setPage] = useState("overview");
  const { ov, livePositions, streamOk, scanning, setScanning, scanStatus, obsPending, setObsPending } = useDashboardRefresh(api);
  const [confirmCfg, setConfirmCfg] = useState(null);
  const [scanStopping, setScanStopping] = useState(false);
  const [scanStopError, setScanStopError] = useState(null);
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
  const obsUp = obs === "running" || obs === "paused";            // process is alive (vs not started)
  const radarState = ov && ov.system && ov.system.riskRadar ? ov.system.riskRadar : {};
  const radarRunning = !!radarState.enabled && obsUp;
  const radarBull = radarState.bullishScore == null ? 50 : Number(radarState.bullishScore);
  const radarBear = radarState.bearishScore == null ? 50 : Number(radarState.bearishScore);
  const pausing = !!obsPending;
  // fire an observer-control command + raise the transition mask until the engine reaches `target`
  // (start/stop go through the supervisor + systemctl ~5-10s; pause/resume apply in the observer loop).
  const ctl = (type, label, target) => { api.cmd(type, {}); setObsPending({ label, target }); };
  // SMART start (shown when not actively opening): process alive but paused → just resume opening new
  // orders; process gone/hung (stopped) → restart the whole observer via the supervisor.
  const smartStart = () => obs === "paused"
    ? ctl("resume", "正在恢复开单…", "running")
    : ctl("observer_start", "正在启动跟单…", "running");
  const pauseOpening = () => ctl("pause", "正在暂停新开仓…", "paused");
  const stopObserver = () => setConfirmCfg({
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
        <div className="brand"><div className="mk">跟</div><div><b>跟单监控台</b><span>COPY-TRADE OPS</span></div></div>
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
            <div className="crumb">{TITLES[page] && TITLES[page].split(" ")[1]} · 模拟盘</div>
            <div className="title">{TITLES[page]}</div>
          </div>
          <div className="topbar-right">
            <span className="pill" style={{ background: "rgba(255,255,255,.05)", color: streamOk ? "var(--green-l)" : "var(--t3)" }}
              title={streamOk ? "SSE 实时推送已连接" : "轮询兜底(SSE 未连接)"}>
              <span className="dot" style={{ background: streamOk ? "var(--green)" : "var(--gray)", animation: streamOk ? "pulse 1.6s infinite" : "none" }} />
              {streamOk ? "实时" : "轮询"}</span>
            {ov && ov.system && ov.system.portfolioDrawdownStop?.active &&
              <span className="pill" style={{ background: "rgba(255,82,82,.14)", color: "var(--red-l)" }}
                title={`权益高水位回撤 ${fNum(ov.system.portfolioDrawdownStop.drawdownPct, 1)}%，已暂停并执行全平`}>
                <span className="dot" style={{ background: "var(--red)" }} /> 总体回撤止损已触发
              </span>}
            {ov && ov.system && <ObserverControl status={obs} busy={pausing}
              onStart={smartStart} onPause={pauseOpening} onStop={stopObserver} />}
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
            <div className="strip-item strip-radar">
              <span className="strip-radar-title"><i className="dot" style={{ background: radarRunning ? "var(--green)" : "var(--red)", animation: radarRunning ? "pulse 1.6s infinite" : "none" }} />风险雷达运行状态</span>
              <div className="strip-radar-values"><b className={radarRunning ? "up" : "down"}>{radarRunning ? "运行中" : "未运行"}</b><small>空 {radarBear.toFixed(0)} / 多 {radarBull.toFixed(0)}</small></div>
              <div className="strip-risk-split" aria-label={`空 ${radarBear.toFixed(0)}，多 ${radarBull.toFixed(0)}`}><i style={{ width: radarBear + "%" }} /><em style={{ width: radarBull + "%" }} /></div>
            </div>
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
        {page === "wallets" && <Wallets confirm={setConfirmCfg} />}
        {page === "risk" && <RiskRadar />}
        {page === "discovery" && <Discovery scanning={scanning} startRescan={startRescan} confirm={setConfirmCfg} />}
        {page === "settings" && <Settings confirm={setConfirmCfg} />}
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
      {obsPending && <ObsMask label={obsPending.label} />}
      <Confirm cfg={confirmCfg} onClose={() => setConfirmCfg(null)} />
    </div>
  );
}

/* ----------------------------------------------------------------- root */
function App() {
  const [authed, setAuthed] = useState(false);
  const [err, setErr] = useState(null);
  const [user, setUser] = useState("admin");
  const [pw, setPw] = useState("");          // empty by default (auto-login tries preview creds for local)

  useEffect(() => {
    const expired = () => {
      setAuthed(false);
      setErr("登录已失效，请重新登录");
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
  }, []);

  // On mount: validate any existing token; if invalid/missing, auto-login (local preview creds only —
  // harmless on prod where the password differs). Survives a server restart that wiped in-memory tokens.
  useEffect(() => {
    (async () => {
      try {
        if (api.token) { await api.get("/api/overview"); setAuthed(true); return; }
      } catch (_e) { /* stale token -> fall through to login */ }
      try { await api.login(DASH_USER, DASH_PW); setAuthed(true); } catch (_e) { setAuthed(false); }
    })();
  }, []);

  const doLogin = async () => {
    try { await api.login(user, pw); setAuthed(true); setErr(null); }
    catch (_e) { setErr("账号或密码错误"); }
  };
  const logout = () => { api.logout(); setAuthed(false); };

  if (authed) return <Dashboard onLogout={logout} />;
  return (
    <div className="login-shell">
      <div className="login-card">
        <div className="login-mark">跟</div>
        <h1>跟单监控台</h1>
        <p>COPY-TRADE OPS · 登录</p>
        {err && <p className="err">{err}</p>}
        <input type="text" value={user} onChange={e => setUser(e.target.value)}
          onKeyDown={e => e.key === "Enter" && doLogin()} placeholder="账号" autoComplete="username" />
        <input type="password" value={pw} onChange={e => setPw(e.target.value)}
          onKeyDown={e => e.key === "Enter" && doLogin()} placeholder="密码" autoComplete="current-password" />
        <button className="btn btn-accent" style={{ width: "100%" }} onClick={doLogin}>登录</button>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
