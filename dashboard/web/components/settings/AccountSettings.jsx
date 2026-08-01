import { api, encryptCredential } from "../../lib/api.js";

const { useCallback, useEffect, useMemo, useState } = React;

const short = value => value ? value.slice(0, 8) + "…" + value.slice(-6) : "—";
const usd = value => value == null ? "—" : "$" + Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 });
const STATUS_LABEL = {
  paper: "Paper",
  live_ready: "Live Ready",
  live_canary: "Live Canary",
  live_running: "Live Running",
  paused: "Paused",
  draining: "Draining",
  reconcile_required: "Reconcile Required",
  credential_error: "Credential Error",
  no_funds: "No Funds",
};

const friendlyError = error => ({
  secure_context_required: "仅允许通过 HTTPS 或本机安全上下文录入私钥",
  credential_worker_not_provisioned: "VPS 凭据解密服务尚未配置",
  credential_verification_failed: "Agent 私钥、Agent 地址、主地址或账户模式验证失败",
  mainnet_credential_not_configured: "请先配置并验证 Mainnet Agent",
  live_preflight_not_passed: "实盘前置检查尚未通过",
  live_confirmation_phrase_mismatch: "确认短语不正确",
  canary_confirmation_phrase_mismatch: "Canary 解锁短语不正确",
  live_canary_minimum_duration_not_met: "Canary 必须连续运行至少 24 小时",
  live_canary_episode_not_completed: "尚未完成一轮真实目标开仓到归零的跟单 episode",
  live_canary_must_be_flat: "解除 Canary 前必须没有真实仓位和未决订单",
  mainnet_credential_in_use: "活跃实盘会话中不能替换或删除 Mainnet Agent；请先排空",
  mainnet_credential_not_verified: "请先验证 Mainnet Agent",
  NO_AVAILABLE_COLLATERAL: "Hyperliquid 账户没有可用 USDC",
  NO_EXECUTABLE_CAPACITY: "可用资金不足以形成最小合法订单",
  ACCOUNT_NOT_CLEAN: "账户仍有仓位或挂单，首次启用要求干净基线",
}[String(error?.message || error)] || String(error?.message || error || "操作失败"));

function CredentialCard({ network, status, wrapKey, reload, confirm }) {
  const existing = status?.credentials?.[network];
  const [accountAddress, setAccountAddress] = useState(existing?.accountAddress || "");
  const [agentAddress, setAgentAddress] = useState(existing?.agentAddress || "");
  const [privateKey, setPrivateKey] = useState("");
  const [validUntil, setValidUntil] = useState(existing?.validUntil || "");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const isMainnet = network === "mainnet";

  useEffect(() => {
    if (!existing) return;
    setAccountAddress(existing.accountAddress || "");
    setAgentAddress(existing.agentAddress || "");
    setValidUntil(existing.validUntil || "");
  }, [existing?.updatedAt]);

  const save = async () => {
    setBusy(true);
    setMessage(null);
    try {
      if (!/^(0x)?[0-9a-fA-F]{64}$/.test(privateKey.trim())) throw new Error("Agent 私钥必须是 32 字节十六进制");
      const context = { network, accountAddress, agentAddress };
      const envelope = await encryptCredential(privateKey, wrapKey, context);
      setPrivateKey("");
      await api.cmdAndWait("credential_upsert", {
        ...context, envelope, validUntil: validUntil || null,
      });
      await api.cmdAndWait("credential_verify", { network });
      setMessage({ ok: true, text: "已加密保存并验证 Agent 授权" });
      await reload();
    } catch (error) {
      setPrivateKey("");
      setMessage({ ok: false, text: friendlyError(error) });
    } finally {
      setBusy(false);
    }
  };

  const remove = () => confirm({
    title: `删除 ${isMainnet ? "Mainnet" : "Testnet"} Agent 凭据`,
    danger: true,
    ok: "删除 VPS 密文",
    body: "这里只删除 VPS 保存的加密密文。你仍需到 Hyperliquid 官方 API 页面撤销该 Agent 的授权。",
    onConfirm: async () => {
      try {
        await api.cmdAndWait("credential_delete", { network });
        setAccountAddress(""); setAgentAddress(""); setPrivateKey(""); setValidUntil("");
        await reload();
      } catch (error) { setMessage({ ok: false, text: friendlyError(error) }); }
    },
  });

  return (
    <section className={"account-credential-card " + (isMainnet ? "mainnet" : "testnet")}>
      <div className="account-card-head">
        <div><span className="account-kicker">{isMainnet ? "REAL CAPITAL" : "EXECUTION LAB"}</span>
          <h3>{isMainnet ? "Mainnet Agent" : "Testnet Agent"}</h3></div>
        <span className={"account-state-chip " + (existing?.status || "missing")}>{existing?.status || "未配置"}</span>
      </div>
      <p className="account-card-copy">{isMainnet
        ? "只用于真实交易签名，不具备提现权限。录入前请再次核对 Hyperliquid Mainnet。"
        : "仅用于 API、精度、订单状态和故障恢复验证，不作为产品运行模式。"}</p>
      <div className="account-form-grid">
        <label><span>Rabby 主地址</span><input value={accountAddress} onChange={e => setAccountAddress(e.target.value.trim())}
          placeholder="0x…" autoComplete="off" spellCheck="false" /></label>
        <label><span>Agent 公开地址</span><input value={agentAddress} onChange={e => setAgentAddress(e.target.value.trim())}
          placeholder="0x…" autoComplete="off" spellCheck="false" /></label>
        <label><span>Agent 私钥（只在浏览器内加密）</span><input type="password" value={privateKey}
          onChange={e => setPrivateKey(e.target.value)} placeholder="0x…" autoComplete="new-password"
          data-1p-ignore="true" data-lpignore="true" spellCheck="false" /></label>
        <label><span>授权到期时间{isMainnet ? "（必填，至少剩余 7 天）" : "（可选）"}</span><input value={validUntil} onChange={e => setValidUntil(e.target.value)}
          placeholder="2027-01-01T00:00:00Z" autoComplete="off" /></label>
      </div>
      <div className="account-card-actions">
        <button className={"btn " + (isMainnet ? "btn-danger" : "btn-accent")} disabled={busy || !privateKey || !wrapKey || (isMainnet && !validUntil)}
          onClick={save}>{busy ? "加密并验证中…" : existing ? "替换并重新验证" : "加密保存并验证"}</button>
        {existing && <button className="btn" disabled={busy} onClick={remove}>删除密文</button>}
        {existing && <span className="account-address-proof">{short(existing.accountAddress)} → {short(existing.agentAddress)}</span>}
      </div>
      {message && <div className={"account-inline-message " + (message.ok ? "ok" : "error")}>{message.text}</div>}
    </section>
  );
}

function PreflightChecks({ checks }) {
  if (!checks) return null;
  const labels = {
    observerStopped: "Observer 已停止", clockSynchronized: "系统时间同步", credentialConfigured: "Agent 已配置",
    credentialVerified: "私钥与 Agent 匹配", agentOwnerMatches: "Agent 归属主地址", unifiedAccount: "Unified 账户",
    rest: "REST 可用", websocket: "WebSocket 可用", strategyRevision: "策略版本有效",
    activeTargets: "存在活跃 Core", marketMetadata: "市场元数据完整", cleanAccount: "账户无未知仓位/挂单", funded: "资金可执行",
  };
  return <div className="preflight-grid">{Object.entries(labels).map(([key, label]) => (
    <div key={key} className={checks[key] ? "pass" : "fail"}><i />{label}<b>{checks[key] ? "PASS" : "FAIL"}</b></div>
  ))}</div>;
}

export function AccountSettings({ confirm }) {
  const [status, setStatus] = useState(null);
  const [wrapKey, setWrapKey] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [preflight, setPreflight] = useState(null);
  const [phrase, setPhrase] = useState("");
  const [canaryPhrase, setCanaryPhrase] = useState("");

  const reload = useCallback(async () => {
    const next = await api.get("/api/execution/status");
    setStatus(next);
    return next;
  }, []);

  useEffect(() => {
    reload().catch(e => setError(friendlyError(e)));
    api.get("/api/credential-wrap-key").then(setWrapKey).catch(e => setError(friendlyError(e)));
    const timer = setInterval(() => reload().catch(() => {}), 5000);
    return () => clearInterval(timer);
  }, [reload]);

  const setMode = async mode => {
    setBusy(true); setError(null);
    try { await api.cmdAndWait("set_execution_mode", { mode }); await reload(); }
    catch (e) { setError(friendlyError(e)); }
    finally { setBusy(false); }
  };

  const runPreflight = async () => {
    setBusy(true); setError(null); setPreflight(null);
    try {
      const result = await api.cmdAndWait("execution_preflight", {} , 90000);
      setPreflight(result); await reload();
      if (!result.ok) setError(result.code || "preflight_failed");
    } catch (e) { setError(friendlyError(e)); }
    finally { setBusy(false); }
  };

  const activate = async () => {
    setBusy(true); setError(null);
    try {
      await api.cmdAndWait("activate_live", {
        preflightId: preflight?.preflightId || status?.preflight?.preflightId,
        confirmationPhrase: phrase,
      });
      setPhrase("");
      await api.cmdAndWait("observer_start", {});
      await reload();
    } catch (e) { setError(friendlyError(e)); }
    finally { setBusy(false); }
  };

  const live = status?.selectedMode === "live";
  const active = !!status?.activeSessionId;
  const paused = status?.state === "paused";
  const canary = !!status?.session?.canary;
  const latest = preflight || status?.preflight;
  const mainnet = status?.credentials?.mainnet;
  const summary = useMemo(() => latest ? [
    ["真实权益", usd(latest.equity)], ["可用抵押品", usd(latest.available)],
    ["保证金计算基数", usd(latest.sizingEquity)], ["仓位 / 挂单", `${latest.positionCount ?? 0} / ${latest.openOrderCount ?? 0}`],
  ] : [], [latest]);
  const runtime = status?.account;

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
    <div className={"execution-mode-console " + (live ? "live" : "paper")}>
      <div><span className="account-kicker">EXECUTION MODE</span><h2>{STATUS_LABEL[status.state] || status.state}</h2>
        <p>{live ? "真实资金路径已选中；任何新增敞口都必须经过预检与会话门禁。" : "当前只记录模拟成交，绝不会签署 Mainnet 订单。"}</p></div>
      <div className="mode-switch" role="group" aria-label="执行模式">
        <button className={!live ? "active" : ""} disabled={busy} onClick={() => setMode("paper")}>Paper</button>
        <button className={live ? "active danger" : ""} disabled={busy || !mainnet} onClick={() => setMode("live")}>实盘 Live</button>
      </div>
    </div>

    {live && <div className="live-danger-ribbon"><b>LIVE · 真金白银</b><span>主地址 {short(mainnet?.accountAddress)} · Agent {short(mainnet?.agentAddress)}</span></div>}
    {error && <div className="account-error-banner">{friendlyError({ message: error })}</div>}

    <div className="account-credential-grid">
      <CredentialCard network="testnet" status={status} wrapKey={wrapKey} reload={reload} confirm={confirm} />
      <CredentialCard network="mainnet" status={status} wrapKey={wrapKey} reload={reload} confirm={confirm} />
    </div>

    <section className="live-launch-console">
      <div className="account-card-head"><div><span className="account-kicker">MAINNET GATE</span><h3>只读预检与实盘启动</h3></div>
        <span className={"account-state-chip " + (latest?.status || "missing")}>{latest?.status || "未执行"}</span></div>
      <p>预检不会下单。它核对 Agent 归属、Unified、资金、策略版本、Core、市场、REST/WS、仓位和挂单，并生成 5 分钟有效的一次性授权。</p>
      {summary.length > 0 && <div className="preflight-summary">{summary.map(([label, value]) => <div key={label}><span>{label}</span><b>{value}</b></div>)}</div>}
      <PreflightChecks checks={latest?.checks} />
      <div className="live-launch-actions">
        <button className="btn btn-accent" disabled={busy || !live || mainnet?.status !== "verified"} onClick={runPreflight}>
          {busy ? "处理中…" : "执行 Mainnet 只读预检"}</button>
        <label><span>通过后输入确认短语</span><input value={phrase} onChange={e => setPhrase(e.target.value)}
          placeholder="启动实盘" autoComplete="off" /></label>
        <button className="btn btn-danger" disabled={busy || latest?.status !== "passed" || phrase !== "启动实盘"} onClick={activate}>启动实盘</button>
      </div>
      <div className="account-safety-note">首次进入 Canary：总实盘保证金限于 min($100, 真实权益的 1%)，最多一个仓位。保证金权益额度只缩放每笔订单的权益计算基数，不是硬资金池。</div>
    </section>

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
