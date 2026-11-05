export const keyforgeTour = {
  id: "keyforge-onboarding",
  specVersion: 1,
  title: "Tour pelo KeyForge",
  description: "Um passeio rápido pelo pool de chaves, modelos e chat do agente.",
  route: "/",
  audience: "end-user",
  estimatedSeconds: 55,
  steps: [
    {
      id: "welcome",
      type: "modal",
      title: "Boas-vindas ao KeyForge",
      body:
        "Este é um agente de código local com pool de chaves e múltiplos provedores. " +
        "Vamos ver o essencial em menos de um minuto.",
    },
    {
      id: "keys",
      type: "highlight",
      anchor: { selector: '[data-tour="keys"]', strategy: "data-tour" },
      title: "Aqui você configura suas chaves de API",
      body:
        "Cadastre chaves da NVIDIA, OpenAI, Google, DeepSeek, OpenRouter ou Anthropic. " +
        "O pool rotaciona sozinho, evita 429 e respeita o limite global compartilhado.",
      placement: "auto",
      maskPadding: 10,
      maskRadius: 16,
    },
    {
      id: "models",
      type: "highlight",
      anchor: { selector: '[data-tour="models"]', strategy: "data-tour" },
      title: "Escolha o modelo ou use o Roteador IA",
      body:
        "O seletor mostra o health check de cada modelo. No modo Auto (Roteador IA), " +
        "o KeyForge escolhe o modelo mais adequado para cada tarefa.",
      placement: "bottom",
      maskPadding: 8,
      maskRadius: 12,
    },
    {
      id: "composer",
      type: "highlight",
      anchor: { selector: '[data-tour="composer"]', strategy: "data-tour" },
      title: "Aqui você conversa com o agente",
      body:
        "Descreva a tarefa e pressione Enter. O agente lê e escreve arquivos no workspace, " +
        "executa comandos com aprovação e mostra cada ferramenta usada.",
      placement: "auto",
      maskPadding: 10,
      maskRadius: 14,
    },
    {
      id: "done",
      type: "modal",
      title: "Pronto! Comece a codar",
      body:
        "Cadastre uma chave, escolha o modelo e envie sua primeira tarefa. " +
        "Você pode repetir este tour quando quiser em Configurações → Fazer tour.",
    },
  ],
};
