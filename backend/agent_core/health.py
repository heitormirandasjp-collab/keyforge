"""Health check dos modelos — descobre o que a conta realmente pode usar.

Algumas contas veem o modelo em ``GET /v1/models`` mas recebem
``404 Function not found for account`` ao chamar. Este módulo testa cada modelo
com um ``POST /chat/completions`` mínimo (``max_tokens = 1``) e guarda o
veredito em ``config.json``::

    "modelHealth": {
      "nvidia::meta/llama-3.3-70b-instruct": {
        "status": "ok",
        "reason": "",
        "httpStatus": 0,
        "latencyMs": 412,
        "checkedAt": "2026-11-04 16:40:12",
        "checkedTs": 1730...
      }
    }

Com isso o seletor mostra só modelos que funcionam, o roteador escolhe entre
eles e o agente troca de modelo quando um deles perde a permissão.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .providers import ChatResult, ProviderError, chat_stream

OK = "ok"
ERROR = "error"
UNKNOWN = "unknown"

PROBE_MESSAGES = [{"role": "user", "content": "oi"}]
PROBE_MAX_TOKENS = 1
FALLBACK_MAX_TOKENS = 16
DEFAULT_WORKERS = 8
DEFAULT_TIMEOUT = 25.0

# Mensagens que significam "este modelo não existe para esta conta".
MISSING_MODEL_PATTERNS = (
    "not found for account",
    "function not found",
    "model not found",
    "no such model",
    "does not exist",
    "has been deprecated",
    "deprecated",
)

ProbeFn = Callable[..., ChatResult]


def key_of(provider: str, model: str) -> str:
    """Chave usada no ``modelHealth`` (``provedor::modelo``)."""
    return f"{provider}::{model}"


def stamp(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts is not None else time.time()))


def entry(
    status: str,
    reason: str = "",
    http_status: int = 0,
    latency_ms: int = 0,
    ts: float | None = None,
) -> dict[str, Any]:
    """Registro de saúde de um modelo (vai direto para o config.json)."""
    now = ts if ts is not None else time.time()
    return {
        "status": status,
        "reason": (reason or "")[:300],
        "httpStatus": int(http_status or 0),
        "latencyMs": int(latency_ms or 0),
        "checkedAt": stamp(now),
        "checkedTs": round(now, 3),
    }


def is_ok(health_map: dict[str, Any] | None, provider: str, model: str) -> bool:
    item = (health_map or {}).get(key_of(provider, model)) or {}
    return item.get("status") == OK


def is_missing_model(exc: ProviderError) -> bool:
    """O erro diz que o modelo não existe/está bloqueado para esta conta?"""
    text = (getattr(exc, "message", "") or "").lower()
    if any(pattern in text for pattern in MISSING_MODEL_PATTERNS):
        return True
    # 404 genérico (mas "page not found" é URL errada, não modelo morto).
    return getattr(exc, "status", 0) == 404 and "page not found" not in text


def pick_key(conf: dict[str, Any]) -> tuple[str, str] | None:
    """Primeira chave utilizável do provedor (chave, rótulo)."""
    now = time.time()
    keys = list(conf.get("keys") or [])
    for item in keys:
        state = item.get("state") or {}
        if state.get("exhausted"):
            continue
        if float(state.get("cooldownUntil") or 0) > now:
            continue
        label = str(item.get("label") or item.get("id") or "chave")
        return str(item.get("key") or ""), label
    return None


def workers_for(conf: dict[str, Any]) -> int:
    """Concorrência do health check (o RPM é controlado por ``interval_for``)."""
    return DEFAULT_WORKERS


def interval_for(conf: dict[str, Any]) -> float:
    """Intervalo mínimo entre testes para respeitar o ``rpmLimit`` do provedor."""
    try:
        rpm = int(conf.get("rpmLimit") or 0)
    except (TypeError, ValueError):
        rpm = 0
    if rpm <= 0:
        return 0.0
    return 60.0 / rpm


def probe_model(
    base_url: str,
    api_key: str,
    model: str,
    chat: ProbeFn = chat_stream,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Testa um modelo com uma chamada mínima e devolve o veredito."""
    started = time.perf_counter()
    for attempt, limit in enumerate((PROBE_MAX_TOKENS, FALLBACK_MAX_TOKENS)):
        try:
            chat(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=list(PROBE_MESSAGES),
                tools=None,
                temperature=0,
                max_tokens=limit,
                timeout=timeout,
            )
        except ProviderError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            # Alguns modelos exigem max_tokens maior: não é defeito do modelo.
            if attempt == 0 and exc.kind == "bad_request" and "max_tokens" in (exc.message or "").lower():
                continue
            if exc.kind == "rate_limit":
                # 429 não diz nada sobre o modelo: fica como "não testado".
                return entry(UNKNOWN, f"429 · {exc.message}", exc.status, latency)
            if exc.kind == "network" and not exc.status:
                return entry(UNKNOWN, exc.message, 0, latency)
            reason = f"{exc.status} · {exc.message}" if exc.status else exc.message
            return entry(ERROR, reason, exc.status, latency)
        latency = int((time.perf_counter() - started) * 1000)
        return entry(OK, "", 0, latency)

    return entry(ERROR, "não foi possível testar o modelo", 0, 0)  # pragma: no cover


def check_models(
    base_url: str,
    api_key: str,
    models: list[str],
    chat: ProbeFn = chat_stream,
    workers: int = DEFAULT_WORKERS,
    timeout: float = DEFAULT_TIMEOUT,
    min_interval: float = 0.0,
) -> dict[str, dict[str, Any]]:
    """Testa a lista em paralelo e devolve ``{modelo: veredito}``.

    ``min_interval`` espaça o *início* das chamadas (ex.: 60/rpm) para não
    estourar a cota do provedor e confundir 429 com modelo reprovado.
    """
    unique = list(dict.fromkeys(str(model) for model in models if model))
    if not unique:
        return {}
    size = max(1, min(int(workers or 1), len(unique)))
    spacing = max(0.0, float(min_interval or 0.0))
    lock = threading.Lock()
    next_slot = [time.monotonic()]

    def run(model: str) -> dict[str, Any]:
        if spacing > 0:
            with lock:
                now = time.monotonic()
                slot = max(now, next_slot[0])
                next_slot[0] = slot + spacing
            if slot > now:
                time.sleep(slot - now)
        return probe_model(base_url, api_key, model, chat=chat, timeout=timeout)

    with ThreadPoolExecutor(max_workers=size, thread_name_prefix="health") as pool:
        results = list(pool.map(run, unique))
    return dict(zip(unique, results))


def summarize(health_map: dict[str, Any] | None, providers: dict[str, Any] | None) -> dict[str, int]:
    """Contagem para o resumo "12 de 45 modelos funcionando"."""
    counts = {"total": 0, "ok": 0, "error": 0, "unknown": 0}
    for name, conf in (providers or {}).items():
        for model in (conf.get("models") or []):
            counts["total"] += 1
            status = ((health_map or {}).get(key_of(name, model)) or {}).get("status")
            if status == OK:
                counts["ok"] += 1
            elif status == ERROR:
                counts["error"] += 1
            else:
                counts["unknown"] += 1
    return counts


def record(
    store: Any,
    provider: str,
    model: str,
    status: str,
    reason: str = "",
    http_status: int = 0,
) -> dict[str, Any]:
    """Grava o veredito de um modelo no config.json (merge) e devolve a entrada."""
    item = entry(status, reason, http_status)
    merged = dict(store.data.get("modelHealth") or {})
    merged[key_of(provider, model)] = item
    store.update({"modelHealth": merged})
    return item
