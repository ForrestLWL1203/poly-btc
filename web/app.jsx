import { api } from "./lib/api.js";
import { Confirm } from "./components/Confirm.jsx";
import { Discovery, ScanMask } from "./components/Discovery.jsx";
import { History } from "./components/History.jsx";
import { ObsMask } from "./components/ObsMask.jsx";
import { Overview } from "./components/Overview.jsx";
import { Positions } from "./components/Positions.jsx";
import { Settings } from "./components/Settings.jsx";
import { ShadowCompare } from "./components/ShadowCompare.jsx";
import { Wallets } from "./components/Wallets.jsx";
import {
  SCANNER_LABEL,
  cls,
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
  ["监控", [["overview", "总览", IC.overview], ["positions", "持仓中", IC.positions], ["history", "历史持仓", IC.history], ["shadow", "影子对比", IC.overview], ["wallets", "跟踪钱包", IC.wallets]]],
  ["控制", [["discovery", "采集", IC.discovery], ["settings", "策略参数", IC.settings]]],
];
const TITLES = { overview: "总览 Overview", positions: "持仓中 Positions", history: "历史持仓 History", shadow: "影子对比 Maker Shadow", wallets: "跟踪钱包 Wallets", discovery: "采集 Discovery", settings: "策略参数 Settings" };

function Dashboard({ onLogout }) {
  const [page, setPage] = useState("overview");
  const { ov, livePositions, streamOk, scanning, setScanning, scanStatus, obsPending, setObsPending } = useDashboardRefresh(api);
  const [confirmCfg, setConfirmCfg] = useState(null);
  const [stopChecked, setStopChecked] = useState(false); // 运行态按钮内「彻底停止」复选框(勾选才升级为杀进程)
  const mobileNavRef = useRef(null);
  const toast = () => {};   // 右上角 tooltip 已废弃 — 各动作改用整页/按钮内联 loading 反馈

  const startRescan = useCallback(async (full = false) => { await api.cmd("rescan", { full: !!full }); setScanning(true); }, []);

  const obs = ov && ov.system ? ov.system.observer : "stopped";   // stopped | running | paused
  const obsUp = obs === "running" || obs === "paused";            // process is alive (vs not started)
  const pausing = !!obsPending;
  // fire an observer-control command + raise the transition mask until the engine reaches `target`
  // (start/stop go through the supervisor + systemctl ~5-10s; pause/resume apply in the observer loop).
  const ctl = (type, label, target) => { api.cmd(type, {}); setObsPending({ label, target }); };
  // SMART start (shown when not actively opening): process alive but paused → just resume opening new
  // orders; process gone/hung (stopped) → restart the whole observer via the supervisor.
  const smartStart = () => obs === "paused"
    ? ctl("resume", "正在恢复开单…", "running")
    : ctl("observer_start", "正在启动跟单…", "running");
  // RUNNING control: default = soft pause (停开新仓、存量继续管); arm the in-button checkbox to ESCALATE to
  // a full process stop. One button, FIXED size — only label/color change between 暂停开单 ↔ 彻底停止跟单.
  const pauseOrStop = () => {
    if (stopChecked) {
      setConfirmCfg({ title: "彻底停止跟单", danger: true, ok: "彻底停止整个进程",
        body: "将停止整个 Observer 进程:不再开新仓,且存量持仓也不再被管理(下次启动会自动重新接管)。只想停开新仓、让存量继续跟到平仓的话,取消勾选即可。",
        onConfirm: () => ctl("observer_stop", "正在停止跟单…", "stopped") });
    } else {
      ctl("pause", "正在暂停开单…", "paused");
    }
  };
  useEffect(() => { if (obs === "stopped") setStopChecked(false); }, [obs]);
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
                ? (k === "positions" ? ov.openCount : k === "wallets" ? ov.system.watchlistCount : null)
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
            <span className="pill pill-paper"><span className="dot" style={{ background: "var(--amber)" }} /> 运行模式 · Paper</span>
            {/* fixed-width control (minWidth 150, centered) so changing the label never resizes the button.
                stopped → 启动跟单(restart) · paused → 恢复开单或彻底停止 · running → 暂停开单或彻底停止。
                两种存活状态都保留 in-button checkbox，避免 paused 后失去停止进程入口。 */}
            {!(ov && ov.system) ? null : obs === "stopped"
              ? <button className="btn btn-go" style={{ minWidth: 150, justifyContent: "center" }} onClick={smartStart} disabled={pausing}>
                  <span className="dot" style={{ width: 7, height: 7, borderRadius: 9, background: "#fff" }} /> 启动跟单</button>
              : obs === "paused"
              ? <button className={"btn " + (stopChecked ? "btn-stop" : "btn-go")} style={{ minWidth: 150, justifyContent: "center" }}
                  onClick={stopChecked ? pauseOrStop : smartStart} disabled={pausing}>
                  <span onClick={(e) => { e.stopPropagation(); setStopChecked(v => !v); }} title="勾选后彻底停止整个进程，不恢复开单"
                    style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 15, height: 15, borderRadius: 4, border: "1.5px solid rgba(255,255,255,.75)", fontSize: 10, lineHeight: 1, cursor: "pointer", flexShrink: 0 }}>
                    {stopChecked ? "✓" : ""}</span>
                  {stopChecked ? "彻底停止跟单" : "恢复开单"}</button>
              : <button className={"btn " + (stopChecked ? "btn-stop" : "btn-accent")} style={{ minWidth: 150, justifyContent: "center" }} disabled={pausing} onClick={pauseOrStop}>
                  <span onClick={(e) => { e.stopPropagation(); setStopChecked(v => !v); }} title="勾选后升级为彻底停止整个进程"
                    style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 15, height: 15, borderRadius: 4, border: "1.5px solid rgba(255,255,255,.75)", fontSize: 10, lineHeight: 1, cursor: "pointer", flexShrink: 0 }}>
                    {stopChecked ? "✓" : ""}</span>
                  {stopChecked ? "彻底停止跟单" : "暂停开单"}</button>}
          </div>
        </div>

        {ov && ov.system && (
          <div className="system-strip" aria-label="系统状态摘要">
            <div className="strip-item"><span>权益</span><b>{fUsd(ov.equity)}</b></div>
            <div className="strip-item"><span>ROI</span><b className={cls(ov.roiPct)}>{fPct(ov.roiPct)}</b></div>
            <div className="strip-item"><span>今日</span><b className={cls(ov.todayPct)}>{fPct(ov.todayPct)}</b></div>
            <div className="strip-item"><span>在持</span><b>{ov.openCount}</b></div>
            <div className="strip-item"><span>可用</span><b>{fUsd(ov.availableBalance)}</b></div>
            <div className="strip-item"><span>被跟</span><b>{ov.system.watchlistCount}</b></div>
            <div className="strip-item"><span>浮动</span><b className={cls(ov.unrealizedPnl)}>{fSign(ov.unrealizedPnl)}</b></div>
            {(() => { const sc = ov.system.scanner, stale = ov.system.scannerStale;
              return <div className="strip-item"><span>采集</span><b style={{ color: scannerColor(sc, stale) }}>{SCANNER_LABEL[sc] || sc}{stale && sc !== "idle" ? " ⚠" : ""}</b></div>; })()}
          </div>
        )}

        {page === "overview" && <Overview ov={ov} />}
        {page === "positions" && <Positions confirm={setConfirmCfg} toast={toast} streamOpen={livePositions} />}
        {page === "history" && <History />}
        {page === "shadow" && <ShadowCompare />}
        {page === "wallets" && <Wallets confirm={setConfirmCfg} toast={toast} />}
        {page === "discovery" && <Discovery scanning={scanning} startRescan={startRescan} confirm={setConfirmCfg} />}
        {page === "settings" && <Settings confirm={setConfirmCfg} />}
      </main>

      <nav className="mobile-nav" aria-label="移动端导航" ref={mobileNavRef}>
        {mobileNavItems.map(([k, label, d]) => {
          const cnt = (ov && ov.system)
            ? (k === "positions" ? ov.openCount : k === "wallets" ? ov.system.watchlistCount : null)
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

      {scanning && <ScanMask status={scanStatus} />}{/* scanning = MANUAL scan only; 24h auto runs silent */}
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
