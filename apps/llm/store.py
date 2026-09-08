"""Key-value con TTL sobre Redis, con fallback en memoria.

Redis es obligatorio para Celery, pero la rotación y la cuota también se usan desde
tests y desde `manage.py probe_model`, donde puede no haber Redis levantado. El
fallback en proceso mantiene todo eso funcionando sin dependencias extra.
"""

from __future__ import annotations

import logging
import threading
import time

from django.conf import settings

log = logging.getLogger(__name__)

_lock = threading.Lock()
_memory: dict[str, tuple[str, float | None]] = {}
_client = None
_client_ready = False
_force_memory = False


def use_memory(enabled: bool = True) -> None:
    """Fuerza el store en memoria. Lo usan los tests: sin esto comparten los
    cooldowns y el contador de cupo con el Redis de desarrollo."""
    global _force_memory
    _force_memory = enabled
    reset_client()


def _redis():
    global _client, _client_ready
    if _force_memory:
        return None
    if not _client_ready:
        with _lock:
            if not _client_ready:
                try:
                    import redis

                    client = redis.Redis.from_url(
                        settings.REDIS_URL, decode_responses=True, socket_timeout=2
                    )
                    client.ping()
                    _client = client
                except Exception as exc:  # noqa: BLE001 - cualquier fallo cae a memoria
                    log.warning("Redis no disponible (%s); usando store en memoria", exc)
                    _client = None
                _client_ready = True
    return _client


def reset_client() -> None:
    """Fuerza la reconexión. Solo para tests."""
    global _client, _client_ready
    with _lock:
        _client, _client_ready = None, False
        _memory.clear()


def _expired(entry: tuple[str, float | None]) -> bool:
    return entry[1] is not None and entry[1] <= time.time()


def get(key: str) -> str | None:
    client = _redis()
    if client is not None:
        try:
            return client.get(key)
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis GET falló (%s)", exc)
    entry = _memory.get(key)
    if entry is None or _expired(entry):
        _memory.pop(key, None)
        return None
    return entry[0]


def set(key: str, value: str, ttl: int | None = None) -> None:  # noqa: A001
    client = _redis()
    if client is not None:
        try:
            client.set(key, value, ex=ttl)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis SET falló (%s)", exc)
    _memory[key] = (value, time.time() + ttl if ttl else None)


def delete(key: str) -> None:
    client = _redis()
    if client is not None:
        try:
            client.delete(key)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis DEL falló (%s)", exc)
    _memory.pop(key, None)


def incr(key: str, ttl: int | None = None) -> int:
    """Incrementa y devuelve el valor nuevo, fijando el TTL solo en la primera escritura."""
    client = _redis()
    if client is not None:
        try:
            value = client.incr(key)
            if value == 1 and ttl:
                client.expire(key, ttl)
            return int(value)
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis INCR falló (%s)", exc)
    with _lock:
        entry = _memory.get(key)
        current = 0 if entry is None or _expired(entry) else int(entry[0])
        expiry = entry[1] if entry and not _expired(entry) else (time.time() + ttl if ttl else None)
        _memory[key] = (str(current + 1), expiry)
        return current + 1


def decr(key: str) -> int:
    client = _redis()
    if client is not None:
        try:
            return int(client.decr(key))
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis DECR falló (%s)", exc)
    with _lock:
        entry = _memory.get(key)
        current = 0 if entry is None or _expired(entry) else int(entry[0])
        _memory[key] = (str(current - 1), entry[1] if entry else None)
        return current - 1


def ttl(key: str) -> int:
    """Segundos restantes. -1 si no expira, -2 si no existe."""
    client = _redis()
    if client is not None:
        try:
            return int(client.ttl(key))
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis TTL falló (%s)", exc)
    entry = _memory.get(key)
    if entry is None or _expired(entry):
        return -2
    return -1 if entry[1] is None else max(0, int(entry[1] - time.time()))
