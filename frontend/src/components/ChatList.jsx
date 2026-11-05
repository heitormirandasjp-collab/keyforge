import Icon from "./Icon";

function formatDate(timestamp) {
  if (!timestamp) return "";
  try {
    return new Date(timestamp).toLocaleString("pt-BR", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

export default function ChatList({ chats, activeId, onSelect, onNew, onDelete }) {
  const ordered = [...chats].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));

  return (
    <aside className="chatlist glass">
      <div className="chatlist-head">
        <h2>Conversas</h2>
        <span className="count" title="conversas salvas">
          {chats.length}
        </span>
      </div>

      <button className="primary small new-chat" onClick={onNew}>
        + Novo chat
      </button>

      <ul className="chat-items">
        {ordered.length === 0 ? (
          <li className="muted small empty">Nenhuma conversa ainda.</li>
        ) : (
          ordered.map((chat) => (
            <li key={chat.id} className={chat.id === activeId ? "active" : ""}>
              <button className="chat-item" onClick={() => onSelect(chat.id)} title={chat.title || "Nova conversa"}>
                <span className="chat-title">{chat.title || "Nova conversa"}</span>
                <span className="chat-date">{formatDate(chat.updatedAt || chat.createdAt)}</span>
              </button>
              <button
                className="icon-button danger-ghost"
                title="Deletar chat"
                onClick={(event) => {
                  event.stopPropagation();
                  onDelete(chat.id);
                }}
              >
                <Icon name="trash" size={14} />
              </button>
            </li>
          ))
        )}
      </ul>

      <p className="muted tiny chatlist-foot">Salvo no navegador (localStorage).</p>
    </aside>
  );
}
