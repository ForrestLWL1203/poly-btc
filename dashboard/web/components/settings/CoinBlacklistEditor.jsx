import { formatCoinList, normalizeCoin, parseCoinList } from "../../lib/format.js";
import { IC, Ico } from "../../lib/icons.jsx";
import { resolveLevel } from "./ParamRow.jsx";

const { useState } = React;
const COLLAPSED_COIN_LIMIT = 4;

export function CoinBlacklistEditor({ param, value, dirty, disabled, onCommit }) {
  const [draft, setDraft] = useState("");
  const [expanded, setExpanded] = useState(false);
  const coins = parseCoinList(value);
  const hiddenCount = Math.max(0, coins.length - COLLAPSED_COIN_LIMIT);
  const visibleCoins = expanded ? coins : coins.slice(0, COLLAPSED_COIN_LIMIT);
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
      <span className="lvl-dot" />
      <div className="pn"><b>{param.name}</b></div>
      <div className="pd">{param.desc}</div>
      <div className="pctl coin-blacklist-ctl">
        <div className="coin-list-inline">
          <div className="coin-list-meta">
            <span className="coin-count"><b>{coins.length}</b> 个币种</span>
            {hiddenCount > 0 && <button type="button" className="coin-list-toggle"
              aria-expanded={expanded} onClick={() => setExpanded(v => !v)}>
              {expanded ? "收起" : "展开全部"}
            </button>}
          </div>
          <div className={"coin-tags" + (expanded ? " expanded" : "")}>
            {coins.length === 0 && <span className="coin-empty">暂无黑名单</span>}
            {visibleCoins.map(c => (
              <button key={c} className="coin-tag" disabled={disabled} title="从黑名单删除"
                onClick={() => commitCoins(coins.filter(x => x !== c))}>
                <span>{c}</span><b>×</b>
              </button>
            ))}
            {!expanded && hiddenCount > 0 && <button type="button" className="coin-more"
              title={`还有 ${hiddenCount} 个币种`} onClick={() => setExpanded(true)}>+{hiddenCount}</button>}
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
    </div>
  );
}
