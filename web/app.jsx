import { api } from "./lib/api.js";
import { Confirm } from "./components/Confirm.jsx";
import { Discovery, ScanMask } from "./components/Discovery.jsx";
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
  fPrice,
  fSign,
  fUsd,
  scannerColor,
  short,
} from "./lib/format.js";
import { IC, Ico } from "./lib/icons.jsx";
import { useApiResource, useDashboardRefresh } from "./lib/refresh.js";

/* 跟单监控台 — precompiled React dashboard. Talks to the live dashboard API. */
const { useState, useEffect, useRef, useCallback } = React;

const DASH_USER = "admin";                       // preview auto-login (matches launch.json env)
const DASH_PW = "mock123";

/* ----------------------------------------------------------------- shell */
function ShadowCompare() {
  const loadShadow = useCallback(() => api.get("/api/shadow"), []);
  const { data: d } = useApiResource(loadShadow, { intervalMs: 10000 });
  if (!d) return <div className="content"><div className="loading">加载中…</div></div>;
  const roi = b => (b.equity / 10000 - 1) * 100;
  const Acct = ({ b, name, tint }) => (
    <div className="card" style={{ flex: 1 }}>
      <div className="card-lbl">{name}</div>
      <div className="kpi" style={{ color: tint }}>{fUsd(b.equity)}</div>
      <div className="muted" style={{ fontSize: 12, lineHeight: 1.7 }}>
        ROI <b className={cls(roi(b))}>{fSign(roi(b), 1)}%</b> · 已实现 <span className={cls(b.realized)}>{fSign(b.realized, 0)}</span> · 浮动 <span className={cls(b.unrealized)}>{fSign(b.unrealized, 0)}</span><br />
        {b.openN} 持仓 · {b.closedN} 平仓 · 胜率 {fNum(b.winRatePct, 0)}%
      </div>
    </div>
  );
  const diff = d.maker.equity - d.taker.equity;
  return (
    <div className="content">
      <h2>影子对比 · Maker vs Taker <span className="muted">· 同一套策略,只差执行方式</span></h2>
      {!d.enabled && <div className="muted" style={{ marginTop: 8 }}>⚠ 影子引擎未启用</div>}
      <div style={{ display: "flex", gap: 14, marginTop: 12 }}>
        <Acct b={d.taker} name="Taker 账(实盘执行)" tint="var(--t1)" />
        <Acct b={d.maker} name="Maker 影子账(挂单执行)" tint="var(--accent)" />
      </div>
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-lbl">Maker − Taker 权益差</div>
        <div className="kpi" style={{ color: diff >= 0 ? "var(--green-l)" : "var(--red-l)" }}>{fSign(diff, 1)}</div>
        <div className="muted" style={{ fontSize: 12 }}>正 = maker 执行更优(省手续费 + 更好入场价,但成交率更低)。两账从同一 $10k 起点、同策略跑,差异纯来自执行。</div>
      </div>
      <h3 style={{ marginTop: 18 }}>Maker 账当前持仓 <span className="muted">· {d.makerPositions.length} 笔</span></h3>
      <table><thead><tr><th>币</th><th>方向</th><th className="num">入场/杠杆</th><th className="num">保证金</th><th className="num">现价</th><th className="num">浮动</th><th>钱包</th></tr></thead>
        <tbody>
          {d.makerPositions.length === 0 && <tr><td colSpan="7" className="empty">影子账暂无持仓(等目标 maker 成交后建仓)</td></tr>}
          {d.makerPositions.map((p, i) => (
            <tr key={i}>
              <td><b>{p.coin}</b>{p.addN > 0 && <span className="pill" style={{ marginLeft: 6 }}>加{p.addN}</span>}</td>
              <td><span className={"tint " + (p.side === "long" ? "tint-green" : "tint-red")}>{p.side === "long" ? "多" : "空"}</span></td>
              <td className="num">{fPrice(p.entry)} · {fNum(p.lev, 0)}x</td>
              <td className="num">{fUsd(p.margin)}</td>
              <td className="num">{fPrice(p.mark)}</td>
              <td className={"num " + cls(p.upnl)}>{fSign(p.upnl, 1)}</td>
              <td className="addr">{short(p.addr)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

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
  useEffect(() => { if (obs !== "running") setStopChecked(false); }, [obs]);  // reset escalation off-running
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
                stopped → 启动跟单(restart) · paused → 恢复开单(resume) · running → 暂停开单 + in-button
                「彻底停止」checkbox that escalates to a full process stop (red). */}
            {!(ov && ov.system) ? null : obs === "stopped"
              ? <button className="btn btn-go" style={{ minWidth: 150, justifyContent: "center" }} onClick={smartStart} disabled={pausing}>
                  <span className="dot" style={{ width: 7, height: 7, borderRadius: 9, background: "#fff" }} /> 启动跟单</button>
              : obs === "paused"
              ? <button className="btn btn-go" style={{ minWidth: 150, justifyContent: "center" }} onClick={smartStart} disabled={pausing}>
                  <span className="dot" style={{ width: 7, height: 7, borderRadius: 9, background: "#fff" }} /> 恢复开单</button>
              : <button className={"btn " + (stopChecked ? "btn-stop" : "btn-accent")} style={{ minWidth: 150, justifyContent: "center" }} disabled={pausing} onClick={pauseOrStop}>
                  <span onClick={(e) => { e.stopPropagation(); setStopChecked(v => !v); }} title="勾选后升级为彻底停止整个进程"
                    style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 15, height: 15, borderRadius: 4, border: "1.5px solid rgba(255,255,255,.75)", fontSize: 10, lineHeight: 1, cursor: "pointer", flexShrink: 0 }}>
                    {stopChecked ? "✓" : ""}</span>
                  {stopChecked ? "彻底停止跟单" : "暂停开单"}</button>}
          </div>
        </div>

        {ov && ov.system && (
          <div className="ticker">
            <div className="chip"><div className="k">权益</div><div className="v">{fUsd(ov.equity)}</div></div>
            <div className="chip"><div className="k">ROI</div><div className={"v " + cls(ov.roiPct)}>{fPct(ov.roiPct)}</div></div>
            <div className="chip"><div className="k">今日</div><div className={"v " + cls(ov.todayPct)}>{fPct(ov.todayPct)}</div></div>
            <div className="chip"><div className="k">在持</div><div className="v">{ov.openCount}</div></div>
            <div className="chip"><div className="k">可用</div><div className="v">{fUsd(ov.availableBalance)}</div></div>
            <div className="chip"><div className="k">被跟</div><div className="v">{ov.system.watchlistCount}</div></div>
            <div className="chip"><div className="k">浮动</div><div className={"v " + cls(ov.unrealizedPnl)}>{fSign(ov.unrealizedPnl)}</div></div>
            <div className="chip"><div className="k">Observer</div><div className="v" style={{ fontSize: 13, color: obs === "stopped" ? "var(--t3)" : obs === "paused" ? "var(--amber)" : "var(--green-l)" }}>{obs === "stopped" ? "已停止" : obs === "paused" ? "已暂停" : "运行中"}</div></div>
            {(() => { const sc = ov.system.scanner, stale = ov.system.scannerStale;
              return <div className="chip"><div className="k">采集</div><div className="v" style={{ fontSize: 13, color: scannerColor(sc, stale) }}>{SCANNER_LABEL[sc] || sc}{stale && sc !== "idle" ? " ⚠" : ""}</div></div>; })()}
          </div>
        )}

        {page === "overview" && <Overview ov={ov} />}
        {page === "positions" && <Positions confirm={setConfirmCfg} toast={toast} streamOpen={livePositions} />}
        {page === "history" && <History />}
        {page === "shadow" && <ShadowCompare />}
        {page === "wallets" && <Wallets confirm={setConfirmCfg} toast={toast} />}
        {page === "discovery" && <Discovery scanning={scanning} startRescan={startRescan} confirm={setConfirmCfg} />}
        {page === "settings" && <Settings startRescan={startRescan} confirm={setConfirmCfg} />}
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
