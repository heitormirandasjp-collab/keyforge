"""Roteador de modelos ("Auto (Roteador IA)").

Quando o usuário escolhe o modo roteador, o agente classifica o pedido em uma de
quatro tarefas — ``coding``, ``reasoning``, ``writing`` ou ``fast-chat`` — e
escolhe o modelo mais adequado dentre os que a(s) chave(s) do usuário enxergam.

A classificação usa um modelo leve (flash/mini/lightning…) quando disponível e
cai para heurísticas de palavra-chave se o classificador falhar. O ranqueamento
é determinístico e testável: cada tarefa tem uma lista de preferências e a
ordem final vira a lista de fallback do agente.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

TASKS = ("coding", "reasoning", "writing", "fast-chat")

CLASSIFIER_SYSTEM = (
    "Você é o classificador de tarefas do KeyForge. Analise o pedido do usuário e "
    "classifique a tarefa em: coding, reasoning, writing, fast-chat. "
    "Responda apenas uma palavra, em minúsculas, sem pontuação."
)

ALIASES = {
    "code": "coding",
    "coding": "coding",
    "coder": "coding",
    "programacao": "coding",
    "programming": "coding",
    "dev": "coding",
    "reasoning": "reasoning",
    "raciocinio": "reasoning",
    "reason": "reasoning",
    "logic": "reasoning",
    "math": "reasoning",
    "writing": "writing",
    "write": "writing",
    "escrita": "writing",
    "creative": "writing",
    "text": "writing",
    "fast": "fast-chat",
    "chat": "fast-chat",
    "fastchat": "fast-chat",
    "fast-chat": "fast-chat",
    "simple": "fast-chat",
    "smalltalk": "fast-chat",
}

# Palavras que orientam a heurística de fallback (sem chamada ao modelo leve).
KEYWORDS: dict[str, tuple[str, ...]] = {
    "coding": (
        "codigo", "código", "code", "python", "javascript", "typescript", "java", "c#",
        "funcao", "função", "function", "bug", "erro", "stack trace", "refator", "refactor",
        "compil", "script", "sql", "html", "css", "react", "api", "endpoint", "regex",
        "teste", "test", "pytest", "npm", "git", "classe", "class", "componente", "backend",
    ),
    "reasoning": (
        "por que", "porque", "explique", "calcule", "quantos", "quanto", "prove", "prova",
        "logica", "lógica", "analise", "análise", "compare", "deriv", "teorema", "capital",
        "quem", "quando", "qual", "matematica", "matemática", "estatistica", "estatística",
    ),
    "writing": (
        "poema", "poesia", "redija", "redaç", "redacao", "artigo", "email", "e-mail",
        "historia", "história", "roteiro", "resuma", "resumo", "copy", "slogan", "carta",
        "escreva um texto", "crie um texto", "post", "legenda", "narrativa", "ensaio",
    ),
    "fast-chat": (
        "oi", "ola", "olá", "tudo bem", "bom dia", "boa tarde", "boa noite", "obrigado",
        "valeu", "quem é você", "quem e voce", "qual seu nome", "curiosidade", "piada",
    ),
}

# Preferência de modelo por tarefa (casa por trecho do id, em minúsculas).
PREFERENCES: dict[str, tuple[str, ...]] = {
    "coding": (
        "laguna", "coder", "codestral", "devstral", "deepseek", "qwen", "nemotron",
        "gpt-5", "claude", "sonnet", "llama",
    ),
    "reasoning": (
        "nemotron", "reason", "r1", "o3", "o1", "deepseek", "gpt-5", "sonnet", "claude",
        "qwen", "llama",
    ),
    "writing": (
        "llama-3.3", "llama-3.1", "llama", "mistral", "mixtral", "gemma", "qwen",
        "claude", "gpt-4", "gpt-5", "sonnet",
    ),
    "fast-chat": (
        "flash", "mini", "small", "light", "lightning", "xs", "tiny", "nano", "turbo",
        "3.5", "instant",
    ),
}

GENERIC_SCORE = 5


def _fold(text: str) -> str:
    """Minúsculas sem acentos, para comparar palavras-chave."""
    normalized = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


def normalize_task(raw: str | None) -> str | None:
    """Extrai a tarefa de uma resposta do classificador (ex.: ' Coding.' )."""
    if not raw:
        return None
    for token in re.findall(r"[a-z][a-z-]{1,20}", _fold(raw)):
        task = ALIASES.get(token)
        if task:
            return task
    return None


def guess_task(text: str) -> str:
    """Classificação heurística usada quando o modelo leve não responde."""
    folded = _fold(text)
    scores = {task: 0 for task in KEYWORDS}
    for task, words in KEYWORDS.items():
        for word in words:
            if word in folded:
                scores[task] += len(word.split())
    best = max(scores.items(), key=lambda item: (item[1], item[0] == "coding"))
    if best[1] <= 0:
        return "fast-chat"
    return best[0]


def score_model(task: str, model: str) -> int:
    """Pontuação de 0..100 de um modelo para a tarefa."""
    name = (model or "").lower()
    for index, pattern in enumerate(PREFERENCES.get(task, ())):
        if pattern in name:
            return max(30, 100 - index * 10)
    return GENERIC_SCORE


def model_ok(cfg: dict[str, Any], provider: str, model: str) -> bool:
    """O health check confirmou que este modelo funciona nesta conta?"""
    entry = (cfg.get("modelHealth") or {}).get(f"{provider}::{model}") or {}
    return entry.get("status") == "ok"


def all_pairs(cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """Todos os pares (provedor, modelo) do catálogo, na ordem dos provedores."""
    providers = cfg.get("providers") or {}
    order = [name for name in (cfg.get("providerOrder") or list(providers)) if name in providers]
    items: list[tuple[str, str]] = []
    for name in order:
        conf = providers.get(name) or {}
        models = [str(model) for model in (conf.get("models") or []) if model]
        default = conf.get("defaultModel")
        if default and default not in models:
            models.insert(0, str(default))
        for model in models:
            items.append((name, model))
    return items


def catalog(cfg: dict[str, Any], only_ok: bool = True) -> list[tuple[str, str]]:
    """Pares (provedor, modelo) do catálogo.

    Com ``only_ok`` (padrão) descarta o que o health check marcou como erro.
    Se ninguém foi testado ainda, devolve o catálogo inteiro para não travar o
    agente antes do primeiro "Testar Todos os Modelos".
    """
    items = all_pairs(cfg)
    if not only_ok:
        return items
    healthy = [item for item in items if model_ok(cfg, *item)]
    return healthy or items


def rank_models(task: str, items: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Ordena (provedor, modelo) do mais adequado ao menos adequado."""
    pairs = list(items)
    order = {provider: index for index, (provider, _) in enumerate(pairs)}
    return sorted(
        pairs,
        key=lambda item: (
            -score_model(task, item[1]),
            order.get(item[0], len(order)),
            item[1].lower(),
        ),
    )


_LIGHT = re.compile(r"(flash|mini|small|light|lightning|xs|tiny|nano|turbo|instant|3\.5)", re.I)


def classifier_candidates(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Modelos leves candidatos a classificador (provedor + modelo)."""
    providers = cfg.get("providers") or {}
    order = [name for name in (cfg.get("providerOrder") or list(providers)) if name in providers]
    candidates: list[dict[str, Any]] = []
    configured = (cfg.get("router") or {}).get("classifierModel")

    if configured and "::" in str(configured):
        provider, model = str(configured).split("::", 1)
        if provider in providers and model:
            candidates.append({"provider": provider, "model": model})

    forced = list(candidates)
    for name in order:
        conf = providers.get(name) or {}
        models = [str(model) for model in (conf.get("models") or []) if model]
        light = [model for model in models if _LIGHT.search(model)]
        rest = list(light)
        default = conf.get("defaultModel")
        if default and default not in rest:
            rest.append(str(default))
        for model in models:
            if model not in rest:
                rest.append(model)
        for model in rest:
            candidate = {"provider": name, "model": model}
            if candidate not in candidates:
                candidates.append(candidate)

    rest = [item for item in candidates if item not in forced]
    healthy = [item for item in rest if model_ok(cfg, item["provider"], item["model"])]
    # O classificador configurado à mão continua valendo; o resto prefere "ok".
    return forced + (healthy or rest)


def describe(task: str, model: str, source: str, classifier: str = "") -> str:
    """Frase curta exibida no chat."""
    origin = {
        "ia": f"classificador {classifier}" if classifier else "classificador IA",
        "heuristica": "heurística local (classificador indisponível)",
        "manual": "escolha manual",
    }.get(source, source)
    return f"Roteador IA selecionou: {model} (tarefa: {task} · {origin})"
