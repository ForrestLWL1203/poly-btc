import { EditableValue } from "./EditableValue.jsx";
import { editableParam, ParamRiskBadge, ParamRow, resolveLevel } from "./ParamRow.jsx";

const WEEK_VLM_MIN = "HARVEST_WEEK_VLM_MIN";
const WEEK_VLM_MAX = "HARVEST_WEEK_VLM_MAX";
const BASIC_SCANNER_KEYS = new Set([
  "HARVEST_MIN_ACCT",
  WEEK_VLM_MIN,
  WEEK_VLM_MAX,
  "EXCLUDE_HFT",
  "inactive_days",
  "CORE_INITIAL_MAX_N",
]);

function HarvestVolumeRangeRow({ paramsByKey, vals, dirty, onChange }) {
  const pMin = paramsByKey.get(WEEK_VLM_MIN);
  const pMax = paramsByKey.get(WEEK_VLM_MAX);
  if (!pMin || !pMax) return null;
  const level = resolveLevel(pMin, pMax);
  return (
    <div className={"prow level-" + level + " range-row" + (dirty[WEEK_VLM_MIN] || dirty[WEEK_VLM_MAX] ? " dirty" : "")}>
      <span className={"lvl-dot lvl-" + level} />
      <div className="pn"><b>周成交量范围</b><ParamRiskBadge level={level} /></div>
      <div className="pd">近7天成交额在此范围内才纳入;下限过滤冷清钱包,上限过滤做市/高频钱包</div>
      <div className="range-ctl">
        <EditableValue value={vals[WEEK_VLM_MIN]} unit="$" ptype="usd" disabled={!editableParam(pMin)} onCommit={v => onChange(WEEK_VLM_MIN, v)} />
        <span>至</span>
        <EditableValue value={vals[WEEK_VLM_MAX]} unit="$" ptype="usd" disabled={!editableParam(pMax)} onCommit={v => onChange(WEEK_VLM_MAX, v)} />
      </div>
    </div>
  );
}

export function ScannerSettingsPanel({ list, vals, dirty, onChange }) {
  const paramsByKey = new Map(list.map(p => [p.key, p]));
  const baseRows = list.filter(p => BASIC_SCANNER_KEYS.has(p.key));
  const advancedRows = list.filter(p => !BASIC_SCANNER_KEYS.has(p.key));
  const advancedDirty = advancedRows.some(p => dirty[p.key]);
  const renderScannerRow = p => {
    if (p.key === WEEK_VLM_MIN) {
      return <HarvestVolumeRangeRow key="week-volume-range" paramsByKey={paramsByKey} vals={vals} dirty={dirty} onChange={onChange} />;
    }
    if (p.key === WEEK_VLM_MAX) return null;
    return <ParamRow key={p.key} param={p} value={vals[p.key]} dirty={dirty[p.key]} onChange={onChange} />;
  };
  return (
    <React.Fragment>
      {baseRows.map(renderScannerRow)}
      {advancedRows.length > 0 && (
        <details className={"scanner-advanced" + (advancedDirty ? " dirty" : "")}>
          <summary className="expand-head">
            <span className="row-caret">▸</span>
            <b>高级采集参数</b>
            <span className="muted">默认折叠;用于排查扫描闸口,评分权重不在这里开放</span>
          </summary>
          <div className="expand-body">
            {advancedRows.map(renderScannerRow)}
          </div>
        </details>
      )}
    </React.Fragment>
  );
}
