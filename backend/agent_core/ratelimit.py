"""Rate limiter por chave (sliding window de 60s).

O free tier da NVIDIA NIM aceita ~40 requisições por minuto por conta. Antes de
gastar uma chamada, o KeyPool pergunta ao limiter se ainda há cota na janela
atual; se não houver, a chave entra em cooldown **sem** enviar a requisição
(economiza cota e evita 429).
"""
from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    """Janela deslizante de ``window`` segundos com no máximo ``limit`` usos.

    ``limit <= 0`` significa ilimitado (provedores sem RPM conhecido).
    """

    def __init__(self, limit: int = 0, window: float = 60.0) -> None:
        self._lock = threading.Lock()
        self._events: deque[float] = deque()
        self.limit = max(0, int(limit))
        self.window = float(window)

    # -- configuração ------------------------------------------------------- #
    def set_limit(self, limit: int) -> None:
        with self._lock:
            self.limit = max(0, int(limit))

    # -- consulta ----------------------------------------------------------- #
    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    def used(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(now)
            return len(self._events)

    def remaining(self, now: float | None = None) -> int:
        if self.limit <= 0:
            return 1_000_000
        return max(0, self.limit - self.used(now))

    def wait_seconds(self, now: float | None = None) -> float:
        """Segundos até liberar uma vaga (0 quando já há vaga)."""
        now = time.monotonic() if now is None else now
        if self.limit <= 0:
            return 0.0
        with self._lock:
            self._prune(now)
            if len(self._events) < self.limit:
                return 0.0
            # A vaga mais antiga sai da janela quando completa `window` segundos.
            oldest = self._events[len(self._events) - self.limit]
            return max(0.0, (oldest + self.window) - now)

    def try_acquire(self, now: float | None = None) -> tuple[bool, float]:
        """Reserva uma vaga. Retorna ``(ok, espera_em_segundos)``."""
        now = time.monotonic() if now is None else now
        if self.limit <= 0:
            with self._lock:
                self._events.append(now)
            return True, 0.0
        with self._lock:
            self._prune(now)
            if len(self._events) >= self.limit:
                oldest = self._events[len(self._events) - self.limit]
                return False, max(0.0, (oldest + self.window) - now)
            self._events.append(now)
            return True, 0.0

    def reserve(self, count: int = 1) -> None:
        """Contabiliza usos externos (ex.: 429 vindo do provedor)."""
        now = time.monotonic()
        with self._lock:
            for _ in range(max(1, count)):
                self._events.append(now)

    def drop_last(self) -> None:
        """Devolve a última vaga reservada (chamada cancelada antes do envio)."""
        with self._lock:
            if self._events:
                self._events.pop()

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


class LimiterRegistry:
    """Um :class:`SlidingWindowLimiter` por chave, criado sob demanda."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._limiters: dict[str, SlidingWindowLimiter] = {}

    def get(self, key_id: str, limit: int) -> SlidingWindowLimiter:
        with self._lock:
            limiter = self._limiters.get(key_id)
            if limiter is None:
                limiter = SlidingWindowLimiter(limit)
                self._limiters[key_id] = limiter
            else:
                limiter.set_limit(limit)
            return limiter

    def peek(self, key_id: str) -> SlidingWindowLimiter | None:
        with self._lock:
            return self._limiters.get(key_id)

    def drop(self, key_id: str) -> None:
        with self._lock:
            self._limiters.pop(key_id, None)
