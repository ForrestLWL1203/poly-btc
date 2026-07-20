import { ParamRow } from "./ParamRow.jsx";
const BASIC_SCANNER_KEYS = new Set([
  "HARVEST_MIN_ACCT",
  "HARVEST_WEEK_VLM_MIN",
  "HARVEST_WEEK_ROI_MIN",
  "HARVEST_MONTH_ROI_MIN",
  "HARVEST_ALL_ROI_MIN",
  "HARVEST_WEEK_PNL_MIN",
  "HARVEST_MONTH_PNL_MIN",
  "HARVEST_ALL_PNL_MIN",
  "HARVEST_PERP_PNL_SHARE_MIN",
  "EXCLUDE_HFT",
  "inactive_days",
  "CORE_INITIAL_MAX_N",
]);

export function ScannerSettingsPanel({ list, vals, dirty, onChange }) {
  const baseRows = list.filter(p => BASIC_SCANNER_KEYS.has(p.key));
  const advancedRows = list.filter(p => !BASIC_SCANNER_KEYS.has(p.key));
  const advancedDirty = advancedRows.some(p => dirty[p.key]);
  const renderScannerRow = p => <ParamRow key={p.key} param={p} value={vals[p.key]} dirty={dirty[p.key]} onChange={onChange} />;
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
