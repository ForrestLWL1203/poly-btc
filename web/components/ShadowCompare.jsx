import { api } from "../lib/api.js";
import { cls, fNum, fPrice, fSign, fUsd, short } from "../lib/format.js";
import { useApiResource } from "../lib/refresh.js";

const { useCallback } = React;

export function ShadowCompare() {
  const loadShadow = useCallback(() => api.get("/api/shadow"), []);
  const { data: d } = useApiResource(loadShadow, { intervalMs: 10000 });
  if (!d) return <div className="content"><div className="loading">加载中…</div></div>;
  const roi = b => (b.equity / 10000 - 1) * 100;
  const Acct = ({ b, name, tint }) => (
    <div className="card" style={{ flex: 1 }}>
      <div className="card-lbl">{name}</div>
      <div className="kpi" style={{ color: tint }}>{fUsd(b.equity)}</div>
      <div className="muted" style={{ fontSize: 12, lineHeight: 1.7 }}>
        ROI <b className={cls(roi(b))}>{fSign(roi(b), 1)}%</b> · 已实现 <span className={cls(b.realized)}>{fSign(b.realized, 0)}</span> · 浮动 <span className={cls(b.unrealized)}>{fSign(b.unrealized, 0)}</span><br />
        {b.openN} 持仓 · {b.closedN} 平仓 · 胜率 {fNum(b.winRatePct, 0)}%
      </div>
    </div>
  );
  const diff = d.maker.equity - d.taker.equity;
  return (
    <div className="content">
      <h2>影子对比 · Maker vs Taker <span className="muted">· 同一套策略,只差执行方式</span></h2>
      {!d.enabled && <div className="muted" style={{ marginTop: 8 }}>⚠ 影子引擎未启用</div>}
      <div style={{ display: "flex", gap: 14, marginTop: 12 }}>
        <Acct b={d.taker} name="Taker 账(实盘执行)" tint="var(--t1)" />
        <Acct b={d.maker} name="Maker 影子账(挂单执行)" tint="var(--accent)" />
      </div>
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-lbl">Maker − Taker 权益差</div>
        <div className="kpi" style={{ color: diff >= 0 ? "var(--green-l)" : "var(--red-l)" }}>{fSign(diff, 1)}</div>
        <div className="muted" style={{ fontSize: 12 }}>正 = maker 执行更优(省手续费 + 更好入场价,但成交率更低)。两账从同一 $10k 起点、同策略跑,差异纯来自执行。</div>
      </div>
      <h3 style={{ marginTop: 18 }}>Maker 账当前持仓 <span className="muted">· {d.makerPositions.length} 笔</span></h3>
      <table><thead><tr><th>币</th><th>方向</th><th className="num">入场/杠杆</th><th className="num">保证金</th><th className="num">现价</th><th className="num">浮动</th><th>钱包</th></tr></thead>
        <tbody>
          {d.makerPositions.length === 0 && <tr><td colSpan="7" className="empty">影子账暂无持仓(等目标 maker 成交后建仓)</td></tr>}
          {d.makerPositions.map((p, i) => (
            <tr key={i}>
              <td><b>{p.coin}</b>{p.addN > 0 && <span className="pill" style={{ marginLeft: 6 }}>加{p.addN}</span>}</td>
              <td><span className={"tint " + (p.side === "long" ? "tint-green" : "tint-red")}>{p.side === "long" ? "多" : "空"}</span></td>
              <td className="num">{fPrice(p.entry)} · {fNum(p.lev, 0)}x</td>
              <td className="num">{fUsd(p.margin)}</td>
              <td className="num">{fPrice(p.mark)}</td>
              <td className={"num " + cls(p.upnl)}>{fSign(p.upnl, 1)}</td>
              <td className="addr">{short(p.addr)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
