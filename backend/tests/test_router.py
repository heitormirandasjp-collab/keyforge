"""Testes do roteador automático de modelos."""
from __future__ import annotations

import pytest

from agent_core import router

CATALOG = [
    ("nvidia", "minimax-m3.1-flash"),
    ("nvidia", "poolside/laguna-xs-2.1"),
    ("nvidia", "nemotron-3-super-49b"),
    ("nvidia", "meta/llama-3.3-70b-instruct"),
]

PROMPTS = {
    "escreva um código em Python que ordena uma lista": ("coding", "poolside/laguna-xs-2.1"),
    "qual a capital da França?": ("reasoning", "nemotron-3-super-49b"),
    "crie um poema sobre o mar": ("writing", "meta/llama-3.3-70b-instruct"),
}


@pytest.mark.parametrize("prompt", list(PROMPTS))
def test_router_picks_expected_model_per_prompt(prompt):
    task, model = PROMPTS[prompt]
    assert router.guess_task(prompt) == task
    assert router.rank_models(task, CATALOG)[0][1] == model


def test_normalize_task_reads_classifier_answer():
    assert router.normalize_task(" Coding.") == "coding"
    assert router.normalize_task("TAREFA: reasoning") == "reasoning"
    assert router.normalize_task("fast-chat") == "fast-chat"
    assert router.normalize_task("não sei responder") is None
    assert router.normalize_task("") is None


def test_guess_task_defaults_to_fast_chat():
    assert router.guess_task("oi, tudo bem?") == "fast-chat"
    assert router.guess_task("") == "fast-chat"


def test_rank_models_is_deterministic_and_complete():
    ranked = router.rank_models("coding", CATALOG)
    assert [pair for pair in ranked] == sorted(
        CATALOG,
        key=lambda item: (-router.score_model("coding", item[1]), CATALOG.index(item), item[1]),
    )
    assert {pair for pair in ranked} == set(CATALOG)


def test_catalog_respects_provider_order_and_default_model():
    cfg = {
        "providerOrder": ["freebuff", "nvidia"],
        "providers": {
            "nvidia": {"models": ["a", "b"], "defaultModel": "a"},
            "freebuff": {"models": ["claude-x"], "defaultModel": "gpt-x"},
        },
    }
    assert router.catalog(cfg) == [
        ("freebuff", "gpt-x"),
        ("freebuff", "claude-x"),
        ("nvidia", "a"),
        ("nvidia", "b"),
    ]


def test_classifier_prefers_light_model():
    cfg = {
        "providerOrder": ["nvidia"],
        "providers": {
            "nvidia": {
                "models": ["nemotron-3-super-49b", "minimax-m3.1-flash", "poolside/laguna-xs-2.1"],
                "defaultModel": "nemotron-3-super-49b",
            }
        },
    }
    candidates = router.classifier_candidates(cfg)
    assert candidates[0] == {"provider": "nvidia", "model": "minimax-m3.1-flash"}
    # Todos os modelos entram como fallback, sem duplicatas.
    assert len(candidates) == 3


def test_classifier_can_be_configured_explicitly():
    cfg = {
        "providerOrder": ["nvidia"],
        "router": {"classifierModel": "nvidia::meta/llama-3.3-70b-instruct"},
        "providers": {
            "nvidia": {"models": ["meta/llama-3.3-70b-instruct", "minimax-m3.1-flash"]}
        },
    }
    assert router.classifier_candidates(cfg)[0] == {
        "provider": "nvidia",
        "model": "meta/llama-3.3-70b-instruct",
    }


def test_describe_mentions_task_and_source():
    text = router.describe("coding", "poolside/laguna-xs-2.1", "ia", "nvidia/minimax-m3.1-flash")
    assert "poolside/laguna-xs-2.1" in text
    assert "coding" in text
    assert "classificador" in text
