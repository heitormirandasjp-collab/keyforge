"""Estado de rate limit compartilhado entre instâncias do KeyForge.

Cada processo mantém um limiter em memória, mas a cota real precisa ser comum a
todas as instâncias: duas janelas abertas na mesma conta NVIDIA não podem somar
80 RPM. Este módulo guarda, por ``provedor::chave``, os timestamps (epoch) das
requisições da janela deslizante de 60s em ``rate_state.json``.

O acesso é serializado por um lock de arquivo (``msvcrt.locking`` no Windows,
``fcntl.flock`` no POSIX) e por um lock de thread dentro do mesmo processo. As
gravações são atômicas (arquivo temporário + ``os.replace``), então leitores
nunca veem JSON pela metade.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

WINDOW_SECONDS = 60.0
DEFAULT_LOCK_TIMEOUT = 5.0
MAX_EVENTS_PER_KEY = 500

_REGISTRY_LOCK = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


class RateStateError(Exception):
    """O estado compartilhado não pôde ser lido/gravado (lock ou I/O)."""


def _process_lock(path: Path) -> threading.Lock:
    """Lock de thread por arquivo (o lock do SO sozinho não basta no Windows)."""
    key = str(path)
    with _REGISTRY_LOCK:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


class FileLock:
    """Lock exclusivo entre processos, sem dependências externas.

    O lock é feito em um arquivo separado (``rate_state.lock``) porque o JSON é
    substituído por ``os.replace``; travar o inode antigo não protegeria nada.
    """

    def __init__(self, path: Path, timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        self.path = Path(path)
        self.timeout = max(0.1, float(timeout))
        self._handle = None
        self._thread_lock: threading.Lock | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_lock = _process_lock(self.path)
        if not self._thread_lock.acquire(timeout=self.timeout):
            raise RateStateError("timeout aguardando o lock local do rate_state")
        try:
            self._handle = open(self.path, "a+b")
            if self._handle.seek(0, os.SEEK_END) == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self._lock_os_file()
                    return self
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RateStateError(
                            "timeout aguardando o lock do rate_state"
                        ) from None
                    time.sleep(0.02)
        except BaseException:
            self._close()
            raise

    def _lock_os_file(self) -> None:
        assert self._handle is not None
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_os_file(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def _close(self) -> None:
        self._unlock_os_file()
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        if self._thread_lock is not None:
            try:
                self._thread_lock.release()
            except RuntimeError:
                pass
            self._thread_lock = None

    def __exit__(self, *_exc: object) -> None:
        self._close()


class SharedRateState:
    """Janela deslizante de 60s persistida em disco e compartilhada."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        window: float = WINDOW_SECONDS,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")
        self.window = max(1.0, float(window))
        self.lock_timeout = max(0.1, float(lock_timeout))

    # -- leitura/escrita ---------------------------------------------------- #
    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "keys": {}}
        if not isinstance(raw, dict) or not isinstance(raw.get("keys"), dict):
            return {"version": 1, "keys": {}}

        keys: dict[str, dict[str, Any]] = {}
        for name, item in raw["keys"].items():
            if not isinstance(name, str) or not isinstance(item, dict):
                continue
            try:
                events = [
                    float(value)
                    for value in (item.get("events") or [])
                    if isinstance(value, (int, float))
                ]
                keys[name[:200]] = {
                    "events": events[-MAX_EVENTS_PER_KEY:],
                    "requests": max(0, int(item.get("requests") or 0)),
                    "failures": max(0, int(item.get("failures") or 0)),
                    "tokens": max(0, int(item.get("tokens") or 0)),
                    "lastUsedAt": float(item.get("lastUsedAt") or 0.0),
                }
            except (TypeError, ValueError):
                continue
        return {"version": 1, "keys": keys}

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": 1, "window": self.window, "keys": state["keys"]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        tmp = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(payload, encoding="utf-8")
        if os.name != "nt":
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, self.path)

    @contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        with FileLock(self.lock_path, self.lock_timeout):
            yield self._read()

    @staticmethod
    def _entry(state: dict[str, Any], key: str) -> dict[str, Any]:
        entry = state["keys"].get(key)
        if not isinstance(entry, dict):
            entry = {
                "events": [],
                "requests": 0,
                "failures": 0,
                "tokens": 0,
                "lastUsedAt": 0.0,
            }
            state["keys"][key] = entry
        return entry

    def _prune(self, events: list[float], now: float) -> list[float]:
        cutoff = now - self.window
        return [value for value in events if value > cutoff]

    # -- API ---------------------------------------------------------------- #
    def snapshot(self, now: float | None = None) -> dict[str, list[float]]:
        """Timestamps ainda dentro da janela, por chave (sem contar a própria)."""
        now = time.time() if now is None else float(now)
        with self._locked_state() as state:
            raw = state["keys"]
        out: dict[str, list[float]] = {}
        for key, entry in raw.items():
            events = self._prune(entry.get("events") or [], now)
            if events:
                out[key] = events
        return out

    def try_reserve(self, key: str, limit: int, now: float | None = None) -> tuple[bool, float]:
        """Reserva uma vaga na janela global. Retorna ``(ok, espera)``."""
        now = time.time() if now is None else float(now)
        if limit <= 0:
            return True, 0.0
        with self._locked_state() as state:
            entry = self._entry(state, key)
            events = self._prune(entry.get("events") or [], now)
            entry["events"] = events
            if len(events) >= limit:
                oldest = events[len(events) - limit]
                self._write(state)
                return False, max(0.0, (oldest + self.window) - now)
            events.append(now)
            self._write(state)
            return True, 0.0

    def release(self, key: str) -> None:
        """Devolve a última reserva (chamada cancelada antes do envio)."""
        with self._locked_state() as state:
            entry = state["keys"].get(key)
            if not isinstance(entry, dict):
                return
            events = list(entry.get("events") or [])
            if events:
                events.pop()
                entry["events"] = events
                self._write(state)

    def record_result(
        self,
        key: str,
        ok: bool,
        tokens: int = 0,
        now: float | None = None,
    ) -> None:
        """Atualiza contadores depois da requisição (sucesso/erro/tokens)."""
        now = time.time() if now is None else float(now)
        with self._locked_state() as state:
            entry = self._entry(state, key)
            entry["events"] = self._prune(entry.get("events") or [], now)
            entry["requests"] = int(entry.get("requests") or 0) + 1
            if not ok:
                entry["failures"] = int(entry.get("failures") or 0) + 1
            entry["tokens"] = int(entry.get("tokens") or 0) + max(0, int(tokens or 0))
            entry["lastUsedAt"] = now
            self._write(state)

    def wait_seconds(
        self,
        events: list[float],
        limit: int,
        now: float | None = None,
    ) -> float:
        """Espera até abrir vaga, considerando a janela compartilhada."""
        if limit <= 0:
            return 0.0
        now = time.time() if now is None else float(now)
        values = sorted(self._prune(list(events), now))
        if len(values) < limit:
            return 0.0
        oldest = values[len(values) - limit]
        return max(0.0, (oldest + self.window) - now)

    def drop(self, key: str) -> None:
        """Remove a chave do estado compartilhado."""
        with self._locked_state() as state:
            if state["keys"].pop(key, None) is not None:
                self._write(state)
