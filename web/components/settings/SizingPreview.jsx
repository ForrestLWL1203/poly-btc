/* 下单沙盘 — 用真实 v10 公式实时换算:杠杆 = 档位上限(floor,clip MIN/MAX_LEV);止损 = 亏SM%保证金 逆向。
   只读展示(无可调目标杠杆),紧凑液态玻璃风。σ 为实采日内最高-最低振幅均值。读自正在编辑的 vals。 */
export function SizingPreview({ vals }) {
  const [bal, setBal] = React.useState(10000);
  const [deploy, setDeploy] = React.useState(20);
  const n = (k, d) => { const v = Number(vals[k]); return isFinite(v) && v > 0 ? v : d; };
  const stMax = n("STABLE_SIGMA_MAX", 4), hiMin = n("HIGH_SIGMA_MIN", 10);
  const MINL = Math.max(1, n("MIN_LEV", 1));
  const SM = n("STOP_MARGIN_PCT", 70);
  const stopOn = vals["COPY_STOP_ENABLE"] !== false;
  const tier = s => s <= stMax ? "stable" : (s >= hiMin ? "high" : "mid");
  const TM = { stable: ["STABLE_MARGIN_MIN_PCT", "STABLE_MARGIN_PCT", "STABLE_LEV_CAP"],
    mid: ["MID_MARGIN_MIN_PCT", "MID_MARGIN_PCT", "MID_LEV_CAP"], high: ["HIGH_MARGIN_MIN_PCT", "HIGH_MARGIN_PCT", "HIGH_LEV_CAP"] };
  const DOT = { stable: "var(--green)", mid: "var(--amber)", high: "var(--red)" };
  const dft = { STABLE_MARGIN_MIN_PCT: 2, STABLE_MARGIN_PCT: 3.5, STABLE_LEV_CAP: 25,
    MID_MARGIN_MIN_PCT: 2, MID_MARGIN_PCT: 3, MID_LEV_CAP: 10, HIGH_MARGIN_MIN_PCT: 1.2, HIGH_MARGIN_PCT: 2, HIGH_LEV_CAP: 4 };
  const usd = x => x >= 1000 ? "$" + (x / 1000).toFixed(x >= 10000 ? 0 : 1) + "k" : "$" + Math.round(x);
  const marginPct = t => {
    const lo = Math.min(n(TM[t][0], dft[TM[t][0]]), n(TM[t][1], dft[TM[t][1]])) / 100;
    const hi = Math.max(n(TM[t][0], dft[TM[t][0]]), n(TM[t][1], dft[TM[t][1]])) / 100;
    const full = n("DEPLOY_FULL_PCT", 40) / 100, lock = n("MAX_DEPLOY_PCT", 80) / 100;
    const d = Math.max(0, deploy / 100);
    if (d <= full) return hi;
    if (d >= lock || lock <= full) return lo;
    return lo + (hi - lo) * (lock - d) / (lock - full);
  };
  const calc = s0 => {
    const s = Math.max(0.1, s0), t = tier(s);
    const mPct = marginPct(t), cap = n(TM[t][2], dft[TM[t][2]]);
    const lev = Math.max(MINL, Math.floor(cap));   // v10: 杠杆 = 档位上限(再被目标杠杆+股票上限封顶)
    const margin = bal * mPct;
    const stopLoss = Math.min(SM / 100, 1), stopDist = stopLoss / lev * 100;  // 硬亏=SM%保证金(固定),逆向价格=SM%÷杠杆
    return { t, margin, lev, notl: margin * lev, stopDist, stopLoss };
  };
  const COINS = [["BTC", 3.9], ["ETH", 5.3], ["ZEC", 14.6]];   /* 每档一个代表:稳定 / 中 / 剧烈 */
  return (
    <div className="sz">
      <div className="sz-hd">
        <div className="sz-ttl">下单沙盘<span>· 按当前参数实时换算</span></div>
        <div className="sz-bal"><label>账户权益</label>
          <input type="number" value={bal} onChange={e => setBal(Number(e.target.value) || 0)} /></div>
        <div className="sz-bal"><label>已占用%</label>
          <input type="number" value={deploy} onChange={e => setDeploy(Number(e.target.value) || 0)} /></div>
      </div>
      <div className="sz-grid">
        <div className="sz-hdr">币种</div><div className="sz-hdr sz-num">σ</div>
        <div className="sz-hdr sz-num">杠杆</div><div className="sz-hdr sz-num">保证金 / 名义</div>
        <div className="sz-hdr sz-num">止损 / 硬亏</div>
        {COINS.map(([sym, sig]) => {
          const r = calc(sig);
          return (
            <div className="sz-row" key={sym}>
              <div className="sz-cell sz-coin"><span className="sz-dot" style={{ color: DOT[r.t] }} />{sym}</div>
              <div className="sz-cell sz-num">{sig.toFixed(1)}%</div>
              <div className="sz-cell sz-lev">{r.lev}x</div>
              <div className="sz-cell sz-num">{usd(r.margin)}<span className="sz-sub"> / {usd(r.notl)}</span></div>
              <div className="sz-cell sz-num">{stopOn
                ? <React.Fragment>−{r.stopDist.toFixed(1)}%<span className="sz-sub"> / 亏{Math.round(r.stopLoss * 100)}%</span></React.Fragment>
                : <span className="sz-sub">已关</span>}</div>
            </div>
          );
        })}
      </div>
      <div className="sz-foot">
        杠杆 = <b>σ 档位上限</b>(σ 定档,再被目标杠杆+股票上限封顶)· 保证金 = 权益 × 动态区间%{stopOn
          ? <React.Fragment> · 止损 = 亏到 <b>{Math.round(SM)}%</b> 保证金就平(与币种无关的硬亏),换算逆向价格 = <b>{Math.round(SM)}%÷杠杆</b></React.Fragment>
          : <React.Fragment> · <b>止损已关闭</b>,仅靠强平兜底</React.Fragment>}
      </div>
    </div>
  );
}
