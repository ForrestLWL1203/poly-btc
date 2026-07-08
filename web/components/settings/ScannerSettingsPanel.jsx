import { EditableValue } from "./EditableValue.jsx";
import { ParamRow } from "./ParamRow.jsx";

const WEEK_VLM_MIN = "HARVEST_WEEK_VLM_MIN";
const WEEK_VLM_MAX = "HARVEST_WEEK_VLM_MAX";

function HarvestVolumeRangeRow({ paramsByKey, vals, dirty, onChange }) {
  const pMin = paramsByKey.get(WEEK_VLM_MIN);
  const pMax = paramsByKey.get(WEEK_VLM_MAX);
  if (!pMin || !pMax) return null;
  return (
    <div className={"prow range-row" + (dirty[WEEK_VLM_MIN] || dirty[WEEK_VLM_MAX] ? " dirty" : "")}>
      <span className="lvl-dot lvl-green" />
      <div className="pn"><b>周成交量范围</b></div>
      <div className="pd">近7天成交额在此范围内才纳入;下限过滤冷清钱包,上限过滤做市/高频钱包</div>
      <div className="range-ctl">
        <EditableValue value={vals[WEEK_VLM_MIN]} unit="$" ptype="usd" disabled={false} onCommit={v => onChange(WEEK_VLM_MIN, v)} />
        <span>至</span>
        <EditableValue value={vals[WEEK_VLM_MAX]} unit="$" ptype="usd" disabled={false} onCommit={v => onChange(WEEK_VLM_MAX, v)} />
      </div>
    </div>
  );
}

export function ScannerSettingsPanel({ list, vals, dirty, onChange }) {
  const paramsByKey = new Map(list.map(p => [p.key, p]));
  return (
    <React.Fragment>
      {list.map(p => {
        if (p.key === WEEK_VLM_MIN) {
          return <HarvestVolumeRangeRow key="week-volume-range" paramsByKey={paramsByKey} vals={vals} dirty={dirty} onChange={onChange} />;
        }
        if (p.key === WEEK_VLM_MAX) return null;
        return <ParamRow key={p.key} param={p} value={vals[p.key]} dirty={dirty[p.key]} onChange={onChange} />;
      })}
    </React.Fragment>
  );
}
