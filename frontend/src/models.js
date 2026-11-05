// Regras do health check de modelos — JS puro (sem React) para poder testar
// com `node --test`: quais modelos o dropdown mostra e qual é o resumo.

export function healthKey(provider, model) {
  return `${provider}::${model}`;
}

export function healthEntry(config, provider, model) {
  const map = config?.modelHealth || {};
  return map[healthKey(provider, model)] || null;
}

/** "ok" | "error" | "unknown" (nunca testado). */
export function modelStatus(config, provider, model) {
  const status = healthEntry(config, provider, model)?.status;
  return status === "ok" || status === "error" ? status : "unknown";
}

export function catalogOf(config, provider) {
  return [...((config?.providers?.[provider]?.models) || [])];
}

export function providerSummary(config, provider) {
  const summary = { total: 0, ok: 0, error: 0, unknown: 0, tested: 0 };
  for (const model of catalogOf(config, provider)) {
    summary.total += 1;
    summary[modelStatus(config, provider, model)] += 1;
  }
  summary.tested = summary.ok + summary.error;
  return summary;
}

export function overallSummary(config) {
  const summary = { total: 0, ok: 0, error: 0, unknown: 0, tested: 0 };
  for (const provider of Object.keys(config?.providers || {})) {
    const part = providerSummary(config, provider);
    for (const field of Object.keys(summary)) summary[field] += part[field] || 0;
  }
  return summary;
}

/**
 * Modelos que o seletor exibe.
 *
 * - `showErrors` ligado → todos (modo debug, com ❌ nos reprovados);
 * - senão → só os aprovados (`status: "ok"`);
 * - se o provedor nunca foi testado, mostra o catálogo inteiro (nada foi
 *   reprovado ainda);
 * - se nenhum modelo passou (teste individual de um modelo que falhou), também
 *   mostra tudo: um seletor vazio não deixa o usuário escolher nada.
 */
export function visibleModels(config, provider, showErrors = false) {
  const models = catalogOf(config, provider);
  if (showErrors) return models;
  if (!providerSummary(config, provider).tested) return models;
  const healthy = models.filter((model) => modelStatus(config, provider, model) === "ok");
  return healthy.length ? healthy : models;
}

export function summaryLabel(summary) {
  const ok = summary?.ok || 0;
  const total = summary?.total || 0;
  return `${ok} de ${total} modelos funcionando`;
}
