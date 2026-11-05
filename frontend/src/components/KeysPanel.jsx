import { useEffect, useMemo, useState } from "react";
import Icon from "./Icon";

const STATUS_LABEL = {
  active: "ativa",
  cooldown: "cooldown",
  exhausted: "esgotada",
};

const HINTS = {
  nvidia: {
    key: "nvapi-…",
    hint: "Chave de build.nvidia.com — 40 RPM por chave",
  },
  freebuff: {
    key: "token do proxy (pode ficar vazio)",
    hint: "Proxy Freebuff2API — o token é o que o proxy aceita",
  },
  openai: {
    key: "sk-…",
    hint: "platform.openai.com → API keys (gpt-4o / gpt-4o-mini)",
  },
  google: {
    key: "AIza…",
    hint: "aistudio.google.com → API key (endpoint OpenAI-compatível do Gemini)",
  },
  deepseek: {
    key: "sk-…",
    hint: "platform.deepseek.com → API keys (deepseek-chat / deepseek-reasoner)",
  },
  openrouter: {
    key: "sk-or-v1-…",
    hint: "openrouter.ai/keys — modelos no formato vendor/modelo",
  },
  anthropic: {
    key: "opcional (o proxy guarda a chave da Anthropic)",
    hint: "Vandamme Proxy em 127.0.0.1:8082/v1 traduz para a API da Anthropic",
  },
};

const FALLBACK_HINT = {
  key: "chave de API",
  hint: "Provedor compatível com OpenAI",
};

function formatTokens(total) {
  const value = Number(total || 0);
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M tok`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k tok`;
  return `${value} tok`;
}

export default function KeysPanel({
  state,
  now,
  fetchedAt,
  onAdd,
  onRemove,
  onReset,
  onTest,
  onRefreshModels,
  onOpenSettings,
  onNotify,
  onSetWorkspace,
  onPickWorkspace,
}) {
  const [openForm, setOpenForm] = useState("");
  const [draft, setDraft] = useState({ key: "", label: "" });
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState("");
  const [workspaceDraft, setWorkspaceDraft] = useState("");
  const [workspaceDirty, setWorkspaceDirty] = useState(false);
  const [savingWorkspace, setSavingWorkspace] = useState(false);
  const [pickingWorkspace, setPickingWorkspace] = useState(false);

  useEffect(() => {
    if (!workspaceDirty) setWorkspaceDraft(state?.workspace || "");
  }, [state?.workspace, workspaceDirty]);

  async function pickWorkspace() {
    setPickingWorkspace(true);
    const data = await onPickWorkspace?.();
    setPickingWorkspace(false);
    if (!data) return;
    if (data.canceled) {
      onNotify?.("seleção de pasta cancelada", "info");
      return;
    }
    setWorkspaceDirty(false);
    setWorkspaceDraft(data.workspace || "");
    onNotify?.(`Diretório de trabalho: ${data.workspace}`, "ok");
  }

  async function saveWorkspace() {
    const path = workspaceDraft.trim();
    if (!path) {
      onNotify?.("Informe o caminho completo da pasta do projeto", "error");
      return;
    }
    setSavingWorkspace(true);
    const data = await onSetWorkspace(path);
    setSavingWorkspace(false);
    if (data) {
      setWorkspaceDirty(false);
      onNotify?.(`Diretório de trabalho: ${data.workspace}`, "ok");
    }
  }

  const providers = useMemo(() => {
    if (!state?.config) return [];
    const order = state.config.providerOrder || Object.keys(state.config.providers || {});
    return order.map((name) => ({
      name,
      conf: state.config.providers?.[name] || {},
      keys: (state.keys || []).filter((key) => key.provider === name),
    }));
  }, [state]);

  const drift = Math.max(0, (now - fetchedAt) / 1000);

  async function submit(provider) {
    setSaving(true);
    await onAdd(provider, draft.key, draft.label);
    setSaving(false);
    setDraft({ key: "", label: "" });
    setOpenForm("");
  }

  async function sync(provider) {
    setSyncing(provider);
    const data = await onRefreshModels(provider);
    setSyncing("");
    if (data) {
      onNotify?.(`${data.count} modelos sincronizados em ${provider}`, "ok");
    }
  }

  return (
    <aside className="sidebar glass" data-tour="keys">
      <div className="sidebar-head">
        <h2>Pool de chaves</h2>
        <button className="icon-button" onClick={onOpenSettings} title="Configurações">
          ⚙
        </button>
      </div>

      <section className="workspace-box">
        <h3>Diretório de trabalho</h3>
        <input
          value={workspaceDraft}
          placeholder="C:\Users\WIN\Desktop\Projeto"
          spellCheck={false}
          onChange={(event) => {
            setWorkspaceDirty(true);
            setWorkspaceDraft(event.target.value);
          }}
          title="Pasta base de read_file, write_file, list_directory e run_shell"
        />
        <div className="row">
          <button className="primary small" onClick={saveWorkspace} disabled={savingWorkspace}>
            {savingWorkspace ? "salvando…" : "usar esta pasta"}
          </button>
          <button className="ghost-button small" onClick={pickWorkspace} disabled={pickingWorkspace}>
            {pickingWorkspace ? (
              "abrindo…"
            ) : (
              <>
                <Icon name="folder" size={13} /> selecionar pasta
              </>
            )}
          </button>
          <button
            className="link-button"
            onClick={() => {
              setWorkspaceDirty(false);
              setWorkspaceDraft(state?.workspace || "");
            }}
          >
            reverter
          </button>
        </div>
        <p className="muted tiny">
          {state?.workspaceExists === false
            ? "pasta inexistente — será criada ao salvar"
            : "sandbox: arquivos fora desta pasta são bloqueados"}
        </p>
      </section>

      {!state ? (
        <p className="muted small">Carregando…</p>
      ) : (
        providers.map((provider) => {
          const hint = HINTS[provider.name] || FALLBACK_HINT;
          return (
            <section className="provider" key={provider.name}>
              <div className="provider-head">
                <div>
                  <h3>{provider.conf.label || provider.name}</h3>
                  <p className="muted tiny" title={provider.conf.baseUrl}>
                    {provider.conf.baseUrl || "sem base URL"}
                  </p>
                </div>
                <span className="count" title="chaves cadastradas">
                  {provider.keys.length}
                </span>
              </div>

              <div className="provider-meta">
                <span className="tag">
                  {provider.conf.rpmLimit ? `${provider.conf.rpmLimit} RPM/chave` : "sem limite local"}
                </span>
                <span className="tag" title="modelos disponíveis no seletor">
                  {(provider.conf.models || []).length} modelos
                </span>
                <button
                  className="link-button"
                  onClick={() => sync(provider.name)}
                  disabled={syncing === provider.name || provider.keys.length === 0}
                  title="Buscar modelos reais em /v1/models"
                >
                  {syncing === provider.name ? "sincronizando…" : "sincronizar modelos"}
                </button>
              </div>

              <div className="keys">
                {provider.keys.length === 0 ? (
                  <p className="muted small">Nenhuma chave — este provedor fica fora da rotação.</p>
                ) : (
                  provider.keys.map((key) => (
                    <KeyCard
                      key={key.id}
                      item={key}
                      drift={drift}
                      onRemove={onRemove}
                      onReset={onReset}
                      onTest={onTest}
                    />
                  ))
                )}
              </div>

              {openForm === provider.name ? (
                <form
                  className="key-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    submit(provider.name);
                  }}
                >
                  <input
                    placeholder="nome (ex.: conta-1)"
                    value={draft.label}
                    onChange={(event) => setDraft((prev) => ({ ...prev, label: event.target.value }))}
                  />
                  <input
                    placeholder={hint.key}
                    value={draft.key}
                    onChange={(event) => setDraft((prev) => ({ ...prev, key: event.target.value }))}
                    autoFocus
                  />
                  <p className="muted tiny">{hint.hint}</p>
                  <div className="row">
                    <button type="submit" className="primary small" disabled={saving}>
                      {saving ? "salvando…" : "Adicionar"}
                    </button>
                    <button type="button" className="link-button" onClick={() => setOpenForm("")}>
                      cancelar
                    </button>
                  </div>
                </form>
              ) : (
                <button
                  className="dashed"
                  onClick={() => {
                    setOpenForm(provider.name);
                    setDraft({ key: "", label: "" });
                  }}
                >
                  + adicionar chave
                </button>
              )}
            </section>
          );
        })
      )}
    </aside>
  );
}

function KeyCard({ item, drift, onRemove, onReset, onTest }) {
  const [testing, setTesting] = useState(false);
  const [feedback, setFeedback] = useState("");

  const remaining = Math.max(0, Math.round((item.cooldownRemaining || 0) - drift));
  const rpm = item.rpm || {};
  const percent = rpm.limit ? Math.min(100, Math.round((rpm.used / rpm.limit) * 100)) : 0;

  async function test() {
    setTesting(true);
    const data = await onTest(item.provider, item.id);
    setTesting(false);
    setFeedback(data ? data.message || "ok" : "falhou");
    window.setTimeout(() => setFeedback(""), 6000);
  }

  return (
    <article className={`key-card status-${item.status}`}>
      <header className="key-head">
        <span className="key-label" title={item.label}>
          {item.label}
        </span>
        <span className={`badge badge-${item.status}`}>{STATUS_LABEL[item.status] || item.status}</span>
      </header>

      <code className="key-value">{item.maskedKey}</code>

      {rpm.limit ? (
        <div className="meter" title={`${rpm.used} de ${rpm.limit} requisições na janela de 60s`}>
          <div className={`meter-fill ${percent >= 90 ? "hot" : ""}`} style={{ width: `${percent}%` }} />
          <span className="meter-text">
            {rpm.used}/{rpm.limit} RPM
          </span>
        </div>
      ) : null}

      <div className="key-stats">
        <span>{item.stats?.requests || 0} req</span>
        <span>{formatTokens(item.stats?.tokens)}</span>
        {item.status === "cooldown" ? <span className="warn">libera em {remaining}s</span> : null}
      </div>

      {item.reason ? <p className="reason">{item.reason}</p> : null}
      {feedback ? <p className="feedback">{feedback}</p> : null}

      <footer className="key-actions">
        <button onClick={test} disabled={testing}>
          {testing ? "testando…" : "testar"}
        </button>
        <button onClick={() => onReset(item.provider, item.id)} disabled={item.status === "active"}>
          reativar
        </button>
        <button className="danger" onClick={() => onRemove(item.provider, item.id)}>
          remover
        </button>
      </footer>
    </article>
  );
}
