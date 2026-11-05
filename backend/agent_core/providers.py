"""Cliente HTTP compatível com OpenAI (streaming SSE).

Serve os dois provedores do KeyForge:

* **NVIDIA NIM** — ``https://integrate.api.nvidia.com/v1``
* **Freebuff** — proxy compatível com OpenAI (ex.: Freebuff2API), normalmente em
  ``http://127.0.0.1:8080/v1``.

A base URL é normalizada antes de qualquer chamada: barras duplicadas, barras
finais e endpoints colados por engano (``/chat/completions``, ``/models``) são
removidos, e ``/v1`` é adicionado quando o usuário informa só o host.

Tudo com a biblioteca padrão (``urllib``) para empacotar sem dependências
nativas no PyInstaller.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .config import log_file, mask_key

DeltaCallback = Callable[[str, str], None]  # (canal, texto)

# Sufixos que o usuário pode colar junto da base URL sem querer.
ENDPOINT_SUFFIXES = ("/chat/completions", "/completions", "/models")


class ProviderError(Exception):
    """Erro de chamada ao provedor, já classificado."""

    def __init__(
        self,
        message: str,
        status: int = 0,
        kind: str = "error",
        retry_after: float | None = None,
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.kind = kind
        self.retry_after = retry_after
        self.url = url

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.status or self.kind}] {self.message}"


@dataclass
class ChatResult:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    model: str | None = None

    @property
    def total_tokens(self) -> int:
        usage = self.usage or {}
        total = usage.get("total_tokens")
        if isinstance(total, int):
            return total
        prompt = usage.get("prompt_tokens") or 0
        completion = usage.get("completion_tokens") or 0
        if isinstance(prompt, int) and isinstance(completion, int):
            return prompt + completion
        return 0


# --------------------------------------------------------------------------- #
# URL
# --------------------------------------------------------------------------- #
def normalize_base_url(base_url: str) -> str:
    """Normaliza a base URL de um provedor compatível com OpenAI.

    Exemplos::

        https://integrate.api.nvidia.com/v1/            -> https://integrate.api.nvidia.com/v1
        https://integrate.api.nvidia.com                -> https://integrate.api.nvidia.com/v1
        https://integrate.api.nvidia.com//v1//chat/completions
                                                        -> https://integrate.api.nvidia.com/v1
        http://127.0.0.1:8080/v1                        -> http://127.0.0.1:8080/v1
    """
    raw = (base_url or "").strip()
    if not raw:
        raise ProviderError("base URL do provedor não configurada", kind="bad_request")
    if "://" not in raw:
        raw = "https://" + raw

    parts = urlsplit(raw)
    if not parts.netloc:
        raise ProviderError(f"base URL inválida: {base_url!r}", kind="bad_request")

    path = re.sub(r"/{2,}", "/", parts.path).strip()
    if path == "/":
        path = ""

    # Remove endpoints colados por engano, repetidamente (ex.: .../v1/chat/completions).
    changed = True
    while changed:
        changed = False
        trimmed = path.rstrip("/")
        for suffix in ENDPOINT_SUFFIXES:
            if trimmed.lower().endswith(suffix):
                path = trimmed[: -len(suffix)]
                changed = True
                break

    path = path.rstrip("/")
    # Sem nenhum caminho: assume a convenção OpenAI (/v1). Caminhos explícitos
    # (ex.: /api, /openai/v1) são preservados como o usuário escreveu.
    if not path:
        path = "/v1"
    elif not path.startswith("/"):
        path = "/" + path

    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def endpoint_url(base_url: str, path: str) -> str:
    """Monta a URL final sem duplicar barras nem versões."""
    base = normalize_base_url(base_url).rstrip("/")
    suffix = "/" + path.lstrip("/")
    return f"{base}{suffix}"


# --------------------------------------------------------------------------- #
# Log de depuração
# --------------------------------------------------------------------------- #
def log_event(event: str, **fields: Any) -> None:
    """Anexa uma linha JSON em ``%APPDATA%\\KeyForge\\logs\\agent.log``.

    Nunca grava a chave em texto puro (apenas a versão mascarada).
    """
    try:
        path = log_file()
        if path.exists() and path.stat().st_size > 1_500_000:
            path.replace(path.with_name("agent.log.1"))
        record = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line[:6000] + "\n")
        if os.environ.get("KEYFORGE_DEBUG"):
            sys.stderr.write(line[:2000] + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Helpers HTTP
# --------------------------------------------------------------------------- #
def _classify(status: int) -> str:
    if status == 429:
        return "rate_limit"
    if status in (401, 403):
        return "auth"
    if status >= 500:
        return "server"
    if status >= 400:
        return "bad_request"
    return "error"


def _retry_after(headers: Any) -> float | None:
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _error_message(body: str, status: int) -> str:
    text = (body or "").strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:800]
        if isinstance(error, str):
            return error[:800]
        for key in ("message", "detail", "msg"):
            if parsed.get(key):
                return str(parsed[key])[:800]
    return text[:800] or f"HTTP {status}"


def _headers(api_key: str, accept: str = "application/json") -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": accept, "User-Agent": "KeyForge/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _open(request: urllib.request.Request, timeout: float):
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - corpo ilegível
            pass
        status = int(exc.code)
        raise ProviderError(
            _error_message(body, status),
            status=status,
            kind=_classify(status),
            retry_after=_retry_after(exc.headers),
        ) from None
    except urllib.error.URLError as exc:
        raise ProviderError(f"falha de rede: {exc.reason}", kind="network") from None
    except TimeoutError:
        raise ProviderError("timeout na chamada do provedor", kind="network") from None


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
def chat_stream(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = 0.2,
    max_tokens: int | None = None,
    timeout: float = 300.0,
    on_delta: DeltaCallback | None = None,
) -> ChatResult:
    """Executa ``POST {base}/chat/completions`` e agrega o stream."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    url = endpoint_url(base_url, "/chat/completions")
    tool_names = [item.get("function", {}).get("name") for item in (tools or [])]
    log_event(
        "chat_request",
        url=url,
        model=model,
        key=mask_key(api_key),
        has_auth=bool(api_key),
        tools=tool_names,
        payload=json.dumps(payload, ensure_ascii=False, default=str),
    )

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(api_key, "text/event-stream"),
        method="POST",
    )
    result = ChatResult(model=model)
    tool_calls: dict[int, dict[str, Any]] = {}

    try:
        response = _open(request, timeout)
    except ProviderError as exc:
        exc.url = url
        log_event("chat_error", url=url, status=exc.status, kind=exc.kind, message=exc.message)
        raise

    with response:
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "application/json" in content_type and "event-stream" not in content_type:
            # Provedor ignorou stream=true: resposta única.
            data = json.loads(response.read().decode("utf-8", "replace") or "{}")
            final = _aggregate_full(data, result, on_delta)
            log_event("chat_response", url=url, mode="json", chars=len(final.text),
                      tool_calls=len(final.tool_calls), usage=final.usage)
            return final

        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str in ("[DONE]", "DONE"):
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            _absorb_chunk(chunk, result, tool_calls, on_delta)
            if chunk.get("usage"):
                result.usage = chunk["usage"]

    if tool_calls:
        result.tool_calls = [tool_calls[i] for i in sorted(tool_calls)]
    if not result.text and not result.reasoning and not result.tool_calls:
        log_event("chat_error", url=url, kind="server", message="resposta vazia")
        raise ProviderError("o provedor respondeu vazio", kind="server", url=url)

    log_event(
        "chat_response",
        url=url,
        mode="stream",
        chars=len(result.text),
        tool_calls=[call.get("function", {}).get("name") for call in result.tool_calls],
        usage=result.usage,
    )
    return result


def _absorb_chunk(
    chunk: dict[str, Any],
    result: ChatResult,
    tool_calls: dict[int, dict[str, Any]],
    on_delta: DeltaCallback | None,
) -> None:
    if chunk.get("model"):
        result.model = chunk["model"]
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or choice.get("message") or {}
        if not isinstance(delta, dict):
            continue
        text = delta.get("content")
        if isinstance(text, str) and text:
            result.text += text
            if on_delta:
                on_delta("content", text)
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            result.reasoning += reasoning
            if on_delta:
                on_delta("reasoning", reasoning)
        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            index = int(call.get("index") or 0)
            slot = tool_calls.setdefault(
                index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if call.get("id"):
                slot["id"] = call["id"]
            function = call.get("function") or {}
            if function.get("name"):
                slot["function"]["name"] += function["name"]
            if function.get("arguments"):
                slot["function"]["arguments"] += function["arguments"]
        if choice.get("finish_reason"):
            result.finish_reason = choice["finish_reason"]


def _aggregate_full(data: dict[str, Any], result: ChatResult, on_delta: DeltaCallback | None) -> ChatResult:
    """Converte uma resposta não-streaming no mesmo formato agregado."""
    if data.get("error"):
        raise ProviderError(str(data["error"])[:800], kind="server")
    for choice in data.get("choices") or []:
        message = choice.get("message") or {}
        text = message.get("content")
        if isinstance(text, str) and text:
            result.text += text
            if on_delta:
                on_delta("content", text)
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            result.reasoning += reasoning
            if on_delta:
                on_delta("reasoning", reasoning)
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                result.tool_calls.append(
                    {
                        "id": call.get("id") or "",
                        "type": "function",
                        "function": {
                            "name": (call.get("function") or {}).get("name", ""),
                            "arguments": (call.get("function") or {}).get("arguments", ""),
                        },
                    }
                )
        if choice.get("finish_reason"):
            result.finish_reason = choice["finish_reason"]
    result.usage = data.get("usage") or {}
    result.model = data.get("model") or result.model
    return result


# --------------------------------------------------------------------------- #
# Modelos e teste de chave
# --------------------------------------------------------------------------- #
def list_models(base_url: str, api_key: str, timeout: float = 30.0) -> list[str]:
    """``GET {base}/models`` — popula o seletor e valida a chave."""
    url = endpoint_url(base_url, "/models")
    log_event("models_request", url=url, key=mask_key(api_key), has_auth=bool(api_key))
    request = urllib.request.Request(url, headers=_headers(api_key), method="GET")
    try:
        response = _open(request, timeout)
    except ProviderError as exc:
        exc.url = url
        log_event("models_error", url=url, status=exc.status, kind=exc.kind, message=exc.message)
        raise

    with response:
        raw = response.read().decode("utf-8", "replace")
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        raise ProviderError("resposta inválida de /models", kind="server", url=url) from None

    items: Any = data.get("data") if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = items.get("models") or items.get("data") or []
    if not isinstance(items, list):
        raise ProviderError("/models não retornou uma lista", kind="server", url=url)

    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("model")
            if name:
                names.append(str(name))

    models = sorted(dict.fromkeys(names))
    log_event("models_response", url=url, count=len(models))
    return models


def probe_key(base_url: str, api_key: str, model: str | None = None, timeout: float = 30.0) -> str:
    """Valida uma chave. Retorna uma mensagem de sucesso.

    Primeiro tenta ``/models``; se o proxy não implementar esse endpoint,
    faz uma chamada mínima de chat.
    """
    try:
        models = list_models(base_url, api_key, timeout=timeout)
        return f"chave válida — {len(models)} modelo(s) visível(is)"
    except ProviderError as exc:
        if exc.kind not in ("bad_request", "server"):
            raise
    if not model:
        raise ProviderError("provedor não expõe /models e não há modelo padrão", kind="bad_request")
    small = [{"role": "user", "content": "ping"}]
    chat_stream(
        base_url,
        api_key,
        model,
        small,
        temperature=0,
        max_tokens=8,
        timeout=timeout,
    )
    return "chave válida — chamada de teste concluída"
