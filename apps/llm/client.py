"""Orquestador de las llamadas al LLM.

El proveedor (OpenRouter o Bedrock) solo sabe hacer llamadas y elegir el siguiente
modelo. Todo lo que decide si una respuesta **sirve** vive acá y es idéntico para los
dos: caché, recuperación de JSON, validación de schema y detección de contaminación.

Un intento cuyo contenido no sirve se devuelve al proveedor como `ContentRejected` y el
generador produce el siguiente modelo. No hay una llamada aparte de "reparación": pedirle
al mismo modelo flojo que arregle su propio JSON cuesta lo mismo que rotar a otro y sale
peor.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field

from django.conf import settings
from pydantic import BaseModel, ValidationError

from apps.llm import cost, prompts
from apps.llm.errors import FATAL_KINDS, ContentRejected, LlmError
from apps.llm.models import LlmCall
from apps.llm.parsing import JsonRecoveryError, extract_json
from apps.llm.providers import Request, get_provider

log = logging.getLogger(__name__)

__all__ = ["LlmError", "LlmResult", "cache_key", "complete_json", "FATAL_KINDS"]


@dataclass(slots=True)
class LlmResult:
    data: dict
    model_id: str
    provider: str = ""
    cached: bool = False
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    attempts: int = 1
    notes: list[str] = field(default_factory=list)


def cache_key(purpose: str, payload: str) -> str:
    """No incluye el modelo a propósito.

    El objetivo del caché es no repetir llamadas, y si rotar de modelo invalidara la
    entrada cada rotación costaría una llamada más. Como solo se persiste después de que
    el schema valida, lo cacheado siempre es bueno venga del modelo que venga.
    """
    normalized = " ".join((payload or "").split())
    raw = f"{purpose}|{prompts.PROMPT_VERSION}|{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --- validación compartida --------------------------------------------------


def _iter_strings(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for value in node.values() for s in _iter_strings(value)]
    if isinstance(node, list):
        return [s for item in node for s in _iter_strings(item)]
    return []


def _copied_from_example(data: dict, example: str, source: str, min_len: int = 25) -> list[str]:
    """Textos que el modelo copió del few-shot en vez de generarlos.

    Los modelos chicos hacen esto seguido: devuelven el ejemplo del prompt tal cual y el
    resultado pasa el schema sin problemas, así que ninguna validación de forma lo
    detecta. Un CV con el "Acerca de" de otra persona es peor que no tener nada.

    La condición doble evita el falso positivo obvio: un texto que está en el ejemplo
    **y también** en el CV real del usuario no es copia, es coincidencia legítima.
    """
    example_norm = " ".join(example.split()).lower()
    source_norm = " ".join(source.split()).lower()
    copied = []
    for text in _iter_strings(data):
        if len(text) < min_len:
            continue
        normalized = " ".join(text.split()).lower()
        if normalized in example_norm and normalized not in source_norm:
            copied.append(text)
    return copied


def _accept(attempt, model_cls: type[BaseModel], example: str, source: str) -> dict:
    """Convierte un intento en datos válidos, o levanta ContentRejected."""
    if attempt.payload is not None:
        # Bedrock con toolChoice forzado ya devuelve el objeto parseado.
        payload = attempt.payload
    else:
        # Chequear el truncamiento ANTES de parsear: lo que falta no está en ningún
        # lado, así que repararlo es imposible. Se rota a otro modelo.
        if attempt.finish_reason == "length":
            raise ContentRejected("truncated", f"{attempt.model_id} truncó la respuesta")
        try:
            payload = extract_json(attempt.raw)
        except JsonRecoveryError as exc:
            raise ContentRejected("bad_json", f"{attempt.model_id}: {exc}") from exc

    try:
        data = model_cls.model_validate(payload).model_dump()
    except ValidationError as exc:
        raise ContentRejected(
            "bad_json", f"{attempt.model_id}: {exc.error_count()} errores de schema") from exc

    if example:
        copied = _copied_from_example(data, example, source)
        if copied:
            log.warning("%s devolvió %d campos copiados del few-shot: %r",
                        attempt.model_id, len(copied), copied[0][:80])
            raise ContentRejected(
                "copied_example", f"{attempt.model_id} copió el ejemplo en vez de generar")
    return data


def _persist(key: str, purpose: str, provider: str, attempt, data: dict, spent: float) -> None:
    """Se guarda recién acá: una respuesta truncada nunca queda cacheada como buena."""
    LlmCall.objects.update_or_create(
        cache_key=key,
        defaults={
            "purpose": purpose,
            "prompt_version": prompts.PROMPT_VERSION,
            "provider": provider,
            "model_id": attempt.model_id,
            "data": data,
            "raw_response": (attempt.raw or "")[:20000],
            "finish_reason": attempt.finish_reason,
            "prompt_tokens": attempt.prompt_tokens,
            "completion_tokens": attempt.completion_tokens,
            "cost_usd": spent,
            "latency_ms": attempt.latency_ms,
            "attempts": attempt.index,
        },
    )


# --- entrada principal ------------------------------------------------------


def complete_json(
    *,
    purpose: str,
    system: str,
    user: str,
    schema: dict | None,
    schema_name: str,
    model_cls: type[BaseModel],
    max_tokens: int,
    example: str = "",
    max_models: int = 4,
    provider_name: str | None = None,
    force: bool = False,
) -> LlmResult:
    """Pide un JSON al LLM, rotando modelos hasta que uno devuelva algo usable."""
    key = cache_key(purpose, f"{system}\n{user}")

    if not force:
        cached = LlmCall.objects.filter(cache_key=key).first()
        if cached and cached.data:
            log.info("Cache hit de %s (%s), 0 llamadas gastadas", purpose, cached.model_id)
            return LlmResult(data=cached.data, model_id=cached.model_id,
                             provider=cached.provider, cached=True,
                             finish_reason=cached.finish_reason)

    if settings.FAKE_LLM:
        return _fake(key, purpose, user, model_cls, example)

    provider = get_provider(provider_name)
    request = Request(purpose=purpose, system=system, user=user, schema=schema,
                      schema_name=schema_name, max_tokens=max_tokens, max_models=max_models,
                      deadline=time.monotonic() + settings.LLM_BUDGET_SECONDS)

    notes: list[str] = []
    last_error: LlmError | None = None

    for attempt in provider.attempts(request):
        notes = attempt.notes
        try:
            data = _accept(attempt, model_cls, example, user)
        except ContentRejected as exc:
            provider.report_failure(attempt.model_id, exc.kind)
            notes.append(f"{attempt.model_id}: {exc.kind}")
            last_error = LlmError(exc.kind, str(exc))
            continue

        provider.report_success(attempt.model_id)
        spent = cost.cost_usd(attempt.model_id, attempt.prompt_tokens,
                              attempt.completion_tokens) if provider.name != "openrouter" else 0.0
        _persist(key, purpose, provider.name, attempt, data, spent)
        log.info("%s resuelto por %s en %dms (%s tokens de salida)",
                 purpose, attempt.model_id, attempt.latency_ms, attempt.completion_tokens)
        return LlmResult(data=data, model_id=attempt.model_id, provider=provider.name,
                         finish_reason=attempt.finish_reason,
                         prompt_tokens=attempt.prompt_tokens,
                         completion_tokens=attempt.completion_tokens,
                         cost_usd=spent, attempts=attempt.index, notes=notes)

    raise last_error or LlmError("no_model", "Ningún modelo pudo resolver la llamada")


def _fake(key: str, purpose: str, user: str, model_cls: type[BaseModel], example: str) -> LlmResult:
    """Camino de fixtures. Corre la misma escalera y la misma validación que el real."""
    path = settings.FIXTURES_DIR / f"{purpose}.json"
    if not path.exists():
        raise LlmError("no_model", f"FAKE_LLM activo pero falta el fixture {path}")
    raw = path.read_text(encoding="utf-8")

    from apps.llm.providers.base import Attempt

    attempt = Attempt(model_id="fake", raw=raw, finish_reason="stop")
    try:
        data = _accept(attempt, model_cls, example, user)
    except ContentRejected as exc:
        raise LlmError(exc.kind, f"El fixture {path.name} no valida: {exc}") from exc
    _persist(key, purpose, "fake", attempt, data, 0.0)
    return LlmResult(data=data, model_id="fake", provider="fake", finish_reason="stop")
