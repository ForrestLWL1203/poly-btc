import { ParamRow } from "./ParamRow.jsx";
const BASIC_SCANNER_KEYS = new Set([
  "HARVEST_WEEK_VLM_MIN",
  "EXCLUDE_HFT",
  "CORE_INITIAL_MAX_N",
]);

export function ScannerSettingsPanel({ list, vals, dirty, onChange }) {
  const rows = list.filter(p => BASIC_SCANNER_KEYS.has(p.key));
  return (
    <div className="settings-param-grid settings-param-grid-scanner">
      {rows.map(p => (
        <ParamRow key={p.key} param={p} value={vals[p.key]} dirty={dirty[p.key]} onChange={onChange} />
      ))}
    </div>
  );
}
