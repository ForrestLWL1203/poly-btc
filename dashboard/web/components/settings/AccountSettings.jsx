import { api, encryptCredential, encryptQuickNodeEndpoint } from "../../lib/api.js";
import { friendlyExecutionError } from "../../lib/execution.js";
import { fUsd } from "../../lib/format.js";

const { useCallback, useEffect, useState } = React;

const expiryText = value => {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
};

const QUICKNODE_LABEL = {
  verified: "已验证",
  fallback: "最近故障",
  error: "验证失败",
  missing: "未配置",
  not_configured: "未配置",
};

function QuickNodeCard({ source, wrapKey, reload }) {
  const [endpoint, setEndpoint] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const quicknode = source?.quicknode || {};
  const status = quicknode.status || "not_configured";

  const save = async () => {
    setSaving(true);
    setMessage(null);
    try {
      const envelope = await encryptQuickNodeEndpoint(endpoint, wrapKey);
      await api.saveQuickNodeEndpoint(envelope);
      setEndpoint("");
      setMessage({ ok: true, text: "Endpoint 验证通过，已安全保存；下次采集可选择 QuickNode。" });
      await reload();
    } catch (error) {
      setMessage({ ok: false, text: friendlyExecutionError(error) });
    } finally {
      setSaving(false);
    }
  };

  return <section className="quicknode-account-card">
    <div className="account-card-head quicknode-card-head">
      <div>
        <span className="account-kicker">COLLECTION PROVIDER</span>
        <h3>QuickNode</h3>
      </div>
      <div className="quicknode-card-status">
        <span className={"account-state-chip " + status}>{QUICKNODE_LABEL[status] || "状态异常"}</span>
        <small>最近验证 {expiryText(quicknode.verifiedAt)}</small>
      </div>
    </div>
    <div className="quicknode-endpoint-row">
      <label><span>Endpoint</span><input type="password" value={endpoint}
        onChange={event => setEndpoint(event.target.value)} placeholder="https://…quiknode.pro/…"
        autoComplete="new-password" data-1p-ignore="true" data-lpignore="true" spellCheck="false" /></label>
      <button className="btn btn-accent" disabled={saving || !endpoint.trim() || !wrapKey} onClick={save}>
        {saving ? "正在验证…" : "保存并验证"}
      </button>
    </div>
    {message && <div className={"account-inline-message " + (message.ok ? "ok" : "error")}>{message.text}</div>}
    {!message && quicknode.errorCode && <div className="account-inline-message error">
      最近失败：{friendlyExecutionError(quicknode.errorCode)}
    </div>}
  </section>;
}

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
      </div>
      <span className={"account-state-chip " + (existing?.status || "missing")}>
        {CREDENTIAL_LABEL[existing?.status] || "未配置"}
      </span>
    </div>

    <div className="live-account-form">
      <label><span>钱包地址</span><input value={accountAddress}
        onChange={e => setAccountAddress(e.target.value.trim())} placeholder="0x…"
        autoComplete="off" spellCheck="false" disabled={active} /></label>
      <label><span>Agent 地址</span><input value={agentAddress}
        onChange={e => setAgentAddress(e.target.value.trim())} placeholder="0x…"
        autoComplete="off" spellCheck="false" disabled={active} /></label>
      <label className="live-private-key-field"><span>Agent 私钥</span><input type="password" value={privateKey}
        onChange={e => setPrivateKey(e.target.value)} placeholder={existing ? "输入新私钥以替换当前凭据" : "0x…"}
        autoComplete="new-password" data-1p-ignore="true" data-lpignore="true" spellCheck="false"
        disabled={active} /></label>
    </div>

    <div className="live-account-actions">
      <button className="btn btn-accent" disabled={busy || active || observerRunning || !privateKey || !wrapKey}
        title={observerRunning ? "请先使用右上角按钮停止当前跟单" : "加密保存并验证 Mainnet Agent"}
        onClick={save}>{busy ? "正在加密并验证…" : existing ? "替换并重新验证" : "加密保存并验证"}</button>
      {existing && <button className="btn" disabled={busy || active || observerRunning}
        onClick={remove}>删除密文</button>}
    </div>

    {message && <div className={"account-inline-message " + (message.ok ? "ok" : "error")}>{message.text}</div>}
  </section>;
}

export function AccountSettings({ confirm, observerState = null, onModeDataChanged = null }) {
  const [status, setStatus] = useState(null);
  const [wrapKey, setWrapKey] = useState(null);
  const [collectionSource, setCollectionSource] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [showLive, setShowLive] = useState(false);

  const reload = useCallback(async () => {
    const next = await api.get("/api/execution/status");
    setStatus(next);
    if (next.selectedMode === "live") setShowLive(true);
    return next;
  }, []);

  const reloadCollection = useCallback(async () => {
    const next = await api.get("/api/collection-source");
    setCollectionSource(next);
    return next;
  }, []);

  useEffect(() => {
    reload().catch(e => setError(friendlyExecutionError(e)));
    reloadCollection().catch(e => setError(friendlyExecutionError(e)));
    api.get("/api/credential-wrap-key").then(setWrapKey).catch(e => setError(friendlyExecutionError(e)));
    const timer = setInterval(() => {
      reload().catch(() => {});
      reloadCollection().catch(() => {});
    }, 5000);
    return () => clearInterval(timer);
  }, [reload, reloadCollection]);

  const live = status?.selectedMode === "live";
  const verified = status?.credentials?.mainnet?.status === "verified";
  const observerRunning = !["stopped", "error", "failed"].includes(observerState);

  const toggleMode = async () => {
    if (busy || observerRunning) return;
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
        <button className={"execution-toggle " + (showLive ? "on" : "off")} type="button"
          role="switch" aria-checked={showLive} aria-label="切换 Paper 与实盘模式"
          title={observerRunning ? "请先使用右上角按钮停止当前跟单" : "切换 Paper 与实盘模式"}
          disabled={busy || observerRunning} onClick={toggleMode}><i /></button>
        <span className="live-label">实盘</span>
      </div>
    </div>

    {error && <div className="account-error-banner">{error}</div>}

    <QuickNodeCard source={collectionSource} wrapKey={wrapKey} reload={reloadCollection} />

    {showLive && <LiveAccountCard status={status} wrapKey={wrapKey} reload={reload}
      refreshDashboard={onModeDataChanged} confirm={confirm}
      observerRunning={observerRunning}
      busy={busy} setBusy={setBusy} setError={setError} />}
  </div>;
}
