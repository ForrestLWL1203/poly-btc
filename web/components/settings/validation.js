import { TIER_GROUPS } from "./paramMeta.js";

export function validateFollowParams(vals) {
  const badKeys = new Set();
  const errors = [];
  const numVal = k => Number(vals[k]);
  const markErr = (msg, keys) => {
    errors.push(msg);
    (keys || []).forEach(k => badKeys.add(k));
  };
  const validatePct = (label, key) => {
    const v = numVal(key);
    if (!Number.isFinite(v)) {
      markErr(`${label} 必须是数字`, [key]);
      return false;
    }
    if (v < 0 || v > 100) {
      markErr(`${label} 必须在 0–100% 之间`, [key]);
      return false;
    }
    return true;
  };

  TIER_GROUPS.forEach(g => {
    const okMin = validatePct(`${g.label}保证金下限`, g.min);
    const okMax = validatePct(`${g.label}保证金上限`, g.max);
    if (okMin && okMax && numVal(g.min) > numVal(g.max)) {
      markErr(`${g.label}保证金下限不能高于上限`, [g.min, g.max]);
    }
  });
  const okFull = validatePct("满火力占用线", "DEPLOY_FULL_PCT");
  const okLock = validatePct("组合部署上限", "MAX_DEPLOY_PCT");
  if (okFull && okLock && numVal("DEPLOY_FULL_PCT") >= numVal("MAX_DEPLOY_PCT")) {
    markErr("满火力占用线必须低于组合部署上限", ["DEPLOY_FULL_PCT", "MAX_DEPLOY_PCT"]);
  }

  return { errors, badKeys };
}
