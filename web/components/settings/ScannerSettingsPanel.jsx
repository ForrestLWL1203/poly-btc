import { ParamRow } from "./ParamRow.jsx";

export function ScannerSettingsPanel({ list, vals, dirty, onChange }) {
  return (
    <React.Fragment>
      {list.map(p => <ParamRow key={p.key} param={p} value={vals[p.key]} dirty={dirty[p.key]} onChange={onChange} />)}
    </React.Fragment>
  );
}
