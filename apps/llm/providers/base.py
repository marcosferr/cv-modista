"""Contrato entre el orquestador y los proveedores de LLM.

El proveedor solo sabe hacer llamadas y decidir a qué modelo va la siguiente. Todo lo
demás —caché, recuperación de JSON, validación de schema, detección de contaminación,
persistencia— vive en `client.py` y es idéntico para los dos proveedores.

El proveedor expone los intentos como un **generador**: produce uno, el orquestador
decide si el contenido sirve, y solo si no sirve el generador produce el siguiente. Un
intento que sale bien corta la generación sin gastar llamadas de más.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class Request:
    purpose: str
    system: str
    user: str
    schema: dict | None
    schema_name: str
    max_tokens: int
    max_models: int = 4
    # Instante de `time.monotonic()` a partir del cual no se empiezan más intentos.
    # Sin esto el presupuesto de reintentos (4 modelos por el timeout de cada uno) puede
    # superar el soft_time_limit de Celery y la task muere con SoftTimeLimitExceeded en
    # vez de devolver un error entendible. Pasó en producción.
    deadline: float | None = None

    def time_left(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - time.monotonic()

    def has_time_for(self, seconds: float) -> bool:
        return self.time_left() >= seconds


@dataclass(slots=True)
class Attempt:
    """Una llamada que devolvió algo. `payload` viene lleno cuando el proveedor
    garantiza estructura (tool use forzado en Bedrock) y no hay nada que parsear."""

    model_id: str
    raw: str = ""
    payload: dict | None = None
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    index: int = 1
    notes: list[str] = field(default_factory=list)


class Provider(Protocol):
    name: str

    def attempts(self, request: Request) -> Iterator[Attempt]:
        """Produce intentos hasta agotar modelos. Levanta LlmError si no puede seguir."""
        ...

    def report_failure(self, model_id: str, kind: str) -> None:
        """El contenido del intento no sirvió: apartar ese modelo."""
        ...

    def report_success(self, model_id: str) -> None: ...

    def status(self) -> dict:
        """Datos para el widget de la UI: cuánto queda de cupo o de presupuesto."""
        ...
