import { api } from "../lib/api.js";
import { cls, fUsd, short } from "../lib/format.js";

const { useCallback, useEffect, useState } = React;

const CONFIRM = { steady: "双次强信号", accelerating: "风险加速", extreme: "单次极端" };
const OUTCOME = { improved: "AI 改善", harmed: "AI 拖累", avoided_loss: "AI 改善", missed_profit: "AI 拖累", flat: "持平", allowed: "允许开仓" };
const signedUsd = (v, d = 1) => v == null ? "—" : (Number(v) >= 0 ? "+" : "−") + fUsd(Math.abs(Number(v)), d);

function Pager({ meta, onPage, note }) {
  const p = meta || { page: 0, total: 0, totalPages: 1 };
  return <div className="radar-pagination">
    <button type="button" aria-label="上一页" disabled={p.page <= 0} onClick={() => onPage(Math.max(0, p.page - 1))}>←</button>
    <span><b>{p.page + 1}</b> / {p.totalPages} 页 · 共 {p.total} 条</span>
    <button type="button" aria-label="下一页" disabled={p.page >= p.totalPages - 1} onClick={() => onPage(p.page + 1)}>→</button>
    {note && <em>{note}</em>}
  </div>;
}

export function RiskRadar() {
  const [radar, setRadar] = useState(null);
  const [assessmentPage, setAssessmentPage] = useState(0);
  const [episodePage, setEpisodePage] = useState(0);
  const [intents, setIntents] = useState([]);
  const [episodePager, setEpisodePager] = useState(null);
  const [thresholds, setThresholds] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const load = useCallback(async () => {
    const [r, i, t] = await Promise.all([
      api.get(`/api/risk-radar?assessmentPage=${assessmentPage}&assessmentSize=10`),
      api.get(`/api/risk-radar/intents?page=${episodePage}&size=5&affectedOnly=1`),
      api.get("/api/risk-radar/thresholds"),
    ]);
    setRadar(r);
    setAssessmentPage(r.assessmentPagination?.page || 0);
    setIntents(i.intents || []);
    setEpisodePager(i.pagination || null);
    setEpisodePage(i.pagination?.page || 0);
    setThresholds(t.comparison || []);
  }, [assessmentPage, episodePage]);
  useEffect(() => {
    load().catch(() => setError("雷达数据加载失败"));
    const id = setInterval(() => load().catch(() => {}), 10000);
    return () => clearInterval(id);
  }, [load]);
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
  const verdict = liveBlock ? `拟拦截开${current.blockSide === "long" ? "多" : "空"}` : "未确认拦截";

  return (
    <div className="content radar-page">
      <div className="radar-command">
        <div className="radar-command-status">
          <span className={"radar-orb " + (radar.mode === "shadow" ? "on" : "off")} />
          <div><b>{radar.mode === "shadow" ? "风险雷达运行中" : "风险雷达已停止"}</b><span>15 分钟 · BTC / ETH · Shadow</span></div>
        </div>
        <div className="radar-command-risk">
          <div className="radar-command-scores">
            <span className="down">空 <b>{bear.toFixed(0)}</b></span>
            <strong className={liveBlock ? "down" : "up"}>{verdict}</strong>
            <span className="up">多 <b>{bull.toFixed(0)}</b></span>
          </div>
          <div className="risk-split"><span style={{ width: bear + "%" }} /><i style={{ left: bear + "%" }} /><em style={{ width: bull + "%" }} /></div>
          <small>{CONFIRM[current?.confirmationMode] || "等待双周期确认"} · {current ? new Date(current.assessedForMs).toLocaleTimeString() : "等待首次评估"}</small>
        </div>
        <button className={"btn " + (radar.mode === "shadow" ? "btn-stop" : "btn-go")} disabled={busy || !radar.workerAvailable || (radar.mode !== "shadow" && radar.connectionStatus !== "connected")} onClick={toggle}>{busy ? <><span className="spin" />处理中</> : radar.mode === "shadow" ? "停止雷达" : "启动雷达"}</button>
      </div>
      {error && <div className="radar-alert">{error}</div>}
      {!radar.workerAvailable && <div className="radar-alert">Observer 当前未在线；雷达不会产生新评估，启停与凭据命令会等待 Observer 处理。</div>}
      {radar.connectionStatus === "not_configured" && <div className="radar-alert">尚未配置 DeepSeek API Key。请前往「策略参数 → 安全与连接」完成加密保存。</div>}
      {radar.connectionStatus === "insufficient_balance" && <div className="radar-alert">DeepSeek 余额不可用；请充值后到「安全与连接」刷新余额。</div>}

      <div className="grid4 radar-kpis">
        <div className="card"><div className="card-lbl">逐次敞口动作</div><div className="kpi">{actionCount}</div><div className="kpi-sub">首仓与每笔加仓分别冻结判断</div></div>
        <div className="card"><div className="card-lbl">AI 过滤 / 延迟入场</div><div className="kpi">{summary.blockedEntries || 0} <small>/ {summary.delayedEntries || 0}</small></div><div className="kpi-sub">拦截不会终止后续加仓观察</div></div>
        <div className="card"><div className="card-lbl">改善 / 拖累</div><div className="kpi"><span className="up">{summary.improvedEpisodes || 0}</span> <small>/</small> <span className="down">{summary.harmedEpisodes || 0}</span></div><div className="kpi-sub">仅统计已结算的动作级 episode</div></div>
        <div className={"card radar-impact " + (benefit < 0 ? "harm" : benefit > 0 ? "protect" : "flat")}><div className="card-lbl">{impactTitle}</div><div className={"kpi " + cls(benefit)}>{signedUsd(benefit)}</div><div className="kpi-sub">{impactNote}</div></div>
      </div>

      <div className="radar-analysis-grid">
        <div className="card radar-current">
          <div className="card-lbl">模型判断</div>
          <h3>{current?.regime || "暂无评估"}</h3>
          <p>{current?.reason || radar.lastError || "启动雷达并配置 DeepSeek 后生成第一条判断。"}</p>
          <div className="radar-meta"><span>置信度 <b>{current?.confidence != null ? current.confidence.toFixed(0) + "%" : "—"}</b></span><span>有效至 <b>{current?.validUntilMs ? new Date(current.validUntilMs).toLocaleTimeString() : "—"}</b></span></div>
        </div>
        <div className="card threshold-list">
          <div className="card-lbl">阈值回放</div>
          <div className="threshold-rows">{thresholds.map(t => <div className="threshold-row" key={t.threshold}><b>{t.threshold}</b><div><span style={{ width: Math.min(100, t.wouldBlock * 8) + "%" }} /></div><em className={cls(t.hypotheticalNetBenefit)}>{t.wouldBlock} 动作 · {signedUsd(t.hypotheticalNetBenefit, 0)}</em></div>)}</div>
        </div>
      </div>

      <div className="section-h"><h2>Shadow 影响记录</h2><span className="muted">仅展示发生拦截、延迟入场或净影响的单子 · 每页 5 条</span></div>
      <div className="card shadow-list">
        {!intents.length && <div className="empty">尚无产生实际差异的 Shadow 记录</div>}
        {intents.map(i => {
          const s = i.shadow;
          const baseline = s?.baselineNetPnl ?? (i.netPnl != null ? i.netPnl : i.estimatedPnl);
          const aiPnl = s?.shadowNetPnl;
          const delta = s?.netBenefit;
          const decision = i.wouldBlock ? "首仓拦截" : s?.delayedEntry ? "延迟入场" : "首仓放行";
          const decisionClass = i.wouldBlock ? "down" : s?.delayedEntry ? "delay" : "up";
          const result = i.outcome ? (OUTCOME[i.outcome] || i.outcome) : "进行中";
          return <div className="shadow-row" key={i.id}>
            <div className="shadow-market"><span className={"tint " + (i.side === "long" ? "tint-green" : "tint-red")}>{i.side === "long" ? "多" : "空"}</span><div><b>{i.coin}</b><span>{short(i.wallet)}</span></div></div>
            <div className="shadow-decision"><b className={decisionClass}>{decision}</b><span>风险 {i.riskScore?.toFixed(0) ?? "—"} · 过滤 {s?.blockedEntries || 0} / 放行 {s?.allowedEntries || 0}</span></div>
            <div className="shadow-metric"><span>基准</span><b className={cls(baseline)}>{baseline == null ? "持仓中" : signedUsd(baseline)}{s?.estimated ? " 估" : ""}</b></div>
            <div className="shadow-metric"><span>AI Shadow</span><b className={cls(aiPnl)}>{aiPnl == null ? "—" : signedUsd(aiPnl)}{s?.estimated ? " 估" : ""}</b></div>
            <div className="shadow-metric"><span>净影响 · {result}</span><b className={cls(delta)}>{delta == null ? "待结算" : signedUsd(delta)}{s?.estimated ? " 估" : ""}</b></div>
            <time dateTime={i.openedAt}>{new Date(i.openedAt).toLocaleString()}</time>
          </div>;
        })}
        <Pager meta={episodePager || { page: 0, total: intents.length, totalPages: 1 }} onPage={setEpisodePage} />
      </div>

      <div className="section-h"><h2>15 分钟判断轨迹</h2><span className="muted">红点 = 拟拦截 · 绿点 = 放行 · 每页 10 条</span></div>
      <div className="card assessment-list">
        {(radar.assessments || []).map(a => <div className="assessment-row" key={a.id}>
          <span className={"assessment-dot " + (a.activeBlock ? "hot" : a.status === "error" ? "err" : "")} />
          <b>{a.status === "error" ? "评估失败" : `${a.bearishScore?.toFixed(0)} 空 / ${a.bullishScore?.toFixed(0)} 多`}</b>
          <p>{a.reason || a.error || "—"}</p>
          <em>{new Date(a.assessedForMs).toLocaleTimeString()}</em>
        </div>)}
        {!radar.assessments?.length && <div className="empty">暂无判断记录</div>}
        <Pager meta={assessmentPager} onPage={setAssessmentPage} note={`数据库保留最近 ${assessmentPager.retentionLimit?.toLocaleString()} 条`} />
      </div>
    </div>
  );
}
