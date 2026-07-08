import { api } from "../../lib/api.js";

const { useState, useEffect } = React;

const valuesFromParams = (params) => {
  const vals = {};
  [...(params?.scanner || []), ...(params?.follow || [])].forEach(p => {
    vals[p.key] = p.value;
  });
  return vals;
};

export function useSettingsParams() {
  const [params, setParams] = useState(null);
  const [vals, setVals] = useState({});
  const [dirty, setDirty] = useState({});
  const [scoreDist, setScoreDist] = useState(null);

  const loadParams = async () => {
    try {
      const next = await api.get("/api/params?includeScoreDist=1");
      setParams(next);
      setVals(valuesFromParams(next));
      if (next.scoreDist) setScoreDist(next.scoreDist);
      return next;
    } catch (_e) {
      const next = await api.get("/api/params");
      setParams(next);
      setVals(valuesFromParams(next));
      api.get("/api/score-dist").then(setScoreDist).catch(() => {});
      return next;
    }
  };

  useEffect(() => {
    loadParams().catch(() => {});
  }, []);

  const setValue = (key, val) => {
    setVals(v => ({ ...v, [key]: val }));
    setDirty(d => ({ ...d, [key]: true }));
  };

  const clearDirty = () => setDirty({});

  const discard = (keys) => {
    const original = valuesFromParams(params);
    setVals(current => {
      const next = { ...current };
      keys.forEach(k => { next[k] = original[k]; });
      return next;
    });
    clearDirty();
  };

  return {
    params,
    vals,
    dirty,
    scoreDist,
    loadParams,
    setValue,
    clearDirty,
    discard,
  };
}
