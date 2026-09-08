"""Cliente de OpenRouter con rotación de modelos, caché y guard de cupo.

La decisión que más importa acá es **clasificar bien los 429**. Hay dos causas
distintas detrás del mismo código HTTP y tratarlas igual es caro:

- throttling transitorio del proveedor -> conviene rotar de modelo y reintentar
- cupo diario de la cuenta agotado     -> reintentar es quemar toda la ventana de
  backoff para nada; el job se marca y se espera a medianoche UTC

Tampoco se manda `provider.require_parameters`: el registry ya elige modelos cuyo
`supported_parameters` incluye structured_outputs, así que el flag sería redundante y
solo agrega el riesgo de un 404 "no endpoints found".
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

import httpx
from django.conf import settings
from pydantic import BaseModel, ValidationError

from apps.llm import prompts, quota, registry
from apps.llm.models import LlmCall
from apps.llm.parsing import JsonRecoveryError, extract_json

log = logging.getLogger(__name__)

# Errores que no se arreglan rotando ni reintentando.
FATAL_KINDS = {"no_key", "quota_exhausted"}


@dataclass(slots=True)
class LlmResult:
    data: dict
    model_id: str
    cached: bool = False
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: int = 1
    notes: list[str] = field(default_factory=list)


class LlmError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.fatal = kind in FATAL_KINDS


def cache_key(purpose: str, payload: str) -> str:
    normalized = " ".join((payload or "").split())
    raw = f"{purpose}|{prompts.PROMPT_VERSION}|{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# OpenRouter distingue en `metadata.limit_source` de dónde vino el 429. Un
# `upstream_provider_shared_pool` es del proveedor y se arregla rotando; el cupo de la
# cuenta no, y ahí reintentar solo quema la ventana de backoff.
_TRANSIENT_SOURCES = {"upstream_provider_shared_pool", "provider", "upstream"}
_DAILY_HINTS = ("per-day", "per day", "daily", "free-models-per-day", "requests per day")


def _classify_http(status: int, body: str) -> tuple[str, str]:
    lowered = (body or "").lower()
    if status in (401, 403):
        return "no_key", "OPENROUTER_API_KEY inválida o sin permisos"
    if status == 402:
        return "quota_exhausted", "OpenRouter reporta créditos insuficientes"
    if status == 429:
        metadata = {}
        try:
            metadata = (json.loads(body).get("error") or {}).get("metadata") or {}
        except (ValueError, AttributeError):
            pass
        if metadata.get("limit_source") in _TRANSIENT_SOURCES:
            provider = metadata.get("provider_name") or "el proveedor"
            return "rate_limit", f"{provider} está throttleando (429 del pool compartido)"
        if any(hint in lowered for hint in _DAILY_HINTS):
            return "quota_exhausted", "Cupo diario de modelos :free agotado"
        return "rate_limit", "429 transitorio del proveedor"
    if status == 404:
        return "no_endpoint", "El modelo no tiene endpoints disponibles"
    if status in (408, 500, 502, 503, 504):
        return "server_error", f"Error del proveedor (HTTP {status})"
    return "server_error", f"HTTP {status}: {body[:200]}"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": settings.OPENROUTER_APP_URL,
        "X-Title": settings.OPENROUTER_APP_NAME,
    }


def _post(model: dict, messages: list[dict], schema: dict | None, schema_name: str,
          max_tokens: int) -> dict:
    """Una llamada HTTP. Devuelve el body ya deserializado o levanta LlmError."""
    capped = min(max_tokens, model["max_output"]) if model.get("max_output") else max_tokens
    payload: dict = {
        "model": model["id"],
        "messages": messages,
        "temperature": 0,
        "max_tokens": capped,
    }
    if schema and model.get("structured"):
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        }

    try:
        response = httpx.post(
            f"{settings.OPENROUTER_BASE_URL}/chat/completions",
            headers=_headers(),
            json=payload,
            timeout=settings.OPENROUTER_TIMEOUT,
        )
    except httpx.ConnectError as exc:
        raise LlmError("connect_error", f"No se pudo conectar con OpenRouter: {exc}") from exc
    except httpx.TimeoutException as exc:
        raise LlmError("timeout", f"OpenRouter no respondió en {settings.OPENROUTER_TIMEOUT}s") from exc

    if response.status_code != 200:
        kind, message = _classify_http(response.status_code, response.text)
        raise LlmError(kind, message)

    body = response.json()
    # OpenRouter también devuelve errores con HTTP 200 y un campo "error".
    if isinstance(body.get("error"), dict):
        error = body["error"]
        kind, message = _classify_http(int(error.get("code") or 500), json.dumps(error))
        raise LlmError(kind, message)
    if not body.get("choices"):
        raise LlmError("server_error", "Respuesta sin choices")
    return body


def _fixture(purpose: str) -> str:
    path = settings.FIXTURES_DIR / f"{purpose}.json"
    if not path.exists():
        raise LlmError("no_model", f"FAKE_LLM activo pero falta el fixture {path}")
    return path.read_text(encoding="utf-8")


def _validate(data: dict, model_cls: type[BaseModel]) -> dict:
    return model_cls.model_validate(data).model_dump()


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
    allow_repair: bool = True,
    force: bool = False,
) -> LlmResult:
    """Pide un JSON al LLM rotando modelos hasta que uno devuelva algo válido."""
    key = cache_key(purpose, f"{system}\n{user}")

    if not force:
        cached = LlmCall.objects.filter(cache_key=key).first()
        if cached and cached.data:
            log.info("Cache hit de %s (%s), 0 llamadas gastadas", purpose, cached.model_id)
            return LlmResult(data=cached.data, model_id=cached.model_id, cached=True,
                             finish_reason=cached.finish_reason)

    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    if settings.FAKE_LLM:
        raw = _fixture(purpose)
        data = _validate(extract_json(raw), model_cls)
        LlmCall.objects.update_or_create(
            cache_key=key,
            defaults={"purpose": purpose, "prompt_version": prompts.PROMPT_VERSION,
                      "model_id": "fake", "data": data, "raw_response": raw[:20000],
                      "finish_reason": "stop"},
        )
        return LlmResult(data=data, model_id="fake", finish_reason="stop")

    if not settings.OPENROUTER_API_KEY:
        raise LlmError("no_key", "Falta OPENROUTER_API_KEY en el .env")

    try:
        pool = registry.candidates(need_structured=schema is not None)
    except registry.NoModelAvailable as exc:
        raise LlmError("no_model", str(exc)) from exc

    notes: list[str] = []
    last_error: LlmError | None = None
    repair_used = False

    for attempt, model in enumerate(pool[:max_models], start=1):
        if not quota.reserve():
            raise LlmError("quota_exhausted",
                           f"Cupo diario agotado ({quota.daily_limit()}/día). "
                           f"Se libera en {quota.reset_seconds() // 3600}h.")

        started = time.monotonic()
        try:
            body = _post(model, messages, schema, schema_name, max_tokens)
        except LlmError as exc:
            if exc.kind == "connect_error":
                quota.release()  # nunca llegó a OpenRouter, no consumió cupo
            if exc.fatal:
                raise
            registry.report_failure(model["id"], exc.kind)
            notes.append(f"{model['id']}: {exc.kind}")
            last_error = exc
            continue

        latency_ms = int((time.monotonic() - started) * 1000)
        choice = body["choices"][0]
        finish_reason = choice.get("finish_reason") or ""
        raw = (choice.get("message") or {}).get("content") or ""
        usage = body.get("usage") or {}

        # Chequear el truncamiento ANTES de parsear: repararlo es imposible, lo que
        # falta no está en ningún lado. Se rota a otro modelo.
        if finish_reason == "length":
            registry.report_failure(model["id"], "truncated")
            notes.append(f"{model['id']}: truncado")
            last_error = LlmError("truncated", f"{model['id']} truncó la respuesta")
            continue

        try:
            payload = extract_json(raw)
        except JsonRecoveryError as exc:
            if allow_repair and not repair_used:
                repair_used = True
                notes.append(f"{model['id']}: JSON roto, reparando")
                try:
                    payload = _repair(raw, str(exc), model, max_tokens)
                except LlmError as repair_exc:
                    registry.report_failure(model["id"], "bad_json")
                    last_error = repair_exc
                    continue
            else:
                registry.report_failure(model["id"], "bad_json")
                notes.append(f"{model['id']}: JSON irrecuperable")
                last_error = LlmError("bad_json", f"{model['id']} no devolvió JSON usable")
                continue

        try:
            data = _validate(payload, model_cls)
        except ValidationError as exc:
            registry.report_failure(model["id"], "bad_json")
            notes.append(f"{model['id']}: no valida el schema")
            last_error = LlmError("bad_json", f"{model['id']}: {exc.error_count()} errores")
            continue

        if example:
            copied = _copied_from_example(data, example, user)
            if copied:
                registry.report_failure(model["id"], "bad_json")
                notes.append(f"{model['id']}: copió el ejemplo del prompt")
                log.warning("%s devolvió %d campos copiados del few-shot: %r",
                            model["id"], len(copied), copied[0][:80])
                last_error = LlmError(
                    "bad_json", f"{model['id']} copió el ejemplo en vez de generar")
                continue

        registry.report_success(model["id"])
        # Se persiste recién acá: una respuesta truncada nunca queda cacheada como buena.
        LlmCall.objects.update_or_create(
            cache_key=key,
            defaults={
                "purpose": purpose,
                "prompt_version": prompts.PROMPT_VERSION,
                "model_id": model["id"],
                "data": data,
                "raw_response": raw[:20000],
                "finish_reason": finish_reason,
                "prompt_tokens": usage.get("prompt_tokens") or 0,
                "completion_tokens": usage.get("completion_tokens") or 0,
                "latency_ms": latency_ms,
                "attempts": attempt,
            },
        )
        log.info("%s resuelto por %s en %dms (%s tokens salida)", purpose, model["id"],
                 latency_ms, usage.get("completion_tokens") or "?")
        return LlmResult(data=data, model_id=model["id"], finish_reason=finish_reason,
                         prompt_tokens=usage.get("prompt_tokens") or 0,
                         completion_tokens=usage.get("completion_tokens") or 0,
                         attempts=attempt, notes=notes)

    raise last_error or LlmError("no_model", "Ningún modelo del pool pudo resolver la llamada")


def _repair(broken: str, error: str, model: dict, max_tokens: int) -> dict:
    """Una única llamada de reparación por job. Solo se llega acá si la escalera falló."""
    if not quota.reserve():
        raise LlmError("quota_exhausted", "Cupo agotado antes de poder reparar el JSON")
    body = _post(
        model,
        [
            {"role": "system", "content": prompts.REPAIR_SYSTEM},
            {"role": "user", "content": prompts.repair_user_prompt(broken, error)},
        ],
        None,
        "repair",
        max_tokens,
    )
    raw = (body["choices"][0].get("message") or {}).get("content") or ""
    try:
        return extract_json(raw)
    except JsonRecoveryError as exc:
        raise LlmError("bad_json", "La reparación tampoco devolvió JSON válido") from exc
