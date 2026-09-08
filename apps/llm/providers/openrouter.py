"""Proveedor OpenRouter: modelos :free con rotación y guard de cupo.

Lo que más importa acá es **clasificar bien los 429**. Hay dos causas distintas detrás
del mismo código HTTP y tratarlas igual sale caro:

- throttling transitorio del proveedor -> rotar de modelo y seguir
- cupo diario de la cuenta agotado     -> reintentar quema toda la ventana de backoff
  para nada; hay que esperar a medianoche UTC

Tampoco se manda `provider.require_parameters`: el registry ya elige modelos cuyo
`supported_parameters` incluye structured_outputs, así que el flag sería redundante y
solo agrega el riesgo de un 404 "no endpoints found".
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator

import httpx
from django.conf import settings

from apps.llm import quota, registry
from apps.llm.errors import LlmError
from apps.llm.providers.base import Attempt, Request

log = logging.getLogger(__name__)

# OpenRouter dice en `metadata.limit_source` de dónde salió el 429.
_TRANSIENT_SOURCES = {"upstream_provider_shared_pool", "provider", "upstream"}
_DAILY_HINTS = ("per-day", "per day", "daily", "free-models-per-day", "requests per day")


def classify_http(status: int, body: str) -> tuple[str, str]:
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


class OpenRouterProvider:
    name = "openrouter"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": settings.OPENROUTER_APP_URL,
            "X-Title": settings.OPENROUTER_APP_NAME,
        }

    def _post(self, model: dict, request: Request) -> dict:
        capped = min(request.max_tokens, model["max_output"]) if model.get("max_output") \
            else request.max_tokens
        payload: dict = {
            "model": model["id"],
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": 0,
            "max_tokens": capped,
        }
        if request.schema and model.get("structured"):
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": request.schema_name, "strict": True,
                                "schema": request.schema},
            }

        try:
            response = httpx.post(
                f"{settings.OPENROUTER_BASE_URL}/chat/completions",
                headers=self._headers(), json=payload, timeout=settings.OPENROUTER_TIMEOUT,
            )
        except httpx.ConnectError as exc:
            raise LlmError("connect_error", f"No se pudo conectar con OpenRouter: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise LlmError(
                "timeout", f"OpenRouter no respondió en {settings.OPENROUTER_TIMEOUT}s") from exc

        if response.status_code != 200:
            raise LlmError(*classify_http(response.status_code, response.text))

        body = response.json()
        # OpenRouter también devuelve errores con HTTP 200 y un campo "error".
        if isinstance(body.get("error"), dict):
            error = body["error"]
            raise LlmError(*classify_http(int(error.get("code") or 500), json.dumps(error)))
        if not body.get("choices"):
            raise LlmError("server_error", "Respuesta sin choices")
        return body

    def attempts(self, request: Request) -> Iterator[Attempt]:
        if not settings.OPENROUTER_API_KEY:
            raise LlmError("no_key", "Falta OPENROUTER_API_KEY en el .env")

        try:
            pool = registry.candidates(need_structured=request.schema is not None)
        except registry.NoModelAvailable as exc:
            raise LlmError("no_model", str(exc)) from exc

        notes: list[str] = []
        for index, model in enumerate(pool[: request.max_models], start=1):
            if not quota.reserve():
                raise LlmError(
                    "quota_exhausted",
                    f"Cupo diario agotado ({quota.daily_limit()}/día). "
                    f"Se libera en {quota.reset_seconds() // 3600}h.",
                )

            started = time.monotonic()
            try:
                body = self._post(model, request)
            except LlmError as exc:
                if exc.kind == "connect_error":
                    quota.release()  # nunca llegó a OpenRouter, no consumió cupo
                if exc.fatal:
                    raise
                self.report_failure(model["id"], exc.kind)
                notes.append(f"{model['id']}: {exc.kind}")
                continue

            choice = body["choices"][0]
            usage = body.get("usage") or {}
            yield Attempt(
                model_id=model["id"],
                raw=(choice.get("message") or {}).get("content") or "",
                finish_reason=choice.get("finish_reason") or "",
                prompt_tokens=usage.get("prompt_tokens") or 0,
                completion_tokens=usage.get("completion_tokens") or 0,
                latency_ms=int((time.monotonic() - started) * 1000),
                index=index,
                notes=list(notes),
            )

    def report_failure(self, model_id: str, kind: str) -> None:
        registry.report_failure(model_id, kind)

    def report_success(self, model_id: str) -> None:
        registry.report_success(model_id)

    def status(self) -> dict:
        state = quota.status()
        return {
            "provider": self.name,
            "unit": "llamadas",
            "used": state["used"],
            "limit": state["limit"],
            "remaining": state["remaining"],
            "exhausted": state["exhausted"],
            "reset_hours": state["reset_hours"],
            "detail": f"{state['remaining']}/{state['limit']} llamadas del día",
        }
