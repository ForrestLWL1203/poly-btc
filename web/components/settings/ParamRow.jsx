import { EditableValue } from "./EditableValue.jsx";
import { PARAM_META, UNIT } from "./paramMeta.js";

export const editableParam = (p) => !(p.type === "display" || p.level === "black");

export function ParamRow({ param, value, dirty, invalid, onChange }) {
  const meta = PARAM_META[param.key] || {};
  const editable = editableParam(param);
  const level = param.level;
  return (
    <div>
      <div className={"prow" + (dirty ? " dirty" : "") + (invalid ? " invalid" : "")}>
        <span className="lvl-dot lvl-green" />
        <div className="pn"><b>{param.name || meta.name || param.key}</b></div>
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
  return (
    <div className={"prow range-row" + (dirty[group.min] || dirty[group.max] ? " dirty" : "") + (badKeys.has(group.min) || badKeys.has(group.max) ? " invalid" : "")}>
      <span className="lvl-dot lvl-green" />
      <div className="pn"><b>{group.label}·保证金区间</b></div>
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
  return (
    <div className={"prow range-row deploy-row" + (dirty.DEPLOY_FULL_PCT || dirty.MAX_DEPLOY_PCT ? " dirty" : "") + (badKeys.has("DEPLOY_FULL_PCT") || badKeys.has("MAX_DEPLOY_PCT") ? " invalid" : "")}>
      <span className="lvl-dot lvl-green" />
      <div className="pn"><b>组合火力区间</b></div>
      <div className="pd">占用≤左值满火力;左值到右值线性缩仓;≥右值停开新仓,保留资金给加仓/平仓管理</div>
      <div className="range-ctl">
        <EditableValue value={vals.DEPLOY_FULL_PCT} unit="%" ptype="pct" disabled={!editableParam(pFull)} onCommit={v => onChange("DEPLOY_FULL_PCT", v)} />
        <span>至</span>
        <EditableValue value={vals.MAX_DEPLOY_PCT} unit="%" ptype="pct" disabled={!editableParam(pLock)} onCommit={v => onChange("MAX_DEPLOY_PCT", v)} />
      </div>
    </div>
  );
}
