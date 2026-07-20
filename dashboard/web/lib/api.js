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

  async cmdAndWait(type, payload, timeoutMs = 50000) {
    const queued = await api.cmd(type, payload);
    if (!queued.commandId) throw new Error(queued.error || "command_failed");
    const until = Date.now() + timeoutMs;
    while (Date.now() < until) {
      const state = await api.get("/api/commands/" + queued.commandId);
      if (state.status === "done") return state.result || state;
      if (state.status === "failed" || state.status === "error") throw new Error(state.error || "command_failed");
      await new Promise(resolve => setTimeout(resolve, 700));
    }
    throw new Error("command_timeout");
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

const bytesToB64 = bytes => {
  let binary = "";
  const view = new Uint8Array(bytes);
  for (let i = 0; i < view.length; i += 0x8000) binary += String.fromCharCode(...view.subarray(i, i + 0x8000));
  return btoa(binary);
};

const b64ToBytes = value => Uint8Array.from(atob(value), c => c.charCodeAt(0));

export async function encryptCredential(secret, wrapKey) {
  if (!window.crypto || !window.crypto.subtle || !wrapKey || !wrapKey.spki) throw new Error("secure_context_required");
  const publicKey = await window.crypto.subtle.importKey(
    "spki", b64ToBytes(wrapKey.spki), { name: "RSA-OAEP", hash: "SHA-256" }, false, ["encrypt"]
  );
  const dek = await window.crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, true, ["encrypt"]);
  const rawDek = await window.crypto.subtle.exportKey("raw", dek);
  const nonce = window.crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await window.crypto.subtle.encrypt(
    { name: "AES-GCM", iv: nonce }, dek, new TextEncoder().encode(secret)
  );
  const wrappedKey = await window.crypto.subtle.encrypt({ name: "RSA-OAEP" }, publicKey, rawDek);
  return { envelopeVersion: wrapKey.envelopeVersion, keyId: wrapKey.keyId,
    wrappedKey: bytesToB64(wrappedKey), nonce: bytesToB64(nonce), ciphertext: bytesToB64(ciphertext) };
}
