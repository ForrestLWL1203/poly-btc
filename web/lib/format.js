export const fUsd = (v, d = 0) => (v == null ? "—" : "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));

export const fSign = (v, d = 0) => (v == null ? "—" : (v >= 0 ? "+" : "") + Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));

export const fTime = (ep) => (ep == null ? "—" : new Date(ep * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }));

export const fPct = (v, d = 1) => (v == null ? "—" : (v >= 0 ? "+" : "") + Number(v).toFixed(d) + "%");

export const fNum = (v, d = 1) => (v == null ? "—" : Number(v).toFixed(d));

const paramDigits = (v, ptype) => {
  if (ptype === "int") return 0;
  if (ptype === "pct") return 1;
  if (ptype === "float") {
    const n = Math.abs(Number(v));
    return n > 0 && n < 1 ? 3 : 2;
  }
  return 1;
};

export const fParam = (v, ptypeOrDigits = null) => {
  if (v == null || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  const d = typeof ptypeOrDigits === "number" ? ptypeOrDigits : paramDigits(v, ptypeOrDigits);
  return String(Number(n.toFixed(d)));
};

export const fPrice = (v) => {
  if (v == null) return "—";
  const a = Math.abs(Number(v));
  if (a === 0) return "0";
  if (a >= 1000) return Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (a >= 1) return Number(v).toFixed(2);
  if (a >= 0.01) return Number(v).toFixed(4);
  if (a >= 0.0001) return Number(v).toFixed(6);
  return Number(v).toPrecision(3);
};

export const fDur = (s) => {
  if (s == null) return "—";
  if (s < 60) return Math.round(s) + "s";
  if (s < 3600) return (s / 60).toFixed(s < 600 ? 1 : 0) + "m";
  if (s < 86400) return (s / 3600).toFixed(1) + "h";
  return (s / 86400).toFixed(1) + "d";
};

export const short = (a) => (a ? a.slice(0, 6) + "…" + a.slice(-4) : "—");

export const cls = (v) => (v == null ? "" : v >= 0 ? "up" : "down");

export const normalizeCoin = (c) => String(c || "").trim().toUpperCase();

export const parseCoinList = (v) => {
  const src = Array.isArray(v) ? v.join(",") : String(v || "");
  return Array.from(new Set(src.split(/[\s,;，、]+/).map(normalizeCoin).filter(Boolean))).sort();
};

export const formatCoinList = (coins) => parseCoinList(coins).join(", ");

export const agoText = (iso) => {
  if (!iso) return "—";
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return s + "s 前";
  if (s < 3600) return Math.floor(s / 60) + "m 前";
  if (s < 86400) return Math.floor(s / 3600) + "h 前";
  return Math.floor(s / 86400) + "d 前";
};

export const SCANNER_LABEL = { rolling: "滚动采集中", scanning: "采集扫描中", idle: "空闲", stopped: "已停止", unknown: "未上报" };

export const scannerColor = (mode, stale) => {
  if (mode === "scanning") return "var(--amber)";
  if (mode === "rolling" && !stale) return "var(--green-l)";
  if (mode === "idle" && !stale) return "var(--t2)";
  return "var(--red-l)";
};
