import { api } from "../../lib/api.js";
import { fNum, fSign } from "../../lib/format.js";

const { useState } = React;

const auditStage = (s) => ({ profile: "画像", watchlist: "名单", follow_line: "跟单线", auto_tune: "调参" }[s] || s || "—");

const auditCopyText = (payload) => {
  const c = payload && payload.copyBt;
  if (!c) return null;
  const v30 = c["30dNetPnl"], v14 = c["14dNetPnl"], v7 = c["7dNetPnl"];
  if (v30 == null && v14 == null && v7 == null) return null;
  return `copy 30d ${fSign(v30 || 0, 0)} / 14d ${fSign(v14 || 0, 0)} / 7d ${fSign(v7 || 0, 0)}`;
};

function WalletAuditBox({ state }) {
  const audit = state || { loading: true, events: [] };
  if (audit.loading) return <div className="audit-box muted">加载审计记录…</div>;
  if (audit.error) return <div className="audit-box down">审计记录读取失败</div>;
  if (!audit.events.length) return <div className="audit-box muted">暂无该钱包的审计记录</div>;
  return (
    <div className="audit-box">
      {audit.events.map(e => {
        const copy = auditCopyText(e.payload);
        return (
          <div className="audit-event" key={e.id}>
            <div>
              <span className={"tint " + (e.status === "active" || e.status === "followed" ? "tint-green" : e.status === "below_line" ? "tint-amber" : "tint-red")}>{auditStage(e.stage)}</span>
              <span className="muted" style={{ marginLeft: 8 }}>{e.status || "—"} · {e.reason || "—"}</span>
            </div>
            <div className="audit-meta">
              <span>{e.stamp ? e.stamp.slice(5, 16).replace("T", " ") : "—"}</span>
              {e.rawScore != null && <span>raw {fNum(e.rawScore, 1)}</span>}
              {e.followScore != null && <span>follow {fNum(e.followScore, 1)}</span>}
              {copy && <span>{copy}</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function useWalletAudit() {
  const [auditOpen, setAuditOpen] = useState({});
  const [audits, setAudits] = useState({});

  const resetAudits = () => {
    setAuditOpen({});
    setAudits({});
  };

  const toggleAudit = (addr) => {
    const key = (addr || "").toLowerCase();
    setAuditOpen(s => ({ ...s, [key]: !s[key] }));
    if (!audits[key]) {
      setAudits(s => ({ ...s, [key]: { loading: true, events: [] } }));
      api.get("/api/pipeline-audit?addr=" + encodeURIComponent(key) + "&limit=8&compact=1")
        .then(res => setAudits(s => ({ ...s, [key]: { loading: false, events: res.events || [] } })))
        .catch(() => setAudits(s => ({ ...s, [key]: { loading: false, error: true, events: [] } })));
    }
  };

  const auditBox = (addr) => {
    const key = (addr || "").toLowerCase();
    return <WalletAuditBox state={audits[key]} />;
  };

  return { auditOpen, resetAudits, toggleAudit, auditBox };
}
