import { api } from "../../lib/api.js";

const { useCallback, useState, useEffect } = React;

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

  const loadParams = useCallback(async () => {
    const next = await api.get("/api/params");
    setParams(next);
    setVals(valuesFromParams(next));
    return next;
  }, []);

  useEffect(() => {
    loadParams().catch(() => {});
  }, [loadParams]);

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
    loadParams,
    setValue,
    clearDirty,
    discard,
  };
}
