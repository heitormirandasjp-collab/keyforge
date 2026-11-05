"""Loop ReAct (Reasoning + Acting) com tool calling e rotação de chaves.

O agente recebe o histórico da conversa, escolhe um par provedor/modelo a partir
da seleção da UI, chama o modelo com as ferramentas disponíveis e executa as
ferramentas pedidas — pedindo aprovação para comandos de shell.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import health
from .config import ConfigStore, default_workspace
from .keypool import KeyPool, Lease
from .providers import ChatResult, ProviderError, chat_stream
from .router import (
    CLASSIFIER_SYSTEM,
    catalog,
    classifier_candidates,
    describe,
    guess_task,
    normalize_task,
    rank_models,
)
from .tools import (
    REQUIRES_APPROVAL,
    TOOL_SCHEMAS,
    ToolError,
    ToolRunner,
    parse_arguments,
)

Emit = Callable[[dict[str, Any]], None]

MAX_HISTORY_MESSAGES = 40
MAX_HISTORY_CHARS = 120_000
# Quantos modelos alternativos do mesmo provedor entram depois do escolhido.
MAX_SELECTED_FALLBACKS = 4

# Só desativamos tools quando o erro realmente fala de ferramentas/funções.
_TOOLS_UNSUPPORTED = re.compile(r"tool|function|schema", re.IGNORECASE)


class AgentError(Exception):
    """Erro de alto nível que deve chegar ao usuário na UI."""


class ApprovalRegistry:
    """Fila de aprovações pendentes para ``run_shell``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}

    def create(self, payload: dict[str, Any]) -> str:
        approval_id = uuid.uuid4().hex[:10]
        with self._lock:
            self._pending[approval_id] = {
                "event": threading.Event(),
                "decision": None,
                "payload": payload,
            }
        return approval_id

    def wait(self, approval_id: str, timeout: float = 900.0) -> bool:
        with self._lock:
            entry = self._pending.get(approval_id)
        if entry is None:
            return False
        decided = entry["event"].wait(timeout)
        with self._lock:
            entry = self._pending.pop(approval_id, entry)
        return bool(decided and entry.get("decision"))

    def resolve(self, approval_id: str, allow: bool) -> bool:
        with self._lock:
            entry = self._pending.get(approval_id)
            if entry is None:
                return False
            entry["decision"] = bool(allow)
            entry["event"].set()
            return True

    def cancel_all(self) -> None:
        with self._lock:
            for entry in self._pending.values():
                entry["decision"] = False
                entry["event"].set()
            self._pending.clear()


def build_plans(cfg: dict[str, Any], model_override: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Ordem de tentativa (provedor + modelo).

    O modelo escolhido na UI vem primeiro; os provedores da ordem configurada
    entram como fallback, cada um com seu modelo padrão.
    """
    providers: dict[str, Any] = cfg.get("providers") or {}
    selected = model_override or cfg.get("selectedModel") or {}
    plans: list[dict[str, Any]] = []

    sel_provider = selected.get("provider")
    sel_model = selected.get("model")
    if sel_provider in providers and sel_provider != "auto" and sel_model:
        label = providers[sel_provider].get("label", sel_provider)
        plans.append({"provider": sel_provider, "model": sel_model, "label": label})
        conf = providers[sel_provider]
        # Se o modelo escolhido perder a permissão (404 "not found for account"),
        # tenta primeiro os outros que o health check aprovou.
        alternates = [
            str(model)
            for model in (conf.get("models") or [])
            if str(model) != sel_model and health.is_ok(cfg.get("modelHealth"), sel_provider, str(model))
        ]
        if not alternates:
            # Nada testado ainda: cai para o modelo padrão do mesmo provedor.
            default = conf.get("defaultModel")
            if default and str(default) != sel_model:
                alternates = [str(default)]
        for model in alternates:
            if len(plans) >= MAX_SELECTED_FALLBACKS:
                break
            if any(plan["model"] == model for plan in plans):
                continue
            plans.append({"provider": sel_provider, "model": model, "label": label})

    for name in cfg.get("providerOrder") or list(providers):
        conf = providers.get(name)
        if not conf:
            continue
        if any(plan["provider"] == name for plan in plans):
            continue
        model = conf.get("defaultModel") or (conf.get("models") or [None])[0]
        if not model:
            continue
        plans.append({"provider": name, "model": model, "label": conf.get("label", name)})

    if not plans:
        raise AgentError(
            "Nenhum modelo configurado. Escolha um modelo no seletor ou sincronize os modelos no painel de chaves."
        )
    return plans


def normalize_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitiza o histórico vindo do navegador (somente user/assistant)."""
    if not isinstance(history, list):
        raise AgentError("histórico inválido")
    clean: list[dict[str, Any]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        if not content.strip():
            continue
        clean.append({"role": role, "content": content})
    clean = clean[-MAX_HISTORY_MESSAGES:]
    total = 0
    trimmed: list[dict[str, Any]] = []
    for message in reversed(clean):
        total += len(message["content"])
        if total > MAX_HISTORY_CHARS:
            break
        trimmed.append(message)
    trimmed.reverse()
    if not trimmed or trimmed[-1]["role"] != "user":
        raise AgentError("a última mensagem do histórico precisa ser do usuário")
    return trimmed


class AgentRuntime:
    def __init__(
        self,
        store: ConfigStore,
        pool: KeyPool,
        approvals: ApprovalRegistry | None = None,
    ) -> None:
        self.store = store
        self.pool = pool
        self.approvals = approvals or ApprovalRegistry()
        # Provedores que rejeitaram o parâmetro `tools` (ex.: modelo sem function calling).
        self._no_tools: set[str] = set()

    # ------------------------------------------------------------------ #
    # Execução de um turno
    # ------------------------------------------------------------------ #
    def run(
        self,
        history: list[dict[str, Any]],
        emit: Emit,
        model_override: dict[str, Any] | None = None,
        auto_approve: bool | None = None,
    ) -> None:
        cfg = self.store.snapshot()
        agent_cfg = cfg.get("agent") or {}
        workspace = cfg.get("workspace") or str(default_workspace())
        runner = ToolRunner(
            workspace,
            shell_timeout=int(agent_cfg.get("shellTimeout") or 120),
            allow_system_commands=bool(agent_cfg.get("allowSystemCommands")),
            allow_read_outside_workspace=bool(agent_cfg.get("allowReadOutsideWorkspace")),
        )
        # A configuração é a única fonte que pode LIGAR a aprovação automática; o
        # corpo da requisição só consegue desligá-la por este turno. Antes, um POST
        # em /api/chat com autoApproveShell=true furava o gate de aprovação.
        auto_approve = bool(agent_cfg.get("autoApproveShell")) and (
            auto_approve is not False
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": str(agent_cfg.get("systemPrompt") or "")}
        ] + normalize_history(history)

        selected = model_override or cfg.get("selectedModel") or {}
        router_info: dict[str, Any] | None = None
        if str(selected.get("provider") or "") == "router":
            plans, router_info = self._router_plans(cfg, messages, emit)
        else:
            plans = build_plans(cfg, model_override)

        max_steps = max(1, int(agent_cfg.get("maxSteps") or 12))
        max_concurrent = max(
            1, min(int(agent_cfg.get("maxConcurrentRequests") or 2), 8)
        )
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        previous_response_id: str | None = None

        emit({"type": "start", "workspace": str(runner.workspace), "plans": plans})
        if router_info:
            emit({"type": "router", **router_info})

        for step in range(1, max_steps + 1):
            emit({"type": "step", "step": step, "maxSteps": max_steps})
            result, lease, plan = self._call_with_rotation(cfg, plans, messages, emit)
            previous_response_id = lease.key_id

            for field in usage_total:
                value = (result.usage or {}).get(field)
                if isinstance(value, int):
                    usage_total[field] += value
            if not usage_total["total_tokens"]:
                usage_total["total_tokens"] = usage_total["prompt_tokens"] + usage_total["completion_tokens"]

            if result.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": result.text or "",
                        "tool_calls": result.tool_calls,
                    }
                )
                self._run_tools(
                    result.tool_calls,
                    messages,
                    runner,
                    emit,
                    bool(auto_approve),
                    max_concurrent,
                )
                continue

            emit(
                {
                    "type": "done",
                    "text": result.text,
                    "usage": usage_total,
                    "model": result.model or plan["model"],
                    "provider": lease.provider,
                    "steps": step,
                }
            )
            return

        emit(
            {
                "type": "error",
                "message": (
                    f"O agente atingiu o limite de {max_steps} passos sem concluir. "
                    "Aumente 'maxSteps' nas configurações ou refine o pedido."
                ),
            }
        )

    # ------------------------------------------------------------------ #
    # Roteador automático
    # ------------------------------------------------------------------ #
    def _router_plans(
        self,
        cfg: dict[str, Any],
        messages: list[dict[str, Any]],
        emit: Emit,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Classifica o pedido e monta a ordem de modelos do roteador."""
        text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                text = str(message.get("content") or "")
                break

        task, source, classifier = self._classify(cfg, text, emit)
        ranked = rank_models(task, catalog(cfg))
        if not ranked:
            raise AgentError(
                "O roteador não encontrou modelos disponíveis. Sincronize os modelos no painel de chaves."
            )

        providers = cfg.get("providers") or {}
        plans = [
            {
                "provider": provider,
                "model": model,
                "label": (providers.get(provider) or {}).get("label", provider),
            }
            for provider, model in ranked[:6]
        ]
        covered = {plan["provider"] for plan in plans}
        for name in cfg.get("providerOrder") or list(providers):
            conf = providers.get(name) or {}
            if name in covered:
                continue
            model = conf.get("defaultModel") or (conf.get("models") or [None])[0]
            if model:
                plans.append({"provider": name, "model": model, "label": conf.get("label", name)})

        top = plans[0]
        info = {
            "task": task,
            "source": source,
            "classifier": classifier,
            "provider": top["provider"],
            "model": top["model"],
            "providerLabel": top["label"],
            "label": describe(task, top["model"], source, classifier),
            "candidates": [f"{plan['provider']}/{plan['model']}" for plan in plans[:5]],
        }
        return plans, info

    def _classify(
        self,
        cfg: dict[str, Any],
        text: str,
        emit: Emit,
    ) -> tuple[str, str, str]:
        """Chama um modelo leve para classificar a tarefa (com fallback local)."""
        prompt = (text or "").strip()[:2000]
        for candidate in classifier_candidates(cfg):
            lease = self.pool.acquire([candidate["provider"]])
            if lease is None:
                continue
            conf = (cfg.get("providers") or {}).get(lease.provider, {})
            try:
                result = chat_stream(
                    base_url=str(conf.get("baseUrl") or ""),
                    api_key=lease.key,
                    model=candidate["model"],
                    messages=[
                        {"role": "system", "content": CLASSIFIER_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    tools=None,
                    temperature=0,
                    max_tokens=6,
                    timeout=30.0,
                )
            except ProviderError as exc:
                if health.is_missing_model(exc):
                    self._mark_model_dead(cfg, candidate["provider"], candidate["model"], exc)
                if exc.kind == "rate_limit":
                    self.pool.report_rate_limited(lease, exc.retry_after)
                elif exc.kind == "auth":
                    self.pool.report_auth_error(lease, exc.message)
                else:
                    self.pool.report_transient_error(lease, exc.message)
                emit(
                    {
                        "type": "notice",
                        "message": (
                            f"roteador: classificador {candidate['model']} falhou "
                            f"({exc.message[:120]}) — tentando outro modelo leve."
                        ),
                    }
                )
                continue

            self.pool.report_success(lease, result.total_tokens)
            task = normalize_task(result.text)
            if task:
                return task, "ia", f"{lease.provider}/{candidate['model']}"

        return guess_task(text), "heuristica", ""

    def _mark_model_dead(
        self, cfg: dict[str, Any], provider: str, model: str, exc: ProviderError
    ) -> None:
        """Grava o veredito de modelo indisponível e atualiza o snapshot do turno."""
        try:
            item = health.record(self.store, provider, model, health.ERROR, exc.message, exc.status)
        except Exception:  # pragma: no cover - config somente leitura
            item = health.entry(health.ERROR, exc.message, exc.status)
        cfg.setdefault("modelHealth", {})[health.key_of(provider, model)] = item

    # ------------------------------------------------------------------ #
    # Chamada com rotação de chaves
    # ------------------------------------------------------------------ #
    def _call_with_rotation(
        self,
        cfg: dict[str, Any],
        plans: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        emit: Emit,
    ) -> tuple[ChatResult, Lease, dict[str, Any]]:
        agent_cfg = cfg.get("agent") or {}
        providers = list(dict.fromkeys(plan["provider"] for plan in plans))
        tried: set[tuple[str, str]] = set()
        max_wait = float(agent_cfg.get("maxWaitSeconds") or 180)
        deadline = time.time() + max_wait
        attempts = 0

        while True:
            attempts += 1
            if attempts > 60:
                raise AgentError("Muitas tentativas de rotação de chaves. Verifique as chaves e tente de novo.")

            lease = self.pool.acquire(providers)
            if lease is None:
                wait = self.pool.wait_seconds(providers)
                if wait is None:
                    raise AgentError(
                        "Nenhuma chave disponível. Cadastre uma chave no painel lateral ou "
                        "reative uma chave esgotada."
                    )
                if time.time() + wait > deadline:
                    raise AgentError(
                        f"Todas as chaves estão em cooldown/limite. A próxima libera em ~{math.ceil(wait)}s."
                    )
                emit(
                    {
                        "type": "waiting",
                        "message": "Aguardando chave disponível…",
                        "retryIn": max(1, math.ceil(wait)),
                    }
                )
                # Dorme até a vaga real (limitado pelo deadline) em vez de
                # acordar de 5 em 5s e arriscar um 429.
                remaining = max(0.5, deadline - time.time())
                time.sleep(min(max(wait, 0.5), remaining))
                continue

            for_provider = [p for p in plans if p["provider"] == lease.provider]
            # Modelos marcados como erro pelo health check saem da fila.
            healthy = [
                p for p in for_provider
                if health.is_ok(cfg.get("modelHealth"), p["provider"], p["model"])
            ]
            allowed = healthy or for_provider  # nada testado ainda? usa o catálogo
            untried = [
                p for p in allowed if (p["provider"], p["model"]) not in tried
            ]
            plan = untried[0] if untried else (allowed[0] if allowed else plans[0])
            provider_conf = (cfg.get("providers") or {}).get(lease.provider, {})
            tools = None if lease.provider in self._no_tools else TOOL_SCHEMAS

            emit(
                {
                    "type": "route",
                    "provider": lease.provider,
                    "providerLabel": provider_conf.get("label", lease.provider),
                    "model": plan["model"],
                    "keyLabel": lease.label,
                    "maskedKey": lease.masked,
                    "rpmLimit": lease.rpm_limit,
                    "toolsEnabled": tools is not None,
                    "tools": [item.get("function", {}).get("name") for item in (tools or [])],
                }
            )

            try:
                result = chat_stream(
                    base_url=str(provider_conf.get("baseUrl") or ""),
                    api_key=lease.key,
                    model=plan["model"],
                    messages=messages,
                    tools=tools,
                    temperature=agent_cfg.get("temperature", 0.2),
                    max_tokens=agent_cfg.get("maxTokens") or None,
                    on_delta=lambda channel, text: emit(
                        {"type": "delta", "channel": channel, "text": text}
                    ),
                )
            except ProviderError as exc:
                if health.is_missing_model(exc):
                    # 404 "Function not found for account": modelo morto para esta conta.
                    self._mark_model_dead(cfg, lease.provider, plan["model"], exc)
                    tried.add((lease.provider, plan["model"]))
                    emit(
                        {
                            "type": "notice",
                            "message": (
                                f"Modelo {plan['model']} falhou ({exc.message[:160]}), "
                                "alternando para o próximo modelo funcional…"
                            ),
                        }
                    )
                    if all((p["provider"], p["model"]) in tried for p in plans):
                        raise AgentError(
                            "Todos os modelos disponíveis falharam. Rode 'Testar Todos os Modelos' "
                            "no seletor de modelos e escolha um que funcione."
                        ) from None
                    continue
                if exc.kind == "bad_request" and tools is not None:
                    if _TOOLS_UNSUPPORTED.search(exc.message or ""):
                        # O modelo realmente não suporta function calling.
                        self._no_tools.add(lease.provider)
                        emit(
                            {
                                "type": "notice",
                                "message": (
                                    f"{plan['model']} recusou o parâmetro de ferramentas "
                                    f"({exc.message[:160]}). Repetindo sem tools — "
                                    "escolha um modelo com function calling para usar o agente completo."
                                ),
                            }
                        )
                        continue
                    raise AgentError(
                        f"{lease.provider} ({plan['model']}): {_failure_text(exc, plan)}"
                    ) from None
                if exc.kind == "rate_limit":
                    self.pool.report_rate_limited(lease, exc.retry_after)
                    emit(
                        {
                            "type": "notice",
                            "message": (
                                f"429 em {lease.provider}/{lease.label} — chave em cooldown"
                                f"{' de ' + str(int(exc.retry_after or 60)) + 's' if True else ''}, "
                                "trocando de chave."
                            ),
                        }
                    )
                    continue
                if exc.kind == "auth":
                    tried.add((lease.provider, plan["model"]))
                    self.pool.report_auth_error(lease, exc.message)
                    emit(
                        {
                            "type": "notice",
                            "message": f"Chave {lease.label} recusada ({exc.message[:160]}). Marcada como esgotada.",
                        }
                    )
                    continue
                if exc.kind in ("server", "network"):
                    tried.add((lease.provider, plan["model"]))
                    self.pool.report_transient_error(lease, exc.message)
                    # próximo modelo do ranking (fallback) ou próxima chave
                    emit(
                        {
                            "type": "notice",
                            "message": f"{lease.provider}: {exc.message[:200]} — tentando outra chave.",
                        }
                    )
                    continue
                raise AgentError(f"{lease.provider} ({plan['model']}): {_failure_text(exc, plan)}") from None
            except Exception as exc:  # pragma: no cover - falha inesperada do cliente
                self.pool.report_transient_error(lease, str(exc))
                emit({"type": "notice", "message": f"erro inesperado: {exc}"})
                continue

            self.pool.report_success(lease, result.total_tokens)
            return result, lease, plan

    # ------------------------------------------------------------------ #
    # Ferramentas
    # ------------------------------------------------------------------ #
    def _run_tools(
        self,
        calls: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        runner: ToolRunner,
        emit: Emit,
        auto_approve: bool,
        max_workers: int,
    ) -> None:
        """Executa um lote de ferramentas com concorrência limitada.

        Os ``tool_call`` saem na ordem do modelo e os resultados são emitidos e
        anexados ao histórico na mesma ordem, independentemente da thread que
        terminou primeiro. Isso mantém o payload OpenAI válido.
        """
        prepared: list[dict[str, Any]] = []
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            call_id = call.get("id") or f"call_{uuid.uuid4().hex[:8]}"
            try:
                args = parse_arguments(function.get("arguments"))
                error = ""
            except ToolError as exc:
                args, error = {}, str(exc)
            if not error:
                emit(
                    {
                        "type": "tool_call",
                        "id": call_id,
                        "name": name,
                        "args": _trim_args(args),
                    }
                )
            prepared.append(
                {"call_id": call_id, "name": name, "args": args, "error": error}
            )

        workers = max(1, min(int(max_workers or 1), len(prepared) or 1))
        if workers == 1 or len(prepared) <= 1:
            results = [
                self._run_single_tool(item, runner, emit, auto_approve)
                for item in prepared
            ]
        else:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="keyforge-tool"
            ) as executor:
                futures = [
                    executor.submit(
                        self._run_single_tool, item, runner, emit, auto_approve
                    )
                    for item in prepared
                ]
                results = [future.result() for future in futures]

        for item, (ok, output) in zip(prepared, results):
            messages.append(
                {"role": "tool", "tool_call_id": item["call_id"], "content": output}
            )
            emit(
                {
                    "type": "tool_result",
                    "id": item["call_id"],
                    "name": item["name"],
                    "ok": ok,
                    "output": output,
                    "args": _trim_args(item["args"]),
                }
            )

    def _run_single_tool(
        self,
        item: dict[str, Any],
        runner: ToolRunner,
        emit: Emit,
        auto_approve: bool,
    ) -> tuple[bool, str]:
        """Executa uma ferramenta (chamado em worker quando há paralelismo)."""
        if item["error"]:
            return False, item["error"]
        name = item["name"]
        args = item["args"]

        if name in REQUIRES_APPROVAL and not auto_approve:
            approval_id = self.approvals.create({"name": name, "args": _trim_args(args)})
            emit(
                {
                    "type": "approval_required",
                    "id": approval_id,
                    "callId": item["call_id"],
                    "name": name,
                    "args": _trim_args(args),
                }
            )
            if not self.approvals.wait(approval_id):
                return (
                    False,
                    "Comando não executado: o usuário negou ou a aprovação expirou.",
                )

        try:
            return True, runner.execute(name, args)
        except ToolError as exc:
            return False, f"erro: {exc}"
        except Exception as exc:  # pragma: no cover - defensivo
            return False, f"erro inesperado: {exc}"


    def reset_tool_support(self, provider: str | None = None) -> None:
        """Reabilita tools (ex.: depois de sincronizar modelos ou trocar a URL)."""
        if provider is None:
            self._no_tools.clear()
        else:
            self._no_tools.discard(provider)


def _failure_text(exc: ProviderError, plan: dict[str, Any]) -> str:
    parts = [exc.message]
    if exc.url:
        parts.append(f"URL: {exc.url}")
    if exc.status == 404:
        parts.append(
            "HTTP 404 — confira a Base URL do provedor em Configurações: "
            "ela deve apontar para a raiz da API (ex.: https://integrate.api.nvidia.com/v1)"
        )
    return " — ".join(parts)


def _trim_args(args: dict[str, Any], limit: int = 4000) -> dict[str, Any]:
    """Recorta argumentos enormes (ex.: conteúdo de write_file) para a UI."""
    trimmed: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if isinstance(value, str) and len(value) > limit:
            trimmed[key] = value[:limit] + f"\n… ({len(value) - limit} caracteres omitidos)"
        else:
            trimmed[key] = value
    try:
        json.dumps(trimmed)
    except (TypeError, ValueError):  # pragma: no cover - objetos exóticos
        return {"raw": str(trimmed)[:limit]}
    return trimmed
