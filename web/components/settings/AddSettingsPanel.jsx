import { ParamRow } from "./ParamRow.jsx";

export function AddSettingsPanel({ list, vals, dirty, openTiers, setOpenTiers, onChange }) {
  const byKey = k => list.find(p => p.key === k);
  const row = p => <ParamRow key={p.key} param={p} value={vals[p.key]} dirty={dirty[p.key]} onChange={onChange} />;
  const smart = !!vals.SMART_ADD;
  const bOpen = openTiers.B === undefined ? true : openTiers.B;
  const sectionLabel = text => (
    <div className="muted" style={{ fontSize: 11, padding: "8px 0 2px", fontWeight: 600, color: "var(--t2)" }}>{text}</div>
  );

  return (
    <React.Fragment>
      <div className="psec-h">加仓策略 · 独立于跟单/采集<span>目标加仓时:我们是否跟、跟多少、跟几次。逆向摊低是重点。</span></div>
      <div>
        <div className={"expand-head" + (openTiers.A ? " open" : "")} onClick={() => setOpenTiers(o => ({ ...o, A: !o.A }))}>
          <span style={{ color: "var(--t3)", width: 12 }}>{openTiers.A ? "▾" : "▸"}</span>
          <span className="pill tint-green">A · 正向加仓</span>
          <span className="muted" style={{ fontSize: 12 }}>盈利单顺势加仓、拉高成本追更大利润</span>
          {!openTiers.A && <span className="muted" style={{ marginLeft: "auto", fontSize: 11 }}>{vals.FOLLOW_POS_ADD ? "跟随" : "不跟(默认)"}</span>}
        </div>
        {openTiers.A && <div className="expand-body">
          {[byKey("FOLLOW_POS_ADD")].filter(Boolean).map(row)}
          <div className="muted" style={{ fontSize: 11, padding: "2px 0 6px" }}>正向较简单:开启后按「比例镜像 + 硬顶 + 三档预算」跟,不用波动闸。</div>
        </div>}
      </div>
      <div>
        <div className={"expand-head" + (bOpen ? " open" : "")} onClick={() => setOpenTiers(o => ({ ...o, B: !(o.B === undefined ? true : o.B) }))}>
          <span style={{ color: "var(--t3)", width: 12 }}>{bOpen ? "▾" : "▸"}</span>
          <span className="pill tint-red">B · 逆向加仓(摊低)</span>
          <span className="muted" style={{ fontSize: 12 }}>目标逆势摊低成本 —— 我们如何跟(二选一)</span>
          {!bOpen && <span className="muted" style={{ marginLeft: "auto", fontSize: 11 }}>{smart ? "② 智能动态" : "① 分档硬cap"}</span>}
        </div>
        {bOpen && <div className="expand-body">
          {[byKey("SMART_ADD")].filter(Boolean).map(row)}
          {smart ? <React.Fragment>
            {sectionLabel("② 智能动态(σ波动闸 + 比例镜像)")}
            {["ADD_GAP_K", "ADD_GAP_SHRINK_G", "ADD_MAX_HARD"].map(byKey).filter(Boolean).map(row)}
            <div className="muted" style={{ fontSize: 11, padding: "4px 0 6px" }}>加仓额封顶到该币「单币上限」剩余预算 —— 该上限是全局灾难闸,在「跟单策略参数 · 保证金与杠杆 σ分档」里调。</div>
          </React.Fragment> : <React.Fragment>
            {sectionLabel("① 分档硬cap(固定次数 + 固定比例)")}
            {["ADD_FRAC", "STABLE_MAX_ADDS", "MID_MAX_ADDS", "HIGH_MAX_ADDS"].map(byKey).filter(Boolean).map(row)}
          </React.Fragment>}
        </div>}
      </div>
    </React.Fragment>
  );
}
