from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_core.providers import (
    ProviderError,
    chat_stream,
    endpoint_url,
    list_models,
    normalize_base_url,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://integrate.api.nvidia.com/v1", "https://integrate.api.nvidia.com/v1"),
        ("https://integrate.api.nvidia.com/v1/", "https://integrate.api.nvidia.com/v1"),
        ("https://integrate.api.nvidia.com//v1//", "https://integrate.api.nvidia.com/v1"),
        ("https://integrate.api.nvidia.com", "https://integrate.api.nvidia.com/v1"),
        ("https://integrate.api.nvidia.com/", "https://integrate.api.nvidia.com/v1"),
        (
            "https://integrate.api.nvidia.com/v1/chat/completions",
            "https://integrate.api.nvidia.com/v1",
        ),
        (
            "https://integrate.api.nvidia.com//v1//chat/completions",
            "https://integrate.api.nvidia.com/v1",
        ),
        ("https://integrate.api.nvidia.com/v1/models", "https://integrate.api.nvidia.com/v1"),
        ("http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1"),
        ("http://127.0.0.1:8080/v1/", "http://127.0.0.1:8080/v1"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080/v1"),
        ("127.0.0.1:8080/v1", "https://127.0.0.1:8080/v1"),
        ("https://proxy.local/openai/v1", "https://proxy.local/openai/v1"),
        (
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ),
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
        ("http://127.0.0.1:8082/v1", "http://127.0.0.1:8082/v1"),
    ],
)
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    "base",
    [
        "https://integrate.api.nvidia.com/v1",
        "https://integrate.api.nvidia.com/v1/",
        "https://integrate.api.nvidia.com/v1//",
        "https://integrate.api.nvidia.com",
        "https://integrate.api.nvidia.com/v1/chat/completions",
        "https://integrate.api.nvidia.com/v1/chat/completions/",
    ],
)
def test_endpoint_url_never_duplicates_path(base):
    assert endpoint_url(base, "/chat/completions") == (
        "https://integrate.api.nvidia.com/v1/chat/completions"
    )
    assert endpoint_url(base, "models") == "https://integrate.api.nvidia.com/v1/models"


def test_normalize_rejects_empty():
    with pytest.raises(ProviderError):
        normalize_base_url("")


def test_google_openai_compatible_endpoint_keeps_v1beta():
    base = "https://generativelanguage.googleapis.com/v1beta/openai"
    assert endpoint_url(base, "/chat/completions") == f"{base}/chat/completions"
    assert endpoint_url(base, "models") == f"{base}/models"


def test_anthropic_proxy_uses_local_v1_path():
    base = "http://127.0.0.1:8082/v1"
    assert endpoint_url(base, "/chat/completions") == f"{base}/chat/completions"


class Recorder(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[dict] = []
    models = [f"model-{i:02d}" for i in range(45)]

    def log_message(self, *args):
        pass

    def _not_found(self) -> None:
        payload = b"404 page not found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        type(self).requests.append(
            {
                "method": "GET",
                "path": self.path,
                "auth": self.headers.get("Authorization"),
            }
        )
        if not self.path.startswith("/v1/"):
            self._not_found()
            return
        payload = json.dumps({"object": "list", "data": [{"id": m} for m in type(self).models]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(
            {
                "method": "POST",
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": body,
            }
        )
        if not self.path.startswith("/v1/"):
            self._not_found()
            return
        chunk = json.dumps({"choices": [{"index": 0, "delta": {"content": "ok"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b"data: " + chunk + b"\n\ndata: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture
def base_url():
    Recorder.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_chat_uses_single_v1_path_and_bearer_header(base_url):
    tools = [
        {
            "type": "function",
            "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    for candidate in (
        f"{base_url}/v1",
        f"{base_url}/v1/",
        f"{base_url}//v1//",
        f"{base_url}/v1/chat/completions",
        base_url,
    ):
        Recorder.requests.clear()
        result = chat_stream(
            candidate,
            "nvapi-abc123",
            "poolside/laguna-xs-2.1",
            [{"role": "user", "content": "oi"}],
            tools=tools,
        )
        assert result.text == "ok"

        request = Recorder.requests[-1]
        assert request["path"] == "/v1/chat/completions", candidate
        assert request["auth"] == "Bearer nvapi-abc123"
        assert request["body"]["tools"][0]["function"]["name"] == "read_file"
        assert request["body"]["tool_choice"] == "auto"
        assert request["body"]["model"] == "poolside/laguna-xs-2.1"


def test_list_models_returns_every_id(base_url):
    models = list_models(f"{base_url}/v1/", "nvapi-key")
    assert len(models) == 45
    assert models[0] == "model-00"
    assert Recorder.requests[-1]["path"] == "/v1/models"
    assert Recorder.requests[-1]["auth"] == "Bearer nvapi-key"


def test_http_404_reports_exact_url(base_url):
    with pytest.raises(ProviderError) as excinfo:
        chat_stream(f"{base_url}/caminho-errado", "k", "m", [{"role": "user", "content": "x"}])
    assert excinfo.value.status == 404
    assert excinfo.value.url == f"{base_url}/caminho-errado/chat/completions"


def test_request_is_logged_with_masked_key(base_url, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    chat_stream(f"{base_url}/v1", "nvapi-secret-value-9876", "m", [{"role": "user", "content": "oi"}])

    log = (tmp_path / "KeyForge" / "logs" / "agent.log").read_text(encoding="utf-8")
    assert "chat_request" in log
    assert f"{base_url}/v1/chat/completions" in log
    assert "nvapi-secret-value-9876" not in log
    assert "payload" in log
