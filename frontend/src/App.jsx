import { useCallback, useEffect, useRef, useState } from "react";
import { api, streamChat } from "./api";
import ChatList from "./components/ChatList";
import ChatPanel from "./components/ChatPanel";
import KeysPanel from "./components/KeysPanel";
import ModelSelector from "./components/ModelSelector";
import SettingsModal from "./components/SettingsModal";
import Icon from "./components/Icon";
import { useTour } from "@waypoint-tours/react";
import { applyTheme, getInitialTheme, saveTheme } from "./theme";
import { closeTourLayer } from "./tourLayer";

const CHATS_KEY = "keyforge.chats.v1";
const TOUR_KEY = "keyforge.tour.v1";
const LEGACY_KEY = "keyforge.chat.v1";
const MAX_STORED_CHATS = 40;
const MAX_STORED_MESSAGES = 60;

const uid = () =>
  typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2);

function titleFrom(messages) {
  const first = (messages || []).find((item) => item.role === "user" && item.content?.trim());
  if (!first) return "";
  const text = first.content.trim().replace(/\s+/g, " ");
  return text.length > 52 ? `${text.slice(0, 52)}…` : text;
}

function newChatRecord() {
  const now = Date.now();
  return { id: uid(), title: "", createdAt: now, updatedAt: now, messages: [] };
}

function loadChats() {
  try {
    const raw = localStorage.getItem(CHATS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        const clean = parsed.filter(
          (chat) => chat && typeof chat.id === "string" && Array.isArray(chat.messages)
        );
        if (clean.length) return clean;
      }
    }
    const legacy = JSON.parse(localStorage.getItem(LEGACY_KEY) || "[]");
    if (Array.isArray(legacy) && legacy.length) {
      const now = Date.now();
      return [
        { id: uid(), title: titleFrom(legacy), createdAt: now, updatedAt: now, messages: legacy },
      ];
    }
  } catch {
    /* storage indisponível */
  }
  return [newChatRecord()];
}

export default function App() {
  const { start: startWaypointTour } = useTour();
  const boot = useRef(null);
  if (!boot.current) {
    const list = loadChats();
    boot.current = { chats: list, activeId: list[0].id };
  }

  const [chats, setChats] = useState(boot.current.chats);
  const [activeId, setActiveId] = useState(boot.current.activeId);
  const [state, setState] = useState(null);
  const [fetchedAt, setFetchedAt] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  const [selection, setSelection] = useState({ provider: "auto", model: "auto" });
  const [busy, setBusy] = useState(false);
  const [route, setRoute] = useState(null);
  const [waiting, setWaiting] = useState(null);
  const [notices, setNotices] = useState([]);
  const [error, setError] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [theme, setTheme] = useState(getInitialTheme);
  const [sidebarOpen, setSidebarOpen] = useState(
    () => (typeof window === "undefined" ? true : window.innerWidth > 900)
  );
  const [chatsOpen, setChatsOpen] = useState(
    () => (typeof window === "undefined" ? true : window.innerWidth > 1024)
  );
  const [syncingModels, setSyncingModels] = useState(false);
  const [testingModels, setTestingModels] = useState(false);

  const abortRef = useRef(null);
  const selectionReady = useRef(false);
  const tourStartRef = useRef(startWaypointTour);

  const activeChat = chats.find((chat) => chat.id === activeId) || chats[0] || null;
  const messages = activeChat?.messages || [];

  // Em telas pequenas as duas barras laterais são gavetas: abrir uma fecha a outra.
  const toggleSidebar = () => {
    setSidebarOpen((open) => {
      const next = !open;
      if (next && window.innerWidth <= 900) setChatsOpen(false);
      return next;
    });
  };

  const toggleChats = () => {
    setChatsOpen((open) => {
      const next = !open;
      if (next && window.innerWidth <= 900) setSidebarOpen(false);
      return next;
    });
  };

  const notify = useCallback((text, tone = "info") => {
    const id = uid();
    setNotices((prev) => [...prev.slice(-2), { id, text, tone }]);
    window.setTimeout(() => {
      setNotices((prev) => prev.filter((item) => item.id !== id));
    }, 8000);
  }, []);

  useEffect(() => {
    tourStartRef.current = startWaypointTour;
  }, [startWaypointTour]);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const toggleTheme = useCallback(() => {
    setTheme((prev) => {
      const next = prev === "dark" ? "light" : "dark";
      saveTheme(next);
      return next;
    });
  }, []);

  const startTour = useCallback(() => {
    try {
      window.localStorage.setItem(TOUR_KEY, "done");
    } catch {
      /* storage indisponível */
    }
    setSettingsOpen(false);
    setError("");
    setSidebarOpen(true);
    if (window.innerWidth <= 1024) setChatsOpen(false);

    const begin = () => {
      if (!document.querySelector('[data-tour="keys"]')) {
        window.setTimeout(begin, 120);
        return;
      }
      tourStartRef.current("keyforge-onboarding").catch((err) => {
        closeTourLayer();
        notify(err?.message || "não foi possível iniciar o tour", "error");
      });
    };
    window.setTimeout(begin, 80);
  }, [notify]);

  // Tour de boas-vindas: uma vez por instalação, no primeiro acesso.
  useEffect(() => {
    let seen = false;
    try {
      seen = window.localStorage.getItem(TOUR_KEY) === "done";
    } catch {
      /* storage indisponível */
    }
    if (seen) return undefined;
    const timer = window.setTimeout(startTour, 1100);
    return () => window.clearTimeout(timer);
  }, [startTour]);

  // O tour abre a gaveta de chaves no passo 2 e libera o chat nos seguintes.
  useEffect(() => {
    function onTourStep(event) {
      const stepId = event.detail?.stepId;
      if (stepId === "keys") {
        setSidebarOpen(true);
        if (window.innerWidth <= 1024) setChatsOpen(false);
        return;
      }
      if (window.innerWidth <= 900) setSidebarOpen(false);
      if (window.innerWidth <= 1024) setChatsOpen(false);
    }
    window.addEventListener("keyforge:tour-step", onTourStep);
    return () => window.removeEventListener("keyforge:tour-step", onTourStep);
  }, []);

  const mergeState = useCallback((data) => {
    if (!data) return;
    setState((prev) => ({ ...(prev || {}), ...data }));
    setFetchedAt(Date.now());
  }, []);

  const refresh = useCallback(async () => {
    try {
      const data = await api.state();
      setState(data);
      setFetchedAt(Date.now());
      if (!selectionReady.current && data.config?.selectedModel) {
        setSelection(data.config.selectedModel);
        selectionReady.current = true;
      }
      return data;
    } catch (err) {
      setError(err.message || "não foi possível falar com o backend local");
      return null;
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const poll = setInterval(refresh, 5000);
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      clearInterval(poll);
      clearInterval(tick);
    };
  }, [refresh]);

  // Persistência do histórico de conversas
  useEffect(() => {
    try {
      const payload = chats.slice(0, MAX_STORED_CHATS).map((chat) => ({
        ...chat,
        messages: chat.messages.slice(-MAX_STORED_MESSAGES),
      }));
      localStorage.setItem(CHATS_KEY, JSON.stringify(payload));
    } catch {
      /* storage cheio — ignora */
    }
  }, [chats]);

  const patchChat = useCallback((chatId, updater) => {
    setChats((prev) =>
      prev.map((chat) => (chat.id === chatId ? { ...updater(chat), updatedAt: Date.now() } : chat))
    );
  }, []);

  const runAction = useCallback(
    async (action, successMessage) => {
      try {
        const data = await action();
        mergeState(data);
        if (successMessage) {
          notify(typeof successMessage === "function" ? successMessage(data) : successMessage, "ok");
        }
        return data;
      } catch (err) {
        const message = err.message || "ação falhou";
        setError(message);
        notify(message, "error");
        return null;
      }
    },
    [mergeState, notify]
  );

  const send = useCallback(
    async (text) => {
      const content = (text || "").trim();
      if (!content || busy || !activeChat) return;

      const chatId = activeChat.id;
      setError("");
      setBusy(true);
      setWaiting(null);
      setRoute(null);

      const userMessage = { id: uid(), role: "user", content };
      const assistantId = uid();
      const assistantMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        reasoning: "",
        tools: [],
        usage: null,
        done: false,
        error: "",
      };
      const history = [...activeChat.messages, userMessage]
        .filter((item) => item.role === "user" || item.role === "assistant")
        .map((item) => ({ role: item.role, content: item.content }));

      setChats((prev) =>
        prev.map((chat) =>
          chat.id === chatId
            ? {
                ...chat,
                messages: [...chat.messages, userMessage, assistantMessage],
                title: chat.title || titleFrom([...chat.messages, userMessage]),
                updatedAt: Date.now(),
              }
            : chat
        )
      );

      const updateAssistant = (id, updater) =>
        patchChat(chatId, (chat) => ({
          ...chat,
          messages: chat.messages.map((item) => (item.id === id ? updater(item) : item)),
        }));

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        await streamChat({
          messages: history,
          model: selection,
          signal: controller.signal,
          onEvent: (event) => {
            switch (event.type) {
              case "delta":
                updateAssistant(assistantId, (item) =>
                  event.channel === "reasoning"
                    ? { ...item, reasoning: item.reasoning + event.text }
                    : { ...item, content: item.content + event.text }
                );
                break;
              case "route":
                setRoute(event);
                setWaiting(null);
                break;
              case "router":
                setWaiting(null);
                notify(event.label, "ok");
                updateAssistant(assistantId, (item) => ({ ...item, router: event }));
                break;
              case "waiting":
                setWaiting(event);
                break;
              case "notice":
                setWaiting(null);
                notify(event.message, "info");
                break;
              case "tool_call":
                setWaiting(null);
                updateAssistant(assistantId, (item) => ({
                  ...item,
                  tools: [
                    ...item.tools,
                    {
                      id: event.id,
                      name: event.name,
                      args: event.args,
                      output: null,
                      ok: null,
                      approval: null,
                    },
                  ],
                }));
                break;
              case "approval_required":
                updateAssistant(assistantId, (item) => ({
                  ...item,
                  tools: item.tools.map((tool) =>
                    tool.id === event.callId
                      ? { ...tool, approval: { id: event.id, pending: true } }
                      : tool
                  ),
                }));
                break;
              case "tool_result":
                updateAssistant(assistantId, (item) => ({
                  ...item,
                  tools: item.tools.map((tool) =>
                    tool.id === event.id
                      ? {
                          ...tool,
                          output: event.output,
                          ok: event.ok,
                          approval: tool.approval ? { ...tool.approval, pending: false } : null,
                        }
                      : tool
                  ),
                }));
                break;
              case "done":
                updateAssistant(assistantId, (item) => ({
                  ...item,
                  content: event.text || item.content,
                  usage: event.usage || null,
                  done: true,
                }));
                break;
              case "error":
                updateAssistant(assistantId, (item) => ({
                  ...item,
                  done: true,
                  error: event.message,
                }));
                setError(event.message);
                break;
              default:
                break;
            }
          },
        });
      } catch (err) {
        if (err.name === "AbortError") {
          updateAssistant(assistantId, (item) => ({
            ...item,
            done: true,
            content: item.content || "(interrompido pelo usuário)",
          }));
        } else {
          setError(err.message || "falha na requisição");
          notify(err.message || "falha na requisição", "error");
          updateAssistant(assistantId, (item) => ({ ...item, done: true, error: err.message }));
        }
      } finally {
        setBusy(false);
        setWaiting(null);
        abortRef.current = null;
        updateAssistant(assistantId, (item) => ({ ...item, done: true }));
        refresh();
      }
    },
    [activeChat, busy, notify, patchChat, refresh, selection]
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const decide = useCallback(
    async (approvalId, allow) => {
      try {
        await api.approve(approvalId, allow);
      } catch (err) {
        notify(err.message, "error");
      }
    },
    [notify]
  );

  const changeSelection = useCallback(
    async (next) => {
      setSelection(next);
      await runAction(() => api.patchConfig({ selectedModel: next }));
    },
    [runAction]
  );

  const swapProviderOrder = useCallback(async () => {
    if (!state?.config?.providerOrder) return;
    const order = [...state.config.providerOrder].reverse();
    await runAction(
      () => api.patchConfig({ providerOrder: order }),
      (data) => `Ordem agora: ${(data.config?.providerOrder || []).join(" → ")}`
    );
  }, [runAction, state]);

  const syncModels = useCallback(async () => {
    if (!state?.config) return;
    const target =
      selection.provider !== "auto"
        ? selection.provider
        : state.config.providerOrder?.[0] || "nvidia";

    setSyncingModels(true);
    const data = await runAction(
      () => api.refreshModels(target),
      (result) => `${result.count} modelos sincronizados em ${target}`
    );
    setSyncingModels(false);

    const models = data?.models || [];
    if (models.length && selection.provider === target && !models.includes(selection.model)) {
      const next = { provider: target, model: models[0] };
      setSelection(next);
      await runAction(() => api.patchConfig({ selectedModel: next }));
    }
  }, [runAction, selection, state]);

  const testAllModels = useCallback(async () => {
    const target =
      selection.provider !== "auto" && selection.provider !== "router"
        ? selection.provider
        : null;
    setTestingModels(true);
    await runAction(
      () => api.testModels(target ? { provider: target } : {}),
      (result) =>
        `${result.summary?.ok ?? 0} de ${result.summary?.total ?? 0} modelos funcionando` +
        (result.summary?.error ? ` · ${result.summary.error} com erro` : "")
    );
    setTestingModels(false);
  }, [runAction, selection.provider]);

  const testOneModel = useCallback(
    async (provider, model) => {
      const data = await runAction(
        () => api.testModels({ provider, model }),
        (result) => {
          const entry = result.results?.[`${provider}::${model}`];
          return entry?.status === "ok"
            ? `✅ ${model}: ok`
            : `❌ ${model}: ${entry?.reason || "erro no teste"}`;
        }
      );
      return data;
    },
    [runAction]
  );

  const startChat = useCallback(() => {
    const chat = newChatRecord();
    setChats((prev) => [chat, ...prev]);
    setActiveId(chat.id);
    setError("");
    setRoute(null);
    setWaiting(null);
  }, []);

  const selectChat = useCallback((id) => {
    setActiveId(id);
    setError("");
    setRoute(null);
    setWaiting(null);
  }, []);

  const deleteChat = useCallback(
    (id) => {
      const remaining = chats.filter((chat) => chat.id !== id);
      if (remaining.length === 0) {
        const fresh = newChatRecord();
        setChats([fresh]);
        setActiveId(fresh.id);
        return;
      }
      setChats(remaining);
      if (activeId === id) setActiveId(remaining[0].id);
    },
    [activeId, chats]
  );

  const clearActiveChat = useCallback(() => {
    if (!activeChat) return;
    patchChat(activeChat.id, (chat) => ({ ...chat, messages: [], title: "" }));
    setError("");
    setRoute(null);
  }, [activeChat, patchChat]);

  const keys = state?.keys || [];
  const activeKeys = keys.filter((key) => key.status === "active").length;
  const totalKeys = keys.length;
  const statusTone = busy ? "busy" : waiting ? "warn" : activeKeys > 0 ? "ok" : "danger";
  const statusText = busy
    ? waiting
      ? "aguardando chave…"
      : "agente trabalhando…"
    : totalKeys === 0
      ? "nenhuma chave cadastrada"
      : `${activeKeys}/${totalKeys} chaves ativas`;

  return (
    <div
      className={[
        "app",
        sidebarOpen ? "" : "sidebar-collapsed",
        chatsOpen ? "" : "chats-collapsed",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <header className="topbar glass">
        <div className="brand">
          <span className="logo" aria-hidden="true">
            <Icon name="key" size={18} />
          </span>
          <div className="brand-text">
            <strong>KeyForge</strong>
            <small>agente · pool de chaves</small>
          </div>
        </div>

        <ModelSelector
          config={state?.config}
          value={selection}
          onChange={changeSelection}
          disabled={busy}
          testingModels={testingModels}
          onTestAll={testAllModels}
          onTestModel={testOneModel}
        />
        <button
          className="ghost-button sync-models"
          onClick={syncModels}
          disabled={syncingModels}
          title="Sincronizar o catálogo de modelos do provedor"
        >
          <Icon name="refresh" size={14} />
          <span className="btn-label">{syncingModels ? "sincronizando…" : "modelos"}</span>
        </button>

        <div className="order-chips" title="Ordem de preferência dos provedores">
          {(state?.config?.providerOrder || []).map((provider, index) => (
            <span key={provider} className={`chip ${index === 0 ? "chip-primary" : ""}`}>
              {state?.config?.providers?.[provider]?.label || provider}
            </span>
          ))}
          <button
            className="icon-button"
            onClick={swapProviderOrder}
            title="Inverter ordem dos provedores"
            aria-label="Inverter ordem dos provedores"
          >
            <Icon name="swap" size={15} />
          </button>
        </div>

        <div className="topbar-actions">
          <button
            className="icon-button theme-toggle"
            onClick={toggleTheme}
            title={theme === "dark" ? "Ativar tema claro" : "Ativar tema escuro"}
            aria-label={theme === "dark" ? "Ativar tema claro" : "Ativar tema escuro"}
          >
            <Icon name={theme === "dark" ? "sun" : "moon"} size={17} />
          </button>
          <span className={`status-pill ${statusTone}`}>
            <span className="dot" />
            {statusText}
          </span>

          <button
            className="icon-button"
            onClick={toggleSidebar}
            title="Painel de chaves"
            aria-label="Alternar painel de chaves"
            aria-expanded={sidebarOpen}
          >
            <Icon name="menu" size={18} />
          </button>
          <button
            className="icon-button"
            onClick={toggleChats}
            title="Histórico de conversas"
            aria-label="Alternar histórico de conversas"
            aria-expanded={chatsOpen}
          >
            <Icon name="history" size={18} />
          </button>
          <button
            className="ghost-button settings-button"
            onClick={() => setSettingsOpen(true)}
            title="Configurações"
            data-tour="settings"
          >
            <Icon name="sliders" size={15} />
            <span className="btn-label">Configurações</span>
          </button>
        </div>
      </header>

      <div className="workspace">
        <KeysPanel
          state={state}
          now={now}
          fetchedAt={fetchedAt}
          onNotify={notify}
          onAdd={(provider, key, label) => runAction(() => api.addKey(provider, key, label))}
          onRemove={(provider, id) => runAction(() => api.removeKey(provider, id))}
          onReset={(provider, id) => runAction(() => api.resetKey(provider, id))}
          onTest={(provider, id) => runAction(() => api.testKey(provider, id))}
          onRefreshModels={(provider) => runAction(() => api.refreshModels(provider))}
          onSetWorkspace={(path) => runAction(() => api.setWorkspace(path))}
          onPickWorkspace={() => runAction(() => api.pickWorkspace())}
          onOpenSettings={() => setSettingsOpen(true)}
        />

        <main className="chat-area">
          <ChatPanel
            messages={messages}
            busy={busy}
            waiting={waiting}
            route={route}
            error={error}
            notices={notices}
            workspace={state?.workspace}
            onSend={send}
            onStop={stop}
            onDecide={decide}
            onClear={clearActiveChat}
          />
        </main>

        <ChatList
          chats={chats}
          activeId={activeChat?.id}
          onSelect={selectChat}
          onNew={startChat}
          onDelete={deleteChat}
        />
      </div>

      <div className="fab-stack" aria-label="Ações rápidas">
        <button
          className="fab glass"
          onClick={startChat}
          title="Novo chat"
          aria-label="Novo chat"
        >
          <Icon name="plus" size={17} />
          <span className="fab-label">Novo chat</span>
        </button>
        <button
          className="fab glass fab-accent"
          onClick={startTour}
          title="Fazer tour guiado"
          aria-label="Fazer tour guiado"
        >
          <Icon name="compass" size={17} />
          <span className="fab-label">Fazer tour</span>
        </button>
      </div>

      <div
        className={`drawer-backdrop drawer-backdrop-chats ${chatsOpen ? "is-visible" : ""}`}
        onClick={() => setChatsOpen(false)}
        aria-hidden="true"
      />
      <div
        className={`drawer-backdrop drawer-backdrop-sidebar ${sidebarOpen ? "is-visible" : ""}`}
        onClick={() => setSidebarOpen(false)}
        aria-hidden="true"
      />

      <footer className="statusbar">
        <span className="statusbar-item" title="Diretório de trabalho do agente">
          <Icon name="folder" size={13} /> {state?.workspace ? shorten(state.workspace) : "—"}
        </span>
        <span className="statusbar-item">
          {route
            ? `⇢ ${route.providerLabel} · ${route.model} · ${route.keyLabel} (${route.maskedKey})`
            : "sem chamada ativa"}
        </span>
        <span className="statusbar-item muted">
          {route ? (route.toolsEnabled ? `ferramentas: ${(route.tools || []).length}` : "sem ferramentas") : ""}
        </span>
        {state?.config?.configPath ? (
          <span className="statusbar-item muted" title={state.config.configPath}>
            config: {shorten(state.config.configPath)}
          </span>
        ) : null}
        <span className="statusbar-item muted">v{state?.version || "?"}</span>
      </footer>

      {settingsOpen ? (
        <SettingsModal
          state={state}
          onClose={() => setSettingsOpen(false)}
          onSave={async (patch) => {
            await runAction(() => api.patchConfig(patch), "Configurações salvas");
            setSettingsOpen(false);
          }}
          onRefreshModels={(provider) =>
            runAction(
              () => api.refreshModels(provider),
              (data) => `${data.count} modelos sincronizados`
            )
          }
          onStartTour={startTour}
        />
      ) : null}
    </div>
  );
}

function shorten(path, limit = 46) {
  if (!path) return "";
  if (path.length <= limit) return path;
  return `…${path.slice(-(limit - 1))}`;
}
