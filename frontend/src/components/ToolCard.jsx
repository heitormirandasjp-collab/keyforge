import { useState } from "react";

const TOOL_LABELS = {
  read_file: "ler arquivo",
  write_file: "escrever arquivo",
  list_directory: "listar diretório",
  run_shell: "shell",
};

function summary(tool) {
  const args = tool.args || {};
  if (args.command) return args.command;
  if (args.path) return args.path;
  return "";
}

export default function ToolCard({ tool, onDecide }) {
  const [sent, setSent] = useState(false);
  const label = TOOL_LABELS[tool.name] || tool.name;
  const state =
    tool.approval?.pending && !sent
      ? "aguardando aprovação"
      : tool.ok === null || tool.ok === undefined
        ? "executando…"
        : tool.ok
          ? "concluído"
          : "falhou";

  async function decide(allow) {
    setSent(true);
    await onDecide(tool.approval.id, allow);
  }

  return (
    <div className={`tool ${tool.ok === false ? "tool-fail" : tool.ok ? "tool-ok" : ""}`}>
      <div className="tool-head">
        <span className="tool-name">{label}</span>
        <code className="tool-summary">{summary(tool)}</code>
        <span className={`tool-state ${tool.ok === false ? "bad" : ""}`}>{state}</span>
      </div>

      {tool.approval?.pending && !sent ? (
        <div className="approval">
          <span>Executar este comando no shell?</span>
          <span className="row">
            <button className="primary small" onClick={() => decide(true)}>
              Executar
            </button>
            <button className="danger small" onClick={() => decide(false)}>
              Negar
            </button>
          </span>
        </div>
      ) : null}

      {sent && tool.approval?.pending ? <p className="muted tiny">enviando decisão…</p> : null}

      {tool.output !== null && tool.output !== undefined ? (
        <details className="tool-output">
          <summary>saída</summary>
          <pre>{tool.output}</pre>
        </details>
      ) : null}

      {tool.name === "write_file" && tool.args?.content ? (
        <details className="tool-output">
          <summary>conteúdo escrito</summary>
          <pre>{tool.args.content}</pre>
        </details>
      ) : null}
    </div>
  );
}
