"""Concorrência das ferramentas limitada por maxConcurrentRequests."""
from __future__ import annotations

import json
import threading
import time

from agent_core.agent import AgentRuntime


class _ConcurrencyProbe:
    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def execute(self, name: str, args: dict) -> str:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        return f"ok:{args['path']}"


def _runtime() -> AgentRuntime:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.approvals = None  # read_file não exige aprovação
    return runtime


def _calls(count: int) -> list[dict]:
    return [
        {
            "id": f"call_{index}",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": f"arquivo-{index}.txt"}),
            },
        }
        for index in range(count)
    ]


def test_tool_batch_respects_max_concurrency():
    runtime = _runtime()
    probe = _ConcurrencyProbe()
    events: list[dict] = []
    messages: list[dict] = []

    runtime._run_tools(_calls(6), messages, probe, events.append, True, 2)

    assert probe.peak == 2
    # Histórico na mesma ordem dos tool_calls, apesar do paralelismo.
    assert [message["tool_call_id"] for message in messages] == [
        f"call_{index}" for index in range(6)
    ]
    assert [event["id"] for event in events if event["type"] == "tool_result"] == [
        f"call_{index}" for index in range(6)
    ]
    assert len([event for event in events if event["type"] == "tool_call"]) == 6


def test_single_worker_keeps_execution_sequential():
    runtime = _runtime()
    probe = _ConcurrencyProbe(delay=0.02)

    runtime._run_tools(_calls(3), [], probe, lambda event: None, True, 1)

    assert probe.peak == 1
