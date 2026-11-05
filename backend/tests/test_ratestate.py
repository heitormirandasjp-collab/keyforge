"""Cota global de 40 RPM compartilhada por duas instâncias do KeyForge."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from agent_core.config import ConfigStore
from agent_core.keypool import KeyPool
from agent_core.ratestate import SharedRateState


def _make_store(tmp_path, monkeypatch, rpm: int = 40) -> ConfigStore:
    monkeypatch.setenv("KEYFORGE_CONFIG", str(tmp_path / "config.json"))
    store = ConfigStore()
    store.update({"providers": {"nvidia": {"rpmLimit": rpm}}})
    store.add_key("nvidia", "nvapi-shared-0001", "conta-1")
    return store


def test_two_instances_share_the_40_rpm_limit(tmp_path, monkeypatch):
    store_a = _make_store(tmp_path, monkeypatch, rpm=40)
    # Segundo ConfigStore com o mesmo arquivo = segunda instância do app.
    store_b = ConfigStore(store_a.path)
    pool_a = KeyPool(store_a)
    pool_b = KeyPool(store_b)

    granted = 0
    for index in range(45):
        pool = pool_a if index % 2 == 0 else pool_b
        if pool.acquire(["nvidia"]) is not None:
            granted += 1

    assert granted == 40
    assert pool_a.acquire(["nvidia"]) is None

    # As duas instâncias enxergam a mesma cota consumida.
    assert sum(item["rpm"]["used"] for item in pool_a.status()) == 40
    assert sum(item["rpm"]["used"] for item in pool_b.status()) == 40

    state = json.loads((tmp_path / "rate_state.json").read_text(encoding="utf-8"))
    assert len(state["keys"]["nvidia::" + store_a.data["providers"]["nvidia"]["keys"][0]["id"]]["events"]) == 40


def test_rate_state_file_records_events_and_after_request_stats(tmp_path):
    state = SharedRateState(tmp_path / "rate_state.json")
    key = "nvidia::abc123"

    assert state.try_reserve(key, 2, now=1000.0) == (True, 0.0)
    assert state.try_reserve(key, 2, now=1000.2)[0] is True

    ok, wait = state.try_reserve(key, 2, now=1000.4)
    assert ok is False
    assert 59.0 < wait <= 60.0

    state.record_result(key, ok=True, tokens=42, now=1000.5)
    entry = json.loads((tmp_path / "rate_state.json").read_text(encoding="utf-8"))["keys"][key]
    assert len(entry["events"]) == 2
    assert entry["requests"] == 1
    assert entry["failures"] == 0
    assert entry["tokens"] == 42

    state.record_result(key, ok=False, now=1000.6)
    entry = json.loads((tmp_path / "rate_state.json").read_text(encoding="utf-8"))["keys"][key]
    assert entry["requests"] == 2
    assert entry["failures"] == 1

    # A janela desliza: 60s depois das primeiras reservas, há vaga de novo.
    assert state.try_reserve(key, 2, now=1060.5)[0] is True


def test_file_lock_is_shared_between_processes(tmp_path):
    """Dois processos reais, não só duas instâncias no mesmo interpretador."""
    state_path = tmp_path / "rate_state.json"
    backend_root = Path(__file__).resolve().parents[1]
    code = (
        "import sys\n"
        "from agent_core.ratestate import SharedRateState\n"
        "state = SharedRateState(sys.argv[1])\n"
        "granted = 0\n"
        "for _ in range(25):\n"
        "    ok, _wait = state.try_reserve('nvidia::cross', 40)\n"
        "    granted += 1 if ok else 0\n"
        "print(granted)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(backend_root)}
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(state_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(2)
    ]
    grants = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        grants.append(int(stdout.strip()))

    assert sum(grants) == 40
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(payload["keys"]["nvidia::cross"]["events"]) == 40


def test_shared_lock_prevents_oversubscription_under_burst(tmp_path):
    state = SharedRateState(tmp_path / "rate_state.json")
    granted: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(10)

    def worker() -> None:
        barrier.wait()
        for _ in range(10):
            ok, _wait = state.try_reserve("nvidia::burst", 40)
            if ok:
                with lock:
                    granted.append(True)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(granted) == 40
    payload = json.loads((tmp_path / "rate_state.json").read_text(encoding="utf-8"))
    assert len(payload["keys"]["nvidia::burst"]["events"]) == 40
