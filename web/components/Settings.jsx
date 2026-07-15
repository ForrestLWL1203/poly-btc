import { api } from "../lib/api.js";
import { AddSettingsPanel } from "./settings/AddSettingsPanel.jsx";
import { FollowSettingsPanel } from "./settings/FollowSettingsPanel.jsx";
import { ScannerSettingsPanel } from "./settings/ScannerSettingsPanel.jsx";
import { SizingPreview } from "./settings/SizingPreview.jsx";
import { ADD_KEYS } from "./settings/paramMeta.js";
import { useSettingsParams } from "./settings/useSettingsParams.js";
import { validateFollowParams } from "./settings/validation.js";

const { useState } = React;

export function Settings({ confirm }) {
  const {
    params,
    vals,
    dirty,
    loadParams,
    setValue,
    clearDirty,
    discard,
  } = useSettingsParams();
  const [tab, setTab] = useState("scanner");
  const [saving, setSaving] = useState(false);
  const [openTiers, setOpenTiers] = useState({});

  if (!params) return <div className="content"><div className="loading">加载中…</div></div>;

  const list = tab === "add" ? params.follow.filter(p => ADD_KEYS.has(p.key)) : params[tab];
  const tabDirty = list.filter(p => dirty[p.key]);
  const followValidation = tab === "follow" ? validateFollowParams(vals) : { errors: [], badKeys: new Set() };
  const validationErrors = followValidation.errors;

  const apply = async () => {
    if (validationErrors.length) return;
    const body = {};
    tabDirty.forEach(p => { body[p.key] = vals[p.key]; });
    const doIt = async () => {
      setSaving(true);
      const t0 = Date.now();
      const category = tab === "add" ? "follow" : tab;
      try { await api.patchParams(category, body); } catch (_e) {}
      clearDirty();
      await new Promise(r => setTimeout(r, Math.max(0, 450 - (Date.now() - t0))));
      setSaving(false);
    };
    if (tab === "scanner") {
      confirm({
        title: "保存采集参数",
        danger: false,
        ok: "保存",
        body: "只保存参数，不会立即启动采集。新参数将在下一次自动采集或用户手动触发采集时生效。",
        onConfirm: doIt,
      });
    } else if (tabDirty.some(p => p.level === "yellow")) {
      const marginEquityChanged = tabDirty.some(p => p.key === "MARGIN_EQUITY_PCT");
      confirm({
        title: "保存跟单参数",
        danger: false,
        ok: "保存",
        body: marginEquityChanged
          ? "保证金权益额度将从下一笔新仓立即生效；已有仓位不变。Core资格和组合回测不会自动重跑，将在下一次重采或单独重评后更新。确认保存?"
          : "包含谨慎级参数(影响每一笔新仓),确认即时生效?",
        onConfirm: doIt,
      });
    } else {
      doIt();
    }
  };

  const resetDefaults = () => {
    const category = tab === "add" ? "follow" : tab;
    const label = category === "scanner" ? "钱包采集" : "跟单策略(含加仓)";
    confirm({
      title: "恢复默认配置",
      danger: true,
      ok: "恢复默认",
      body: `将把「${label}」全部参数强制恢复为代码默认值,覆盖你在此页的所有修改。不会自动启动采集。`,
      onConfirm: async () => {
        setSaving(true);
        const t0 = Date.now();
        try {
          await fetch("/api/params/" + category + "/reset", {
            method: "POST",
            headers: { Authorization: "Bearer " + api.token },
          });
        } catch (_e) {}
        try {
          await loadParams();
          clearDirty();
        } catch (_e) {}
        await new Promise(r => setTimeout(r, Math.max(0, 450 - (Date.now() - t0))));
        setSaving(false);
      },
    });
  };

  return (
    <div className="content">
      {saving && <div className="mask"><span className="spin" style={{ width: 34, height: 34, borderWidth: 3 }} /><h2 style={{ marginTop: 22 }}>保存中…</h2></div>}
      <div className="tabs">
        <div className={"tab" + (tab === "scanner" ? " on" : "")} onClick={() => setTab("scanner")}>钱包采集参数</div>
        <div className={"tab" + (tab === "follow" ? " on" : "")} onClick={() => setTab("follow")}>跟单策略参数</div>
        <div className={"tab" + (tab === "add" ? " on" : "")} onClick={() => setTab("add")}>加仓策略</div>
        <button className="btn" title="把本页参数强制恢复为代码默认值" onClick={resetDefaults}
          style={{ marginLeft: "auto", alignSelf: "center", fontSize: 12, padding: "4px 12px" }}>↺ 恢复默认</button>
      </div>

      {tab === "follow" && <SizingPreview vals={vals} />}

      <div className="tbl-wrap">
        {tab === "scanner" && <ScannerSettingsPanel list={list} vals={vals} dirty={dirty} onChange={setValue} />}
        {tab === "follow" && <FollowSettingsPanel list={list} vals={vals} dirty={dirty}
          openTiers={openTiers} setOpenTiers={setOpenTiers}
          validationErrors={validationErrors} badKeys={followValidation.badKeys} onChange={setValue} />}
        {tab === "add" && <AddSettingsPanel list={list} vals={vals} dirty={dirty}
          openTiers={openTiers} setOpenTiers={setOpenTiers} onChange={setValue} />}
      </div>

      {tabDirty.length > 0 && (
        <div className="apply-bar">
          <div className="ab-l">{tabDirty.length} 项未应用改动{tab === "scanner" ? "(需重采生效)" : "(即时生效)"}</div>
          <div style={{ display: "flex", gap: 10 }}>
            <button className="btn" onClick={() => discard(tabDirty.map(p => p.key))}>放弃</button>
            <button className="btn btn-accent" disabled={validationErrors.length > 0} onClick={apply}>{tab === "scanner" ? "保存(下次采集生效)" : "保存(即时生效)"}</button>
          </div>
        </div>
      )}
    </div>
  );
}
