"""Contabilidad de gasto para los proveedores pagos.

Con registro abierto, un proveedor que cobra por token es una superficie de abuso: sin
tope, cualquiera que encuentre el dominio puede generarte factura. Este módulo lleva el
gasto del día y corta cuando se pasa del presupuesto.

Se cuenta en **micro-dólares enteros** porque el contador del store es un INCR de Redis
y los flotantes ahí no son atómicos.

El chequeo va antes de la llamada y el registro después, así que el gasto puede pasarse
del tope por una sola llamada. Con costos de milésimas de dólar por CV, no vale la pena
resolverlo con reservas.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from django.conf import settings

from apps.llm import store

log = logging.getLogger(__name__)

SPEND_KEY = "llm:spend:{day}"
MICROS = 1_000_000

# USD por 1.000 tokens en us-east-1, on-demand. Salen de la Pricing API de AWS.
PRICES = {
    "deepseek.v3.2": (0.00062, 0.00222),
    "amazon.nova-lite-v1:0": (0.00006, 0.00024),
    "amazon.nova-micro-v1:0": (0.000035, 0.00014),
    "amazon.nova-pro-v1:0": (0.0008, 0.0032),
}
# Para un modelo que no está en la tabla se asume caro: el guard tiene que errar
# cortando de más, no de menos.
DEFAULT_PRICE = (0.003, 0.015)


def price_for(model_id: str) -> tuple[float, float]:
    if model_id in PRICES:
        return PRICES[model_id]
    # Los perfiles de inferencia llevan prefijo de región: "us.deepseek.v3.2".
    bare = model_id.split(".", 1)[1] if model_id[:3] in {"us.", "eu.", "ap."} else model_id
    return PRICES.get(bare, DEFAULT_PRICE)


def cost_usd(model_id: str, prompt_tokens: int, completion_tokens: int) -> float:
    price_in, price_out = price_for(model_id)
    return (prompt_tokens / 1000) * price_in + (completion_tokens / 1000) * price_out


def _seconds_to_midnight() -> int:
    now = datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def budget_usd() -> float:
    return float(settings.BEDROCK_DAILY_USD_BUDGET)


def spent_today_usd() -> float:
    raw = store.get(SPEND_KEY.format(day=_today()))
    try:
        return max(0.0, int(raw) / MICROS) if raw else 0.0
    except ValueError:
        return 0.0


def record(model_id: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Suma el costo de una llamada al total del día y lo devuelve."""
    amount = cost_usd(model_id, prompt_tokens, completion_tokens)
    micros = max(1, round(amount * MICROS))
    store.incr_by(SPEND_KEY.format(day=_today()), micros, ttl=_seconds_to_midnight())
    return amount


def exhausted() -> bool:
    return spent_today_usd() >= budget_usd()


def status() -> dict:
    spent = spent_today_usd()
    budget = budget_usd()
    seconds = _seconds_to_midnight()
    return {
        "spent": round(spent, 4),
        "budget": budget,
        "remaining": round(max(0.0, budget - spent), 4),
        "exhausted": spent >= budget,
        "reset_seconds": seconds,
        "reset_hours": round(seconds / 3600, 1),
    }
