"""Guard del cupo diario de la cuenta para modelos :free.

El contador local es **advisory**: cuenta las llamadas que hace esta app, pero la
cuenta puede haberse usado desde otro lado. La verdad la tiene OpenRouter, así que
un 429 de cupo agotado pisa el contador y lo deja lleno hasta medianoche UTC.

Se reserva cupo solo antes de una llamada HTTP real. Un hit de caché de LlmCall no
consume nada.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from django.conf import settings

from apps.llm import store

log = logging.getLogger(__name__)

QUOTA_KEY = "llm:quota:{day}"
LIMIT_KEY = "llm:quota:limit"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _seconds_to_midnight() -> int:
    now = datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


def daily_limit() -> int:
    """Cupo diario, detectado desde la cuenta si se pudo reconciliar."""
    cached = store.get(LIMIT_KEY)
    if cached:
        try:
            return int(cached)
        except ValueError:
            pass
    return settings.OPENROUTER_DAILY_QUOTA


def used_today() -> int:
    raw = store.get(QUOTA_KEY.format(day=_today()))
    try:
        return max(0, int(raw)) if raw else 0
    except ValueError:
        return 0


def remaining() -> int:
    return max(0, daily_limit() - used_today())


def reset_seconds() -> int:
    return _seconds_to_midnight()


def reserve() -> bool:
    """Toma una unidad de cupo. Devuelve False si ya no queda, sin dejar el contador pasado."""
    key = QUOTA_KEY.format(day=_today())
    used = store.incr(key, ttl=_seconds_to_midnight())
    if used > daily_limit():
        store.decr(key)
        return False
    return True


def release() -> None:
    """Devuelve una reserva cuando la llamada no llegó a salir (ej. no había modelo sano)."""
    key = QUOTA_KEY.format(day=_today())
    if used_today() > 0:
        store.decr(key)


def mark_exhausted() -> None:
    """OpenRouter dijo que el cupo diario se acabó: el contador local queda lleno."""
    store.set(QUOTA_KEY.format(day=_today()), str(daily_limit()), ttl=_seconds_to_midnight())
    log.warning("Cupo diario de OpenRouter agotado; se libera en %ds", _seconds_to_midnight())


def reconcile() -> dict | None:
    """Consulta GET /api/v1/key para ajustar el límite al de la cuenta real.

    `is_free_tier: false` significa que la cuenta compró créditos alguna vez, lo que
    sube el cupo de modelos :free de 50 a 1000 por día.
    """
    if not settings.OPENROUTER_API_KEY:
        return None
    try:
        response = httpx.get(
            f"{settings.OPENROUTER_BASE_URL}/key",
            headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
            timeout=15.0,
        )
        response.raise_for_status()
        data = response.json().get("data", {})
    except Exception as exc:  # noqa: BLE001 - reconciliar es best-effort
        log.warning("No se pudo reconciliar el cupo con OpenRouter (%s)", exc)
        return None

    if "is_free_tier" in data:
        limit = 50 if data.get("is_free_tier") else 1000
        store.set(LIMIT_KEY, str(limit), ttl=24 * 3600)
        log.info("Cupo diario reconciliado: %d/día (is_free_tier=%s)", limit,
                 data.get("is_free_tier"))
    return data


def status() -> dict:
    """Datos para el widget de cupo del formulario."""
    limit = daily_limit()
    used = used_today()
    seconds = reset_seconds()
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "exhausted": used >= limit,
        "reset_seconds": seconds,
        "reset_hours": round(seconds / 3600, 1),
    }
