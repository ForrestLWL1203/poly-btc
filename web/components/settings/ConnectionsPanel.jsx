import { api, encryptCredential } from "../../lib/api.js";

const { useEffect, useState } = React;

function statusLabel(status) {
  return status === "valid" || status === "connected" ? "已连接" : status === "insufficient_balance" ? "余额不足" : status === "error" ? "连接异常" : "未配置";
}

export function ConnectionsPanel({ confirm }) {
  const [data, setData] = useState(null);
  const [wrap, setWrap] = useState(null);
  const [key, setKey] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(null);
  const [message, setMessage] = useState(null);
  const load = async () => {
    const [connections, wrapping] = await Promise.all([api.get("/api/connections"), api.get("/api/credential-wrap-key")]);
    setData(connections); setWrap(wrapping);
  };
  useEffect(() => { load().catch(() => setMessage({ bad: true, text: "连接状态加载失败" })); }, []);

  const save = async () => {
    const secret = key.trim();
    if (!secret || !wrap || !wrap.ready) return;
    setBusy("save"); setMessage(null);
    try {
      const envelope = await encryptCredential(secret, wrap);
      setKey(""); setShow(false);
      await api.cmdAndWait("set_provider_credential", { provider: "deepseek", envelope });
      await load();
      setMessage({ text: "已加密保存并通过 DeepSeek 验证" });
    } catch (e) {
      setMessage({ bad: true, text: e.message === "secure_context_required" ? "浏览器必须运行在 HTTPS 或 localhost 安全上下文" : "保存或验证失败，请检查 API Key 与 Observer 状态" });
    } finally { setBusy(null); }
  };

  const test = async () => {
    setBusy("test"); setMessage(null);
    try { await api.cmdAndWait("test_provider_connection", { provider: "deepseek" }); await load(); setMessage({ text: "连接与余额查询正常" }); }
    catch (_e) { setMessage({ bad: true, text: "连接测试失败" }); }
    finally { setBusy(null); }
  };

  const remove = () => confirm({ title: "删除 DeepSeek 凭据", danger: true, ok: "删除",
    body: "删除后风险雷达会进入缺少凭据状态，已有 Shadow 历史不会被删除。",
    onConfirm: async () => { setBusy("delete"); try { await api.cmdAndWait("delete_provider_credential", { provider: "deepseek" }); await load(); } finally { setBusy(null); } } });

  if (!data) return <div className="connections-panel"><div className="loading">加载中…</div></div>;
  const ds = data.deepseek || {};
  const bal = ds.balance;
  return (
    <div className="connections-panel">
      <div className="connection-card">
        <div className="connection-head">
          <div><span className="provider-mark ds">DS</span><div><h3>DeepSeek</h3><p>风险研判模型 · BYOK</p></div></div>
          <span className={"tint " + (ds.status === "insufficient_balance" || ds.status === "error" ? "tint-red" : ds.configured ? "tint-green" : "tint-gray")}>{statusLabel(ds.status)}</span>
        </div>
        <div className="secret-row">
          <input type={show ? "text" : "password"} value={key} onChange={e => setKey(e.target.value)}
            autoComplete="off" spellCheck="false" placeholder={ds.configured ? "输入新 Key 可安全替换" : "sk-••••••••••••"} />
          <button className="btn" onClick={() => setShow(v => !v)}>{show ? "隐藏" : "显示"}</button>
          <button className="btn btn-accent" disabled={!key.trim() || busy || !wrap?.ready || !data.workerAvailable} onClick={save}>{busy === "save" ? <><span className="spin" />验证中</> : "加密保存"}</button>
        </div>
        {!wrap?.ready && <div className="connection-note bad">需要先启动一次 Observer，生成本实例的凭据包装公钥。</div>}
        {!data.workerAvailable && <div className="connection-note bad">Observer 当前未在线；请先启动跟单进程，再保存、测试或删除连接。</div>}
        <div className="connection-note">API Key 在浏览器内通过 AES-GCM 加密，随机密钥再由本实例 RSA 公钥包装；命令通道与数据库只接触密文。</div>
        {bal && <div className="balance-strip">
          <div><span>余额</span><b>{bal.total != null ? Number(bal.total).toFixed(2) + " " + (bal.currency || "") : "—"}</b></div>
          <div><span>预计请求</span><b>{bal.estimatedRequests != null ? Math.floor(bal.estimatedRequests).toLocaleString() : "待积累成本样本"}</b></div>
          <div><span>预计运行</span><b>{bal.estimatedDays != null ? Number(bal.estimatedDays).toFixed(1) + " 天" : "待积累成本样本"}</b></div>
          <div><span>检查时间</span><b>{bal.checkedAt ? new Date(bal.checkedAt).toLocaleString() : "—"}</b></div>
        </div>}
        <div className="connection-actions">
          <button className="btn" disabled={!ds.configured || busy || !data.workerAvailable} onClick={test}>{busy === "test" ? <><span className="spin" />测试中</> : "测试连接 / 刷新余额"}</button>
          {ds.configured && <button className="btn btn-danger" disabled={busy || !data.workerAvailable} onClick={remove}>删除凭据</button>}
          {message && <span className={message.bad ? "down" : "up"}>{message.text}</span>}
        </div>
      </div>

      <div className="connection-card placeholder">
        <div className="connection-head">
          <div><span className="provider-mark hl">HL</span><div><h3>Hyperliquid 实盘</h3><p>第三方钱包授权 + Agent Wallet</p></div></div>
          <span className="tint tint-gray">占位 · 未实现</span>
        </div>
        <div className="connection-note">后续使用钱包签名授权，再为交易进程创建独立 Agent Wallet。这里不会要求或保存主钱包 Private Key。</div>
        <button className="btn" disabled>连接钱包（后续开放）</button>
      </div>
    </div>
  );
}
