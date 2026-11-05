"""Teste ponta a ponta: rotação, tool calling, aprovação e SSE.

Sobe um provedor falso compatível com OpenAI em uma porta efêmera, aponta o
KeyForge para ele e exercita a API HTTP real.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_core.config import ConfigStore
from agent_core.keypool import Lease
from agent_core.server import KeyForgeApp, create_server


class MockProvider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls: list[dict] = []
    tool_name = "list_directory"
    tool_args = '{"path": "."}'
    models = [f"model-{i:02d}" for i in range(45)]
    models_error = ""
    reject_tools = False
    bad_request_reason = ""

    def log_message(self, *args):  # silencia
        pass

    def do_GET(self):
        type(self).calls.append(
            {"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")}
        )
        if type(self).models_error:
            payload = json.dumps({"error": {"message": type(self).models_error}}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = json.dumps(
            {"object": "list", "data": [{"id": name} for name in type(self).models]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        auth = self.headers.get("Authorization", "")
        type(self).calls.append({"path": self.path, "auth": auth, "body": body})

        # Classificador do roteador: responde uma palavra conforme o pedido.
        system = next(
            (m.get("content") or "" for m in body.get("messages", []) if m.get("role") == "system"),
            "",
        )
        if "classificador de tarefas" in system:
            text = next(
                (
                    (m.get("content") or "")
                    for m in reversed(body.get("messages", []))
                    if m.get("role") == "user"
                ),
                "",
            ).lower()
            if "python" in text or "código" in text or "codigo" in text:
                word = "coding"
            elif "capital" in text or "por que" in text:
                word = "reasoning"
            elif "poema" in text:
                word = "writing"
            else:
                word = "fast-chat"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            for chunk in (
                {"choices": [{"index": 0, "delta": {"content": word}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ):
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        if type(self).bad_request_reason:
            payload = json.dumps({"error": {"message": type(self).bad_request_reason}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if type(self).reject_tools and body.get("tools"):
            payload = json.dumps(
                {"error": {"message": "tools are not supported by this model"}}
            ).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if "bad" in auth:
            payload = json.dumps({"error": {"message": "quota exceeded"}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "30")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        already_used_tool = any(m.get("role") == "tool" for m in body.get("messages", []))
        chunks = []
        if not already_used_tool:
            chunks.append({"choices": [{"index": 0, "delta": {"content": "Vou inspecionar."}}]})
            chunks.append(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": type(self).tool_name,
                                            "arguments": type(self).tool_args,
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
            chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        else:
            chunks.append({"choices": [{"index": 0, "delta": {"content": "Pronto."}}]})
            chunks.append(
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
                }
            )

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KEYFORGE_CONFIG", str(tmp_path / "config.json"))
    MockProvider.calls = []
    MockProvider.tool_name = "list_directory"
    MockProvider.tool_args = '{"path": "."}'
    MockProvider.models_error = ""
    MockProvider.reject_tools = False
    MockProvider.bad_request_reason = ""

    provider = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    provider_port = provider.server_address[1]

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "leia-me.txt").write_text("conteudo", encoding="utf-8")

    store = ConfigStore()
    store.update(
        {
            "providers": {
                "nvidia": {
                    "baseUrl": f"http://127.0.0.1:{provider_port}/v1",
                    "rpmLimit": 40,
                    "models": ["mock-model"],
                    "defaultModel": "mock-model",
                },
                "freebuff": {"baseUrl": f"http://127.0.0.1:{provider_port}/v1"},
            },
            "providerOrder": ["nvidia", "freebuff"],
            "workspace": str(workspace),
            "selectedModel": {"provider": "nvidia", "model": "mock-model"},
        }
    )
    # Os templates multi-provedor não devem participar deste cenário de roteamento.
    for name, conf in store.data["providers"].items():
        if name not in ("nvidia", "freebuff"):
            conf["models"] = []
            conf["defaultModel"] = ""
    store.save()
    store.add_key("nvidia", "nvapi-bad-000001", "ruim")
    store.add_key("nvidia", "nvapi-good-000002", "boa")

    app = KeyForgeApp(store=store, static_root=None)
    server = create_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    yield {"app": app, "store": store, "port": server.server_address[1], "workspace": workspace}

    server.shutdown()
    server.server_close()
    provider.shutdown()
    provider.server_close()


def _post(url: str, body: dict, timeout: float = 30.0):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=timeout)


def run_chat(port: int, messages: list[dict], on_event=None, model: dict | None = None) -> list[dict]:
    payload = {"messages": messages}
    if model:
        payload["model"] = model
    events: list[dict] = []
    with _post(f"http://127.0.0.1:{port}/api/chat", payload) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            events.append(event)
            if on_event:
                on_event(event)
    return events


def test_rotates_on_429_executes_tool_and_finishes(env):
    events = run_chat(env["port"], [{"role": "user", "content": "liste os arquivos"}])

    routes = [e for e in events if e["type"] == "route"]
    # Step 1: chave "ruim" toma 429 -> rotação; step 2 (após o tool) usa a "boa".
    assert [r["keyLabel"] for r in routes] == ["ruim", "boa", "boa"]
    assert all(r["provider"] == "nvidia" for r in routes)

    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert tool_results and tool_results[0]["ok"] is True
    assert "leia-me.txt" in tool_results[0]["output"]

    done = [e for e in events if e["type"] == "done"]
    assert done and done[0]["text"] == "Pronto."
    assert done[0]["usage"]["total_tokens"] == 13

    status = {k["label"]: k for k in env["app"].pool.status()}
    assert status["ruim"]["status"] == "cooldown"
    assert status["ruim"]["cooldownRemaining"] > 0
    assert status["boa"]["stats"]["requests"] == 2


def test_shell_tool_waits_for_approval(env, monkeypatch):
    MockProvider.tool_name = "run_shell"
    MockProvider.tool_args = '{"command": "echo aprovado-pela-ui"}'

    approvals: list[str] = []

    def approve(event: dict) -> None:
        if event["type"] == "approval_required":
            approvals.append(event["id"])
            # Simula o clique do usuário em outra conexão.
            with _post(f"http://127.0.0.1:{env['port']}/api/approvals/{event['id']}", {"decision": "allow"}) as r:
                r.read()

    events = run_chat(env["port"], [{"role": "user", "content": "rode echo"}], on_event=approve)

    assert approvals, "o agente deveria pedir aprovação para run_shell"
    results = [e for e in events if e["type"] == "tool_result"]
    assert results and results[0]["ok"] is True
    assert "aprovado-pela-ui" in results[0]["output"]


def test_shell_tool_denied_does_not_execute(env):
    MockProvider.tool_name = "run_shell"
    MockProvider.tool_args = '{"command": "echo NUNCA"}'

    def deny(event: dict) -> None:
        if event["type"] == "approval_required":
            with _post(f"http://127.0.0.1:{env['port']}/api/approvals/{event['id']}", {"decision": "deny"}) as r:
                r.read()

    events = run_chat(env["port"], [{"role": "user", "content": "rode echo"}], on_event=deny)
    results = [e for e in events if e["type"] == "tool_result"]
    assert results and results[0]["ok"] is False
    assert "negou" in results[0]["output"]


def test_state_api_masks_secrets(env):
    with urllib.request.urlopen(f"http://127.0.0.1:{env['port']}/api/state", timeout=10) as response:
        data = json.loads(response.read())
    raw = json.dumps(data)
    assert data["ok"] is True
    assert "nvapi-good-000002" not in raw
    assert len(data["keys"]) == 2
    assert data["config"]["workspace"] == str(env["workspace"])


def test_add_and_remove_key_via_api(env):
    with _post(
        f"http://127.0.0.1:{env['port']}/api/keys",
        {"provider": "freebuff", "key": "fb-token-abc", "label": "fb-1"},
    ) as response:
        data = json.loads(response.read())
    assert data["ok"] is True
    key_id = data["id"]
    assert any(k["id"] == key_id for k in data["keys"])

    request = urllib.request.Request(
        f"http://127.0.0.1:{env['port']}/api/keys/freebuff/{key_id}", method="DELETE"
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        after = json.loads(response.read())
    assert all(k["id"] != key_id for k in after["keys"])


def test_chat_rejects_empty_messages(env):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(f"http://127.0.0.1:{env['port']}/api/chat", {"messages": []})
    assert excinfo.value.code == 400


def test_unknown_route_returns_404(env):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"http://127.0.0.1:{env['port']}/api/nope", timeout=10)
    assert excinfo.value.code == 404


def test_foreign_origin_is_forbidden(env):
    request = urllib.request.Request(
        f"http://127.0.0.1:{env['port']}/api/state",
        headers={"Origin": "https://evil.example"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 403


def test_other_loopback_port_is_forbidden(env):
    request = urllib.request.Request(
        f"http://127.0.0.1:{env['port']}/api/state",
        headers={"Origin": "http://127.0.0.1:9999"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 403


def test_own_origin_is_allowed(env):
    request = urllib.request.Request(
        f"http://127.0.0.1:{env['port']}/api/state",
        headers={"Origin": f"http://127.0.0.1:{env['port']}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
        assert response.headers.get("Access-Control-Allow-Origin") == (
            f"http://127.0.0.1:{env['port']}"
        )


def test_rebinding_host_is_forbidden(env):
    request = urllib.request.Request(
        f"http://127.0.0.1:{env['port']}/api/state",
        headers={"Host": "evil.example"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 403


def test_chat_body_cannot_force_shell_auto_approval(env):
    MockProvider.tool_name = "run_shell"
    MockProvider.tool_args = json.dumps({"command": "echo NUNCA"})
    assert env["store"].data["agent"]["autoApproveShell"] is False

    events: list[dict] = []
    with _post(
        f"http://127.0.0.1:{env['port']}/api/chat",
        {"messages": [{"role": "user", "content": "rode"}], "autoApproveShell": True},
    ) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            events.append(event)
            if event["type"] == "approval_required":
                with _post(
                    f"http://127.0.0.1:{env['port']}/api/approvals/{event['id']}",
                    {"decision": "deny"},
                ) as approval:
                    approval.read()

    assert any(e["type"] == "approval_required" for e in events)
    results = [e for e in events if e["type"] == "tool_result"]
    assert results and results[0]["ok"] is False


def test_static_server_never_serves_config_json(tmp_path, monkeypatch):
    monkeypatch.setenv("KEYFORGE_CONFIG", str(tmp_path / "config.json"))
    store = ConfigStore()
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    (root / "config.json").write_text('{"providers": {}}', encoding="utf-8")

    app = KeyForgeApp(store=store, static_root=root)
    server = create_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        for path in ("/config.json", "/%2e%2e/config.json", "/../config.json"):
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10)
            assert excinfo.value.code in (403, 404)

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
            assert b"ok" in response.read()
            assert "Content-Security-Policy" in response.headers
    finally:
        server.shutdown()
        server.server_close()


def test_all_keys_unavailable_returns_clear_error(env):
    store = env["store"]
    for key in list(store.data["providers"]["nvidia"]["keys"]):
        lease = Lease("nvidia", key["id"], key["key"], key["label"], 40)
        env["app"].pool.report_auth_error(lease, "sem cota")

    events = run_chat(env["port"], [{"role": "user", "content": "oi"}])
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "Nenhuma chave" in errors[0]["message"]


def test_chat_request_sends_tools_and_correct_url(env):
    MockProvider.calls.clear()
    run_chat(env["port"], [{"role": "user", "content": "liste os arquivos"}])

    first = MockProvider.calls[0]
    assert first["path"] == "/v1/chat/completions"
    assert first["auth"].startswith("Bearer nvapi-")
    assert first["body"]["tool_choice"] == "auto"
    names = [tool["function"]["name"] for tool in first["body"]["tools"]]
    assert names == ["read_file", "write_file", "list_directory", "run_shell"]

    # A rotação gera 3 chamadas (429 da chave ruim, tool call, resposta final).
    last_messages = MockProvider.calls[-1]["body"]["messages"]
    tool_messages = [m for m in last_messages if m.get("role") == "tool"]
    assert tool_messages, "o resultado da ferramenta deveria voltar para o modelo"
    assert "name" not in tool_messages[0]
    assert tool_messages[0]["tool_call_id"] == "call_1"


def test_refresh_models_replaces_catalog(env):
    MockProvider.calls.clear()
    with _post(f"http://127.0.0.1:{env['port']}/api/providers/nvidia/models/refresh", {}) as response:
        data = json.loads(response.read())

    assert data["ok"] is True
    assert data["count"] == 45
    assert MockProvider.calls[-1]["path"] == "/v1/models"

    with urllib.request.urlopen(f"http://127.0.0.1:{env['port']}/api/state", timeout=10) as response:
        state = json.loads(response.read())
    provider = state["config"]["providers"]["nvidia"]
    assert len(provider["models"]) == 45
    assert "model-44" in provider["models"]
    assert provider["defaultModel"] == "mock-model"


def test_refresh_models_failure_is_reported(env):
    MockProvider.models_error = "upstream indisponível"
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(f"http://127.0.0.1:{env['port']}/api/providers/nvidia/models/refresh", {})
    assert excinfo.value.code == 502
    body = json.loads(excinfo.value.read())
    assert "upstream indisponível" in body["error"]
    assert "/v1/models" in body["error"]


def test_unrelated_400_surfaces_error_with_url(env):
    MockProvider.bad_request_reason = "max_tokens inválido"
    events = run_chat(env["port"], [{"role": "user", "content": "oi"}])

    errors = [e for e in events if e["type"] == "error"]
    assert errors, "um 400 não relacionado a tools deve virar erro claro"
    assert "max_tokens inválido" in errors[0]["message"]
    assert "/v1/chat/completions" in errors[0]["message"]


def test_tools_rejection_falls_back_without_tools(env):
    MockProvider.reject_tools = True
    events = run_chat(env["port"], [{"role": "user", "content": "liste"}])

    notices = [e["message"] for e in events if e["type"] == "notice"]
    assert any("sem tools" in message for message in notices)
    assert "tools" not in MockProvider.calls[1]["body"]
    assert [e for e in events if e["type"] == "done"]


def test_workspace_can_be_changed_via_api(env, tmp_path):
    target = tmp_path / "Desktop" / "Projeto"
    with _post(
        f"http://127.0.0.1:{env['port']}/api/workspace", {"path": str(target)}
    ) as response:
        data = json.loads(response.read())

    assert data["ok"] is True
    assert data["workspace"].endswith("Projeto")
    assert target.is_dir()

    # As ferramentas passam a usar o novo diretório imediatamente.
    MockProvider.tool_name = "write_file"
    MockProvider.tool_args = json.dumps({"path": "src/app.txt", "content": "real\n"})
    run_chat(env["port"], [{"role": "user", "content": "crie o arquivo"}])

    created = target / "src" / "app.txt"
    assert created.exists()
    assert created.read_text(encoding="utf-8") == "real\n"


def test_write_file_tool_writes_on_disk(env):
    MockProvider.tool_name = "write_file"
    MockProvider.tool_args = json.dumps({"path": "notas.txt", "content": "linha 1\nlinha 2\n"})
    events = run_chat(env["port"], [{"role": "user", "content": "escreva"}])

    target = env["workspace"] / "notas.txt"
    assert target.exists(), "write_file deve criar o arquivo no disco"
    assert target.read_text(encoding="utf-8").startswith("linha 1")

    results = [e for e in events if e["type"] == "tool_result"]
    assert results and results[0]["ok"] is True
    assert "criado" in results[0]["output"]


def test_run_shell_tool_executes_real_command(env):
    MockProvider.tool_name = "run_shell"
    MockProvider.tool_args = json.dumps({"command": "echo real > shell.txt"})

    def approve(event):
        if event["type"] == "approval_required":
            with _post(
                f"http://127.0.0.1:{env['port']}/api/approvals/{event['id']}",
                {"decision": "allow"},
            ) as response:
                response.read()

    events = run_chat(
        env["port"], [{"role": "user", "content": "rode o comando"}], on_event=approve
    )

    target = env["workspace"] / "shell.txt"
    assert target.exists(), "o comando deveria criar o arquivo dentro do workspace"
    results = [e for e in events if e["type"] == "tool_result"]
    assert results and results[0]["ok"] is True


def test_router_selects_model_per_task(env):
    env["store"].update({"selectedModel": {"provider": "router", "model": "auto"}})
    env["store"].update(
        {
            "providers": {
                "nvidia": {
                    "models": [
                        "minimax-m3.1-flash",
                        "poolside/laguna-xs-2.1",
                        "nemotron-3-super-49b",
                        "meta/llama-3.3-70b-instruct",
                    ]
                }
            }
        }
    )

    cases = {
        "escreva um código em Python que ordena uma lista": (
            "coding",
            "poolside/laguna-xs-2.1",
        ),
        "qual a capital da França?": ("reasoning", "nemotron-3-super-49b"),
        "crie um poema sobre o mar": ("writing", "meta/llama-3.3-70b-instruct"),
    }

    for prompt, (task, model) in cases.items():
        MockProvider.calls.clear()
        events = run_chat(env["port"], [{"role": "user", "content": prompt}])

        routers = [e for e in events if e["type"] == "router"]
        assert routers, f"o modo roteador deveria emitir evento router para: {prompt}"
        info = routers[0]
        assert info["task"] == task
        assert info["model"] == model
        assert model in info["label"]
        assert "classificador" in info["label"]

        def is_classifier(call):
            system = call["body"]["messages"][0].get("content") or ""
            return "classificador de tarefas" in system

        classifier_calls = [c for c in MockProvider.calls if is_classifier(c)]
        chat_calls = [c for c in MockProvider.calls if not is_classifier(c)]
        assert classifier_calls, "deveria haver uma chamada ao modelo leve"
        assert "tools" not in classifier_calls[0]["body"]
        assert chat_calls[0]["body"]["model"] == model
        assert [e for e in events if e["type"] == "done"]


def test_router_falls_back_to_next_model_on_failure(env):
    env["store"].update({"selectedModel": {"provider": "router", "model": "auto"}})
    env["store"].update(
        {
            "providers": {
                "nvidia": {
                    "models": ["poolside/laguna-xs-2.1", "meta/llama-3.3-70b-instruct"]
                }
            }
        }
    )
    MockProvider.calls.clear()
    events = run_chat(env["port"], [{"role": "user", "content": "escreva um código em Python"}])

    routers = [e for e in events if e["type"] == "router"]
    assert routers and routers[0]["task"] == "coding"
    assert routers[0]["model"] == "poolside/laguna-xs-2.1"
    assert routers[0]["candidates"][1] == "nvidia/meta/llama-3.3-70b-instruct"


def test_workspace_picker_saves_choice(env, tmp_path, monkeypatch):
    import agent_core.server as server_module

    target = tmp_path / "Pasta Escolhida"
    target.mkdir()
    monkeypatch.setattr(server_module, "pick_directory", lambda initial=None: str(target))

    with _post(f"http://127.0.0.1:{env['port']}/api/workspace/pick", {}) as response:
        data = json.loads(response.read())

    assert data["ok"] is True
    assert data["canceled"] is False
    assert data["workspace"] == str(target.resolve())
    assert env["store"].data["workspace"] == str(target.resolve())


def test_workspace_picker_cancel_keeps_current(env, monkeypatch):
    import agent_core.server as server_module

    before = env["store"].data["workspace"]
    monkeypatch.setattr(server_module, "pick_directory", lambda initial=None: None)

    with _post(f"http://127.0.0.1:{env['port']}/api/workspace/pick", {}) as response:
        data = json.loads(response.read())

    assert data["canceled"] is True
    assert data["workspace"] == before


def test_workspace_picker_without_gui_explains_alternative(env, monkeypatch):
    import agent_core.server as server_module

    def boom(initial=None):
        raise server_module.DialogUnavailable("sem ambiente gráfico")

    monkeypatch.setattr(server_module, "pick_directory", boom)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(f"http://127.0.0.1:{env['port']}/api/workspace/pick", {})

    assert excinfo.value.code == 400
    assert "digite o caminho" in json.loads(excinfo.value.read())["error"]


def test_selected_router_model_is_persisted(env):
    request = urllib.request.Request(
        f"http://127.0.0.1:{env['port']}/api/config",
        data=json.dumps({"selectedModel": {"provider": "router", "model": ""}}).encode(),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read())
    assert data["config"]["selectedModel"] == {"provider": "router", "model": "auto"}


def test_tool_cannot_escape_workspace(env):
    MockProvider.tool_name = "read_file"
    MockProvider.tool_args = json.dumps({"path": "../../segredo.txt"})
    events = run_chat(env["port"], [{"role": "user", "content": "leia"}])

    results = [e for e in events if e["type"] == "tool_result"]
    assert results and results[0]["ok"] is False
    assert "fora do workspace" in results[0]["output"]
