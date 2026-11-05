"""Health check dos modelos: veredito por modelo, filtro e fallback 404.

Cenário central: catálogo de 5 modelos onde 3 funcionam e 2 respondem
``404 Function not found for account`` — exatamente o caso de permissão parcial
da conta NVIDIA.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_core import health
from agent_core.config import ConfigStore
from agent_core.providers import ChatResult, ProviderError
from agent_core.router import catalog
from agent_core.server import KeyForgeApp, create_server

GOOD = ["good/alpha", "good/beta", "good/gamma"]
DEAD = ["dead/one", "dead/two"]
CATALOG = GOOD + DEAD


class FakeProvider(BaseHTTPRequestHandler):
    """Provedor compatível com OpenAI: 3 modelos ok e 2 com 404 de permissão."""

    protocol_version = "HTTP/1.1"
    calls: list[dict] = []
    reject_key = False

    def log_message(self, *args):  # silencia
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        type(self).calls.append({"kind": "models", "path": self.path})
        self._json(200, {"object": "list", "data": [{"id": model} for model in CATALOG]})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        model = body.get("model")
        type(self).calls.append({"kind": "chat", "model": model, "body": body})

        if type(self).reject_key:
            self._json(401, {"error": {"message": "invalid api key"}})
            return
        if model in DEAD:
            self._json(
                404,
                {"error": {"message": f"Function not found for account: {model}"}},
            )
            return

        chunk = json.dumps({"choices": [{"index": 0, "delta": {"content": "oi"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b"data: " + chunk + b"\n\ndata: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KEYFORGE_CONFIG", str(tmp_path / "config.json"))
    FakeProvider.calls = []
    FakeProvider.reject_key = False

    provider = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    provider_port = provider.server_address[1]

    store = ConfigStore()
    store.update(
        {
            "providers": {
                "nvidia": {
                    "label": "NVIDIA NIM",
                    "baseUrl": f"http://127.0.0.1:{provider_port}/v1",
                    "rpmLimit": 0,
                    "models": list(CATALOG),
                    "defaultModel": GOOD[0],
                },
                # O provedor padrão do app fica fora do cenário.
                "freebuff": {"models": [], "defaultModel": ""},
            },
            "providerOrder": ["nvidia"],
            "workspace": str(tmp_path),
            "selectedModel": {"provider": "nvidia", "model": GOOD[0]},
        }
    )
    # Isola o cenário: os templates multi-provedor não entram no catálogo testado.
    for name, conf in store.data["providers"].items():
        if name != "nvidia":
            conf["models"] = []
            conf["defaultModel"] = ""
    store.save()
    store.add_key("nvidia", "nvapi-good-000001", "principal")

    app = KeyForgeApp(store=store, static_root=None)
    server = create_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    yield {
        "app": app,
        "store": store,
        "port": server.server_address[1],
        "base": f"http://127.0.0.1:{server.server_address[1]}",
    }

    server.shutdown()
    server.server_close()
    provider.shutdown()
    provider.server_close()


def _post_json(url: str, body: dict, timeout: float = 60.0) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def run_chat(port: int, messages: list[dict], model: dict | None = None) -> list[dict]:
    payload: dict = {"messages": messages}
    if model:
        payload["model"] = model
    events: list[dict] = []
    with urllib.request.urlopen(
        urllib.request.Request(
            f"http://127.0.0.1:{port}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        ),
        timeout=60,
    ) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            events.append(json.loads(line[5:].strip()))
    return events


# --------------------------------------------------------------------------- #
# Health check (3 ok, 2 com 404)
# --------------------------------------------------------------------------- #
def test_health_check_marks_each_model_and_reports_summary(env):
    body = _post_json(f"{env['base']}/api/models/health", {"provider": "nvidia"})

    assert body["summary"] == {"total": 5, "ok": 3, "error": 2, "unknown": 0}
    entries = body["config"]["modelHealth"]
    assert {key.split("::")[1] for key, item in entries.items() if item["status"] == "ok"} == set(GOOD)

    dead = entries["nvidia::dead/one"]
    assert dead["status"] == "error"
    assert "not found for account" in dead["reason"].lower()
    assert dead["httpStatus"] == 404
    assert dead["checkedAt"]  # data/hora legível
    assert dead["checkedTs"] > 0

    # gravado no config.json de verdade
    saved = json.loads(env["store"].path.read_text(encoding="utf-8"))
    assert saved["modelHealth"]["nvidia::dead/two"]["status"] == "error"
    assert body["testedAt"]


def test_individual_test_only_touches_that_model(env):
    body = _post_json(
        f"{env['base']}/api/models/health", {"provider": "nvidia", "model": "good/beta"}
    )

    assert list(body["results"]) == ["nvidia::good/beta"]
    assert body["results"]["nvidia::good/beta"]["status"] == "ok"
    assert body["summary"] == {"total": 5, "ok": 1, "error": 0, "unknown": 4}


def test_invalid_key_does_not_blame_the_models(env):
    FakeProvider.reject_key = True
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post_json(f"{env['base']}/api/models/health", {"provider": "nvidia"})

    assert excinfo.value.code == 502
    assert "recusada" in excinfo.value.read().decode("utf-8")
    assert env["store"].data["modelHealth"] == {}


def test_probe_treats_rate_limit_as_unknown():
    def fake_chat(**kwargs):
        raise ProviderError("429 too many requests", status=429, kind="rate_limit")

    verdict = health.probe_model("http://127.0.0.1:9/v1", "k", "m", chat=fake_chat)
    assert verdict["status"] == health.UNKNOWN
    assert "429" in verdict["reason"]


def test_probe_marks_404_as_error_with_reason():
    def fake_chat(**kwargs):
        raise ProviderError("Function not found for account", status=404, kind="bad_request")

    verdict = health.probe_model("http://127.0.0.1:9/v1", "k", "m", chat=fake_chat)
    assert verdict["status"] == health.ERROR
    assert "Function not found" in verdict["reason"]
    assert verdict["httpStatus"] == 404


def test_check_models_spaces_calls_to_respect_rpm():
    stamps: list[float] = []

    def fake_chat(**kwargs):
        stamps.append(time.monotonic())
        return ChatResult(text="ok")

    health.check_models(
        "http://127.0.0.1:9/v1",
        "k",
        [f"model-{i}" for i in range(6)],
        chat=fake_chat,
        workers=6,
        min_interval=0.05,
    )

    starts = sorted(stamps)
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
    assert all(gap >= 0.035 for gap in gaps), gaps


def test_check_models_runs_in_parallel():
    def fake_chat(**kwargs):
        time.sleep(0.25)
        return ChatResult(text="ok")

    started = time.perf_counter()
    result = health.check_models(
        "http://127.0.0.1:9/v1", "k", [f"model-{i}" for i in range(8)], chat=fake_chat, workers=8
    )
    elapsed = time.perf_counter() - started

    assert len(result) == 8
    assert all(item["status"] == health.OK for item in result.values())
    assert elapsed < 1.2, f"health check não foi paralelo: {elapsed:.2f}s"


# --------------------------------------------------------------------------- #
# Filtro e fallback
# --------------------------------------------------------------------------- #
def test_router_catalog_only_offers_models_marked_ok(env):
    _post_json(f"{env['base']}/api/models/health", {"provider": "nvidia"})
    cfg = env["store"].snapshot()

    assert {model for _, model in catalog(cfg)} == set(GOOD)
    # sem nenhum teste o catálogo continua completo (não trava o agente)
    cfg["modelHealth"] = {}
    assert {model for _, model in catalog(cfg)} == set(CATALOG)


def test_router_never_picks_a_dead_model(env):
    _post_json(f"{env['base']}/api/models/health", {"provider": "nvidia"})
    FakeProvider.calls = []

    events = run_chat(
        env["port"],
        [{"role": "user", "content": "escreva um código em python"}],
        model={"provider": "router", "model": "auto"},
    )

    assert any(event["type"] == "router" for event in events)
    done = next(event for event in events if event["type"] == "done")
    assert done["model"] in GOOD
    used = [call["model"] for call in FakeProvider.calls if call["kind"] == "chat"]
    assert used, "nenhuma chamada ao provedor"
    assert all(model in GOOD for model in used), used


def test_agent_marks_404_model_and_falls_back(env):
    env["store"].update({"selectedModel": {"provider": "nvidia", "model": "dead/one"}})
    FakeProvider.calls = []

    events = run_chat(env["port"], [{"role": "user", "content": "oi"}])

    notices = [event["message"] for event in events if event["type"] == "notice"]
    assert any("dead/one" in text and "alternando" in text for text in notices), notices

    done = next(event for event in events if event["type"] == "done")
    assert done["model"] in GOOD

    used = [call["model"] for call in FakeProvider.calls if call["kind"] == "chat"]
    assert used[0] == "dead/one"
    assert used[1] in GOOD

    saved = json.loads(env["store"].path.read_text(encoding="utf-8"))
    assert saved["modelHealth"]["nvidia::dead/one"]["status"] == "error"
    assert "not found for account" in saved["modelHealth"]["nvidia::dead/one"]["reason"].lower()
