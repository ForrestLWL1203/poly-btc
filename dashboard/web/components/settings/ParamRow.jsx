import { EditableValue } from "./EditableValue.jsx";
import { PARAM_META, UNIT } from "./paramMeta.js";

export const editableParam = (p) => !(p.type === "display" || p.level === "black");

export const LEVEL_META = {
  green: { label: "安全", title: "可直接编辑", rank: 1 },
  yellow: { label: "谨慎", title: "影响资金或扫描结果,保存前会确认", rank: 3 },
  blue: { label: "高级", title: "高级/诊断参数", rank: 2 },
  black: { label: "只读", title: "当前不可编辑", rank: 4 },
};

export const resolveLevel = (...params) => {
  let picked = "green";
  params.filter(Boolean).forEach(p => {
    const lvl = p.type === "display" ? "black" : (p.level || "green");
    if ((LEVEL_META[lvl]?.rank || 0) > (LEVEL_META[picked]?.rank || 0)) picked = lvl;
  });
  return LEVEL_META[picked] ? picked : "green";
};

export function ParamRiskBadge({ level }) {
  const meta = LEVEL_META[level] || LEVEL_META.green;
  return <span className={"param-risk-badge " + level} title={meta.title}>{meta.label}</span>;
}

export function ParamRow({ param, value, dirty, invalid, onChange }) {
  const meta = PARAM_META[param.key] || {};
  const editable = editableParam(param);
  const level = resolveLevel(param);
  return (
    <div>
      <div className={"prow level-" + level + (dirty ? " dirty" : "") + (invalid ? " invalid" : "")}>
        <span className={"lvl-dot lvl-" + level} />
        <div className="pn"><b>{param.name || meta.name || param.key}</b><ParamRiskBadge level={level} /></div>
        <div className="pd">
          {param.desc || meta.desc}
          {meta.range && meta.range !== "—" && <span style={{ color: "var(--t4)" }}> · 建议 {meta.range}</span>}
        </div>
        <div className="pctl">
          {param.type === "bool" ? (
            <div className={"toggle " + (value ? "on" : "")}
              onClick={() => editable && onChange(param.key, !value)}
              style={{ opacity: editable ? 1 : .5 }}>
              <div className="knob" />
            </div>
          ) : param.type === "display" ? (
            <span className="mono" style={{ color: "var(--t2)", fontSize: 12 }}>{param.value}</span>
          ) : (
            <EditableValue value={value} unit={UNIT[param.type] || ""} ptype={param.type}
              disabled={!editable} onCommit={v => onChange(param.key, v)} />
          )}
          {(level === "black" || param.type === "display") && <span className="plock">只读</span>}
        </div>
      </div>
    </div>
  );
}

export function RangeRow({ group, paramsByKey, vals, dirty, badKeys, onChange }) {
  const pMin = paramsByKey.get(group.min);
  const pMax = paramsByKey.get(group.max);
  if (!pMin || !pMax) return null;
  const level = resolveLevel(pMin, pMax);
  return (
    <div className={"prow level-" + level + " range-row" + (dirty[group.min] || dirty[group.max] ? " dirty" : "") + (badKeys.has(group.min) || badKeys.has(group.max) ? " invalid" : "")}>
      <span className={"lvl-dot lvl-" + level} />
      <div className="pn"><b>{group.label}·保证金区间</b><ParamRiskBadge level={level} /></div>
      <div className="pd">低占用用上限,拥挤时线性缩到下限。自动调参只改上限</div>
      <div className="range-ctl">
        <EditableValue value={vals[group.min]} unit="%" ptype="pct" disabled={!editableParam(pMin)} onCommit={v => onChange(group.min, v)} />
        <span>至</span>
        <EditableValue value={vals[group.max]} unit="%" ptype="pct" disabled={!editableParam(pMax)} onCommit={v => onChange(group.max, v)} />
      </div>
    </div>
  );
}

export function DeployRangeRow({ paramsByKey, vals, dirty, badKeys, onChange }) {
  const pFull = paramsByKey.get("DEPLOY_FULL_PCT");
  const pLock = paramsByKey.get("MAX_DEPLOY_PCT");
  if (!pFull || !pLock) return null;
  const level = resolveLevel(pFull, pLock);
  return (
    <div className={"prow level-" + level + " range-row deploy-row" + (dirty.DEPLOY_FULL_PCT || dirty.MAX_DEPLOY_PCT ? " dirty" : "") + (badKeys.has("DEPLOY_FULL_PCT") || badKeys.has("MAX_DEPLOY_PCT") ? " invalid" : "")}>
      <span className={"lvl-dot lvl-" + level} />
      <div className="pn"><b>组合火力区间</b><ParamRiskBadge level={level} /></div>
      <div className="pd">占用≤左值满火力;左值到右值线性缩仓;≥右值停开新仓,保留资金给加仓/平仓管理</div>
      <div className="range-ctl">
        <EditableValue value={vals.DEPLOY_FULL_PCT} unit="%" ptype="pct" disabled={!editableParam(pFull)} onCommit={v => onChange("DEPLOY_FULL_PCT", v)} />
        <span>至</span>
        <EditableValue value={vals.MAX_DEPLOY_PCT} unit="%" ptype="pct" disabled={!editableParam(pLock)} onCommit={v => onChange("MAX_DEPLOY_PCT", v)} />
      </div>
    </div>
  );
}
