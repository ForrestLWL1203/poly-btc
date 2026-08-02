import { api, encryptCredential } from "../../lib/api.js";
import { friendlyExecutionError } from "../../lib/execution.js";
import { fUsd } from "../../lib/format.js";

const { useCallback, useEffect, useState } = React;

const short = value => value ? value.slice(0, 8) + "…" + value.slice(-6) : "—";
const expiryText = value => {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
};

const CREDENTIAL_LABEL = {
  verified: "已验证",
  encrypted: "待验证",
  error: "验证失败",
  expired: "已过期",
  revoked: "已撤销",
};

function LiveAccountCard({ status, wrapKey, reload, refreshDashboard, confirm, observerRunning,
  busy, setBusy, setError }) {
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
      await api.cmdAndWait("set_execution_mode", { mode: "live" }, 90000);
      const preview = verified.accountPreview || {};
      setMessage({
        ok: true,
        text: `验证完成并已载入真实账户：权益 ${fUsd(preview.equity)}，可用 ${fUsd(preview.available)}；官方授权有效至 ${expiryText(verified.validUntil)}。启动跟单仍使用右上角按钮。`,
      });
      await reload();
      if (refreshDashboard) await refreshDashboard();
    } catch (error) {
      setMessage({ ok: false, text: friendlyExecutionError(error) });
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
        if (refreshDashboard) await refreshDashboard();
      } catch (error) { setMessage({ ok: false, text: friendlyExecutionError(error) }); }
      finally { setBusy(false); }
    },
  });

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
      {status?.accountPreview && <React.Fragment>
        <span><small>真实权益</small>{fUsd(status.accountPreview.equity)}</span>
        <span><small>可用资金</small>{fUsd(status.accountPreview.available)}</span>
        <span><small>账户仓位 / 挂单</small>{status.accountPreview.positionCount} / {status.accountPreview.openOrderCount}</span>
      </React.Fragment>}
    </div>}

    <div className="live-account-actions">
      <button className="btn btn-accent" disabled={busy || active || observerRunning || !privateKey || !wrapKey}
        title={observerRunning ? "请先使用右上角按钮停止当前跟单" : "加密保存并验证 Mainnet Agent"}
        onClick={save}>{busy ? "正在加密并验证…" : existing ? "替换并重新验证" : "加密保存并验证"}</button>
      {existing && <button className="btn" disabled={busy || active || observerRunning}
        onClick={remove}>删除密文</button>}
    </div>

    {message && <div className={"account-inline-message " + (message.ok ? "ok" : "error")}>{message.text}</div>}
    <p className="account-safety-note">
      此处只完成凭据加密、Agent 归属、官方授权与 Unified 基础验证。资金、Core、市场、REST/WS、仓位和挂单检查，将在右上角启动跟单时自动执行。
    </p>
  </section>;
}

export function AccountSettings({ confirm, observerState = null, onModeDataChanged = null }) {
  const [status, setStatus] = useState(null);
  const [wrapKey, setWrapKey] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [showLive, setShowLive] = useState(false);

  const reload = useCallback(async () => {
    const next = await api.get("/api/execution/status");
    setStatus(next);
    if (next.selectedMode === "live") setShowLive(true);
    return next;
  }, []);

  useEffect(() => {
    reload().catch(e => setError(friendlyExecutionError(e)));
    api.get("/api/credential-wrap-key").then(setWrapKey).catch(e => setError(friendlyExecutionError(e)));
    const timer = setInterval(() => reload().catch(() => {}), 5000);
    return () => clearInterval(timer);
  }, [reload]);

  const active = !!status?.activeSessionId;
  const live = status?.selectedMode === "live";
  const verified = status?.credentials?.mainnet?.status === "verified";
  const observerRunning = !["stopped", "error", "failed"].includes(observerState);

  const toggleMode = async () => {
    if (busy || active || observerRunning) return;
    setError(null);
    if (live) {
      setBusy(true);
      try {
        await api.cmdAndWait("set_execution_mode", { mode: "paper" });
        await reload();
        if (onModeDataChanged) await onModeDataChanged();
      }
      catch (e) { setError(friendlyExecutionError(e)); }
      finally { setBusy(false); }
      setShowLive(false);
      return;
    }
    if (showLive && !verified) {
      setShowLive(false);
      return;
    }
    if (!showLive) setShowLive(true);
    if (!verified) return;
    setBusy(true);
    try {
      await api.cmdAndWait("credential_verify", { network: "mainnet" }, 90000);
      await api.cmdAndWait("set_execution_mode", { mode: "live" });
      await reload();
      if (onModeDataChanged) await onModeDataChanged();
    }
    catch (e) { setError(friendlyExecutionError(e)); }
    finally { setBusy(false); }
  };

  if (!status) return <div className="account-loading">加载账户执行状态…</div>;
  return <div className="account-settings">
    <div className={"execution-mode-row " + (live ? "live" : showLive ? "config" : "paper")}>
      <div className="execution-mode-copy">
        <span>执行模式</span>
        <p><b>{showLive ? (live ? "实盘" : "实盘配置") : "Paper"}</b>{showLive
          ? live
            ? "已选择实盘；真正启动与停止统一使用页面右上角按钮。"
            : "填写并验证 Mainnet Agent；验证后自动选择实盘并载入真实账户。"
          : "模拟账本独立运行，不连接或签署真实账户。"}</p>
      </div>
      <div className="execution-toggle-wrap">
        <span>Paper</span>
        <button className={"execution-toggle " + (live ? "on" : "off")} type="button"
          role="switch" aria-checked={live} aria-label="切换 Paper 与实盘模式"
          title={observerRunning ? "请先使用右上角按钮停止当前跟单" : "切换 Paper 与实盘模式"}
          disabled={busy || active || observerRunning} onClick={toggleMode}><i /></button>
        <span className="live-label">实盘</span>
      </div>
    </div>

    {error && <div className="account-error-banner">{error}</div>}

    {showLive && <LiveAccountCard status={status} wrapKey={wrapKey} reload={reload}
      refreshDashboard={onModeDataChanged} confirm={confirm}
      observerRunning={observerRunning}
      busy={busy} setBusy={setBusy} setError={setError} />}
  </div>;
}
