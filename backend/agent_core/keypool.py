"""Pool de chaves com rotação, cooldown e fallback entre provedores.

Fluxo de uma requisição:

1. percorre as chaves do provedor preferido na ordem cadastrada;
2. usa a primeira chave com status *ativa* e cota disponível na janela de RPM;
3. em 429 (ou cota local estourada) marca a chave em cooldown e passa para a
   próxima do mesmo provedor;
4. se todas as chaves do provedor estiverem em cooldown/esgotadas, passa para o
   próximo provedor da ordem configurada;
5. se nada estiver disponível, informa quanto falta para a próxima liberar.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from .config import ConfigStore
from .ratelimit import LimiterRegistry
from .ratestate import RateStateError, SharedRateState

ACTIVE = "active"
COOLDOWN = "cooldown"
EXHAUSTED = "exhausted"


@dataclass
class Lease:
    """Uma chave reservada para uma chamada."""

    provider: str
    key_id: str
    key: str
    label: str
    rpm_limit: int

    @property
    def masked(self) -> str:
        from .config import mask_key

        return mask_key(self.key)


class KeyPool:
    def __init__(self, store: ConfigStore, shared: SharedRateState | None = None) -> None:
        self.store = store
        self.limiters = LimiterRegistry()
        # Fica ao lado do config.json (%APPDATA%\KeyForge\rate_state.json), então
        # todas as instâncias que usam o mesmo config compartilham a cota.
        self.shared = shared or SharedRateState(store.path.parent / "rate_state.json")

    # ------------------------------------------------------------------ #
    # Configuração
    # ------------------------------------------------------------------ #
    def _provider_conf(self, provider: str) -> dict[str, Any]:
        return self.store.data["providers"].get(provider, {})

    def _keys(self, provider: str) -> list[dict[str, Any]]:
        return list(self._provider_conf(provider).get("keys", []))

    def provider_order(self, preferred: Iterable[str] | None = None) -> list[str]:
        providers = self.store.data["providers"]
        order: list[str] = []
        for name in list(preferred or []) + list(self.store.data.get("providerOrder", [])):
            if name in providers and name not in order:
                order.append(name)
        for name in providers:
            if name not in order:
                order.append(name)
        return order

    def _limiter(self, provider: str, key_id: str):
        rpm = int(self._provider_conf(provider).get("rpmLimit") or 0)
        return self.limiters.get(key_id, rpm)

    @staticmethod
    def _shared_key(provider: str, key_id: str) -> str:
        return f"{provider}::{key_id}"

    def _shared_reserve(self, shared_key: str, limit: int) -> tuple[bool, float]:
        if limit <= 0:
            return True, 0.0
        try:
            return self.shared.try_reserve(shared_key, limit)
        except (RateStateError, OSError):
            # Estado compartilhado indisponível (disco/lock): mantém o app
            # utilizável com o limiter em memória em vez de travar tudo.
            return True, 0.0

    def _release_shared(self, shared_key: str, limit: int) -> None:
        if limit <= 0:
            return
        try:
            self.shared.release(shared_key)
        except (RateStateError, OSError):
            pass

    def _shared_snapshot(self) -> dict[str, list[float]] | None:
        try:
            return self.shared.snapshot()
        except (RateStateError, OSError):
            return None

    def _shared_record(self, lease: Lease, ok: bool, tokens: int = 0) -> None:
        try:
            self.shared.record_result(
                self._shared_key(lease.provider, lease.key_id), ok, tokens
            )
        except (RateStateError, OSError):
            pass

    # ------------------------------------------------------------------ #
    # Aquisição
    # ------------------------------------------------------------------ #
    def _key_state(self, entry: dict[str, Any]) -> dict[str, Any]:
        return entry.setdefault("state", {"cooldownUntil": 0.0, "exhausted": False, "reason": ""})

    def acquire(self, providers: Iterable[str] | None = None) -> Lease | None:
        """Reserva a primeira chave ativa na ordem de provedores/chaves.

        A cota é checada primeiro no estado compartilhado (``rate_state.json``),
        comum a todas as instâncias, e depois no limiter em memória. Duas janelas
        abertas na mesma conta não somam 80 RPM.
        """
        now = time.time()
        changed = False
        for provider in self.provider_order(providers):
            for entry in self._keys(provider):
                state = self._key_state(entry)
                if state.get("exhausted"):
                    continue
                if float(state.get("cooldownUntil") or 0) > now:
                    continue
                limiter = self._limiter(provider, entry["id"])
                shared_key = self._shared_key(provider, entry["id"])
                ok, wait = self._shared_reserve(shared_key, limiter.limit)
                if not ok:
                    state["cooldownUntil"] = now + max(1.0, wait)
                    state["reason"] = f"limite global de {limiter.limit} RPM"
                    changed = True
                    continue
                ok, wait = limiter.try_acquire()
                if not ok:
                    # Limiter local à frente do arquivo: devolve a reserva global.
                    self._release_shared(shared_key, limiter.limit)
                    state["cooldownUntil"] = now + max(1.0, wait)
                    state["reason"] = f"limite local de {limiter.limit} RPM"
                    changed = True
                    continue
                if state.get("cooldownUntil"):
                    state["cooldownUntil"] = 0.0
                    state["reason"] = ""
                    changed = True
                if changed:
                    self.store.save()
                return Lease(
                    provider=provider,
                    key_id=entry["id"],
                    key=entry.get("key", ""),
                    label=entry.get("label") or entry["id"],
                    rpm_limit=limiter.limit,
                )
        if changed:
            self.store.save()
        return None

    def next_available_at(self, providers: Iterable[str] | None = None) -> float | None:
        """Timestamp (epoch) em que alguma chave volta a ficar disponível."""
        now = time.time()
        snapshot = self._shared_snapshot()
        best: float | None = None
        for provider in self.provider_order(providers):
            for entry in self._keys(provider):
                state = self._key_state(entry)
                if state.get("exhausted"):
                    continue
                limiter = self._limiter(provider, entry["id"])
                candidate = max(float(state.get("cooldownUntil") or 0), now + limiter.wait_seconds())
                if limiter.limit > 0 and snapshot is not None:
                    events = snapshot.get(self._shared_key(provider, entry["id"]), [])
                    candidate = max(candidate, now + self.shared.wait_seconds(events, limiter.limit, now))
                if candidate <= now:
                    return now
                best = candidate if best is None else min(best, candidate)
        return best

    def wait_seconds(self, providers: Iterable[str] | None = None) -> float | None:
        nxt = self.next_available_at(providers)
        if nxt is None:
            return None
        return max(0.0, nxt - time.time())

    # ------------------------------------------------------------------ #
    # Feedback das chamadas
    # ------------------------------------------------------------------ #
    def report_success(self, lease: Lease, tokens: int = 0) -> None:
        entry = self.store.get_key(lease.provider, lease.key_id)
        if not entry:
            return
        stats = entry.setdefault("stats", {})
        stats["requests"] = int(stats.get("requests") or 0) + 1
        stats["tokens"] = int(stats.get("tokens") or 0) + max(0, int(tokens or 0))
        stats["lastUsedAt"] = time.time()
        state = self._key_state(entry)
        state["cooldownUntil"] = 0.0
        state["reason"] = ""
        self._shared_record(lease, True, tokens)
        self.store.save()

    def report_rate_limited(self, lease: Lease, retry_after: float | None = None) -> None:
        entry = self.store.get_key(lease.provider, lease.key_id)
        if not entry:
            return
        now = time.time()
        cooldown = float(retry_after) if retry_after and retry_after > 0 else 60.0
        cooldown = max(5.0, min(cooldown, 3600.0))
        state = self._key_state(entry)
        state["cooldownUntil"] = now + cooldown
        state["reason"] = f"429 — cooldown de {int(cooldown)}s"
        stats = entry.setdefault("stats", {})
        stats["failures"] = int(stats.get("failures") or 0) + 1
        self._shared_record(lease, False)
        self.store.save()

    def report_transient_error(self, lease: Lease, reason: str = "erro do provedor") -> None:
        entry = self.store.get_key(lease.provider, lease.key_id)
        if not entry:
            return
        state = self._key_state(entry)
        state["cooldownUntil"] = time.time() + 20.0
        state["reason"] = reason
        stats = entry.setdefault("stats", {})
        stats["failures"] = int(stats.get("failures") or 0) + 1
        self._shared_record(lease, False)
        self.store.save()

    def report_auth_error(self, lease: Lease, reason: str = "chave inválida") -> None:
        entry = self.store.get_key(lease.provider, lease.key_id)
        if not entry:
            return
        state = self._key_state(entry)
        state["exhausted"] = True
        state["reason"] = reason
        stats = entry.setdefault("stats", {})
        stats["failures"] = int(stats.get("failures") or 0) + 1
        self._shared_record(lease, False)
        self.store.save()

    def release_unused(self, lease: Lease) -> None:
        """Devolve a vaga quando a chave foi reservada mas a chamada não ocorreu."""
        limiter = self.limiters.peek(lease.key_id)
        if limiter is not None:
            limiter.drop_last()
        self._release_shared(
            self._shared_key(lease.provider, lease.key_id), lease.rpm_limit
        )

    def drop_key(self, provider: str, key_id: str) -> None:
        """Remove a cota em memória e a compartilhada de uma chave apagada."""
        self.limiters.drop(key_id)
        try:
            self.shared.drop(self._shared_key(provider, key_id))
        except (RateStateError, OSError):
            pass

    # ------------------------------------------------------------------ #
    # Status para a UI
    # ------------------------------------------------------------------ #
    def status(self) -> list[dict[str, Any]]:
        from .config import mask_key

        now = time.time()
        snapshot = self._shared_snapshot()
        out: list[dict[str, Any]] = []
        for provider, conf in self.store.data["providers"].items():
            rpm_limit = int(conf.get("rpmLimit") or 0)
            for entry in conf.get("keys", []):
                state = self._key_state(entry)
                limiter = self._limiter(provider, entry["id"])
                events = (
                    snapshot.get(self._shared_key(provider, entry["id"]))
                    if snapshot is not None
                    else None
                )
                used = len(events) if (rpm_limit and events is not None) else limiter.used()
                cooldown_until = float(state.get("cooldownUntil") or 0)
                if state.get("exhausted"):
                    status = EXHAUSTED
                elif cooldown_until > now:
                    status = COOLDOWN
                else:
                    status = ACTIVE
                out.append(
                    {
                        "provider": provider,
                        "providerLabel": conf.get("label", provider),
                        "id": entry["id"],
                        "label": entry.get("label") or entry["id"],
                        "maskedKey": mask_key(entry.get("key", "")),
                        "status": status,
                        "reason": state.get("reason") or "",
                        "cooldownRemaining": max(0, round(cooldown_until - now, 1)),
                        "rpm": {
                            "limit": rpm_limit,
                            "used": used if rpm_limit else 0,
                            "remaining": max(0, rpm_limit - used) if rpm_limit else None,
                        },
                        "stats": {
                            "requests": int(entry.get("stats", {}).get("requests") or 0),
                            "tokens": int(entry.get("stats", {}).get("tokens") or 0),
                            "failures": int(entry.get("stats", {}).get("failures") or 0),
                            "lastUsedAt": float(entry.get("stats", {}).get("lastUsedAt") or 0),
                        },
                    }
                )
        return out
