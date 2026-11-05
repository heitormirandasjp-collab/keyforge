from __future__ import annotations

import pytest

from agent_core.config import ConfigStore


@pytest.fixture
def store(tmp_path, monkeypatch) -> ConfigStore:
    monkeypatch.setenv("KEYFORGE_CONFIG", str(tmp_path / "config.json"))
    return ConfigStore()
