// Cliente da API local do KeyForge (mesma origem do backend).

async function request(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      throw new Error(`resposta inválida do backend (HTTP ${response.status})`);
    }
  }
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

export const api = {
  state: () => request("/api/state"),
  health: () => request("/api/health"),
  addKey: (provider, key, label) =>
    request("/api/keys", { method: "POST", body: { provider, key, label } }),
  removeKey: (provider, id) => request(`/api/keys/${provider}/${id}`, { method: "DELETE" }),
  resetKey: (provider, id) => request(`/api/keys/${provider}/${id}/reset`, { method: "POST" }),
  testKey: (provider, id) => request(`/api/keys/${provider}/${id}/test`, { method: "POST" }),
  reorderKeys: (provider, ids) =>
    request(`/api/keys/${provider}/reorder`, { method: "POST", body: { ids } }),
  refreshModels: (provider) =>
    request(`/api/providers/${provider}/models/refresh`, { method: "POST" }),
  testModels: (body = {}) => request("/api/models/health", { method: "POST", body }),
  patchConfig: (patch) => request("/api/config", { method: "PATCH", body: patch }),
  setWorkspace: (path) => request("/api/workspace", { method: "POST", body: { path } }),
  pickWorkspace: () => request("/api/workspace/pick", { method: "POST", body: {} }),
  logs: (tail = 200) => request(`/api/logs?tail=${tail}`),
  approve: (approvalId, allow) =>
    request(`/api/approvals/${approvalId}`, {
      method: "POST",
      body: { decision: allow ? "allow" : "deny" },
    }),
};

/**
 * Envia um turno para o agente e consome o stream SSE de eventos.
 * Eventos: start, step, route, waiting, notice, delta, tool_call,
 * approval_required, tool_result, done, error.
 */
export async function streamChat({ messages, model, autoApproveShell, onEvent, signal }) {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages, model, autoApproveShell }),
    signal,
  });

  if (!response.ok || !response.body) {
    let message = `HTTP ${response.status}`;
    try {
      const data = await response.json();
      message = data.error || message;
    } catch {
      /* corpo não-JSON */
    }
    throw new Error(message);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const line = block.split("\n").find((item) => item.startsWith("data:"));
      if (line) {
        try {
          onEvent(JSON.parse(line.slice(5).trim()));
        } catch {
          /* evento malformado — ignora */
        }
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
}
