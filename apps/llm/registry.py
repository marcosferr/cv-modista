"""Anillo de rotación de modelos :free de OpenRouter.

Los límites del tier free son **por cuenta**, no por modelo: OpenRouter los gobierna
globalmente, así que rotar no multiplica el cupo diario. Rotar sí resuelve lo otro,
que es lo que rompe en la práctica:

- un proveedor concreto tira 429 aunque a la cuenta le quede cupo
- un modelo desaparece de la lista :free (rota seguido) o deja de tener endpoints
- un modelo devuelve JSON irrecuperable una y otra vez

Cada fallo manda al modelo a un cooldown proporcional a su gravedad y el cursor
round-robin sigue al siguiente sano.
"""

from __future__ import annotations

import json
import logging
import re
import time

import httpx
from django.conf import settings

from apps.llm import store

log = logging.getLogger(__name__)

POOL_KEY = "llm:pool"
POOL_TTL = 6 * 3600
CURSOR_KEY = "llm:cursor"
HEALTH_KEY = "llm:health:{model_id}"

COOLDOWN_SECONDS = {
    "rate_limit": 90,  # throttling transitorio del proveedor
    "server_error": 300,
    "timeout": 300,
    "bad_json": 1800,  # el modelo no sabe emitir JSON: apartarlo un buen rato
    "truncated": 900,
    "no_endpoint": 86400,  # 404 sin endpoints: no vuelve hoy
}

# Red de seguridad para cuando /api/v1/models no responde. Se reemplaza en cuanto
# el sync funciona: la lista real de modelos :free cambia todo el tiempo.
SEED_POOL = [
    {"id": "dots-studio/dots-3-note-preview:free", "context": 512000, "max_output": 460800,
     "structured": True, "tiny": False},
    {"id": "nex-agi/nex-n2.5-pro:free", "context": 262144, "max_output": 235929,
     "structured": True, "tiny": False},
    {"id": "nvidia/nemotron-3-super-120b-a12b:free", "context": 262144, "max_output": 235929,
     "structured": True, "tiny": False},
    {"id": "nex-agi/nex-n2.5-mini:free", "context": 262144, "max_output": 235929,
     "structured": True, "tiny": False},
    {"id": "google/gemma-4-31b-it:free", "context": 262144, "max_output": 32768,
     "structured": False, "tiny": False},
]


class NoModelAvailable(RuntimeError):
    """Todo el pool está en cooldown."""


# Muchos ids codifican el tamaño del modelo: "...-2.6b", "...-120b-a12b".
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)b(?:[-:]|$)")
# Por debajo de esto el modelo copia el ejemplo few-shot en vez de generar, omite
# entradas del CV y recorta bullets sin criterio. Se deja en el pool como último
# recurso, nunca como primera opción.
TINY_THRESHOLD_B = 7.0


def size_hint(model_id: str) -> float | None:
    """Parámetros en miles de millones si el id lo dice, None si no se puede saber.

    Sin dato se asume capaz: la mayoría de los modelos buenos no ponen el tamaño en el id.
    """
    match = _SIZE_RE.search(model_id.lower())
    return float(match.group(1)) if match else None


def is_tiny(model_id: str) -> bool:
    size = size_hint(model_id)
    return size is not None and size < TINY_THRESHOLD_B


def _normalize(raw: dict) -> dict | None:
    model_id = raw.get("id") or ""
    if not model_id.endswith(":free"):
        return None
    supported = raw.get("supported_parameters") or []
    top = raw.get("top_provider") or {}
    context = raw.get("context_length") or top.get("context_length") or 0
    return {
        "id": model_id,
        "context": int(context or 0),
        "max_output": int(top.get("max_completion_tokens") or 0) or None,
        "structured": "structured_outputs" in supported,
        "tiny": is_tiny(model_id),
    }


def fetch_pool() -> list[dict]:
    """GET /api/v1/models. Endpoint público, no consume cupo ni necesita API key."""
    response = httpx.get(f"{settings.OPENROUTER_BASE_URL}/models", timeout=20.0)
    response.raise_for_status()
    models = [m for m in (_normalize(r) for r in response.json().get("data", [])) if m]
    # Structured output primero, los diminutos al final, después por contexto.
    models.sort(key=lambda m: (not m["structured"], m["tiny"], -m["context"]))
    return models


def sync_pool(force: bool = False) -> list[dict]:
    """Devuelve el pool, refrescándolo desde OpenRouter si el caché venció."""
    if not force:
        cached = store.get(POOL_KEY)
        if cached:
            try:
                return json.loads(cached)
            except ValueError:
                pass
    try:
        pool = fetch_pool()
    except Exception as exc:  # noqa: BLE001 - sin red seguimos con el pool semilla
        log.warning("No se pudo sincronizar el pool de modelos (%s); usando semilla", exc)
        return list(SEED_POOL)

    if not pool:
        log.warning("OpenRouter no devolvió modelos :free; usando semilla")
        return list(SEED_POOL)

    store.set(POOL_KEY, json.dumps(pool), ttl=POOL_TTL)
    log.info("Pool sincronizado: %d modelos :free (%d con structured output)",
             len(pool), sum(1 for m in pool if m["structured"]))
    return pool


def cooldown_remaining(model_id: str) -> int:
    remaining = store.ttl(HEALTH_KEY.format(model_id=model_id))
    return max(0, remaining) if remaining >= 0 else 0


def is_healthy(model_id: str) -> bool:
    return store.get(HEALTH_KEY.format(model_id=model_id)) is None


def report_failure(model_id: str, kind: str) -> None:
    seconds = COOLDOWN_SECONDS.get(kind, 300)
    store.set(HEALTH_KEY.format(model_id=model_id), kind, ttl=seconds)
    log.warning("Modelo %s en cooldown %ds por %s", model_id, seconds, kind)


def report_success(model_id: str) -> None:
    store.delete(HEALTH_KEY.format(model_id=model_id))


def candidates(need_structured: bool = False, include_unhealthy: bool = False) -> list[dict]:
    """Modelos elegibles empezando por el que toca según el cursor round-robin.

    `need_structured` es una preferencia, no un filtro: si ninguno de los que soportan
    structured output está sano, se sigue con el resto, que igual reciben el shape
    exacto como few-shot en el prompt.
    """
    pool = sync_pool()
    if not pool:
        raise NoModelAvailable("el pool de modelos está vacío")

    healthy = pool if include_unhealthy else [m for m in pool if is_healthy(m["id"])]
    if not healthy:
        raise NoModelAvailable(
            "todos los modelos están en cooldown; el más cercano vuelve en "
            f"{min(cooldown_remaining(m['id']) for m in pool)}s"
        )

    # Rotar el arranque para no castigar siempre al mismo modelo.
    offset = store.incr(CURSOR_KEY) % len(healthy)
    rotated = healthy[offset:] + healthy[:offset]

    # El orden dentro del anillo se reordena, pero los diminutos quedan siempre últimos.
    rotated.sort(key=lambda m: (m.get("tiny", False), not m["structured"] if need_structured else False))
    return rotated


def describe_pool() -> list[dict]:
    """Estado del pool para la UI: incluye los que están en cooldown y cuánto les falta."""
    out = []
    for model in sync_pool():
        remaining = cooldown_remaining(model["id"])
        out.append({**model, "cooldown": remaining, "healthy": remaining == 0})
    return out


def wait_hint() -> int:
    """Segundos hasta que vuelva a haber algún modelo sano."""
    pool = sync_pool()
    if not pool:
        return 0
    return min((cooldown_remaining(m["id"]) for m in pool), default=0)


def now() -> float:
    return time.time()
