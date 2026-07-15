import { api } from "../lib/api.js";
import { cls, fUsd, short } from "../lib/format.js";

const { useCallback, useEffect, useState } = React;

const CONFIRM = { steady: "双次强信号", accelerating: "风险加速", extreme: "单次极端" };
const OUTCOME = { improved: "AI 改善", harmed: "AI 拖累", avoided_loss: "AI 改善", missed_profit: "AI 拖累", flat: "持平", allowed: "允许开仓" };
const DECISION = {
  blocked_open: ["首仓拦截", "block"], blocked_add: ["加仓拦截", "block"],
  allowed_open: ["首仓放行", "allow"], allowed_add: ["加仓放行", "allow"],
  delayed_entry: ["延迟入场", "delay"], radar_unavailable_allow: ["失效放行", "muted"],
  mandatory_exit: ["退出执行", "exit"],
};
const signedUsd = (v, d = 1) => v == null ? "—" : (Number(v) >= 0 ? "+" : "−") + fUsd(Math.abs(Number(v)), d);

export function RiskRadar() {
  const [radar, setRadar] = useState(null);
  const [assessmentPage, setAssessmentPage] = useState(0);
  const [intents, setIntents] = useState([]);
  const [thresholds, setThresholds] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const load = useCallback(async () => {
    const [r, i, t] = await Promise.all([api.get(`/api/risk-radar?assessmentPage=${assessmentPage}&assessmentSize=10`), api.get("/api/risk-radar/intents?limit=80"), api.get("/api/risk-radar/thresholds")]);
    setRadar(r); setAssessmentPage(r.assessmentPagination?.page || 0); setIntents(i.intents || []); setThresholds(t.comparison || []);
  }, [assessmentPage]);
  useEffect(() => { load().catch(() => setError("雷达数据加载失败")); const id = setInterval(() => load().catch(() => {}), 10000); return () => clearInterval(id); }, [load]);
  const toggle = async () => {
    setBusy(true); setError(null);
    try { await api.cmdAndWait(radar.mode === "shadow" ? "risk_radar_stop" : "risk_radar_start", {}); await load(); }
    catch (_e) { setError("启停失败：请确认 Observer 正在运行且 DeepSeek 已配置"); }
    finally { setBusy(false); }
  };
  if (!radar) return <div className="content"><div className="loading">加载中…</div></div>;
  const current = radar.currentAssessment || radar.assessments?.find(a => a.id === radar.currentAssessmentId) || radar.assessments?.[0];
  const bull = current?.bullishScore ?? 50, bear = current?.bearishScore ?? 50;
  const liveBlock = radar.mode === "shadow" && current?.activeBlock;
  const summary = radar.summary || {};
  const benefit = summary.hypotheticalNetBenefit || 0;
  const actionCount = (summary.blockedEntries || 0) + (summary.allowedEntries || 0);
  const impactTitle = benefit > 0 ? "AI 净保护" : benefit < 0 ? "AI 净伤害" : "AI 净影响";
  const impactNote = benefit > 0 ? "过滤动作后收益优于基准" : benefit < 0 ? "过滤动作后收益低于基准" : "已结算样本暂无净差异";
  const assessmentPager = radar.assessmentPagination || { page: 0, total: radar.assessments?.length || 0, totalPages: 1, retentionLimit: 0 };
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
        <div className="card"><div className="card-lbl">逐次敞口动作</div><div className="kpi">{actionCount}</div><div className="kpi-sub">首仓与每笔加仓分别冻结判断</div></div>
        <div className="card"><div className="card-lbl">AI 过滤 / 延迟入场</div><div className="kpi">{summary.blockedEntries || 0} <small>/ {summary.delayedEntries || 0}</small></div><div className="kpi-sub">拦截不会终止后续加仓观察</div></div>
        <div className="card"><div className="card-lbl">改善 / 拖累</div><div className="kpi"><span className="up">{summary.improvedEpisodes || 0}</span> <small>/</small> <span className="down">{summary.harmedEpisodes || 0}</span></div><div className="kpi-sub">仅统计已结算的动作级 episode</div></div>
        <div className={"card radar-impact " + (benefit < 0 ? "harm" : benefit > 0 ? "protect" : "flat")}><div className="card-lbl">{impactTitle}</div><div className={"kpi " + cls(benefit)}>{signedUsd(benefit)}</div><div className="kpi-sub">{impactNote}</div></div>
      </div>

      <div className="radar-ledgers">
        <div><span>基准 Paper 账本</span><b className={cls(summary.baselinePnl)}>{summary.baselinePnl == null ? "等待 V2 结算" : signedUsd(summary.baselinePnl)}</b></div>
        <i>→</i>
        <div><span>AI 动作过滤账本</span><b className={cls(summary.shadowPnl)}>{summary.shadowPnl == null ? "等待 V2 结算" : signedUsd(summary.shadowPnl)}</b></div>
        <i>=</i>
        <div><span>净影响 · {summary.resolvedEpisodes || 0} 单</span><b className={cls(benefit)}>{signedUsd(benefit)}</b></div>
      </div>

      <div className="section-h"><h2>Shadow 动作轨迹</h2><span className="muted">只过滤增加敞口；减仓和平仓永远执行</span></div>
      <div className="shadow-episodes">
        {!intents.length && <div className="card empty">尚无雷达开启后的 Crypto 敞口动作</div>}
        {intents.map(i => { const s = i.shadow; const baseline = s?.baselineNetPnl ?? (i.netPnl != null ? i.netPnl : i.estimatedPnl); const aiPnl = s?.shadowNetPnl; const delta = s?.netBenefit; return <article className="card shadow-episode" key={i.id}>
          <div className="episode-head"><div><span className={"tint " + (i.side === "long" ? "tint-green" : "tint-red")}>{i.side === "long" ? "多" : "空"}</span><b>{i.coin}</b><span className="addr">{short(i.wallet)}</span>{s?.delayedEntry && <span className="tint tint-blue">AI 延迟入场</span>}</div><time dateTime={i.openedAt}>{new Date(i.openedAt).toLocaleString()}</time></div>
          <div className="episode-ledger"><div><span>基准</span><b className={cls(baseline)}>{baseline == null ? "持仓中" : signedUsd(baseline)}{s?.estimated ? " 估" : ""}</b></div><div><span>AI Shadow</span><b className={cls(aiPnl)}>{aiPnl == null ? "—" : signedUsd(aiPnl)}{s?.estimated ? " 估" : ""}</b></div><div><span>净影响</span><b className={cls(delta)}>{delta == null ? "待结算" : signedUsd(delta)}{s?.estimated ? " 估" : ""}</b></div><div><span>结果</span>{i.outcome ? <b className={i.outcome === "improved" || i.outcome === "avoided_loss" ? "up" : i.outcome === "harmed" || i.outcome === "missed_profit" ? "down" : ""}>{OUTCOME[i.outcome] || i.outcome}</b> : <b className="muted">进行中</b>}</div></div>
          <div className="action-rail">{(i.actions || []).length ? i.actions.map(a => { const d = DECISION[a.decision] || [a.decision, "muted"]; return <div className={"risk-action " + d[1]} key={a.id}><i /><div><b>{d[0]}</b><span>{a.action === "open" ? "首仓" : a.action === "add" ? "加仓" : a.action === "reduce" ? "减仓" : "平仓"} @ {a.baselinePx ? Number(a.baselinePx).toLocaleString() : "—"}</span></div><em>{a.riskScore != null ? `风险 ${a.riskScore.toFixed(0)}` : "强制退出"}</em></div>; }) : <span className="muted">旧版整单样本，无动作轨迹</span>}</div>
        </article>; })}
      </div>

      <div className="radar-bottom">
        <div><div className="section-h"><h2>15 分钟判断轨迹</h2><span className="muted">每页 10 条</span></div><div className="card assessment-list">{(radar.assessments || []).map(a => <div className="assessment-row" key={a.id}><span className={"assessment-dot " + (a.activeBlock ? "hot" : a.status === "error" ? "err" : "")} /><div><b>{a.status === "error" ? "评估失败" : `${a.bearishScore?.toFixed(0)} 空 / ${a.bullishScore?.toFixed(0)} 多`}</b><span>{a.reason || a.error || "—"}</span></div><em>{new Date(a.assessedForMs).toLocaleTimeString()}</em></div>)}{!radar.assessments?.length && <div className="empty">暂无判断记录</div>}<div className="radar-pagination"><button type="button" aria-label="上一页" disabled={assessmentPager.page <= 0} onClick={() => setAssessmentPage(p => Math.max(0, p - 1))}>←</button><span><b>{assessmentPager.page + 1}</b> / {assessmentPager.totalPages} 页 · 共 {assessmentPager.total} 条</span><button type="button" aria-label="下一页" disabled={assessmentPager.page >= assessmentPager.totalPages - 1} onClick={() => setAssessmentPage(p => p + 1)}>→</button><em>数据库保留最近 {assessmentPager.retentionLimit?.toLocaleString()} 条</em></div></div></div>
        <div><div className="section-h"><h2>阈值回放</h2></div><div className="card threshold-list">{thresholds.map(t => <div className="threshold-row" key={t.threshold}><b>{t.threshold}</b><div><span style={{ width: Math.min(100, t.wouldBlock * 8) + "%" }} /></div><em className={cls(t.hypotheticalNetBenefit)}>{t.wouldBlock} 动作 · {signedUsd(t.hypotheticalNetBenefit, 0)}</em></div>)}<p>逐动作重放不同阈值；正式标签仍采用动作发生时冻结的双周期确认结果。</p></div></div>
      </div>
    </div>
  );
}
