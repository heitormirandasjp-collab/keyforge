from __future__ import annotations

import json

import pytest

from agent_core.config import ConfigStore, mask_key


def test_defaults_are_created_on_first_load(store, tmp_path):
    assert store.path.exists()
    data = store.snapshot()
    assert data["providerOrder"] == [
        "nvidia",
        "freebuff",
        "openai",
        "google",
        "deepseek",
        "openrouter",
        "anthropic",
    ]
    assert data["providers"]["nvidia"]["rpmLimit"] == 40
    assert data["providers"]["nvidia"]["keys"] == []
    assert data["providers"]["openai"]["baseUrl"] == "https://api.openai.com/v1"
    assert data["providers"]["openai"]["models"] == ["gpt-4o", "gpt-4o-mini"]
    assert data["providers"]["google"]["baseUrl"].endswith("/v1beta/openai")
    assert data["providers"]["deepseek"]["defaultModel"] == "deepseek-chat"
    assert data["providers"]["openrouter"]["baseUrl"] == "https://openrouter.ai/api/v1"
    assert data["providers"]["anthropic"]["baseUrl"] == "http://127.0.0.1:8082/v1"
    assert data["agent"]["maxConcurrentRequests"] == 2


def test_legacy_config_receives_new_provider_templates(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "providerOrder": ["nvidia", "freebuff"],
                "providers": {
                    "nvidia": {
                        "label": "NVIDIA NIM",
                        "baseUrl": "https://integrate.api.nvidia.com/v1",
                        "rpmLimit": 40,
                        "defaultModel": "poolside/laguna-xs-2.1",
                        "models": ["poolside/laguna-xs-2.1"],
                        "keys": [],
                    },
                    "freebuff": {
                        "label": "Freebuff (proxy)",
                        "baseUrl": "http://127.0.0.1:8080/v1",
                        "rpmLimit": 0,
                        "defaultModel": "anthropic/claude-sonnet-4.5",
                        "models": ["anthropic/claude-sonnet-4.5"],
                        "keys": [],
                    },
                },
                "selectedModel": {
                    "provider": "nvidia",
                    "model": "poolside/laguna-xs-2.1",
                },
            }
        ),
        encoding="utf-8",
    )

    data = ConfigStore(path).snapshot()
    assert data["providerOrder"][:2] == ["nvidia", "freebuff"]
    for name in ("openai", "google", "deepseek", "openrouter", "anthropic"):
        assert name in data["providerOrder"]
        assert data["providers"][name]["baseUrl"]

    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert "anthropic" in reloaded["providerOrder"]


def test_empty_key_allowed_only_for_local_proxy(store):
    entry = store.add_key("anthropic", "", "vandamme")
    assert entry["key"] == ""
    with pytest.raises(ValueError):
        store.add_key("openai", "", "sem chave")


def test_max_concurrent_requests_is_persisted(store):
    store.update({"agent": {"maxConcurrentRequests": 3}})
    assert ConfigStore(store.path).snapshot()["agent"]["maxConcurrentRequests"] == 3


def test_keys_are_masked_in_public_view(store):
    entry = store.add_key("nvidia", "nvapi-super-secret-1234", "conta 1")
    public = store.public()
    raw = json.dumps(public)
    assert "nvapi-super-secret-1234" not in raw
    assert entry["id"] in raw
    assert mask_key("nvapi-super-secret-1234").endswith("1234")


def test_config_survives_reload(store):
    store.add_key("nvidia", "nvapi-aaaa-1111", "a")
    store.update({"providerOrder": ["freebuff", "nvidia"]})

    reloaded = ConfigStore(store.path).snapshot()
    assert reloaded["providerOrder"][:2] == ["freebuff", "nvidia"]
    assert "openai" in reloaded["providers"] and "anthropic" in reloaded["providers"]
    assert len(reloaded["providers"]["nvidia"]["keys"]) == 1
    assert reloaded["providers"]["nvidia"]["keys"][0]["key"] == "nvapi-aaaa-1111"


def test_duplicate_key_is_rejected(store):
    store.add_key("nvidia", "nvapi-duplicada-0001", "a")
    try:
        store.add_key("nvidia", "nvapi-duplicada-0001", "b")
    except ValueError as exc:
        assert "já está cadastrada" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("chave duplicada deveria falhar")


def test_auto_selection_is_persisted(store):
    store.update({"selectedModel": {"provider": "auto", "model": "auto"}})
    assert store.snapshot()["selectedModel"] == {"provider": "auto", "model": "auto"}


def test_invalid_selected_model_falls_back(store):
    store.update({"selectedModel": {"provider": "auto", "model": "auto"}})
    store.update({"selectedModel": {"provider": "provedor-inexistente", "model": "x"}})
    selected = store.snapshot()["selectedModel"]
    assert selected["provider"] == "nvidia"
    assert selected["model"] == store.snapshot()["providers"]["nvidia"]["defaultModel"]


def test_remove_and_reset_key(store):
    entry = store.add_key("nvidia", "nvapi-reset-0002", "a")
    store.data["providers"]["nvidia"]["keys"][0]["state"] = {
        "cooldownUntil": 9e9,
        "exhausted": True,
        "reason": "429",
    }
    assert store.reset_key("nvidia", entry["id"]) is True
    state = store.data["providers"]["nvidia"]["keys"][0]["state"]
    assert state["exhausted"] is False and state["cooldownUntil"] == 0

    assert store.remove_key("nvidia", entry["id"]) is True
    assert store.snapshot()["providers"]["nvidia"]["keys"] == []
    assert store.remove_key("nvidia", entry["id"]) is False


def test_reorder_keys(store):
    first = store.add_key("nvidia", "nvapi-1111-aaaa", "1")
    second = store.add_key("nvidia", "nvapi-2222-bbbb", "2")
    assert store.reorder_keys("nvidia", [second["id"], first["id"]]) is True
    keys = store.snapshot()["providers"]["nvidia"]["keys"]
    assert [k["label"] for k in keys] == ["2", "1"]
