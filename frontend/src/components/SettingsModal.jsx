import { useEffect, useState } from "react";
import { api } from "../api";
import Icon from "./Icon";

export default function SettingsModal({ state, onClose, onSave, onRefreshModels, onStartTour }) {
  const config = state?.config || {};
  const [draft, setDraft] = useState(() => ({
    providerOrder: [...(config.providerOrder || [])],
    providers: JSON.parse(JSON.stringify(config.providers || {})),
    agent: { ...(config.agent || {}) },
    workspace: state?.workspace || "",
  }));
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState("");
  const [error, setError] = useState("");
  const [logLines, setLogLines] = useState(null);
  const [logPath, setLogPath] = useState("");

  async function loadLogs() {
    try {
      const data = await api.logs(80);
      setLogPath(data.path || "");
      setLogLines(data.lines || []);
    } catch (err) {
      setError(err.message || "falha ao ler o log");
    }
  }

  useEffect(() => {
    function onKey(event) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  function setProvider(name, field, value) {
    setDraft((prev) => ({
      ...prev,
      providers: { ...prev.providers, [name]: { ...prev.providers[name], [field]: value } },
    }));
  }

  function setAgent(field, value) {
    setDraft((prev) => ({ ...prev, agent: { ...prev.agent, [field]: value } }));
  }

  function move(index, delta) {
    const order = [...draft.providerOrder];
    const target = index + delta;
    if (target < 0 || target >= order.length) return;
    [order[index], order[target]] = [order[target], order[index]];
    setDraft((prev) => ({ ...prev, providerOrder: order }));
  }

  async function save() {
    setSaving(true);
    setError("");
    const patch = {
      providerOrder: draft.providerOrder,
      providers: Object.fromEntries(
        Object.entries(draft.providers).map(([name, conf]) => [
          name,
          {
            baseUrl: conf.baseUrl,
            rpmLimit: Number(conf.rpmLimit) || 0,
            defaultModel: conf.defaultModel,
          },
        ])
      ),
      agent: {
        ...draft.agent,
        maxSteps: Number(draft.agent.maxSteps) || 12,
        temperature: Number(draft.agent.temperature) || 0,
        maxTokens: Number(draft.agent.maxTokens) || 0,
        maxWaitSeconds: Number(draft.agent.maxWaitSeconds) || 180,
        shellTimeout: Number(draft.agent.shellTimeout) || 120,
        maxConcurrentRequests: Math.max(
          1,
          Math.min(8, Number(draft.agent.maxConcurrentRequests) || 2)
        ),
      },
      workspace: draft.workspace,
    };
    try {
      await onSave(patch);
    } catch (err) {
      setError(err.message || "falha ao salvar");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose} role="presentation">
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Configurações"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <h2>Configurações</h2>
          <button className="icon-button" onClick={onClose} title="Fechar" aria-label="Fechar">
            <Icon name="close" size={15} />
          </button>
        </header>

        <div className="modal-body">
          <section className="tour-settings-cta">
            <div>
              <h3>Tour guiado</h3>
              <p className="muted small">
                Conheça o pool de chaves, o seletor de modelos e o chat em cinco passos.
              </p>
            </div>
            <button className="ghost-button" onClick={onStartTour}>
              <Icon name="compass" size={15} /> Fazer tour
            </button>
          </section>

          <section>
            <h3>Ordem de preferência</h3>
            <p className="muted small">
              A rotação tenta o primeiro provedor; se todas as chaves dele estiverem em cooldown
              ou esgotadas, passa para o próximo.
            </p>
            <ul className="order-list">
              {draft.providerOrder.map((name, index) => (
                <li key={name}>
                  <span>
                    {index + 1}. {draft.providers[name]?.label || name}
                  </span>
                  <span className="row">
                    <button className="icon-button" onClick={() => move(index, -1)} disabled={index === 0}>
                      ↑
                    </button>
                    <button
                      className="icon-button"
                      onClick={() => move(index, 1)}
                      disabled={index === draft.providerOrder.length - 1}
                    >
                      ↓
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          </section>

          {Object.entries(draft.providers).map(([name, conf]) => (
            <section key={name}>
              <h3>{conf.label || name}</h3>
              <div className="grid-2">
                <label>
                  Base URL (compatível com OpenAI)
                  <input
                    value={conf.baseUrl || ""}
                    onChange={(event) => setProvider(name, "baseUrl", event.target.value)}
                    placeholder="https://integrate.api.nvidia.com/v1"
                  />
                </label>
                <label>
                  Limite local (RPM por chave)
                  <input
                    type="number"
                    min="0"
                    value={conf.rpmLimit ?? 0}
                    onChange={(event) => setProvider(name, "rpmLimit", event.target.value)}
                  />
                </label>
                <label>
                  Modelo padrão (usado no fallback)
                  <input
                    value={conf.defaultModel || ""}
                    onChange={(event) => setProvider(name, "defaultModel", event.target.value)}
                  />
                </label>
                {name === "anthropic" ? (
                  <p className="muted tiny" style={{ gridColumn: "1 / -1" }}>
                    A Anthropic usa um proxy local compatível com OpenAI. Suba o Vandamme
                    Proxy em <code>127.0.0.1:8082</code>; a chave da Anthropic fica nele, não
                    no KeyForge.
                  </p>
                ) : null}
                <label>
                  Modelos no catálogo
                  <div className="catalog">
                    <span className="muted small">{(conf.models || []).length} modelo(s)</span>
                    <button
                      className="link-button"
                      disabled={syncing === name}
                      onClick={async () => {
                        setSyncing(name);
                        await onRefreshModels(name);
                        setSyncing("");
                      }}
                    >
                      {syncing === name ? "sincronizando…" : "sincronizar via /v1/models"}
                    </button>
                  </div>
                </label>
              </div>
            </section>
          ))}

          <section>
            <h3>Diretório de trabalho</h3>
            <label>
              Caminho absoluto onde o agente lê/escreve arquivos
              <input
                value={draft.workspace || ""}
                onChange={(event) => setDraft((prev) => ({ ...prev, workspace: event.target.value }))}
                placeholder="C:\Users\voce\projetos\meu-app"
              />
            </label>
          </section>

          <section>
            <h3>Agente</h3>
            <div className="grid-3">
              <label>
                Passos máx. (ReAct)
                <input
                  type="number"
                  min="1"
                  max="40"
                  value={draft.agent.maxSteps ?? 12}
                  onChange={(event) => setAgent("maxSteps", event.target.value)}
                />
              </label>
              <label>
                Temperatura
                <input
                  type="number"
                  step="0.1"
                  min="0"
                  max="2"
                  value={draft.agent.temperature ?? 0.2}
                  onChange={(event) => setAgent("temperature", event.target.value)}
                />
              </label>
              <label>
                Max tokens resposta
                <input
                  type="number"
                  min="0"
                  value={draft.agent.maxTokens ?? 4096}
                  onChange={(event) => setAgent("maxTokens", event.target.value)}
                />
              </label>
              <label>
                Espera máx. por chave (s)
                <input
                  type="number"
                  min="0"
                  value={draft.agent.maxWaitSeconds ?? 180}
                  onChange={(event) => setAgent("maxWaitSeconds", event.target.value)}
                />
              </label>
              <label>
                Timeout de shell (s)
                <input
                  type="number"
                  min="5"
                  value={draft.agent.shellTimeout ?? 120}
                  onChange={(event) => setAgent("shellTimeout", event.target.value)}
                />
              </label>
              <label>
                Ferramentas em paralelo
                <input
                  type="number"
                  min="1"
                  max="8"
                  value={draft.agent.maxConcurrentRequests ?? 2}
                  onChange={(event) =>
                    setAgent("maxConcurrentRequests", event.target.value)
                  }
                />
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={Boolean(draft.agent.autoApproveShell)}
                  onChange={(event) => setAgent("autoApproveShell", event.target.checked)}
                />
                Aprovar comandos de shell automaticamente
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={Boolean(draft.agent.allowSystemCommands)}
                  onChange={(event) => setAgent("allowSystemCommands", event.target.checked)}
                />
                Permitir execução de comandos do sistema (necessário para compilar e rodar scripts
                externos)
              </label>
            </div>
          </section>

          <section>
            <h3>Log de depuração</h3>
            <p className="muted small">
              Cada chamada ao provedor registra a URL exata, o modelo, as ferramentas e o payload
              JSON (chaves sempre mascaradas). Use para diagnosticar 404/400.
            </p>
            <div className="row">
              <button className="ghost-button" onClick={loadLogs}>
                ver últimas 80 linhas
              </button>
              <span className="muted tiny" title={logPath}>
                {logPath || "%APPDATA%\\KeyForge\\logs\\agent.log"}
              </span>
            </div>
            {logLines ? (
              <pre className="log-view">{logLines.join("\n") || "(log vazio)"}</pre>
            ) : null}
          </section>

          <section>
            <h3>Prompt do sistema</h3>
            <textarea
              rows={6}
              value={draft.agent.systemPrompt || ""}
              onChange={(event) => setAgent("systemPrompt", event.target.value)}
            />
          </section>

          {error ? <p className="error-text">{error}</p> : null}
        </div>

        <footer className="modal-foot">
          <span className="muted tiny" title={config.configPath}>
            {config.configPath}
          </span>
          <span className="row">
            <button className="ghost-button" onClick={onClose}>
              Cancelar
            </button>
            <button className="primary" onClick={save} disabled={saving}>
              {saving ? "salvando…" : "Salvar"}
            </button>
          </span>
        </footer>
      </div>
    </div>
  );
}
