import { fParam } from "../../lib/format.js";
import { CoinBlacklistEditor } from "./CoinBlacklistEditor.jsx";
import { editableParam, ParamRow, RangeRow } from "./ParamRow.jsx";
import {
  AUTO_TUNE_KEY,
  BLACKLIST_KEY,
  TIER_GROUPS,
} from "./paramMeta.js";

const marginEquityKey = "MARGIN_EQUITY_PCT";

export function FollowSettingsPanel({
  list,
  vals,
  dirty,
  openTiers,
  setOpenTiers,
  validationErrors,
  badKeys,
  onChange,
}) {
  const paramsByKey = new Map(list.map(p => [p.key, p]));
  const autoTuneParam = paramsByKey.get(AUTO_TUNE_KEY);
  const blacklistParam = paramsByKey.get(BLACKLIST_KEY);
  const marginEquityParam = paramsByKey.get(marginEquityKey);
  const row = p => (
    <ParamRow key={p.key} param={p} value={vals[p.key]} dirty={dirty[p.key]}
      invalid={badKeys.has(p.key)} onChange={onChange} />
  );
  return (
    <React.Fragment>
      <section className="settings-section settings-section-basic">
        {blacklistParam && <div className="settings-wide-card">
          <CoinBlacklistEditor key={blacklistParam.key} param={blacklistParam}
            value={vals[BLACKLIST_KEY]} dirty={!!dirty[BLACKLIST_KEY]} disabled={!editableParam(blacklistParam)}
            onCommit={v2 => onChange(BLACKLIST_KEY, v2)} />
        </div>}
      </section>

      <section className="settings-section settings-section-margin">
        <div className="settings-section-head settings-section-head-row">
          <div><b>保证金与杠杆</b><span>BTC 固定稳定档，其余市场按波动率 σ 自动分档</span></div>
          {autoTuneParam && <div className={"psec-switch" + (dirty[AUTO_TUNE_KEY] ? " dirty" : "")} title={autoTuneParam.desc}>
            <span>新代际自动调参</span>
            <div className={"toggle " + (vals[AUTO_TUNE_KEY] ? "on" : "")}
              onClick={() => editableParam(autoTuneParam) && onChange(AUTO_TUNE_KEY, !vals[AUTO_TUNE_KEY])}
              style={{ opacity: editableParam(autoTuneParam) ? 1 : .5 }}>
              <div className="knob" />
            </div>
          </div>}
        </div>
        {marginEquityParam && <div className="settings-margin-card">
          {row(marginEquityParam)}
          <div className="param-inline-note">
            已有仓位不变；Core 资格与组合回测在下次重采或重评后更新。
          </div>
        </div>}
        {validationErrors.length > 0 && (
          <div className="param-errors">
            {validationErrors.map((e, i) => <div key={i}>{e}</div>)}
          </div>
        )}
        <div className="settings-tier-grid">
          {TIER_GROUPS.map(group => {
            const open = openTiers[group.key];
            const rows = [group.lev, group.cap].map(k => paramsByKey.get(k)).filter(Boolean);
            return (
              <div className={"settings-tier-card" + (open ? " open" : "")} key={group.key}>
                <div className={"expand-head settings-tier-head" + (open ? " open" : "")}
                  onClick={() => setOpenTiers(o => ({ ...o, [group.key]: !o[group.key] }))}>
                  <span className="settings-caret">{open ? "▾" : "▸"}</span>
                  <div className="settings-tier-copy">
                    <span className={"pill " + group.tint}>{group.label}</span>
                    <span>{group.sub}</span>
                  </div>
                  <div className="settings-tier-summary">
                    <span><b>{fParam(vals[group.max], "pct")}%</b>保证金</span>
                    <span><b>≤{fParam(vals[group.lev], "x")}x</b>杠杆</span>
                    <span><b>{fParam(vals[group.cap], "pct")}%</b>单币上限</span>
                  </div>
                </div>
                {open && <div className="expand-body settings-tier-body">
                  <RangeRow group={group} paramsByKey={paramsByKey} vals={vals} dirty={dirty} badKeys={badKeys} onChange={onChange} />
                  {rows.map(row)}
                </div>}
              </div>
            );
          })}
        </div>
      </section>
    </React.Fragment>
  );
}
