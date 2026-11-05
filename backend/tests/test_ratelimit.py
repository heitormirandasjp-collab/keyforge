from __future__ import annotations

from agent_core.ratelimit import LimiterRegistry, SlidingWindowLimiter


def test_blocks_after_limit_within_window():
    limiter = SlidingWindowLimiter(3, window=60)
    t = 1000.0
    assert limiter.try_acquire(t) == (True, 0.0)
    assert limiter.try_acquire(t + 1) == (True, 0.0)
    assert limiter.try_acquire(t + 2)[0] is True

    ok, wait = limiter.try_acquire(t + 3)
    assert ok is False
    assert 55 < wait <= 60

    assert limiter.remaining(t + 3) == 0
    assert limiter.used(t + 3) == 3


def test_window_slides_and_reopens():
    limiter = SlidingWindowLimiter(40, window=60)
    t = 500.0
    for i in range(40):
        assert limiter.try_acquire(t + i * 0.01)[0] is True
    assert limiter.remaining(t + 1) == 0

    # 60s depois da primeira requisição, todas as antigas saíram da janela.
    assert limiter.try_acquire(t + 60.5)[0] is True
    assert limiter.used(t + 60.5) == 1
    assert limiter.remaining(t + 60.5) == 39


def test_never_exceeds_limit_under_burst():
    limiter = SlidingWindowLimiter(5, window=60)
    granted = sum(1 for _ in range(50) if limiter.try_acquire(2000.0)[0])
    assert granted == 5


def test_unlimited_when_limit_is_zero():
    limiter = SlidingWindowLimiter(0)
    for _ in range(500):
        assert limiter.try_acquire(0.0)[0] is True
    assert limiter.wait_seconds(0.0) == 0.0


def test_registry_updates_limit_on_reuse():
    registry = LimiterRegistry()
    limiter = registry.get("k1", 40)
    assert limiter.limit == 40
    assert registry.get("k1", 10).limit == 10
    assert registry.peek("k1") is limiter

    registry.peek("k1").try_acquire(1.0)
    assert registry.peek("k1").used(1.0) == 1
    registry.peek("k1").drop_last()
    assert registry.peek("k1").used(1.0) == 0
