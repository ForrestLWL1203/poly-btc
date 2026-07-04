const { useState, useEffect, useRef } = React;

const api = {
  async get(p) { return (await fetch(p)).json(); },
  async post(p, body) { return (await fetch(p, { method: "POST", body: JSON.stringify(body || {}) })).json(); },
};
const SVC_LABEL = {
  dashboard: ["监控台", "读取面板 · 常开"],
  observe: ["跟单引擎", "复制目标交易(纸面)"],
  scan: ["扫描器", "发现优质钱包"],
  timer: ["每日扫描", "定时全量重扫"],
};

function App() {
  const [meta, setMeta] = useState({ targets: [], pubkey: "", repoRoot: "" });
  const [view, setView] = useState("home");       // home | deploy | ops
  const [active, setActive] = useState(null);
  const [toast, setToast] = useState(null);
  const say = (m) => { setToast(m); setTimeout(() => setToast(null), 2600); };
  const reload = () => api.get("/api/targets").then(setMeta).catch(() => {});
  useEffect(() => { reload(); }, []);

  return (
    <div className="wrap">
      <div className="top">
        <div className="mk">▸</div>
        <div>
          <h1>poly-btc 部署运维台</h1>
          <div className="sub">一键把跟单系统部署到本地或远程 VPS · 长期启停 / 更新 / 日志</div>
        </div>
        <div className="spacer" />
        {view !== "home" && <button className="btn btn-sm" onClick={() => { setView("home"); reload(); }}>← 返回</button>}
      </div>

      {view === "home" && <Home meta={meta} onDeploy={() => setView("deploy")}
        onOps={(t) => { setActive(t); setView("ops"); }} onReload={reload} say={say} />}
      {view === "deploy" && <Deploy meta={meta} say={say}
        onDone={() => { setView("home"); reload(); }} />}
      {view === "ops" && active && <Ops target={active} say={say} onBack={() => { setView("home"); reload(); }} />}

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

/* ─────────────────────────────────────────── 首页 */
function Home({ meta, onDeploy, onOps, onReload, say }) {
  const [showKey, setShowKey] = useState(false);
  const del = async (t) => {
    if (!confirm(`删除档案「${t.name || t.host}」?(不影响已部署的机器)`)) return;
    await api.post("/api/targets/delete", { id: t.id }); onReload(); say("已删除");
  };
  return (
    <React.Fragment>
      <div className="card">
        <div className="row">
          <div>
            <h2>部署新系统</h2>
            <div className="hint" style={{ marginBottom: 0 }}>本地一键起 dashboard,或把整套部署到远程 VPS</div>
          </div>
          <div className="spacer" />
          <button className="btn btn-accent" onClick={onDeploy}>＋ 新建部署</button>
        </div>
      </div>

      <div className="card">
        <h2>已保存的目标</h2>
        <div className="hint">部署过的机器会记在这里(只存连接信息,不存密码),点「运维」做启停/更新/日志</div>
        {(!meta.targets || meta.targets.length === 0) && <div className="muted" style={{ fontSize: 13 }}>还没有目标 —— 先新建一次部署</div>}
        {(meta.targets || []).map((t) => (
          <div className="tgt" key={t.id}>
            <div className="ic">{t.mode === "local" ? "🖥" : "☁"}</div>
            <div>
              <div className="nm">{t.name || t.host || "本地"}</div>
              <div className="meta">{t.mode === "local" ? "本地 · " + (t.app_dir || "") : `${t.user}@${t.host}:${t.ssh_port || 22}`}
                {t.domain ? " · " + t.domain : ""}</div>
            </div>
            <div className="spacer" />
            <button className="btn btn-sm" onClick={() => onOps(t)}>运维</button>
            <button className="btn btn-sm btn-danger" onClick={() => del(t)}>删</button>
          </div>
        ))}
      </div>

      <div className="card">
        <div className="row" style={{ cursor: "pointer" }} onClick={() => setShowKey(!showKey)}>
          <h2 style={{ marginBottom: 0 }}>launcher SSH 公钥</h2>
          <div className="spacer" />
          <span className="muted" style={{ fontSize: 12 }}>{showKey ? "▾" : "▸"}</span>
        </div>
        {showKey && <React.Fragment>
          <div className="hint" style={{ marginTop: 10 }}>部署时会自动装到 VPS(转免密)。也可手动加到目标机 ~/.ssh/authorized_keys:</div>
          <div className="term" style={{ maxHeight: 80 }}>{meta.pubkey}</div>
        </React.Fragment>}
      </div>
    </React.Fragment>
  );
}

/* ─────────────────────────────────────────── 部署向导 */
function Deploy({ meta, onDone, say }) {
  const [mode, setMode] = useState(null);         // local | vps
  const [f, setF] = useState({ user: "root", ssh_port: 22, port: 8810, dash_user: "admin",
    app_dir: "/root/poly-btc" });
  const [deployId, setDeployId] = useState(null);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const pickMode = (m) => {
    setMode(m);
    setF((x) => ({ ...x, mode: m, name: m === "local" ? "本地" : x.name,
      app_dir: m === "local" ? (meta.repoRoot || x.app_dir) : "/root/poly-btc" }));
  };
  const start = async () => {
    const r = await api.post("/api/deploy", { ...f, mode });
    if (r.deployId) setDeployId(r.deployId); else say("启动失败: " + (r.error || "?"));
  };

  if (deployId) return <Progress deployId={deployId} onDone={onDone} say={say} />;

  const F = (k, label, opts = {}) => (
    <label className="f">
      <span className="lb"><b>{label}</b>{opts.note && <span className="muted"> · {opts.note}</span>}</span>
      <input className="i" type={opts.pw ? "password" : "text"} value={f[k] || ""} onChange={set(k)}
        placeholder={opts.ph || ""} />
    </label>
  );

  return (
    <React.Fragment>
      <div className="card">
        <h2>① 选择部署方式</h2>
        <div className="hint">本地无需任何连接信息;远程 VPS 需要 IP 和 root 密码(仅首次用于装公钥)</div>
        <div className="modes">
          <div className={"mode-c" + (mode === "local" ? " on" : "")} onClick={() => pickMode("local")}>
            <div className="em">🖥</div><div className="t">本地部署</div>
            <div className="d">在这台机器直接起 dashboard,浏览器打开即用。无需 VPS / 域名。</div>
          </div>
          <div className={"mode-c" + (mode === "vps" ? " on" : "")} onClick={() => pickMode("vps")}>
            <div className="em">☁</div><div className="t">远程 VPS</div>
            <div className="d">SSH 到你的服务器,自动装环境 + systemd + 域名 HTTPS,长期在线。</div>
          </div>
        </div>
      </div>

      {mode && <div className="card">
        <h2>② 填写信息</h2>
        <div className="hint">{mode === "local" ? "本地部署只需设一个 dashboard 登录密码" : "带 * 的必填"}</div>
        {mode === "vps" && <React.Fragment>
          {F("name", "备注名", { ph: "东京 VPS" })}
          <div className="grid2">
            {F("host", "服务器 IP *", { ph: "1.2.3.4" })}
            {F("ssh_port", "SSH 端口", { note: "默认22" })}
          </div>
          <div className="grid2">
            {F("user", "登录用户", { note: "通常 root" })}
            {F("password", "root 密码 *", { pw: true, note: "仅首次装公钥用,不保存" })}
          </div>
          {F("domain", "域名", { note: "可选,配了才有 HTTPS", ph: "dashboard.example.com" })}
        </React.Fragment>}
        <div className="grid2">
          {F("dash_user", "监控台用户名", { note: "默认 admin" })}
          {F("dash_password", "监控台密码 *", { pw: true, note: "登录 dashboard 用" })}
        </div>
        {mode === "local" && F("app_dir", "代码目录", { note: "默认当前仓库" })}
        {mode === "vps" && <details style={{ marginTop: 4 }}>
          <summary className="muted" style={{ fontSize: 12, cursor: "pointer" }}>高级(代码目录 / 端口)</summary>
          <div className="grid2" style={{ marginTop: 10 }}>
            {F("app_dir", "代码目录", { note: "默认 /root/poly-btc" })}
            {F("port", "dashboard 端口", { note: "默认8810" })}
          </div>
        </details>}
        {mode === "vps" && f.domain && <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          ⚠ 部署前请先把 <b className="mono">{f.domain}</b> 的 DNS A 记录指向 <b className="mono">{f.host || "服务器IP"}</b>,Caddy 才能自动签发证书。</div>}
        <div className="divider" />
        <button className="btn btn-accent" onClick={start}
          disabled={mode === "vps" && (!f.host || !f.password)}>开始部署 →</button>
      </div>}
    </React.Fragment>
  );
}

/* ─────────────────────────────────────────── 部署进度(SSE) */
function Progress({ deployId, onDone, say }) {
  const [steps, setSteps] = useState([]);
  const [status, setStatus] = useState({});       // id -> running|done|error
  const [errs, setErrs] = useState({});
  const [log, setLog] = useState([]);
  const [end, setEnd] = useState(null);           // {ok, url, failed}
  const termRef = useRef(null);

  useEffect(() => {
    const es = new EventSource(`/api/deploy/${deployId}/events`);
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      if (ev.type === "begin") setSteps(ev.steps);
      else if (ev.type === "step") {
        if (ev.status) setStatus((s) => ({ ...s, [ev.id]: ev.status }));
        if (ev.error) setErrs((e) => ({ ...e, [ev.id]: ev.error }));
      } else if (ev.type === "log") setLog((l) => [...l, ev.line]);
      else if (ev.type === "end") { setEnd(ev); es.close(); }
    };
    es.onerror = () => es.close();
    return () => es.close();
  }, [deployId]);
  useEffect(() => { if (termRef.current) termRef.current.scrollTop = termRef.current.scrollHeight; }, [log]);

  const CMD_PFX = ["apt", "systemctl", "curl", "git", "nohup", "export", "cd "];
  const lnClass = (l) => l.startsWith("✓") ? "ln-ok" : l.startsWith("⚠") ? "ln-warn"
    : CMD_PFX.some((p) => l.startsWith(p)) ? "ln-cmd" : "";

  return (
    <React.Fragment>
      <div className="card">
        <h2>部署进度</h2>
        <div className="hint">每一步实时执行 · 失败可修好后重跑</div>
        <div className="steps">
          {steps.map((s) => {
            const st = status[s.id] || "pending";
            return (
              <div className={"step " + st} key={s.id}>
                <div className="dot">{st === "done" ? "✓" : st === "error" ? "✕" : ""}</div>
                <div className="st">{s.title}</div>
                {errs[s.id] && <div className="err">{errs[s.id]}</div>}
              </div>
            );
          })}
        </div>
        {log.length > 0 && <div className="term" ref={termRef}>
          {log.map((l, i) => <div key={i} className={lnClass(l)}>{l}</div>)}
        </div>}
      </div>

      {end && <div className="card">
        {end.ok ? <React.Fragment>
          <h2 style={{ color: "var(--green-l)" }}>✓ 部署成功</h2>
          <div className="hint" style={{ marginBottom: 12 }}>监控台已就绪。</div>
          <div className="row">
            <a className="btn btn-accent" href={end.url} target="_blank">打开监控台 ↗</a>
            <span className="mono muted" style={{ fontSize: 12 }}>{end.url}</span>
            <div className="spacer" />
            <button className="btn" onClick={onDone}>完成</button>
          </div>
        </React.Fragment> : <React.Fragment>
          <h2 style={{ color: "var(--red-l)" }}>✕ 部署在「{end.failed}」步失败</h2>
          <div className="hint">看上方日志定位原因,修好后可从头重跑(每步幂等)。</div>
          <button className="btn" onClick={onDone}>返回</button>
        </React.Fragment>}
      </div>}
    </React.Fragment>
  );
}

/* ─────────────────────────────────────────── 运维台 */
function Ops({ target, onBack, say }) {
  const [st, setSt] = useState(null);
  const [busy, setBusy] = useState(null);
  const [logUnit, setLogUnit] = useState(null);
  const [logText, setLogText] = useState("");

  const refresh = async () => { setSt(await api.post("/api/ops/status", { id: target.id })); };
  useEffect(() => { refresh(); }, [target.id]);

  const doAction = async (op, unit) => {
    setBusy(op + unit);
    const r = await api.post("/api/ops/action", { id: target.id, op, unit });
    setBusy(null); say(r.ok === false ? "失败: " + (r.error || r.out || "?") : `${unit} 已${{ start: "启动", stop: "停止", restart: "重启" }[op]}`);
    setTimeout(refresh, 800);
  };
  const update = async () => {
    setBusy("update"); const r = await api.post("/api/ops/update", { id: target.id });
    setBusy(null); say(r.ok ? "代码已更新并重启" : "更新失败: " + (r.error || "?")); setTimeout(refresh, 800);
  };
  const resetParams = async () => {
    if (!confirm("把全部策略参数强制恢复为代码默认值?覆盖 dashboard 上的所有调整。")) return;
    setBusy("reset"); const r = await api.post("/api/ops/reset-params", { id: target.id, category: null });
    setBusy(null); say(r.ok ? "参数已恢复默认(" + (r.out || "") + ")" : "失败: " + (r.error || "?"));
  };
  const viewLog = async (unit) => {
    setLogUnit(unit); setLogText("加载中…");
    const r = await api.post("/api/ops/logs", { id: target.id, unit, lines: 120 });
    setLogText(r.log || r.error || "(空)");
  };

  const units = target.mode === "local" ? ["dashboard", "observe", "scan"]
    : ["dashboard", "observe", "scan", "timer"];

  return (
    <React.Fragment>
      <div className="card">
        <div className="row">
          <div>
            <h2 style={{ marginBottom: 2 }}>{target.name || target.host} · 运维</h2>
            <div className="meta mono muted" style={{ fontSize: 12 }}>
              {target.mode === "local" ? "本地 " + (target.app_dir || "") : `${target.user}@${target.host}`}
              {st && st.commit ? " · " + st.commit : ""}</div>
          </div>
          <div className="spacer" />
          {st && st.url && <a className="btn btn-sm btn-accent" href={st.url} target="_blank">打开监控台 ↗</a>}
          <button className="btn btn-sm" onClick={refresh}>刷新</button>
        </div>
      </div>

      <div className="card">
        <h2>服务状态</h2>
        <div className="hint">{st ? (st.dashboardHttp ? "监控台 HTTP " + st.dashboardHttp : "") : "读取中…"}</div>
        {st && st.services && units.map((u) => {
          const state = st.services[u] || "unknown";
          const on = state === "active" || state === "running";
          return (
            <div className="svc" key={u}>
              <div className="nm">{SVC_LABEL[u][0]}<small>{SVC_LABEL[u][1]}</small></div>
              <span className={"stt " + (on ? "active" : state === "n/a" ? "inactive" : state)}>{state}</span>
              <div className="spacer" />
              {u !== "timer" && <React.Fragment>
                {!on && <button className="btn btn-sm" disabled={busy} onClick={() => doAction("start", u)}>启动</button>}
                {on && <button className="btn btn-sm" disabled={busy} onClick={() => doAction("stop", u)}>停止</button>}
                <button className="btn btn-sm" disabled={busy} onClick={() => doAction("restart", u)}>重启</button>
              </React.Fragment>}
              <button className="btn btn-sm" onClick={() => viewLog(u)}>日志</button>
            </div>
          );
        })}
        {!st && <div className="muted" style={{ fontSize: 13 }}>连接中…(远程首次可能几秒)</div>}
      </div>

      <div className="card">
        <h2>维护操作</h2>
        <div className="hint">代码更新 = git 拉取最新并重启(无需构建,前端已预编译)</div>
        <div className="row">
          <button className="btn" disabled={busy} onClick={update}>⟳ 代码更新并重启</button>
          <button className="btn btn-danger" disabled={busy} onClick={resetParams}>↺ 恢复默认参数</button>
        </div>
      </div>

      {logUnit && <div className="card">
        <div className="row"><h2 style={{ marginBottom: 0 }}>日志 · {SVC_LABEL[logUnit][0]}</h2>
          <div className="spacer" /><button className="btn btn-sm" onClick={() => setLogUnit(null)}>关闭</button></div>
        <div className="term" style={{ marginTop: 12 }}>{logText}</div>
      </div>}

      {busy && <div className="mask"><div className="box"><div className="spin" /><div>执行中…</div></div></div>}
    </React.Fragment>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
