import { useEffect, useRef, useState } from "react";
import Icon from "./Icon";
import {
  healthEntry,
  modelStatus,
  overallSummary,
  providerSummary,
  summaryLabel,
  visibleModels,
} from "../models";

const ICON = { ok: "check", error: "x", unknown: "·" };

export default function ModelSelector({
  config,
  value,
  onChange,
  disabled,
  testingModels,
  onTestAll,
  onTestModel,
}) {
  const [open, setOpen] = useState(false);
  const [showErrors, setShowErrors] = useState(false);
  const [testingKey, setTestingKey] = useState("");
  const boxRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (event) => {
      if (boxRef.current && !boxRef.current.contains(event.target)) setOpen(false);
    };
    const onKey = (event) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const providers = config?.providers || {};
  const summary = overallSummary(config);
  const current = `${value?.provider || "auto"}::${value?.model || "auto"}`;
  const currentLabel =
    value?.provider === "router"
      ? "Auto (Roteador IA)"
      : value?.provider === "auto"
        ? "Automático"
        : `${providers[value?.provider]?.label || value?.provider || "?"} · ${value?.model || "?"}`;

  const choose = (provider, model) => {
    setOpen(false);
    if (provider !== value?.provider || model !== value?.model) onChange({ provider, model });
  };

  const testOne = async (provider, model) => {
    if (!onTestModel) return;
    const key = `${provider}::${model}`;
    setTestingKey(key);
    try {
      await onTestModel(provider, model);
    } finally {
      setTestingKey("");
    }
  };

  return (
    <div className="model-picker" data-tour="models" ref={boxRef}>
      <button
        type="button"
        className={`model-picker-trigger ${open ? "is-open" : ""}`}
        onClick={() => setOpen((prev) => !prev)}
        disabled={disabled}
        title="Modelo usado no próximo turno — clique para ver o health check"
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="model-picker-label">Modelo</span>
        <span className="model-picker-current">{currentLabel}</span>
        {summary.tested ? (
          <span
            className={`health-badge ${summary.error ? "warn" : "ok"}`}
            title={`${summary.error} modelo(s) reprovado(s) no último teste`}
          >
            {summary.ok}/{summary.total}
          </span>
        ) : null}
        <span className="model-picker-caret" aria-hidden="true">
          <Icon name="chevron" size={14} />
        </span>
      </button>

      {open ? (
        <div className="model-picker-menu" role="listbox">
          <div className="model-picker-head">
            <strong title="Resultado do último health check">{summaryLabel(summary)}</strong>
            <button
              type="button"
              className="mini-button"
              onClick={() => onTestAll && onTestAll()}
              disabled={Boolean(testingModels) || !onTestAll}
              title="POST mínimo (max_tokens=1) em cada modelo sincronizado, em paralelo"
            >
              {testingModels ? "testando…" : "Testar Todos os Modelos"}
            </button>
            <label className="mini-toggle" title="Mostra também os modelos que falharam (❌)">
              <input
                type="checkbox"
                checked={showErrors}
                onChange={(event) => setShowErrors(event.target.checked)}
              />
              Mostrar modelos com erro (debug)
            </label>
            {summary.unknown > 0 ? (
              <span className="model-picker-note">
                {summary.unknown} modelo(s) ainda sem teste
              </span>
            ) : null}
          </div>

          <div className="model-picker-list">
            <div className={`model-row ${value?.provider === "router" ? "is-selected" : ""}`}>
              <button
                type="button"
                className="model-row-pick"
                onClick={() => choose("router", "auto")}
              >
                <span className="health-dot unknown">
                  <Icon name="compass" size={13} />
                </span>
                <span className="model-row-name">Auto (Roteador IA)</span>
                <span className="model-row-hint">escolhe por tarefa (só modelos ok)</span>
              </button>
            </div>
            <div className={`model-row ${value?.provider === "auto" ? "is-selected" : ""}`}>
              <button
                type="button"
                className="model-row-pick"
                onClick={() => choose("auto", "auto")}
              >
                <span className="health-dot unknown">
                  <Icon name="swap" size={13} />
                </span>
                <span className="model-row-name">Automático</span>
                <span className="model-row-hint">segue a ordem de provedores</span>
              </button>
            </div>

            {Object.entries(providers).map(([name, conf]) => {
              const list = visibleModels(config, name, showErrors);
              const part = providerSummary(config, name);
              const hidden = (conf.models || []).length - list.length;
              return (
                <div className="model-group" key={name}>
                  <div className="model-group-head">
                    <span>{conf.label || name}</span>
                    <span className="muted">
                      {part.tested ? `${part.ok}/${part.total} ok` : `${part.total} modelos`}
                    </span>
                  </div>
                  {list.length === 0 ? (
                    <p className="model-empty">
                      Nenhum modelo funcionando. Rode “Testar Todos os Modelos”.
                    </p>
                  ) : null}
                  {list.map((model) => {
                    const entry = healthEntry(config, name, model);
                    const status = modelStatus(config, name, model);
                    const key = `${name}::${model}`;
                    const selected = value?.provider === name && value?.model === model;
                    return (
                      <div key={key} className={`model-row ${selected ? "is-selected" : ""}`}>
                        <button
                          type="button"
                          className="model-row-pick"
                          onClick={() => choose(name, model)}
                          title={
                            entry
                              ? `${entry.status === "ok" ? "ok" : "erro"} · ${entry.checkedAt || ""}${
                                  entry.reason ? `\n${entry.reason}` : ""
                                }`
                              : "nunca testado"
                          }
                        >
                          <span className={`health-dot ${status}`}>
                            {status === "unknown" ? "·" : <Icon name={ICON[status]} size={13} />}
                          </span>
                          <span className="model-row-name">{model}</span>
                        </button>
                        <button
                          type="button"
                          className="model-test-btn"
                          onClick={() => testOne(name, model)}
                          disabled={testingKey === key}
                          title={entry?.reason || "Testar este modelo"}
                        >
                          {testingKey === key ? "…" : <Icon name="bolt" size={12} />}
                        </button>
                      </div>
                    );
                  })}
                  {hidden > 0 ? (
                    <p className="model-hidden">
                      {hidden} modelo(s) com erro oculto(s) — ligue “Mostrar modelos com erro”.
                    </p>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      <span className="sr-only" aria-live="polite">
        {current}
      </span>
    </div>
  );
}
