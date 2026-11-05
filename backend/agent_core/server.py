"""Servidor HTTP local do KeyForge (somente biblioteca padrão).

Rotas:

* ``GET  /api/state``                      → config (mascarada) + status das chaves
* ``POST /api/keys``                       → cadastra chave
* ``DELETE /api/keys/{provider}/{id}``     → remove chave
* ``POST /api/keys/{provider}/{id}/reset`` → limpa cooldown/esgotada
* ``POST /api/keys/{provider}/reorder``    → reordena a rotação
* ``POST /api/keys/{provider}/{id}/test``  → valida a chave
* ``PATCH /api/config``                    → preferências (ordem, rpm, modelo…)
* ``POST /api/providers/{p}/models/refresh`` → sincroniza modelos do provedor
* ``POST /api/models/health``              → testa modelos (todos ou um só)
* ``POST /api/chat``                       → loop do agente em SSE
* ``POST /api/approvals/{id}``             → aprova/nega comando de shell
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, health
from .agent import AgentError, AgentRuntime, ApprovalRegistry
from .config import APP_NAME, ConfigStore, default_workspace, log_file, read_log_tail
from .dialog import DialogUnavailable, pick_directory
from .keypool import KeyPool
from .providers import ProviderError, list_models, probe_key

MAX_BODY_BYTES = 8 * 1024 * 1024
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}
# Portas aceitas como origem: a porta em que este servidor está escutando, a
# porta canônica (8787) e a do Vite em desenvolvimento (5173, via proxy).
# Antes qualquer porta de loopback era aceita.
CANONICAL_PORTS = {8787, 5173}
# Nomes que nunca podem sair pelo servidor estático, mesmo por engano de build.
SENSITIVE_STATIC_NAMES = {"config.json", "config.json.tmp", "last-port.txt", "agent.log"}
STATIC_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


class ClientGone(Exception):
    """O navegador fechou a conexão SSE."""


class KeyForgeApp:
    def __init__(self, store: ConfigStore | None = None, static_root: str | Path | None = None) -> None:
        self.store = store or ConfigStore()
        self.pool = KeyPool(self.store)
        self.approvals = ApprovalRegistry()
        self.agent = AgentRuntime(self.store, self.pool, self.approvals)
        self.static_root = Path(static_root) if static_root else None
        self.started_at = time.time()

    # ------------------------------------------------------------------ #
    def state(self) -> dict[str, Any]:
        workspace = self.store.data.get("workspace") or str(default_workspace())
        return {
            "app": APP_NAME,
            "version": __version__,
            "config": self.store.public(),
            "keys": self.pool.status(),
            "workspace": workspace,
            "workspaceExists": Path(workspace).exists(),
            "uptime": round(time.time() - self.started_at, 1),
        }

    def workspace_path(self) -> Path:
        raw = self.store.data.get("workspace")
        if raw:
            return Path(str(raw)).expanduser().resolve()
        return default_workspace()

    def pick_workspace(self) -> dict[str, Any]:
        """Abre o seletor nativo de pastas e salva a escolha."""
        try:
            chosen = pick_directory(str(self.workspace_path()))
        except DialogUnavailable as exc:
            raise ValueError(f"{exc} — digite o caminho no campo e clique em 'usar esta pasta'") from None
        if not chosen:
            return {"ok": True, "canceled": True, **self.state()}
        target = Path(chosen).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"não foi possível criar o diretório: {exc}") from None
        if not target.is_dir():
            raise ValueError("o caminho escolhido precisa ser um diretório")
        self.store.update({"workspace": str(target.resolve())})
        return {"ok": True, "canceled": False, **self.state()}

    # ------------------------------------------------------------------ #
    # Ações
    # ------------------------------------------------------------------ #
    def add_key(self, body: dict[str, Any]) -> dict[str, Any]:
        provider = str(body.get("provider") or "").strip()
        key = str(body.get("key") or "").strip()
        label = str(body.get("label") or "").strip()
        entry = self.store.add_key(provider, key, label)
        return {"ok": True, "id": entry["id"], "label": entry["label"]}

    def test_key(self, provider: str, key_id: str) -> dict[str, Any]:
        entry = self.store.get_key(provider, key_id)
        if not entry:
            raise KeyError("chave não encontrada")
        conf = self.store.data["providers"].get(provider, {})
        message = probe_key(
            str(conf.get("baseUrl") or ""),
            entry.get("key", ""),
            conf.get("defaultModel"),
        )
        self.store.reset_key(provider, key_id)
        return {"ok": True, "message": message}

    def refresh_models(self, provider: str) -> dict[str, Any]:
        conf = self.store.data["providers"].get(provider)
        if not conf:
            raise KeyError("provedor desconhecido")
        keys = conf.get("keys") or []
        if not keys:
            raise ValueError("cadastre pelo menos uma chave para sincronizar os modelos")
        base_url = str(conf.get("baseUrl") or "")
        models: list[str] = []
        errors: list[str] = []
        last_error: ProviderError | None = None
        # Tenta cada chave: uma chave inválida não deve impedir o catálogo de 45 modelos.
        for entry in keys:
            try:
                models = list_models(base_url, entry.get("key", ""))
            except ProviderError as exc:
                last_error = exc
                errors.append(f"{entry.get('label') or 'chave'}: {exc.message}")
                if exc.kind in ("auth", "bad_request", "server", "rate_limit"):
                    continue
                raise
            if models:
                break
        if not models:
            if last_error is not None:
                # Mantém 502 + URL final (importante para diagnosticar 401/404).
                raise last_error
            raise ValueError("o provedor não retornou modelos em /models")
        self.store.update({"providers": {provider: {"models": models}}})
        # Catálogo novo pode incluir modelos com function calling: reabilita tools.
        self.agent.reset_tool_support(provider)
        return {"ok": True, "models": models, "count": len(models)}

    def test_models(self, provider: str | None = None, model: str | None = None) -> dict[str, Any]:
        """Health check: um POST mínimo por modelo, em paralelo.

        Sem ``provider`` testa todos os provedores; com ``model`` testa só ele.
        O veredito fica em ``config.json`` (``modelHealth``).
        """
        providers = self.store.data["providers"]
        if provider and provider not in providers:
            raise KeyError("provedor desconhecido")
        targets = [provider] if provider else list(providers)
        results: dict[str, dict[str, Any]] = {}

        for name in targets:
            conf = providers.get(name) or {}
            catalog = [str(item) for item in (conf.get("models") or []) if item]
            models = [model] if (model and provider) else catalog
            if not models:
                continue
            picked = health.pick_key(conf)
            if picked is None:
                if provider:
                    raise ValueError(
                        "nenhuma chave disponível para testar os modelos "
                        "(cadastre uma chave ou aguarde o cooldown)"
                    )
                continue
            key, label = picked
            batch = health.check_models(
                str(conf.get("baseUrl") or ""),
                key,
                models,
                workers=health.workers_for(conf),
                min_interval=health.interval_for(conf),
            )
            if batch and all(
                item["status"] == health.ERROR and item.get("httpStatus") in (401, 403)
                for item in batch.values()
            ):
                # Chave inválida não é defeito do modelo: não grava veredito.
                message = next(iter(batch.values())).get("reason") or "chave recusada"
                if provider:
                    raise ProviderError(
                        f"a chave {label} foi recusada ({message}) — "
                        "o teste de modelos precisa de uma chave válida",
                        status=401,
                        kind="auth",
                    )
                continue
            for tested, verdict in batch.items():
                results[health.key_of(name, tested)] = {**verdict, "keyLabel": label}

        if not results:
            raise ValueError("nenhum modelo para testar — sincronize os modelos do provedor")

        merged = dict(self.store.data.get("modelHealth") or {})
        merged.update(results)
        self.store.update({"modelHealth": merged})
        health_map = self.store.data.get("modelHealth") or {}
        return {
            "ok": True,
            "results": results,
            "summary": health.summarize(health_map, providers),
            "testedAt": health.stamp(),
        }


def _trim(value: str, limit: int = 500) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[:limit] + "…"


def make_handler(app: KeyForgeApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"KeyForge/{__version__}"
        protocol_version = "HTTP/1.1"

        # -- infraestrutura ------------------------------------------------- #
        def log_message(self, fmt: str, *args: Any) -> None:  # silencia o log padrão
            if os.environ.get("KEYFORGE_DEBUG"):
                sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

        def _server_port(self) -> int:
            try:
                return int(self.server.server_address[1])
            except (AttributeError, IndexError, TypeError, ValueError):
                return 8787

        def _allowed_ports(self) -> set[int]:
            return {self._server_port(), *CANONICAL_PORTS}

        def _host_allowed(self) -> bool:
            """Bloqueia DNS rebinding: o Host precisa ser um nome de loopback."""
            host = (self.headers.get("Host") or "").strip()
            if not host:
                return True
            try:
                name = (urlparse(f"//{host}").hostname or "").lower()
            except ValueError:
                return False
            return name in LOCAL_HOSTNAMES

        def _origin_allowed(self) -> bool:
            """Aceita apenas a própria origem local (127.0.0.1/localhost/::1)."""
            if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True
            try:
                parsed = urlparse(origin)
                name = (parsed.hostname or "").lower()
                port = parsed.port
            except ValueError:
                return False
            if parsed.scheme not in ("http", "https"):
                return False
            if name not in LOCAL_HOSTNAMES:
                return False
            return port in self._allowed_ports()

        def _cors(self) -> None:
            origin = self.headers.get("Origin")
            if origin and self._origin_allowed():
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _error(self, status: int, message: str) -> None:
            self._send_json(status, {"ok": False, "error": message})

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return {}
            if length > MAX_BODY_BYTES:
                raise ValueError("corpo da requisição grande demais")
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ValueError("JSON inválido no corpo da requisição") from None
            if not isinstance(data, dict):
                raise ValueError("o corpo deve ser um objeto JSON")
            return data

        # -- verbos --------------------------------------------------------- #
        def do_OPTIONS(self) -> None:
            if not self._host_allowed():
                self._error(403, "host não autorizado")
                return
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PATCH(self) -> None:
            self._dispatch("PATCH")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            if not self._host_allowed():
                self._error(403, "host não autorizado")
                return
            path = urlparse(self.path).path
            if not path.startswith("/api/"):
                if method == "GET":
                    self._serve_static(path)
                else:
                    self._error(405, "método não suportado")
                return
            if not self._origin_allowed():
                self._error(403, "origem não autorizada")
                return
            try:
                self._handle_api(method, path)
            except ClientGone:
                return
            except (KeyError, ValueError) as exc:
                message = str(exc).strip("'\"")
                self._error(400, message)
            except ProviderError as exc:
                message = exc.message
                if exc.url:
                    message = f"{message} (URL: {exc.url})"
                self._error(502, message)
            except Exception as exc:  # pragma: no cover - defensivo
                if os.environ.get("KEYFORGE_DEBUG"):
                    traceback.print_exc()
                self._error(500, f"erro interno: {exc}")

        # -- API ------------------------------------------------------------ #
        def _handle_api(self, method: str, path: str) -> None:
            if method == "GET" and path == "/api/health":
                self._send_json(
                    200, {"ok": True, "app": APP_NAME, "version": __version__, "uptime": round(time.time() - app.started_at, 1)}
                )
                return
            if method == "GET" and path == "/api/state":
                self._send_json(200, {"ok": True, **app.state()})
                return
            if method == "GET" and path == "/api/logs":
                query = parse_qs(urlparse(self.path).query)
                try:
                    tail = int((query.get("tail") or ["200"])[0])
                except ValueError:
                    tail = 200
                tail = max(1, min(tail, 2000))
                self._send_json(
                    200,
                    {"ok": True, "path": str(log_file()), "lines": read_log_tail(tail)},
                )
                return
            if method == "POST" and path == "/api/chat":
                self._chat()
                return
            if method == "PATCH" and path == "/api/config":
                body = self._read_json()
                self._apply_config(body)
                self._send_json(200, {"ok": True, **app.state()})
                return
            if method == "POST" and path == "/api/workspace/pick":
                self._read_json()
                self._send_json(200, app.pick_workspace())
                return
            if method == "POST" and path == "/api/workspace":
                body = self._read_json()
                raw = str(body.get("path") or "").strip()
                if not raw:
                    raise ValueError("informe o caminho do diretório de trabalho")
                target = Path(raw).expanduser()
                if not target.exists():
                    target.mkdir(parents=True, exist_ok=True)
                if not target.is_dir():
                    raise ValueError("o caminho precisa ser um diretório")
                app.store.update({"workspace": str(target.resolve())})
                self._send_json(200, {"ok": True, **app.state()})
                return
            if method == "POST" and path == "/api/keys":
                body = self._read_json()
                result = app.add_key(body)
                self._send_json(200, {"ok": True, **result, **app.state()})
                return

            match = re.match(r"^/api/keys/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)/reset$", path)
            if method == "POST" and match:
                provider, key_id = match.groups()
                if not app.store.reset_key(provider, key_id):
                    raise KeyError("chave não encontrada")
                self._send_json(200, {"ok": True, **app.state()})
                return

            match = re.match(r"^/api/keys/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)/test$", path)
            if method == "POST" and match:
                provider, key_id = match.groups()
                result = app.test_key(provider, key_id)
                self._send_json(200, {**result, **app.state()})
                return

            match = re.match(r"^/api/keys/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)$", path)
            if method == "DELETE" and match:
                provider, key_id = match.groups()
                if not app.store.remove_key(provider, key_id):
                    raise KeyError("chave não encontrada")
                app.pool.drop_key(provider, key_id)
                self._send_json(200, {"ok": True, **app.state()})
                return

            match = re.match(r"^/api/keys/([a-zA-Z0-9_-]+)/reorder$", path)
            if method == "POST" and match:
                body = self._read_json()
                ids = body.get("ids") or []
                if not isinstance(ids, list):
                    raise ValueError("'ids' deve ser uma lista")
                if not app.store.reorder_keys(match.group(1), [str(i) for i in ids]):
                    raise KeyError("provedor desconhecido")
                self._send_json(200, {"ok": True, **app.state()})
                return

            match = re.match(r"^/api/providers/([a-zA-Z0-9_-]+)/models/refresh$", path)
            if method == "POST" and match:
                result = app.refresh_models(match.group(1))
                self._send_json(200, {**result, **app.state()})
                return

            if method == "POST" and path == "/api/models/health":
                body = self._read_json()
                provider = str(body.get("provider") or "").strip() or None
                model = str(body.get("model") or "").strip() or None
                result = app.test_models(provider, model)
                self._send_json(200, {**result, **app.state()})
                return

            match = re.match(r"^/api/approvals/([a-zA-Z0-9_-]+)$", path)
            if method == "POST" and match:
                body = self._read_json()
                decision = str(body.get("decision") or "").lower()
                allow = decision in ("allow", "approve", "sim", "yes", "true")
                if not app.approvals.resolve(match.group(1), allow):
                    raise KeyError("aprovação não encontrada ou já resolvida")
                self._send_json(200, {"ok": True, "allowed": allow})
                return

            self._error(404, f"rota não encontrada: {method} {path}")

        def _apply_config(self, body: dict[str, Any]) -> None:
            patch: dict[str, Any] = {}
            for field in ("providerOrder", "selectedModel", "agent", "providers", "workspace"):
                if field in body:
                    patch[field] = body[field]

            if "workspace" in patch:
                raw_workspace = str(patch.get("workspace") or "").strip()
                if not raw_workspace:
                    patch["workspace"] = ""
                else:
                    target = Path(raw_workspace).expanduser()
                    try:
                        target.mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        raise ValueError(f"não foi possível criar o diretório: {exc}") from None
                    if not target.is_dir():
                        raise ValueError("o caminho do workspace precisa ser um diretório")
                    patch["workspace"] = str(target.resolve())

            if "providerOrder" in patch:
                order = patch["providerOrder"]
                if not isinstance(order, list):
                    raise ValueError("providerOrder deve ser uma lista")
                providers = app.store.data["providers"]
                patch["providerOrder"] = [str(p) for p in order if str(p) in providers]

            if "selectedModel" in patch:
                selected = patch["selectedModel"]
                if not isinstance(selected, dict):
                    raise ValueError("selectedModel inválido")
                provider = str(selected.get("provider") or "")
                model = str(selected.get("model") or "")
                if provider not in ("auto", "router", *app.store.data["providers"].keys()):
                    raise ValueError("provedor do modelo inválido")
                if provider in ("auto", "router"):
                    model = "auto"
                elif not model:
                    raise ValueError("modelo não informado")
                patch["selectedModel"] = {"provider": provider, "model": model}

            app.store.update(patch)
            if "providers" in patch:
                # Base URL/modelos podem ter mudado: tools voltam a ser tentadas.
                app.agent.reset_tool_support()
            for provider, conf in (patch.get("providers") or {}).items():
                if isinstance(conf, dict) and "rpmLimit" in conf:
                    for entry in app.store.data["providers"].get(provider, {}).get("keys", []):
                        limiter = app.pool.limiters.peek(entry["id"])
                        if limiter:
                            limiter.set_limit(int(app.store.data["providers"][provider]["rpmLimit"]))

        # -- chat (SSE) ----------------------------------------------------- #
        def _start_sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self._cors()
            self.end_headers()
            self.close_connection = True

        def _emit(self, event: dict[str, Any]) -> None:
            payload = f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n".encode("utf-8")
            try:
                self.wfile.write(payload)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                raise ClientGone() from None

        def _chat(self) -> None:
            body = self._read_json()
            history = body.get("messages")
            if not isinstance(history, list) or not history:
                raise ValueError("'messages' deve ser uma lista não vazia")

            model_override = body.get("model")
            if model_override is not None and not isinstance(model_override, dict):
                raise ValueError("'model' deve ser um objeto {provider, model}")
            auto_approve = body.get("autoApproveShell")
            if auto_approve is not None:
                auto_approve = bool(auto_approve)

            self._start_sse()
            # Emite eventos de forma serializada: ferramentas em paralelo podem
            # escrever no mesmo stream SSE em threads diferentes.
            emit_lock = threading.Lock()

            def emit(event: dict[str, Any]) -> None:
                with emit_lock:
                    self._emit(event)

            try:
                app.agent.run(
                    history,
                    emit,
                    model_override=model_override,
                    auto_approve=auto_approve,
                )
            except ClientGone:
                return
            except AgentError as exc:
                self._emit({"type": "error", "message": str(exc)})
            except ProviderError as exc:
                self._emit({"type": "error", "message": exc.message})
            except Exception as exc:  # pragma: no cover - defensivo
                if os.environ.get("KEYFORGE_DEBUG"):
                    traceback.print_exc()
                self._emit({"type": "error", "message": f"erro interno: {exc}"})

        # -- estáticos ------------------------------------------------------ #
        def _serve_static(self, path: str) -> None:
            root = app.static_root
            if root is None or not root.exists():
                self._error(
                    500,
                    "frontend não encontrado. Rode 'npm run build' em frontend/ ou use o instalador.",
                )
                return
            root = root.resolve()
            # `unquote` fecha o buraco de `%2e%2e%2f` (path traversal codificado).
            relative = unquote(path).lstrip("/") or "index.html"
            parts = [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]
            if any(part.startswith(".") for part in parts) or any(
                part.lower() in SENSITIVE_STATIC_NAMES for part in parts
            ):
                self._error(404, "arquivo não encontrado")
                return
            try:
                target = (root / relative).resolve()
            except (OSError, ValueError):
                self._error(400, "caminho inválido")
                return
            if target != root and root not in target.parents:
                self._error(403, "caminho inválido")
                return
            if target.is_dir():
                target = target / "index.html"
            if not target.exists():
                target = root / "index.html"
                if not target.exists():
                    self._error(404, "arquivo não encontrado")
                    return
            try:
                data = target.read_bytes()
            except OSError as exc:
                self._error(500, f"falha ao ler arquivo: {exc}")
                return
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            cache = "no-cache"
            if "/assets/" in str(target).replace("\\", "/"):
                cache = "public, max-age=31536000, immutable"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if target.suffix.lower() in (".html", ".htm"):
                self.send_header("Content-Security-Policy", STATIC_CSP)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def find_free_port(host: str = "127.0.0.1", start: int = 8787, attempts: int = 25) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"nenhuma porta livre entre {start} e {start + attempts - 1}")


def create_server(app: KeyForgeApp, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server
