const TOK_KEY = "hl_dash_token";

export const api = {
  token: localStorage.getItem(TOK_KEY) || null,

  async login(username, pw) {
    const r = await fetch("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password: pw }) });
    if (!r.ok) throw new Error("login_failed");
    const d = await r.json();
    api.token = d.token;
    localStorage.setItem(TOK_KEY, d.token);
    return d;
  },

  logout() {
    api.token = null;
    localStorage.removeItem(TOK_KEY);
  },

  async get(path) {
    const r = await fetch(path, { headers: { Authorization: "Bearer " + api.token } });
    if (r.status === 401) {
      api.logout();
      throw new Error("unauth");
    }
    return (await r.json()).data;
  },

  async cmd(type, payload) {
    const r = await fetch("/api/commands", {
      method: "POST",
      headers: { Authorization: "Bearer " + api.token },
      body: JSON.stringify({ type, payload }),
    });
    return r.json();
  },

  async patchParams(category, body) {
    const r = await fetch("/api/params/" + category, {
      method: "PATCH",
      headers: { Authorization: "Bearer " + api.token },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error("param_patch_failed");
    return (await r.json()).data;
  },
};
