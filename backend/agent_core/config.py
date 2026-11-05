"""Configuração e persistência do KeyForge.

O arquivo de configuração (chaves de API incluídas) vive em
``%APPDATA%\\KeyForge\\config.json`` e nunca é versionado no Git. As chaves
jamais saem do backend em texto puro: a API expõe apenas a versão mascarada.

Um ``KEYFORGE_CONFIG`` no ambiente sobrescreve o caminho (usado nos testes).
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

APP_NAME = "KeyForge"

DEFAULT_SYSTEM_PROMPT = (
    "Você é o KeyForge, um agente de programação autônomo que roda na máquina do "
    "usuário e fala português do Brasil.\n\n"
    "Você trabalha dentro de um diretório de trabalho (workspace) e tem ferramentas "
    "para ler arquivos, escrever arquivos, listar diretórios e executar comandos no "
    "shell (com aprovação do usuário).\n\n"
    "Regras:\n"
    "- Antes de alterar um arquivo, leia o trecho relevante.\n"
    "- Prefira mudanças pequenas e completas; explique o que mudou no final.\n"
    "- Sempre que usar uma ferramenta, use os resultados reais — nunca invente saída.\n"
    "- Comandos de shell são para build, testes e inspeção; evite comandos destrutivos.\n"
    "- Se um comando falhar, leia o erro e corrija a causa antes de tentar de novo.\n"
    "- Finalize com um resumo curto do que foi feito e dos arquivos tocados."
)

NVIDIA_DEFAULT_MODELS = [
    "poolside/laguna-xs-2.1",
    "minimax-m3.1-flash",
    "nemotron-3.5-lightning-30b-a3b",
]

FREEBUFF_DEFAULT_MODELS = [
    "anthropic/claude-sonnet-4.5",
    "openai/gpt-5",
]

OPENAI_DEFAULT_MODELS = ["gpt-4o", "gpt-4o-mini"]
GOOGLE_DEFAULT_MODELS = ["gemini-2.5-pro", "gemini-2.5-flash"]
DEEPSEEK_DEFAULT_MODELS = ["deepseek-chat", "deepseek-reasoner"]
OPENROUTER_DEFAULT_MODELS = [
    "openai/gpt-4o-mini",
    "openai/gpt-4o",
    "anthropic/claude-sonnet-4.5",
    "google/gemini-2.5-flash",
]
ANTHROPIC_DEFAULT_MODELS = ["claude-sonnet-4-5", "claude-haiku-4-5"]

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "providerOrder": [
        "nvidia",
        "freebuff",
        "openai",
        "google",
        "deepseek",
        "openrouter",
        "anthropic",
    ],
    "providers": {
        "nvidia": {
            "label": "NVIDIA NIM",
            "baseUrl": "https://integrate.api.nvidia.com/v1",
            "rpmLimit": 40,
            "defaultModel": NVIDIA_DEFAULT_MODELS[0],
            "models": list(NVIDIA_DEFAULT_MODELS),
            "keys": [],
        },
        "freebuff": {
            "label": "Freebuff (proxy)",
            "baseUrl": "http://127.0.0.1:8080/v1",
            "rpmLimit": 0,
            "defaultModel": FREEBUFF_DEFAULT_MODELS[0],
            "models": list(FREEBUFF_DEFAULT_MODELS),
            "keys": [],
        },
        "openai": {
            "label": "OpenAI",
            "baseUrl": "https://api.openai.com/v1",
            "rpmLimit": 40,
            "defaultModel": OPENAI_DEFAULT_MODELS[0],
            "models": list(OPENAI_DEFAULT_MODELS),
            "keys": [],
        },
        "google": {
            "label": "Google Gemini",
            # Endpoint compatível com OpenAI (o /v1beta é preservado na normalização).
            "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
            "rpmLimit": 10,
            "defaultModel": GOOGLE_DEFAULT_MODELS[0],
            "models": list(GOOGLE_DEFAULT_MODELS),
            "keys": [],
        },
        "deepseek": {
            "label": "DeepSeek",
            "baseUrl": "https://api.deepseek.com/v1",
            "rpmLimit": 40,
            "defaultModel": DEEPSEEK_DEFAULT_MODELS[0],
            "models": list(DEEPSEEK_DEFAULT_MODELS),
            "keys": [],
        },
        "openrouter": {
            "label": "OpenRouter",
            "baseUrl": "https://openrouter.ai/api/v1",
            "rpmLimit": 40,
            # IDs no formato vendor/modelo; "sincronizar modelos" traz o catálogo completo.
            "defaultModel": OPENROUTER_DEFAULT_MODELS[0],
            "models": list(OPENROUTER_DEFAULT_MODELS),
            "keys": [],
        },
        "anthropic": {
            # A API da Anthropic não é compatível com OpenAI: use o Vandamme Proxy
            # (ou similar) escutando em 127.0.0.1:8082 e traduzindo para /v1/messages.
            "label": "Anthropic (proxy local)",
            "baseUrl": "http://127.0.0.1:8082/v1",
            "rpmLimit": 40,
            "defaultModel": ANTHROPIC_DEFAULT_MODELS[0],
            "models": list(ANTHROPIC_DEFAULT_MODELS),
            "keys": [],
        },
    },
    "selectedModel": {"provider": "nvidia", "model": NVIDIA_DEFAULT_MODELS[0]},
    "router": {"classifierModel": ""},
    # Veredito do health check por modelo: "provedor::modelo" -> {status, reason…}
    "modelHealth": {},
    "agent": {
        "maxSteps": 12,
        "temperature": 0.2,
        "maxTokens": 4096,
        "maxWaitSeconds": 180,
        "shellTimeout": 120,
        "maxConcurrentRequests": 2,
        "autoApproveShell": False,
        "allowSystemCommands": False,
        "allowReadOutsideWorkspace": False,
        "systemPrompt": DEFAULT_SYSTEM_PROMPT,
    },
    "workspace": "",
}


# --------------------------------------------------------------------------- #
# Caminhos
# --------------------------------------------------------------------------- #
def appdata_dir() -> Path:
    """Pasta de configuração do usuário (``%APPDATA%\\KeyForge``)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    override = os.environ.get("KEYFORGE_CONFIG")
    if override:
        path = Path(override)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return appdata_dir() / "config.json"


def default_workspace() -> Path:
    ws = Path.home() / f"{APP_NAME}Workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def log_file() -> Path:
    """Arquivo de log de depuração (URL/payload das chamadas ao provedor)."""
    directory = appdata_dir() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "agent.log"


def read_log_tail(lines: int = 200) -> list[str]:
    path = log_file()
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return content[-max(1, int(lines)):]


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return "(sem chave)"
    if len(key) <= 10:
        return "•" * len(key)
    return f"{key[:6]}…{key[-4:]}"


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Mescla ``override`` em cima de ``base`` preservando chaves desconhecidas."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


LOCAL_PROXY_HOSTS = {"127.0.0.1", "localhost", "::1"}


def provider_allows_empty_key(conf: dict[str, Any]) -> bool:
    """Proxies locais (Freebuff, Anthropic via Vandamme) podem não exigir token."""
    raw = str(conf.get("baseUrl") or "").strip()
    if not raw:
        return False
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = urlsplit(raw).hostname
    except ValueError:
        return False
    return (host or "").lower() in LOCAL_PROXY_HOSTS


def new_key_entry(key: str, label: str = "") -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12],
        "label": label.strip(),
        "key": (key or "").strip(),
        "createdAt": time.time(),
        "stats": {
            "requests": 0,
            "tokens": 0,
            "failures": 0,
            "lastUsedAt": 0.0,
        },
        "state": {
            "cooldownUntil": 0.0,
            "exhausted": False,
            "reason": "",
        },
    }


class ConfigStore:
    """Store thread-safe para o config.json com escrita atômica."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else config_path()
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    # -- leitura/escrita ---------------------------------------------------- #
    def load(self) -> dict[str, Any]:
        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    raw = {}
                self._data = _deep_merge(DEFAULT_CONFIG, raw if isinstance(raw, dict) else {})
                # Migração: configs antigos ganham os provedores novos na ordem e
                # passam a ser persistidos já normalizados.
                before = copy.deepcopy(self._data)
                self._normalize()
                if self._data != before:
                    self._persist()
            else:
                self._data = copy.deepcopy(DEFAULT_CONFIG)
                self._persist()
            return self._data

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        if os.name != "nt":
            # No Windows o arquivo herda a ACL do perfil do usuário.
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, self.path)

    def save(self) -> None:
        with self._lock:
            self._persist()

    @property
    def data(self) -> dict[str, Any]:
        """Referência viva (chamadores já serializam com ``public``/snapshot)."""
        return self._data

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Atualiza campos de nível superior de forma rasa e persiste."""
        with self._lock:
            for key in (
                "providerOrder",
                "selectedModel",
                "agent",
                "workspace",
                "providers",
                "modelHealth",
            ):
                if key not in patch:
                    continue
                value = patch[key]
                if key == "agent" and isinstance(value, dict):
                    self._data["agent"].update(
                        {k: v for k, v in value.items() if k in DEFAULT_CONFIG["agent"]}
                    )
                elif key == "providers" and isinstance(value, dict):
                    self._update_providers(value)
                else:
                    self._data[key] = copy.deepcopy(value)
            self._normalize()
            self._persist()
            return copy.deepcopy(self._data)

    def _update_providers(self, incoming: dict[str, Any]) -> None:
        allowed = {"label", "baseUrl", "rpmLimit", "defaultModel", "models"}
        for name, conf in incoming.items():
            current = self._data["providers"].setdefault(name, copy.deepcopy(
                DEFAULT_CONFIG["providers"].get(name, {})
            ))
            if not isinstance(conf, dict):
                continue
            for field, value in conf.items():
                if field in allowed:
                    current[field] = copy.deepcopy(value)

    def _normalize(self) -> None:
        """Garante invariantes (ordem de provedores, modelo selecionado válido)."""
        providers = self._data["providers"]
        order = [p for p in self._data.get("providerOrder", []) if p in providers]
        for name in providers:
            if name not in order:
                order.append(name)
        if not order:
            order = list(DEFAULT_CONFIG["providerOrder"])
        self._data["providerOrder"] = order

        for name, conf in providers.items():
            conf.setdefault("keys", [])
            conf.setdefault("models", [])
            if not conf.get("defaultModel") and conf["models"]:
                conf["defaultModel"] = conf["models"][0]
            try:
                conf["rpmLimit"] = max(0, int(conf.get("rpmLimit") or 0))
            except (TypeError, ValueError):
                conf["rpmLimit"] = 0

        selected = self._data.get("selectedModel") or {}
        provider = selected.get("provider")
        model = selected.get("model")
        if provider in ("auto", "router"):
            self._data["selectedModel"] = {"provider": provider, "model": "auto"}
        else:
            if provider not in providers:
                provider = order[0]
                model = providers[provider].get("defaultModel")
            if not model:
                model = providers[provider].get("defaultModel")
            self._data["selectedModel"] = {"provider": provider, "model": model}

        try:
            self._data["workspace"] = str(self._data.get("workspace") or "")
        except (TypeError, ValueError):
            self._data["workspace"] = ""

        self._normalize_health(providers)

    def _normalize_health(self, providers: dict[str, Any]) -> None:
        """Mantém só vereditos de modelos que ainda existem no catálogo."""
        raw = self._data.get("modelHealth")
        catalog = {
            f"{name}::{model}"
            for name, conf in providers.items()
            for model in (conf.get("models") or [])
        }
        clean: dict[str, Any] = {}
        if isinstance(raw, dict):
            for key, item in raw.items():
                if not isinstance(item, dict) or key not in catalog:
                    continue
                status = "ok" if item.get("status") == "ok" else "error"
                clean[str(key)] = {
                    "status": status,
                    "reason": str(item.get("reason") or "")[:300],
                    "httpStatus": int(item.get("httpStatus") or 0),
                    "latencyMs": int(item.get("latencyMs") or 0),
                    "checkedAt": str(item.get("checkedAt") or ""),
                    "checkedTs": float(item.get("checkedTs") or 0),
                }
        self._data["modelHealth"] = clean

    # -- chaves ------------------------------------------------------------- #
    def add_key(self, provider: str, key: str, label: str = "") -> dict[str, Any]:
        with self._lock:
            if provider not in self._data["providers"]:
                raise KeyError(f"provedor desconhecido: {provider}")
            key = (key or "").strip()
            if not key and not provider_allows_empty_key(self._data["providers"][provider]):
                raise ValueError("informe uma chave de API")
            for existing in self._data["providers"][provider]["keys"]:
                if existing["key"] == key:
                    raise ValueError("essa chave já está cadastrada nesse provedor")
            entry = new_key_entry(key, label or f"chave {len(self._data['providers'][provider]['keys']) + 1}")
            self._data["providers"][provider]["keys"].append(entry)
            self._persist()
            return copy.deepcopy(entry)

    def remove_key(self, provider: str, key_id: str) -> bool:
        with self._lock:
            conf = self._data["providers"].get(provider)
            if not conf:
                return False
            before = len(conf["keys"])
            conf["keys"] = [k for k in conf["keys"] if k["id"] != key_id]
            changed = len(conf["keys"]) != before
            if changed:
                self._persist()
            return changed

    def get_key(self, provider: str, key_id: str) -> dict[str, Any] | None:
        with self._lock:
            for entry in self._data["providers"].get(provider, {}).get("keys", []):
                if entry["id"] == key_id:
                    return entry
            return None

    def reset_key(self, provider: str, key_id: str) -> bool:
        with self._lock:
            entry = self.get_key(provider, key_id)
            if not entry:
                return False
            entry["state"] = {"cooldownUntil": 0.0, "exhausted": False, "reason": ""}
            self._persist()
            return True

    def reorder_keys(self, provider: str, ordered_ids: list[str]) -> bool:
        with self._lock:
            conf = self._data["providers"].get(provider)
            if not conf:
                return False
            by_id = {k["id"]: k for k in conf["keys"]}
            new_list = [by_id[i] for i in ordered_ids if i in by_id]
            for entry in conf["keys"]:
                if entry["id"] not in ordered_ids:
                    new_list.append(entry)
            conf["keys"] = new_list
            self._persist()
            return True

    # -- visão pública ------------------------------------------------------ #
    def public(self) -> dict[str, Any]:
        """Config sem segredos — é isso que vai para o navegador."""
        with self._lock:
            data = copy.deepcopy(self._data)
        for conf in data["providers"].values():
            for entry in conf.get("keys", []):
                entry["key"] = mask_key(entry.get("key", ""))
                entry.pop("rawKey", None)
        data["configPath"] = str(self.path)
        return data