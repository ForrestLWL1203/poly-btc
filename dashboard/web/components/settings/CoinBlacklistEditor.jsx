import { formatCoinList, normalizeCoin, parseCoinList } from "../../lib/format.js";
import { IC, Ico } from "../../lib/icons.jsx";
import { ParamRiskBadge, resolveLevel } from "./ParamRow.jsx";

const { useState } = React;

export function CoinBlacklistEditor({ param, value, dirty, disabled, onCommit }) {
  const [draft, setDraft] = useState("");
  const coins = parseCoinList(value);
  const level = resolveLevel(param);
  const commitCoins = (next) => onCommit(formatCoinList(next));
  const add = () => {
    const c = normalizeCoin(draft);
    if (!c || coins.includes(c)) { setDraft(""); return; }
    commitCoins([...coins, c]);
    setDraft("");
  };
  return (
    <div className={"prow level-" + level + " coin-blacklist-row" + (dirty ? " dirty" : "")}>
      <span className={"lvl-dot lvl-" + level} />
      <div className="pn"><b>{param.name}</b><ParamRiskBadge level={level} /></div>
      <div className="pd">{param.desc}</div>
      <div className="pctl coin-blacklist-ctl">
        <div className="coin-tags">
          {coins.length === 0 && <span className="coin-empty">暂无黑名单</span>}
          {coins.map(c => (
            <button key={c} className="coin-tag" disabled={disabled} title="从黑名单删除"
              onClick={() => commitCoins(coins.filter(x => x !== c))}>
              <span>{c}</span><b>×</b>
            </button>
          ))}
        </div>
        <div className="coin-add">
          <input value={draft} disabled={disabled} placeholder="XYZ:SHKX"
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") add(); else if (e.key === "Escape") setDraft(""); }} />
          <button className="btn btn-sm" disabled={disabled || !normalizeCoin(draft)}
            title="添加币种" onClick={add}><Ico d={IC.plus} /></button>
        </div>
      </div>
    </div>
  );
}
