import { fParam } from "../../lib/format.js";
import { CoinBlacklistEditor } from "./CoinBlacklistEditor.jsx";
import { DeployRangeRow, editableParam, ParamRow, RangeRow } from "./ParamRow.jsx";
import {
  ADD_KEYS,
  AUTO_TUNE_KEY,
  BLACKLIST_KEY,
  TIER_GROUPS,
  WALLET_SIDE_CAPS,
} from "./paramMeta.js";

const tierKeys = new Set(TIER_GROUPS.flatMap(g => [g.min, g.max, g.lev, g.notl, g.cap]));
const deployKeys = new Set(["DEPLOY_FULL_PCT", "MAX_DEPLOY_PCT"]);
const walletCapKey = "WALLET_MARGIN_CAP_PCT";
const walletSideKeys = new Set(WALLET_SIDE_CAPS.map(item => item.key));
const marginEquityKey = "MARGIN_EQUITY_PCT";
const tailEnableKey = "TAIL_CLOSE_ENABLE";
const tailChildKeys = [
  "TAIL_CLOSE_HARD_REMAIN_PCT",
  "TAIL_CLOSE_RISK_REMAIN_PCT",
  "TAIL_CLOSE_PROFIT_GIVEBACK_PCT",
];
const tailKeys = new Set([tailEnableKey, ...tailChildKeys]);

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
  const walletCapParam = paramsByKey.get(walletCapKey);
  const walletSideParams = WALLET_SIDE_CAPS.map(item => paramsByKey.get(item.key)).filter(Boolean);
  const tailEnableParam = paramsByKey.get(tailEnableKey);
  const tailChildParams = tailChildKeys.map(key => paramsByKey.get(key)).filter(Boolean);
  const row = p => (
    <ParamRow key={p.key} param={p} value={vals[p.key]} dirty={dirty[p.key]}
      invalid={badKeys.has(p.key)} onChange={onChange} />
  );
  const visibleTopRows = list.filter(p => !(
    tierKeys.has(p.key) || deployKeys.has(p.key) || walletSideKeys.has(p.key)
    || ADD_KEYS.has(p.key) || tailKeys.has(p.key) || p.key === AUTO_TUNE_KEY
    || p.key === BLACKLIST_KEY || p.key === marginEquityKey || p.key === walletCapKey
  ));

  return (
    <React.Fragment>
      {visibleTopRows.map(row)}
      {tailEnableParam && <div className="tail-dependent-group">
        <button type="button"
          className={"expand-head tail-dependent-head" + (vals[tailEnableKey] ? " open" : "") + (dirty[tailEnableKey] ? " dirty" : "")}
          aria-expanded={!!vals[tailEnableKey]}
          aria-pressed={!!vals[tailEnableKey]}
          disabled={!editableParam(tailEnableParam)}
          onClick={() => editableParam(tailEnableParam) && onChange(tailEnableKey, !vals[tailEnableKey])}>
          <span className="tail-dependent-caret">{vals[tailEnableKey] ? "▾" : "▸"}</span>
          <span className="pill tint-green">盈利尾仓保护</span>
          <span className="tail-dependent-desc">{tailEnableParam.desc}</span>
          <span className="tail-dependent-summary">{vals[tailEnableKey] ? "3 项参数已生效" : "已关闭"}</span>
          <span className={"toggle " + (vals[tailEnableKey] ? "on" : "")} aria-hidden="true">
            <span className="knob" />
          </span>
        </button>
        {vals[tailEnableKey] && <div className="expand-body tail-dependent-body">
          {tailChildParams.map(row)}
        </div>}
      </div>}
      {blacklistParam && <CoinBlacklistEditor key={blacklistParam.key} param={blacklistParam}
        value={vals[BLACKLIST_KEY]} dirty={!!dirty[BLACKLIST_KEY]} disabled={!editableParam(blacklistParam)}
        onCommit={v2 => onChange(BLACKLIST_KEY, v2)} />}
      <div className="psec-h psec-h-row">
        <div className="psec-title-block">保证金与杠杆 · BTC固定稳定档，其余按波动率 σ 分档
          <span>BTC始终使用稳定档；其余市场由 σ 进入中档/剧烈档，这里设各档的单笔保证金% 与杠杆上限</span></div>
        {autoTuneParam && <div className={"psec-switch" + (dirty[AUTO_TUNE_KEY] ? " dirty" : "")} title={autoTuneParam.desc}>
          <span>自动调保证金</span>
          <div className={"toggle " + (vals[AUTO_TUNE_KEY] ? "on" : "")}
            onClick={() => editableParam(autoTuneParam) && onChange(AUTO_TUNE_KEY, !vals[AUTO_TUNE_KEY])}
            style={{ opacity: editableParam(autoTuneParam) ? 1 : .5 }}>
            <div className="knob" />
          </div>
        </div>}
      </div>
      {marginEquityParam && row(marginEquityParam)}
      {marginEquityParam && <div className="param-inline-note">
        只缩小每笔新仓的保证金计算基数；未计入的权益仍是可用资金，不会被冻结。新开仓立即生效，Core资格和组合回测在下次重采或重评后更新。
      </div>}
      <DeployRangeRow paramsByKey={paramsByKey} vals={vals} dirty={dirty} badKeys={badKeys} onChange={onChange} />
      {(walletCapParam || walletSideParams.length > 0) && <div className="psec-h psec-h-row">
        <div className="psec-title-block">单钱包集中度上限
          <span>总上限约束整个目标钱包；下列四个输入框分别约束同钱包、同板块、同方向的保证金</span>
        </div>
      </div>}
      {walletCapParam && row(walletCapParam)}
      {walletSideParams.map(row)}
      {walletSideParams.length > 0 && <div className="param-inline-note">
        低波/中波/高波 Crypto 与美股板块分别设置，不再使用已废弃且隐藏的旧全局值。新开与加仓受限；存量超限仓位只管理退出，不会被强平。
      </div>}
      {validationErrors.length > 0 && (
        <div className="param-errors">
          {validationErrors.map((e, i) => <div key={i}>{e}</div>)}
        </div>
      )}
      {TIER_GROUPS.map(group => {
        const open = openTiers[group.key];
        const rows = [group.lev, group.notl, group.cap].map(k => paramsByKey.get(k)).filter(Boolean);
        return (
          <div key={group.key}>
            <div className={"expand-head" + (open ? " open" : "")} onClick={() => setOpenTiers(o => ({ ...o, [group.key]: !o[group.key] }))}>
              <span style={{ color: "var(--t3)", width: 12 }}>{open ? "▾" : "▸"}</span>
              <span className={"pill " + group.tint}>{group.label}</span>
              <span className="muted" style={{ fontSize: 12 }}>{group.sub}</span>
              {!open && <span className="muted" style={{ marginLeft: "auto", fontSize: 11 }}>
                保证金 {fParam(vals[group.min], "pct")}–{fParam(vals[group.max], "pct")}% · 杠杆 ≤{fParam(vals[group.lev], "x")}x · 最低 ${fParam(vals[group.notl], "usd")} · 单币上限 {fParam(vals[group.cap], "pct")}%
              </span>}
            </div>
            {open && <div className="expand-body">
              <RangeRow group={group} paramsByKey={paramsByKey} vals={vals} dirty={dirty} badKeys={badKeys} onChange={onChange} />
              {rows.map(row)}
            </div>}
          </div>
        );
      })}
    </React.Fragment>
  );
}
