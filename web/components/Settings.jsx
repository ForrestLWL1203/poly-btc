import { api } from "../lib/api.js";
import { fParam } from "../lib/format.js";
import { CoinBlacklistEditor } from "./settings/CoinBlacklistEditor.jsx";
import { EditableValue } from "./settings/EditableValue.jsx";
import { SizingPreview } from "./settings/SizingPreview.jsx";
import {
  ADD_KEYS,
  AUTO_TUNE_KEY,
  BLACKLIST_KEY,
  PARAM_META,
  TIER_GROUPS,
  UNIT,
} from "./settings/paramMeta.js";

const { useState, useEffect } = React;

export function Settings({ startRescan, confirm }) {
  const [params, setParams] = useState(null);
  const [tab, setTab] = useState("scanner");
  const [vals, setVals] = useState({});
  const [dirty, setDirty] = useState({});
  const [saving, setSaving] = useState(false);                    // 保存时的短暂全页 loading(替代右上角 toast)
  const [openTiers, setOpenTiers] = useState({});                 // 档位折叠(默认全部收起)
  const [scoreDist, setScoreDist] = useState(null);               // watchlist 全体显示分(0-100),供跟单线实时计数

  const loadParams = async () => {
    const p = await api.get("/api/params?includeScoreDist=1");
    setParams(p);
    const v = {};
    [...p.scanner, ...p.follow].forEach(x => { v[x.key] = x.value; });
    setVals(v);
    if (p.scoreDist) setScoreDist(p.scoreDist);
  };

  useEffect(() => {
    loadParams().catch(() => {
      api.get("/api/params").then(p => {
        setParams(p);
        const v = {}; [...p.scanner, ...p.follow].forEach(x => { v[x.key] = x.value; });
        setVals(v);
      }).catch(() => {});
      api.get("/api/score-dist").then(setScoreDist).catch(() => {});
    });
  }, []);

  if (!params) return <div className="content"><div className="loading">加载中…</div></div>;
  //  (单币上限 STABLE/MID/HIGH_COIN_CAP_PCT 已挪回「跟单策略 · σ分档」—— 它是全局灾难闸,管开仓+加仓,不是加仓专属)
  const list = tab === "add" ? params.follow.filter(p => ADD_KEYS.has(p.key)) : params[tab];
  const editable = (p) => !(p.type === "display" || p.level === "black");
  const set = (key, val) => { setVals(v => ({ ...v, [key]: val })); setDirty(dd => ({ ...dd, [key]: true })); };
  const tabDirty = list.filter(p => dirty[p.key]);
  const autoTuneParam = tab === "follow" ? list.find(p => p.key === AUTO_TUNE_KEY) : null;
  const blacklistParam = tab === "follow" ? list.find(p => p.key === BLACKLIST_KEY) : null;
  const byKey = k => list.find(p => p.key === k);

  const Prow = (p) => {
    const m = PARAM_META[p.key] || {}; const ed = editable(p); const lvl = p.level;
    return (
      <div key={p.key}>
        <div className={"prow" + (dirty[p.key] ? " dirty" : "")}>
          <span className="lvl-dot lvl-green" />
          <div className="pn"><b>{p.name || m.name || p.key}</b></div>
          <div className="pd">{p.desc || m.desc}{m.range && m.range !== "—" && <span style={{ color: "var(--t4)" }}> · 建议 {m.range}</span>}</div>
          <div className="pctl">
            {p.type === "bool" ? (
              <div className={"toggle " + (vals[p.key] ? "on" : "")} onClick={() => ed && set(p.key, !vals[p.key])} style={{ opacity: ed ? 1 : .5 }}><div className="knob" /></div>
            ) : p.type === "display" ? (
              <span className="mono" style={{ color: "var(--t2)", fontSize: 12 }}>{p.value}</span>
            ) : (
              <EditableValue value={vals[p.key]} unit={UNIT[p.type] || ""} ptype={p.type}
                disabled={!ed} onCommit={v => set(p.key, v)} />
            )}
            {(lvl === "black" || p.type === "display") && <span className="plock">只读</span>}
          </div>
        </div>
      </div>
    );
  };
  const tierKeys = new Set(TIER_GROUPS.flatMap(g => [g.min, g.max, g.lev, g.notl, g.cap]));
  const deployKeys = new Set(["DEPLOY_FULL_PCT", "MAX_DEPLOY_PCT"]);
  const validationBadKeys = new Set();
  const validationErrors = [];
  const numVal = k => Number(vals[k]);
  const markErr = (msg, keys) => {
    validationErrors.push(msg);
    (keys || []).forEach(k => validationBadKeys.add(k));
  };
  const validatePct = (label, key) => {
    const v = numVal(key);
    if (!Number.isFinite(v)) { markErr(`${label} 必须是数字`, [key]); return false; }
    if (v < 0 || v > 100) { markErr(`${label} 必须在 0–100% 之间`, [key]); return false; }
    return true;
  };
  if (tab === "follow") {
    TIER_GROUPS.forEach(g => {
      const okMin = validatePct(`${g.label}保证金下限`, g.min);
      const okMax = validatePct(`${g.label}保证金上限`, g.max);
      if (okMin && okMax && numVal(g.min) > numVal(g.max)) {
        markErr(`${g.label}保证金下限不能高于上限`, [g.min, g.max]);
      }
    });
    const okFull = validatePct("满火力占用线", "DEPLOY_FULL_PCT");
    const okLock = validatePct("组合部署上限", "MAX_DEPLOY_PCT");
    if (okFull && okLock && numVal("DEPLOY_FULL_PCT") >= numVal("MAX_DEPLOY_PCT")) {
      markErr("满火力占用线必须低于组合部署上限", ["DEPLOY_FULL_PCT", "MAX_DEPLOY_PCT"]);
    }
  }

  const RangeRow = (g) => {
    const pMin = byKey(g.min), pMax = byKey(g.max);
    if (!pMin || !pMax) return null;
    return (
      <div className={"prow range-row" + (dirty[g.min] || dirty[g.max] ? " dirty" : "") + (validationBadKeys.has(g.min) || validationBadKeys.has(g.max) ? " invalid" : "")}>
        <span className="lvl-dot lvl-green" />
        <div className="pn"><b>{g.label}·保证金区间</b></div>
        <div className="pd">低占用用上限,拥挤时线性缩到下限。自动调参只改上限</div>
        <div className="range-ctl">
          <EditableValue value={vals[g.min]} unit="%" ptype="pct" disabled={!editable(pMin)} onCommit={v => set(g.min, v)} />
          <span>至</span>
          <EditableValue value={vals[g.max]} unit="%" ptype="pct" disabled={!editable(pMax)} onCommit={v => set(g.max, v)} />
        </div>
      </div>
    );
  };

  const DeployRangeRow = () => {
    const pFull = byKey("DEPLOY_FULL_PCT"), pLock = byKey("MAX_DEPLOY_PCT");
    if (!pFull || !pLock) return null;
    return (
      <div className={"prow range-row deploy-row" + (dirty.DEPLOY_FULL_PCT || dirty.MAX_DEPLOY_PCT ? " dirty" : "") + (validationBadKeys.has("DEPLOY_FULL_PCT") || validationBadKeys.has("MAX_DEPLOY_PCT") ? " invalid" : "")}>
        <span className="lvl-dot lvl-green" />
        <div className="pn"><b>组合火力区间</b></div>
        <div className="pd">占用≤左值满火力;左值到右值线性缩仓;≥右值停开新仓,保留资金给加仓/平仓管理</div>
        <div className="range-ctl">
          <EditableValue value={vals.DEPLOY_FULL_PCT} unit="%" ptype="pct" disabled={!editable(pFull)} onCommit={v => set("DEPLOY_FULL_PCT", v)} />
          <span>至</span>
          <EditableValue value={vals.MAX_DEPLOY_PCT} unit="%" ptype="pct" disabled={!editable(pLock)} onCommit={v => set("MAX_DEPLOY_PCT", v)} />
        </div>
      </div>
    );
  };

  const apply = async () => {
    if (validationErrors.length) return;
    const body = {}; tabDirty.forEach(p => { body[p.key] = vals[p.key]; });
    const doIt = async () => {
      setSaving(true);                                  // 短暂全页 loading 代替右上角 tooltip
      const t0 = Date.now();
      const cat = tab === "add" ? "follow" : tab;              // 加仓参数在后端属 follow 类
      try { await api.patchParams(cat, body); } catch (_e) {}
      setDirty({});
      if (tab === "follow" || tab === "add") { try { await api.cmd("reload_params", {}); } catch (_e) {} }  // observer ~1.5s 内生效
      await new Promise(r => setTimeout(r, Math.max(0, 450 - (Date.now() - t0))));   // 让 loading 可感知
      setSaving(false);
      if (tab === "scanner") startRescan();             // 重采有自己的整页遮罩接管
    };
    if (tab === "scanner") confirm({ title: "应用并重采", danger: false, ok: "应用并重采", body: "采集参数改动需重采才生效,将立即触发全量重采。", onConfirm: doIt });
    else if (tabDirty.some(p => p.level === "yellow")) confirm({ title: "保存跟单参数", danger: false, ok: "保存",
      body: "包含谨慎级参数(影响每一笔新仓),确认即时生效?", onConfirm: doIt });
    else doIt();
  };

  // 恢复默认配置:把当前页所属类别(scanner / follow — add 属 follow)全部参数强制写回代码默认值,覆盖操作员修改。
  const resetDefaults = () => {
    const cat = tab === "add" ? "follow" : tab;
    const label = cat === "scanner" ? "钱包采集" : "跟单策略(含加仓)";
    confirm({
      title: "恢复默认配置", danger: true, ok: "恢复默认",
      body: `将把「${label}」全部参数强制恢复为代码默认值,覆盖你在此页的所有修改。不可撤销。`,
      onConfirm: async () => {
        setSaving(true);
        const t0 = Date.now();
        try { await fetch("/api/params/" + cat + "/reset", { method: "POST", headers: { Authorization: "Bearer " + api.token } }); } catch (_e) {}
        try { await loadParams(); setDirty({}); } catch (_e) {}   // 重取,把重置后的值刷回界面
        if (cat === "follow") { try { await api.cmd("reload_params", {}); } catch (_e) {} }   // observer ~1.5s 内生效
        await new Promise(r => setTimeout(r, Math.max(0, 450 - (Date.now() - t0))));
        setSaving(false);
        if (cat === "scanner") startRescan();                     // 采集默认值需重采才生效(重采有自己的整页遮罩)
      },
    });
  };

  return (
    <div className="content">
      {saving && <div className="mask"><span className="spin" style={{ width: 34, height: 34, borderWidth: 3 }} /><h2 style={{ marginTop: 22 }}>保存中…</h2></div>}
      <div className="tabs">
        <div className={"tab" + (tab === "scanner" ? " on" : "")} onClick={() => setTab("scanner")}>钱包采集参数</div>
        <div className={"tab" + (tab === "follow" ? " on" : "")} onClick={() => setTab("follow")}>跟单策略参数</div>
        <div className={"tab" + (tab === "add" ? " on" : "")} onClick={() => setTab("add")}>加仓策略</div>
        <button className="btn" title="把本页参数强制恢复为代码默认值" onClick={resetDefaults}
          style={{ marginLeft: "auto", alignSelf: "center", fontSize: 12, padding: "4px 12px" }}>↺ 恢复默认</button>
      </div>

      {tab === "follow" && <SizingPreview vals={vals} />}

      <div className="tbl-wrap">
        {tab === "add" && (() => {
          const bk = k => list.find(p => p.key === k);
          const smart = !!vals.SMART_ADD, bOpen = openTiers.B === undefined ? true : openTiers.B;
          const secLbl = t => <div className="muted" style={{ fontSize: 11, padding: "8px 0 2px", fontWeight: 600, color: "var(--t2)" }}>{t}</div>;
          return <React.Fragment>
            <div className="psec-h">加仓策略 · 独立于跟单/采集<span>目标加仓时:我们是否跟、跟多少、跟几次。逆向摊低是重点。</span></div>
            <div>
              <div className={"expand-head" + (openTiers.A ? " open" : "")} onClick={() => setOpenTiers(o => ({ ...o, A: !o.A }))}>
                <span style={{ color: "var(--t3)", width: 12 }}>{openTiers.A ? "▾" : "▸"}</span>
                <span className="pill tint-green">A · 正向加仓</span>
                <span className="muted" style={{ fontSize: 12 }}>盈利单顺势加仓、拉高成本追更大利润</span>
                {!openTiers.A && <span className="muted" style={{ marginLeft: "auto", fontSize: 11 }}>{vals.FOLLOW_POS_ADD ? "跟随" : "不跟(默认)"}</span>}
              </div>
              {openTiers.A && <div className="expand-body">
                {[bk("FOLLOW_POS_ADD")].filter(Boolean).map(Prow)}
                <div className="muted" style={{ fontSize: 11, padding: "2px 0 6px" }}>正向较简单:开启后按「比例镜像 + 硬顶 + 三档预算」跟,不用波动闸。</div>
              </div>}
            </div>
            <div>
              <div className={"expand-head" + (bOpen ? " open" : "")} onClick={() => setOpenTiers(o => ({ ...o, B: !(o.B === undefined ? true : o.B) }))}>
                <span style={{ color: "var(--t3)", width: 12 }}>{bOpen ? "▾" : "▸"}</span>
                <span className="pill tint-red">B · 逆向加仓(摊低)</span>
                <span className="muted" style={{ fontSize: 12 }}>目标逆势摊低成本 —— 我们如何跟(二选一)</span>
                {!bOpen && <span className="muted" style={{ marginLeft: "auto", fontSize: 11 }}>{smart ? "② 智能动态" : "① 分档硬cap"}</span>}
              </div>
              {bOpen && <div className="expand-body">
                {[bk("SMART_ADD")].filter(Boolean).map(Prow)}
                {smart ? <React.Fragment>
                  {secLbl("② 智能动态(σ波动闸 + 比例镜像)")}
                  {["ADD_GAP_K", "ADD_GAP_SHRINK_G", "ADD_MAX_HARD"].map(bk).filter(Boolean).map(Prow)}
                  <div className="muted" style={{ fontSize: 11, padding: "4px 0 6px" }}>加仓额封顶到该币「单币上限」剩余预算 —— 该上限是全局灾难闸,在「跟单策略参数 · 保证金与杠杆 σ分档」里调。</div>
                </React.Fragment> : <React.Fragment>
                  {secLbl("① 分档硬cap(固定次数 + 固定比例)")}
                  {["ADD_FRAC", "STABLE_MAX_ADDS", "MID_MAX_ADDS", "HIGH_MAX_ADDS"].map(bk).filter(Boolean).map(Prow)}
                </React.Fragment>}
              </div>}
            </div>
          </React.Fragment>;
        })()}
        {tab !== "add" && list.filter(p => !(tab === "follow" && (tierKeys.has(p.key) || deployKeys.has(p.key) || ADD_KEYS.has(p.key) || p.key === AUTO_TUNE_KEY || p.key === BLACKLIST_KEY))).map(p => {
          if (tab === "follow" && p.key === "MIN_FOLLOW_SCORE") {
            const v = Number(vals.MIN_FOLLOW_SCORE);
            const n = scoreDist ? scoreDist.scores.filter(s => s >= v).length : null;
            return (
              <React.Fragment key={p.key}>
                {Prow(p)}
                <div className="score-hint">
                  {n == null ? "加载钱包分布…" : <React.Fragment>
                    评分 ≥ <b>{isFinite(v) ? v : "—"}</b> 时,当前 watchlist 有 <b style={{ color: "var(--accent)" }}>{n}</b> 个钱包达标会被跟单
                    <span className="muted"> / 共 {scoreDist.total} 个候选</span></React.Fragment>}
                </div>
                {blacklistParam && <CoinBlacklistEditor key={blacklistParam.key} param={blacklistParam}
                  value={vals[BLACKLIST_KEY]} dirty={!!dirty[BLACKLIST_KEY]} disabled={!editable(blacklistParam)}
                  onCommit={v => set(BLACKLIST_KEY, v)} />}
              </React.Fragment>
            );
          }
          return Prow(p);
        })}
        {tab === "follow" && <div className="psec-h psec-h-row">
          <div className="psec-title-block">保证金与杠杆 · 按波动率 σ 分档
            <span>杠杆 = σ 所在档位的上限(σ 定档),这里设各档的单笔保证金% 与杠杆上限</span></div>
          {autoTuneParam && <div className={"psec-switch" + (dirty[AUTO_TUNE_KEY] ? " dirty" : "")} title={autoTuneParam.desc}>
            <span>自动调保证金</span>
            <div className={"toggle " + (vals[AUTO_TUNE_KEY] ? "on" : "")}
              onClick={() => editable(autoTuneParam) && set(AUTO_TUNE_KEY, !vals[AUTO_TUNE_KEY])}
              style={{ opacity: editable(autoTuneParam) ? 1 : .5 }}><div className="knob" /></div>
          </div>}
        </div>}
        {tab === "follow" && DeployRangeRow()}
        {tab === "follow" && validationErrors.length > 0 && (
          <div className="param-errors">
            {validationErrors.map((e, i) => <div key={i}>{e}</div>)}
          </div>
        )}
        {tab === "follow" && TIER_GROUPS.map(g => {
          const open = openTiers[g.key];
          const rows = [g.lev, g.notl, g.cap].map(byKey).filter(Boolean);
          return (
            <div key={g.key}>
              <div className={"expand-head" + (open ? " open" : "")} onClick={() => setOpenTiers(o => ({ ...o, [g.key]: !o[g.key] }))}>
                <span style={{ color: "var(--t3)", width: 12 }}>{open ? "▾" : "▸"}</span>
                <span className={"pill " + g.tint}>{g.label}</span>
                <span className="muted" style={{ fontSize: 12 }}>{g.sub}</span>
                {!open && <span className="muted" style={{ marginLeft: "auto", fontSize: 11 }}>
                  保证金 {fParam(vals[g.min], "pct")}–{fParam(vals[g.max], "pct")}% · 杠杆 ≤{fParam(vals[g.lev], "x")}x · 最低 ${fParam(vals[g.notl], "usd")} · 单币上限 {fParam(vals[g.cap], "pct")}%</span>}
              </div>
              {open && <div className="expand-body">
                {RangeRow(g)}
                {rows.map(Prow)}
              </div>}
            </div>
          );
        })}
      </div>

      {tabDirty.length > 0 && (
        <div className="apply-bar">
          <div className="ab-l">{tabDirty.length} 项未应用改动{tab === "scanner" ? "(需重采生效)" : "(即时生效)"}</div>
          <div style={{ display: "flex", gap: 10 }}>
            <button className="btn" onClick={() => { setVals(v => { const nv = { ...v }; const o = {}; [...params.scanner, ...params.follow].forEach(x => o[x.key] = x.value); tabDirty.forEach(p => nv[p.key] = o[p.key]); return nv; }); setDirty({}); }}>放弃</button>
            <button className="btn btn-accent" disabled={validationErrors.length > 0} onClick={apply}>{tab === "scanner" ? "应用并重采" : "保存(即时生效)"}</button>
          </div>
        </div>
      )}
    </div>
  );
}
