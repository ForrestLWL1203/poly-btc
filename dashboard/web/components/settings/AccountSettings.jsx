import { api, encryptCredential } from "../../lib/api.js";

const { useCallback, useEffect, useState } = React;

const short = value => value ? value.slice(0, 8) + "…" + value.slice(-6) : "—";
const usd = value => value == null ? "—" : "$" + Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 });
const expiryText = value => {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
};

const STATUS_LABEL = {
  paper: "Paper",
  live_ready: "待启动",
  live_canary: "实盘 Canary",
  live_running: "实盘运行中",
  paused: "已暂停新增",
  draining: "排空中",
  reconcile_required: "需要对账",
  credential_error: "账户验证失败",
  no_funds: "资金不足",
};

const CREDENTIAL_LABEL = {
  verified: "已验证",
  encrypted: "待验证",
  error: "验证失败",
  expired: "已过期",
  revoked: "已撤销",
};

const friendlyError = error => ({
  secure_context_required: "仅允许通过 HTTPS 或本机安全上下文录入私钥",
  credential_worker_not_provisioned: "VPS 凭据解密服务尚未配置",
  credential_verification_failed: "验证失败：请核对主钱包、Agent 地址、私钥以及 Hyperliquid 官方授权",
  mainnet_credential_not_configured: "请先配置并验证实盘 Agent",
  mainnet_credential_not_verified: "请先完成实盘 Agent 验证",
  mainnet_credential_in_use: "实盘会话运行期间不能替换或删除 Agent；请先排空停止",
  live_preflight_not_passed: "实盘启动检查尚未通过",
  live_confirmation_phrase_mismatch: "实盘启动确认失败",
  live_exposure_prevents_paper_switch: "仍有真实仓位或订单，不能切回 Paper",
  OBSERVER_MUST_BE_STOPPED: "请先停止 Paper 跟单，再启动实盘",
  SYSTEM_CLOCK_NOT_SYNCHRONIZED: "VPS 系统时间尚未同步",
  STRATEGY_REVISION_INVALID: "当前策略版本不可执行",
  NO_EXECUTABLE_CORE_TARGETS: "当前没有可执行的 Core 钱包",
  MARKET_METADATA_INCOMPLETE: "实盘所需市场元数据不完整",
  WEBSOCKET_UNAVAILABLE: "Hyperliquid WebSocket 暂不可用",
  AGENT_MISMATCH: "Agent 未授权给当前主钱包",
  UNSUPPORTED_ACCOUNT_MODE: "Hyperliquid 账户不是 Unified 模式",
  ACCOUNT_NOT_CLEAN: "首次启动实盘前，账户必须没有仓位和挂单",
  NO_AVAILABLE_COLLATERAL: "Hyperliquid 账户没有可用 USDC",
  NO_EXECUTABLE_CAPACITY: "可用资金不足以形成最小合法订单",
}[String(error?.message || error)] || String(error?.message || error || "操作失败"));

function LiveAccountCard({ status, wrapKey, reload, confirm, busy, setBusy, setError, onLaunch }) {
  const existing = status?.credentials?.mainnet;
  const active = !!status?.activeSessionId;
  const [accountAddress, setAccountAddress] = useState(existing?.accountAddress || "");
  const [agentAddress, setAgentAddress] = useState(existing?.agentAddress || "");
  const [privateKey, setPrivateKey] = useState("");
  const [message, setMessage] = useState(null);

  useEffect(() => {
    if (!existing) return;
    setAccountAddress(existing.accountAddress || "");
    setAgentAddress(existing.agentAddress || "");
  }, [existing?.updatedAt]);

  const save = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      if (!/^(0x)?[0-9a-fA-F]{64}$/.test(privateKey.trim())) {
        throw new Error("Agent 私钥必须是 32 字节十六进制");
      }
      const context = { network: "mainnet", accountAddress, agentAddress };
      const envelope = await encryptCredential(privateKey, wrapKey, context);
      await api.cmdAndWait("credential_upsert", { ...context, envelope });
      const verified = await api.cmdAndWait("credential_verify", { network: "mainnet" }, 90000);
      setMessage({
        ok: true,
        text: `验证完成，官方授权有效至 ${expiryText(verified.validUntil)}。启动时将自动检查资金、策略和交易通道。`,
      });
      await reload();
    } catch (error) {
      setMessage({ ok: false, text: friendlyError(error) });
    } finally {
      setPrivateKey("");
      setBusy(false);
    }
  };

  const remove = () => confirm({
    title: "删除实盘 Agent 凭据",
    danger: true,
    ok: "删除 VPS 密文",
    body: "这里只删除 VPS 保存的加密密文。你仍需到 Hyperliquid 官方 API 页面撤销该 Agent 授权。",
    onConfirm: async () => {
      setBusy(true); setError(null);
      try {
        await api.cmdAndWait("credential_delete", { network: "mainnet" });
        setAccountAddress(""); setAgentAddress(""); setPrivateKey(""); setMessage(null);
        await reload();
      } catch (error) { setMessage({ ok: false, text: friendlyError(error) }); }
      finally { setBusy(false); }
    },
  });

  const verified = existing?.status === "verified";
  return <section className="live-account-card">
    <div className="account-card-head">
      <div>
        <span className="account-kicker">MAINNET ACCOUNT</span>
        <h3>实盘账户</h3>
        <p>私钥只在浏览器内加密。VPS 仅用于交易签名，不具备提现权限。</p>
      </div>
      <span className={"account-state-chip " + (existing?.status || "missing")}>
        {CREDENTIAL_LABEL[existing?.status] || "未配置"}
      </span>
    </div>

    <div className="live-account-form">
      <label><span>Rabby 主钱包地址</span><input value={accountAddress}
        onChange={e => setAccountAddress(e.target.value.trim())} placeholder="0x…"
        autoComplete="off" spellCheck="false" disabled={active} /></label>
      <label><span>Agent 公开地址</span><input value={agentAddress}
        onChange={e => setAgentAddress(e.target.value.trim())} placeholder="0x…"
        autoComplete="off" spellCheck="false" disabled={active} /></label>
      <label className="live-private-key-field"><span>Agent 私钥</span><input type="password" value={privateKey}
        onChange={e => setPrivateKey(e.target.value)} placeholder={existing ? "输入新私钥以替换当前凭据" : "0x…"}
        autoComplete="new-password" data-1p-ignore="true" data-lpignore="true" spellCheck="false"
        disabled={active} /></label>
    </div>

    {existing && <div className="live-credential-proof">
      <span><small>主钱包</small>{short(existing.accountAddress)}</span>
      <span><small>Agent</small>{short(existing.agentAddress)}</span>
      <span><small>官方授权有效至</small>{expiryText(existing.validUntil)}</span>
    </div>}

    <div className="live-account-actions">
      <button className="btn btn-accent" disabled={busy || active || !privateKey || !wrapKey}
        onClick={save}>{busy ? "正在加密并验证…" : existing ? "替换并重新验证" : "加密保存并完成验证"}</button>
      {existing && <button className="btn" disabled={busy || active} onClick={remove}>删除密文</button>}
      {!active && <button className="btn btn-danger" disabled={busy || !verified} onClick={onLaunch}>
        启动实盘跟单</button>}
    </div>

    {message && <div className={"account-inline-message " + (message.ok ? "ok" : "error")}>{message.text}</div>}
    {!active && <p className="account-safety-note">
      点击启动后会自动完成资金、Unified、Core、市场、REST/WS、仓位和挂单检查；首次实盘自动进入小额 Canary。
    </p>}
  </section>;
}

export function AccountSettings({ confirm }) {
  const [status, setStatus] = useState(null);
  const [wrapKey, setWrapKey] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [showLive, setShowLive] = useState(false);
  const [canaryPhrase, setCanaryPhrase] = useState("");

  const reload = useCallback(async () => {
    const next = await api.get("/api/execution/status");
    setStatus(next);
    if (next.selectedMode === "live") setShowLive(true);
    return next;
  }, []);

  useEffect(() => {
    reload().catch(e => setError(friendlyError(e)));
    api.get("/api/credential-wrap-key").then(setWrapKey).catch(e => setError(friendlyError(e)));
    const timer = setInterval(() => reload().catch(() => {}), 5000);
    return () => clearInterval(timer);
  }, [reload]);

  const active = !!status?.activeSessionId;
  const live = status?.selectedMode === "live";
  const paused = status?.state === "paused";
  const canary = !!status?.session?.canary;
  const runtime = status?.account;

  const toggleMode = async () => {
    if (busy || active) return;
    setError(null);
    if (!showLive) {
      setShowLive(true);
      return;
    }
    if (live) {
      setBusy(true);
      try { await api.cmdAndWait("set_execution_mode", { mode: "paper" }); await reload(); }
      catch (e) { setError(friendlyError(e)); }
      finally { setBusy(false); }
    }
    setShowLive(false);
  };

  const launchLive = async () => {
    setBusy(true); setError(null);
    let selectedLive = false;
    let activated = false;
    try {
      const overview = await api.get("/api/overview");
      if (overview?.system?.observer !== "stopped") throw new Error("OBSERVER_MUST_BE_STOPPED");
      await api.cmdAndWait("set_execution_mode", { mode: "live" });
      selectedLive = true;
      const check = await api.cmdAndWait("execution_preflight", {}, 90000);
      if (!check.ok) throw new Error(check.code || "live_preflight_not_passed");
      await api.cmdAndWait("activate_live", {
        preflightId: check.preflightId,
        confirmationPhrase: "启动实盘",
      });
      activated = true;
      await api.cmdAndWait("observer_start", {}, 90000);
      await reload();
    } catch (e) {
      if (selectedLive && !activated) {
        try { await api.cmdAndWait("set_execution_mode", { mode: "paper" }); }
        catch (_rollbackError) { /* status reload below remains authoritative */ }
      }
      setError(friendlyError(e));
      await reload().catch(() => {});
    } finally { setBusy(false); }
  };

  const requestLaunch = () => confirm({
    title: "启动实盘跟单",
    danger: true,
    ok: "确认启动实盘",
    body: "系统将自动执行完整启动检查，通过后创建真实资金会话并启动 Observer。首次运行受 Canary 小额上限保护。",
    onConfirm: launchLive,
  });

  const runControl = async (type, payload = {}, timeout = 90000) => {
    setBusy(true); setError(null);
    try { await api.cmdAndWait(type, payload, timeout); await reload(); }
    catch (e) { setError(friendlyError(e)); }
    finally { setBusy(false); }
  };

  const confirmControl = (type, title, body, ok) => confirm({
    title, body, ok, danger: true,
    onConfirm: () => runControl(type),
  });

  const unlockCanary = async () => {
    await runControl("unlock_live_canary", { confirmationPhrase: canaryPhrase });
    setCanaryPhrase("");
  };

  if (!status) return <div className="account-loading">加载账户执行状态…</div>;
  return <div className="account-settings">
    <div className={"execution-mode-row " + (showLive ? "live" : "paper")}>
      <div className="execution-mode-copy">
        <span>执行模式</span>
        <p><b>{showLive ? (live ? "实盘" : "实盘配置") : "Paper"}</b>{showLive
          ? "配置 Mainnet Agent；启动后使用真实资金与独立实盘账本。"
          : "模拟账本独立运行，不连接或签署真实账户。"}</p>
      </div>
      <div className="execution-toggle-wrap">
        <span>Paper</span>
        <button className={"execution-toggle " + (showLive ? "on" : "off")} type="button"
          role="switch" aria-checked={showLive} aria-label="切换 Paper 与实盘模式"
          disabled={busy || active} onClick={toggleMode}><i /></button>
        <span className="live-label">实盘</span>
      </div>
    </div>

    {error && <div className="account-error-banner">{error}</div>}

    {showLive && <LiveAccountCard status={status} wrapKey={wrapKey} reload={reload} confirm={confirm}
      busy={busy} setBusy={setBusy} setError={setError} onLaunch={requestLaunch} />}

    {live && active && <section className="live-operations-console">
      <div className="account-card-head"><div><span className="account-kicker">LIVE OPERATIONS</span><h3>实盘运行控制</h3></div>
        <span className={"account-state-chip " + status.state}>{STATUS_LABEL[status.state] || status.state}</span></div>
      {runtime && <div className="preflight-summary">
        <div><span>真实权益</span><b>{usd(runtime.equity)}</b></div>
        <div><span>真实可用</span><b>{usd(runtime.available)}</b></div>
        <div><span>已用保证金</span><b>{usd(runtime.marginUsed)}</b></div>
        <div><span>真实仓位 / 未决订单</span><b>{runtime.positionCount ?? 0} / {runtime.activeOrderCount ?? 0}</b></div>
      </div>}
      <div className="live-operation-actions">
        <button className="btn" disabled={busy || status.state === "draining" || status.state === "reconcile_required"}
          onClick={() => runControl(paused ? "resume" : "pause")}>{paused ? "Resume 恢复新增" : "Pause 暂停新增"}</button>
        <button className="btn btn-danger" disabled={busy || status.state === "draining"}
          onClick={() => confirmControl("drain", "进入实盘 Draining", "立即禁止新增敞口，继续管理减仓和平仓，归零后自动停止。", "确认排空")}>排空后停止</button>
        <button className="btn btn-danger live-emergency" disabled={busy}
          onClick={() => confirmControl("emergency_close_all", "紧急平掉全部实盘仓位", "系统将先取消本会话已知订单，再对全部系统管理仓位逐仓提交 reduce-only 平仓。未知交易所状态会失败关闭并要求人工对账。", "确认紧急平仓")}>Emergency Close All</button>
      </div>
      <p className="account-freshness">最近对账 {status.reconcile?.createdAt || "—"} · 状态 {status.reconcile?.status || "尚无"} · 账户观测 {runtime?.observedAt || "—"}</p>
      {canary && <div className="canary-unlock-row">
        <label><span>完成 24h 与完整 episode 后输入</span><input value={canaryPhrase} onChange={e => setCanaryPhrase(e.target.value)}
          placeholder="解除 Canary" autoComplete="off" /></label>
        <button className="btn btn-danger" disabled={busy || canaryPhrase !== "解除 Canary"} onClick={unlockCanary}>解除 Canary 上限</button>
      </div>}
    </section>}
  </div>;
}
