import { fParam } from "../../lib/format.js";

const { useState, useEffect, useRef } = React;

/* 行内编辑值:平时是一段带轻微底色的文本(值+单位),点击变成输入框,失焦/回车提交并复原成文本。
   提交只更新暂存(vals/dirty),实际落库仍由底部 apply-bar(确认/重采)。Esc 取消。 */
export function EditableValue({ value, unit, ptype, disabled, onCommit }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const ref = useRef(null);
  useEffect(() => { setDraft(value); }, [value]);                       // 外部值变化(保存后)时同步
  useEffect(() => { if (editing && ref.current) { ref.current.focus(); ref.current.select(); } }, [editing]);
  const commit = () => {
    setEditing(false);
    const v = draft === "" || draft == null ? null : Number(draft);
    if (v !== value && !(v == null && value == null)) onCommit(v);
  };
  if (disabled) return <span className="ev ev-ro">{value == null ? "—" : fParam(value, ptype)}{unit && <i className="ev-u">{unit}</i>}</span>;
  if (editing) return (
    <input ref={ref} className="ev-input" type={ptype === "nullable" ? "text" : "number"} value={draft == null ? "" : draft}
      placeholder={ptype === "nullable" ? "关闭" : ""}
      onChange={e => setDraft(e.target.value)} onBlur={commit}
      onKeyDown={e => { if (e.key === "Enter") commit(); else if (e.key === "Escape") { setDraft(value); setEditing(false); } }} />
  );
  return (
    <span className="ev" title="点击编辑" onClick={() => { setDraft(value); setEditing(true); }}>
      {value == null ? <span className="ev-empty">关闭</span> : fParam(value, ptype)}{value != null && unit && <i className="ev-u">{unit}</i>}
    </span>
  );
}
