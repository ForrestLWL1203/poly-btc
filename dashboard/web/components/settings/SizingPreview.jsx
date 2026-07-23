/* 下单沙盘 — 用真实 v10 公式实时换算:杠杆 = 档位上限(floor,clip MIN/MAX_LEV)。
   只读展示(无可调目标杠杆),紧凑液态玻璃风。σ 为实采日内最高-最低振幅均值。读自正在编辑的 vals。 */
export function SizingPreview({ vals }) {
  const [bal, setBal] = React.useState(10000);
  const n = (k, d) => { const v = Number(vals[k]); return isFinite(v) && v > 0 ? v : d; };
  const hiMin = n("HIGH_SIGMA_MIN", 9);
  const MINL = Math.max(1, n("MIN_LEV", 1));
  const marginEquityPct = Math.max(10, Math.min(100, n("MARGIN_EQUITY_PCT", 100)));
  const marginEquity = bal * marginEquityPct / 100;
  const tier = (sym, s) => sym === "BTC" ? "stable" : (s >= hiMin ? "high" : "mid");
  const TM = { stable: ["STABLE_MARGIN_PCT", "STABLE_LEV_CAP"],
    mid: ["MID_MARGIN_PCT", "MID_LEV_CAP"], high: ["HIGH_MARGIN_PCT", "HIGH_LEV_CAP"] };
  const TC = { stable: "STABLE_COIN_CAP_PCT", mid: "MID_COIN_CAP_PCT", high: "HIGH_COIN_CAP_PCT" };
  const DOT = { stable: "var(--green)", mid: "var(--amber)", high: "var(--red)" };
  const dft = { STABLE_MARGIN_PCT: 3.5, STABLE_LEV_CAP: 25,
    MID_MARGIN_PCT: 3, MID_LEV_CAP: 10, HIGH_MARGIN_PCT: 2, HIGH_LEV_CAP: 4 };
  const usd = x => x >= 1000 ? "$" + (x / 1000).toFixed(x >= 10000 ? 0 : 1) + "k" : "$" + Math.round(x);
  const marginPct = t => n(TM[t][0], dft[TM[t][0]]) / 100;
  const calc = (sym, s0) => {
    const s = Math.max(0.1, s0), t = tier(sym, s);
    const mPct = marginPct(t), levCap = n(TM[t][1], dft[TM[t][1]]);
    const lev = Math.max(MINL, Math.floor(levCap));
    const coinCap = bal * n(TC[t], t === "stable" ? 30 : (t === "mid" ? 22 : 15)) / 100;
    const minAdd = marginEquity * n("MIN_OPEN_MARGIN_PCT", 0.5) / 100;
    const margin = Math.min(marginEquity * mPct, Math.max(0, (coinCap - minAdd) / 4));
    const fourthAdd = Math.max(0, coinCap - 4 * margin);
    return { t, margin, lev, notl: margin * lev, fourthAdd };
  };
  const COINS = [["BTC", 3.9], ["ETH", 5.3], ["ZEC", 14.6]];   /* 每档一个代表:稳定 / 中 / 剧烈 */
  return (
    <div className="sz">
      <div className="sz-hd">
        <div className="sz-ttl">下单沙盘<span>· 按当前参数实时换算</span></div>
        <div className="sz-bal"><label>账户权益</label>
          <input type="number" value={bal} onChange={e => setBal(Number(e.target.value) || 0)} /></div>
        <div className="sz-bal"><label>保证金计算权益</label><b>{usd(marginEquity)} · {marginEquityPct}%</b></div>
      </div>
      <div className="sz-grid">
        <div className="sz-hdr">币种</div><div className="sz-hdr sz-num">σ</div>
        <div className="sz-hdr sz-num">杠杆</div><div className="sz-hdr sz-num">保证金 / 名义 · 第4次余量</div>
        {COINS.map(([sym, sig]) => {
          const r = calc(sym, sig);
          return (
            <div className="sz-row" key={sym}>
              <div className="sz-cell sz-coin"><span className="sz-dot" style={{ color: DOT[r.t] }} />{sym}</div>
              <div className="sz-cell sz-num">{sig.toFixed(1)}%</div>
              <div className="sz-cell sz-lev">{r.lev}x</div>
              <div className="sz-cell sz-num">{usd(r.margin)}<span className="sz-sub"> / {usd(r.notl)} · {usd(r.fourthAdd)}</span></div>
            </div>
          );
        })}
      </div>
      <div className="sz-foot">
        每档首仓都为至少 <b>4 次加仓</b>预留单币空间；每个目标加仓订单最多一个首仓额度，最后不足整笔时填满单币上限
      </div>
    </div>
  );
}
