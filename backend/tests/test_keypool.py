from __future__ import annotations

import time

from agent_core.keypool import KeyPool


def _seed(store, nvidia: int = 2, freebuff: int = 1, rpm: int = 40) -> None:
    store.update({"providers": {"nvidia": {"rpmLimit": rpm}, "freebuff": {"rpmLimit": 0}}})
    for i in range(nvidia):
        store.add_key("nvidia", f"nvapi-nvidia-{i:04d}", f"nv{i}")
    for i in range(freebuff):
        store.add_key("freebuff", f"fb-token-{i:04d}", f"fb{i}")


def test_rotates_to_next_key_after_429(store):
    _seed(store)
    pool = KeyPool(store)

    first = pool.acquire(["nvidia"])
    assert first is not None and first.label == "nv0"
    pool.report_rate_limited(first, retry_after=60)

    second = pool.acquire(["nvidia"])
    assert second is not None and second.label == "nv1"
    pool.report_rate_limited(second, retry_after=60)

    third = pool.acquire(["nvidia", "freebuff"])
    assert third is not None and third.provider == "freebuff"

    pool.report_rate_limited(third, retry_after=60)
    assert pool.acquire(["nvidia", "freebuff"]) is None

    wait = pool.wait_seconds(["nvidia", "freebuff"])
    assert wait is not None and 0 < wait <= 61


def test_local_rpm_limit_marks_cooldown_without_request(store):
    _seed(store, nvidia=1, freebuff=1, rpm=2)
    pool = KeyPool(store)

    for _ in range(2):
        lease = pool.acquire(["nvidia"])
        assert lease is not None and lease.provider == "nvidia"

    # Terceira tentativa: NVIDIA estourou 2 RPM -> cai para o Freebuff.
    fallback = pool.acquire(["nvidia"])
    assert fallback is not None and fallback.provider == "freebuff"

    status = {k["label"]: k for k in pool.status()}
    assert status["nv0"]["status"] == "cooldown"
    assert "limite" in status["nv0"]["reason"]
    assert status["nv0"]["rpm"]["used"] == 2
    assert status["nv0"]["rpm"]["limit"] == 2


def test_provider_order_preference_is_respected(store):
    _seed(store)
    store.update({"providerOrder": ["freebuff", "nvidia"]})
    pool = KeyPool(store)

    lease = pool.acquire(["freebuff", "nvidia"])
    assert lease is not None and lease.provider == "freebuff"


def test_auth_error_marks_key_exhausted(store):
    _seed(store, nvidia=2, freebuff=0)
    pool = KeyPool(store)

    first = pool.acquire(["nvidia"])
    pool.report_auth_error(first, "invalid api key")

    status = {k["label"]: k for k in pool.status()}
    assert status["nv0"]["status"] == "exhausted"
    assert "invalid api key" in status["nv0"]["reason"]

    # A chave esgotada nunca é escolhida de novo, mesmo após reset de cooldown.
    lease = pool.acquire(["nvidia"])
    assert lease is not None and lease.label == "nv1"

    store.reset_key("nvidia", first.key_id)
    assert {k["label"]: k for k in pool.status()}["nv0"]["status"] == "active"


def test_report_success_updates_stats(store):
    _seed(store)
    pool = KeyPool(store)
    lease = pool.acquire(["nvidia"])
    pool.report_success(lease, tokens=123)

    status = {k["label"]: k for k in pool.status()}
    assert status["nv0"]["stats"]["requests"] == 1
    assert status["nv0"]["stats"]["tokens"] == 123
    assert status["nv0"]["status"] == "active"


def test_no_keys_means_no_lease(store):
    pool = KeyPool(store)
    assert pool.acquire() is None
    assert pool.next_available_at() is None
    assert pool.wait_seconds() is None


def test_masked_key_never_exposes_secret(store):
    _seed(store)
    pool = KeyPool(store)
    status = pool.status()[0]
    assert "nvapi-nvidia-0" not in status["maskedKey"]
    assert status["maskedKey"].startswith("nvapi-")


def test_lease_carries_key_metadata(store):
    _seed(store, rpm=40)
    pool = KeyPool(store)
    lease = pool.acquire(["nvidia"])
    assert lease is not None
    assert lease.rpm_limit == 40
    assert lease.provider == "nvidia"
    assert lease.key.startswith("nvapi-")
    assert lease.masked != lease.key
    assert lease.masked.startswith("nvapi-") and lease.masked.endswith("0000")
