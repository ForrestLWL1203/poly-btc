import { api } from "../lib/api.js";
import { cls, fSign, fTime, fUsd, short } from "../lib/format.js";

const { useCallback, useEffect, useState } = React;

const CONFIRM = { steady: "双次强信号", accelerating: "风险加速", extreme: "单次极端" };
const OUTCOME = { avoided_loss: "本可避免亏损", missed_profit: "会错过盈利", flat: "持平", allowed: "允许开仓" };

export function RiskRadar() {
  const [radar, setRadar] = useState(null);
  const [intents, setIntents] = useState([]);
  const [thresholds, setThresholds] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const load = useCallback(async () => {
    const [r, i, t] = await Promise.all([api.get("/api/risk-radar"), api.get("/api/risk-radar/intents?limit=80"), api.get("/api/risk-radar/thresholds")]);
    setRadar(r); setIntents(i.intents || []); setThresholds(t.comparison || []);
  }, []);
  useEffect(() => { load().catch(() => setError("雷达数据加载失败")); const id = setInterval(() => load().catch(() => {}), 10000); return () => clearInterval(id); }, [load]);
  const toggle = async () => {
    setBusy(true); setError(null);
    try { await api.cmdAndWait(radar.mode === "shadow" ? "risk_radar_stop" : "risk_radar_start", {}); await load(); }
    catch (_e) { setError("启停失败：请确认 Observer 正在运行且 DeepSeek 已配置"); }
    finally { setBusy(false); }
  };
  if (!radar) return <div className="content"><div className="loading">加载中…</div></div>;
  const current = radar.assessments?.find(a => a.id === radar.currentAssessmentId) || radar.assessments?.[0];
  const bull = current?.bullishScore ?? 50, bear = current?.bearishScore ?? 50;
  const liveBlock = radar.mode === "shadow" && current?.activeBlock;
  return (
    <div className="content radar-page">
      <div className="radar-command">
        <div><span className={"radar-orb " + (radar.mode === "shadow" ? "on" : "off")} /><div><b>{radar.mode === "shadow" ? "风险雷达运行中" : "风险雷达已停止"}</b><span>每 15 分钟 · BTC / ETH · Shadow 模式</span></div></div>
        <button className={"btn " + (radar.mode === "shadow" ? "btn-stop" : "btn-go")} disabled={busy || !radar.workerAvailable || (radar.mode !== "shadow" && radar.connectionStatus !== "connected")} onClick={toggle}>{busy ? <><span className="spin" />处理中</> : radar.mode === "shadow" ? "停止雷达" : "启动雷达"}</button>
      </div>
      {error && <div className="radar-alert">{error}</div>}
      {!radar.workerAvailable && <div className="radar-alert">Observer 当前未在线；雷达不会产生新评估，启停与凭据命令会等待 Observer 处理。</div>}
      {radar.connectionStatus === "not_configured" && <div className="radar-alert">尚未配置 DeepSeek API Key。请前往「策略参数 → 安全与连接」完成加密保存。</div>}
      {radar.connectionStatus === "insufficient_balance" && <div className="radar-alert">DeepSeek 余额不可用；请充值后到「安全与连接」刷新余额。</div>}

      <div className="radar-hero">
        <div className="card radar-score-card">
          <div className="card-lbl">当前方向风险 · {current ? new Date(current.assessedForMs).toLocaleString() : "等待首次评估"}</div>
          <div className="radar-score-head"><div><span>空头压力</span><b className="down">{bear.toFixed(1)}</b></div><div className="radar-verdict">
            <span className={"tint " + (liveBlock ? "tint-red" : "tint-green")}>{liveBlock ? `拟拦截开${current.blockSide === "long" ? "多" : "空"}` : "未确认拦截"}</span>
            <small>{CONFIRM[current?.confirmationMode] || "等待双周期确认"}</small></div><div><span>多头压力</span><b className="up">{bull.toFixed(1)}</b></div></div>
          <div className="risk-split"><span style={{ width: bear + "%" }} /><i style={{ left: bear + "%" }} /><em style={{ width: bull + "%" }} /></div>
          <div className="risk-scale"><span>0</span><span>55</span><span>75 拟拦截线</span><span>100</span></div>
        </div>
        <div className="card radar-current">
          <div className="card-lbl">模型判断</div>
          <h3>{current?.regime || "暂无评估"}</h3>
          <p>{current?.reason || radar.lastError || "启动雷达并配置 DeepSeek 后生成第一条判断。"}</p>
          <div className="radar-meta"><span>置信度 <b>{current?.confidence != null ? current.confidence.toFixed(0) + "%" : "—"}</b></span><span>连接 <b>{radar.connectionStatus}</b></span><span>有效至 <b>{current?.validUntilMs ? new Date(current.validUntilMs).toLocaleTimeString() : "—"}</b></span></div>
        </div>
      </div>

      <div className="grid4 radar-kpis">
        <div className="card"><div className="card-lbl">真实开仓意图</div><div className="kpi">{radar.summary.intents}</div><div className="kpi-sub">仅记录通过原执行闸门的首开仓</div></div>
        <div className="card"><div className="card-lbl">AI 拟拦截</div><div className="kpi">{radar.summary.wouldBlock}</div><div className="kpi-sub">Shadow，不影响实际 Paper 下单</div></div>
        <div className="card"><div className="card-lbl">本可避免亏损</div><div className="kpi up">{radar.summary.avoidedLosses}</div><div className="kpi-sub">拟拦截且最终亏损</div></div>
        <div className="card"><div className="card-lbl">假设净改善</div><div className={"kpi " + cls(radar.summary.hypotheticalNetBenefit)}>{fSign(radar.summary.hypotheticalNetBenefit, 1)}</div><div className="kpi-sub">历史结算单的反事实总和</div></div>
      </div>

      <div className="section-h"><h2>Shadow 逐单对比</h2><span className="muted">开仓时冻结判断，整单最终平仓后结算一次</span></div>
      <div className="tbl-wrap"><table><thead><tr><th>时间</th><th>币种 / 方向</th><th>钱包</th><th className="num">风险分</th><th>Shadow 决策</th><th className="num">实际结果</th><th>归因</th></tr></thead><tbody>
        {!intents.length && <tr><td colSpan="7" className="empty">尚无雷达开启后的真实首开仓样本</td></tr>}
        {intents.map(i => { const displayPnl = i.netPnl != null ? i.netPnl : i.estimatedPnl; return <tr key={i.id}><td className="mono muted">{new Date(i.openedAt).toLocaleString()}</td><td><b>{i.coin}</b> <span className={"tint " + (i.side === "long" ? "tint-green" : "tint-red")}>{i.side === "long" ? "多" : "空"}</span></td><td className="addr">{short(i.wallet)}</td><td className="num mono">{i.riskScore != null ? i.riskScore.toFixed(1) : "—"}</td><td>{i.wouldBlock ? <span className="tint tint-red">AI 拟拦截 · {CONFIRM[i.confirmationMode] || i.confirmationMode}</span> : <span className="tint tint-gray">允许</span>}</td><td className={"num " + cls(displayPnl)}>{displayPnl != null ? fUsd(displayPnl) + (i.status === "open" ? " 估" : "") : "持仓中"}</td><td>{i.outcome ? <span className={"tint " + (i.outcome === "avoided_loss" ? "tint-green" : i.outcome === "missed_profit" ? "tint-red" : "tint-gray")}>{OUTCOME[i.outcome] || i.outcome}</span> : <span className="muted">待结算</span>}</td></tr>; })}
      </tbody></table></div>

      <div className="radar-bottom">
        <div><div className="section-h"><h2>15 分钟判断轨迹</h2></div><div className="card assessment-list">{(radar.assessments || []).slice(0, 10).map(a => <div className="assessment-row" key={a.id}><span className={"assessment-dot " + (a.activeBlock ? "hot" : a.status === "error" ? "err" : "")} /><div><b>{a.status === "error" ? "评估失败" : `${a.bearishScore?.toFixed(0)} 空 / ${a.bullishScore?.toFixed(0)} 多`}</b><span>{a.reason || a.error || "—"}</span></div><em>{new Date(a.assessedForMs).toLocaleTimeString()}</em></div>)}</div></div>
        <div><div className="section-h"><h2>阈值回放</h2></div><div className="card threshold-list">{thresholds.map(t => <div className="threshold-row" key={t.threshold}><b>{t.threshold}</b><div><span style={{ width: Math.min(100, t.wouldBlock * 8) + "%" }} /></div><em>{t.wouldBlock} 单 · {fSign(t.hypotheticalNetBenefit, 0)}</em></div>)}<p>仅作阈值敏感性参考；正式标签始终采用开仓当时的双周期确认结果。</p></div></div>
      </div>
    </div>
  );
}
