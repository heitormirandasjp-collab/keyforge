import { useEffect, useRef, useState } from "react";
import Icon from "./Icon";
import ToolCard from "./ToolCard";

const SUGGESTIONS = [
  {
    title: "Explorar o projeto",
    text: "Liste os arquivos do workspace e explique a estrutura do projeto.",
  },
  {
    title: "Rodar os testes",
    text: "Descubra como rodar os testes deste projeto e execute-os.",
  },
  {
    title: "Caçar um bug",
    text: "Leia o código principal, aponte um bug provável e mostre a correção.",
  },
];

function formatUsage(usage) {
  if (!usage) return "";
  const prompt = usage.prompt_tokens || 0;
  const completion = usage.completion_tokens || 0;
  const total = usage.total_tokens || prompt + completion;
  if (!total) return "";
  return `${total} tokens (${prompt} entrada · ${completion} saída)`;
}

function MessageBubble({ message, onDecide }) {
  const isUser = message.role === "user";
  return (
    <article className={`message ${isUser ? "message-user" : "message-assistant"}`}>
      <header className="message-head">
        <span className="author">{isUser ? "você" : "KeyForge"}</span>
        {message.usage ? <span className="muted tiny">{formatUsage(message.usage)}</span> : null}
      </header>

      {message.router ? (
        <div className="router-badge" title={(message.router.candidates || []).join(" → ")}>
          <Icon name="compass" size={14} />
          {message.router.label ||
            `Roteador IA selecionou: ${message.router.model} (tarefa: ${message.router.task})`}
          {(message.router.candidates || []).length > 1 ? (
            <span className="muted tiny">
              {" "}· fallback: {message.router.candidates.slice(1, 3).join(" → ")}
            </span>
          ) : null}
        </div>
      ) : null}

      {message.reasoning ? (
        <details className="reasoning">
          <summary>raciocínio do modelo</summary>
          <pre>{message.reasoning}</pre>
        </details>
      ) : null}

      {message.content ? <div className="message-body">{message.content}</div> : null}

      {(message.tools || []).map((tool) => (
        <ToolCard key={tool.id} tool={tool} onDecide={onDecide} />
      ))}

      {!message.content && !message.done && !(message.tools || []).length ? (
        <div className="message-body muted">…</div>
      ) : null}

      {message.error ? <div className="message-error">{message.error}</div> : null}
    </article>
  );
}

function EmptyState({ workspace, onPick }) {
  return (
    <div className="empty-state">
      <div className="empty-icon" aria-hidden="true">
        <Icon name="sparkle" size={22} />
      </div>
      <h1>Como posso ajudar hoje?</h1>
      <p className="muted small">Descreva uma tarefa ou comece por uma das sugestões abaixo.</p>
      <p className="tiny muted workspace-line" title={workspace}>
        workspace: <code>{workspace || "não configurado"}</code>
      </p>
      <div className="suggestions">
        {SUGGESTIONS.map((suggestion) => (
          <button key={suggestion.title} className="suggestion" onClick={() => onPick(suggestion.text)}>
            <strong>{suggestion.title}</strong>
            <span className="muted small">{suggestion.text}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

export default function ChatPanel({
  messages,
  busy,
  waiting,
  route,
  error,
  notices,
  workspace,
  onSend,
  onStop,
  onDecide,
  onClear,
}) {
  const [draft, setDraft] = useState("");
  const scrollRef = useRef(null);
  const textareaRef = useRef(null);
  const lastCount = useRef(messages.length);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const nearBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 160;
    if (!nearBottom) return;
    // Mensagem nova rola suave; tokens do streaming rolam direto para não travar.
    const behavior = messages.length !== lastCount.current ? "smooth" : "instant";
    lastCount.current = messages.length;
    element.scrollTo({ top: element.scrollHeight, behavior });
  }, [messages, waiting]);

  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 220)}px`;
  }, [draft]);

  function submit() {
    const text = draft.trim();
    if (!text || busy) return;
    onSend(text);
    setDraft("");
  }

  function onKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className="chat-panel" data-tour="chat">
      <div className="chat-scroll" ref={scrollRef}>
        {messages.length === 0 ? <EmptyState workspace={workspace} onPick={onSend} /> : null}

        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} onDecide={onDecide} />
        ))}

        {waiting ? (
          <div className="waiting">
            <span className="spinner" />
            {waiting.message} <strong>{waiting.retryIn}s</strong>
          </div>
        ) : null}

        {busy && !waiting ? (
          <div className="thinking">
            <span className="spinner" />
            {route ? `${route.providerLabel} · ${route.model} · ${route.keyLabel}` : "pensando…"}
            {route && route.toolsEnabled === false ? " · sem ferramentas" : ""}
          </div>
        ) : null}
      </div>

      {notices.length ? (
        <div className="notices">            {notices.map((notice) => (
            <div key={notice.id} className={`notice ${notice.tone || ""}`}>
              {notice.text}
            </div>
          ))}
        </div>
      ) : null}

      {error ? <div className="error-banner">{error}</div> : null}

      <div className="composer" data-tour="composer">
        <textarea
          ref={textareaRef}
          rows={1}
          value={draft}
          placeholder="Descreva a tarefa… (Enter envia · Shift+Enter quebra linha)"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="composer-actions">
          <button className="link-button" onClick={onClear} title="Limpar a conversa">
            limpar
          </button>
          {busy ? (
            <button className="danger" onClick={onStop}>
              Parar
            </button>
          ) : (
            <button className="primary" onClick={submit} disabled={!draft.trim()}>
              Enviar
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
