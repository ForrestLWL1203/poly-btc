import { editableParam, ParamRiskBadge } from "./ParamRow.jsx";

const OPTIONS = [
  {
    value: "aggressive",
    label: "激进",
    kicker: "收益优先",
    desc: "不接受收益让渡；爆仓与复刻度只作为不恶化的安全闸。",
    tone: "hot",
  },
  {
    value: "balanced",
    label: "平衡",
    kicker: "推荐",
    desc: "保留至少80%收益潜力，优先减少爆仓并提高完整路径复刻。",
    tone: "balanced",
  },
  {
    value: "conservative",
    label: "保守",
    kicker: "生存优先",
    desc: "保留至少60%收益潜力，接受更多让渡换取更少断链和尾部风险。",
    tone: "safe",
  },
];

export function TuneRiskProfileSelector({ param, value, dirty, onChange }) {
  if (!param) return null;
  const disabled = !editableParam(param);
  return (
    <section className={"tune-profile" + (dirty ? " dirty" : "")}>
      <div className="tune-profile-head">
        <div>
          <div className="tune-profile-title">自动调参目标 <ParamRiskBadge level={param.level} /></div>
          <p>每轮只运行一个目标，不会同时回测三套策略；下一次完整采集后的调参阶段生效。</p>
        </div>
        <span className="tune-profile-one">ONE PASS</span>
      </div>
      <div className="tune-profile-options" role="radiogroup" aria-label="自动调参目标档位">
        {OPTIONS.map(option => (
          <label key={option.value} className={"tune-profile-option " + option.tone + (value === option.value ? " selected" : "") + (disabled ? " disabled" : "")}>
            <input type="radio" name="auto-tune-risk-profile" value={option.value}
              checked={value === option.value} disabled={disabled}
              onChange={() => onChange(param.key, option.value)} />
            <span className="tune-profile-dot" aria-hidden="true" />
            <span className="tune-profile-copy">
              <b>{option.label}<i>{option.kicker}</i></b>
              <small>{option.desc}</small>
            </span>
          </label>
        ))}
      </div>
    </section>
  );
}
