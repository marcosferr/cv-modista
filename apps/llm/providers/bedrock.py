"""Proveedor Bedrock: modelos pagos con estructura garantizada.

La diferencia de fondo con OpenRouter no es el precio, es la **fiabilidad del formato**.
Se usa la API Converse con `toolConfig` y `toolChoice` forzado a una herramienta cuyo
inputSchema es el schema que queremos: Bedrock devuelve el objeto ya parseado en
`toolUse.input`. No hay JSON que rescatar, ni fences, ni truncamiento a mitad de un
string. Toda la escalera de recuperación queda como red de seguridad y casi nunca actúa.

Por qué DeepSeek V3.2 y no "v4 Flash": v4 Flash no existe en Bedrock. Los DeepSeek
disponibles son `deepseek.v3.2` (ON_DEMAND, el que se usa) y `deepseek.r1-v1:0`, que
además necesita perfil de inferencia. A ~USD 0,0044 por CV, V3.2 es barato en términos
absolutos; si querés bajarlo otro orden de magnitud, `BEDROCK_MODEL_ID=amazon.nova-lite-v1:0`
sale unas 9 veces menos.

Las credenciales salen de la cadena estándar de boto3: en la EC2 es el rol de instancia,
en local el perfil de AWS_PROFILE. Nunca hay una key en la base ni en el .env.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

from django.conf import settings

from apps.llm import cost
from apps.llm.errors import LlmError
from apps.llm.providers.base import Attempt, Request

log = logging.getLogger(__name__)

# Cómo se traduce cada error de Bedrock a nuestra taxonomía.
ERROR_KINDS = {
    "ThrottlingException": ("rate_limit", "Bedrock está throttleando"),
    "ServiceQuotaExceededException": ("rate_limit", "Cuota de Bedrock superada"),
    "ModelNotReadyException": ("server_error", "El modelo todavía no está listo"),
    "ModelTimeoutException": ("timeout", "El modelo no respondió a tiempo"),
    "InternalServerException": ("server_error", "Error interno de Bedrock"),
    "ServiceUnavailableException": ("server_error", "Bedrock no disponible"),
    "AccessDeniedException": ("no_key", "Sin acceso al modelo: habilitalo en la consola de Bedrock"),
    "UnrecognizedClientException": ("no_key", "Credenciales de AWS inválidas"),
    "ValidationException": ("no_endpoint", "El modelo rechazó el pedido"),
    "ResourceNotFoundException": ("no_endpoint", "El modelo no existe en esta región"),
}


class BedrockProvider:
    name = "bedrock"

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise LlmError("no_key", "Falta boto3: instalalo con `uv sync`") from exc
            self._client = boto3.client("bedrock-runtime", region_name=settings.BEDROCK_REGION)
        return self._client

    def models(self) -> list[str]:
        """Modelo principal y su respaldo, sin repetir si son el mismo."""
        chain = [settings.BEDROCK_MODEL_ID]
        fallback = settings.BEDROCK_FALLBACK_MODEL_ID
        if fallback and fallback != settings.BEDROCK_MODEL_ID:
            chain.append(fallback)
        return chain

    def _converse(self, model_id: str, request: Request) -> dict:
        from botocore.exceptions import BotoCoreError, ClientError

        kwargs: dict = {
            "modelId": model_id,
            "messages": [{"role": "user", "content": [{"text": request.user}]}],
            "system": [{"text": request.system}],
            "inferenceConfig": {"maxTokens": request.max_tokens, "temperature": 0},
        }
        if request.schema:
            # toolChoice forzado: el modelo no puede contestar con prosa, tiene que
            # llenar el schema. Es lo que hace innecesaria la escalera de JSON.
            kwargs["toolConfig"] = {
                "tools": [{"toolSpec": {
                    "name": request.schema_name,
                    "description": "Emite el resultado con esta estructura exacta.",
                    "inputSchema": {"json": request.schema},
                }}],
                "toolChoice": {"tool": {"name": request.schema_name}},
            }

        try:
            return self.client.converse(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            kind, message = ERROR_KINDS.get(code, ("server_error", f"Bedrock: {code}"))
            detail = exc.response.get("Error", {}).get("Message", "")[:200]
            raise LlmError(kind, f"{message} ({model_id}): {detail}") from exc
        except BotoCoreError as exc:
            raise LlmError("connect_error", f"No se pudo hablar con Bedrock: {exc}") from exc

    def attempts(self, request: Request) -> Iterator[Attempt]:
        if cost.exhausted():
            state = cost.status()
            raise LlmError(
                "budget_exhausted",
                f"Presupuesto diario de Bedrock agotado (USD {state['spent']:.2f} de "
                f"{state['budget']:.2f}). Se reinicia en {state['reset_hours']}h.",
            )

        notes: list[str] = []
        for index, model_id in enumerate(self.models()[: request.max_models], start=1):
            if not request.has_time_for(30):
                log.warning("Sin tiempo para otro intento (quedan %.0fs)", request.time_left())
                notes.append("sin tiempo para más intentos")
                break
            started = time.monotonic()
            try:
                body = self._converse(model_id, request)
            except LlmError as exc:
                if exc.fatal:
                    raise
                self.report_failure(model_id, exc.kind)
                notes.append(f"{model_id}: {exc.kind}")
                continue

            usage = body.get("usage") or {}
            prompt_tokens = usage.get("inputTokens") or 0
            completion_tokens = usage.get("outputTokens") or 0
            spent = cost.record(model_id, prompt_tokens, completion_tokens)

            blocks = (body.get("output") or {}).get("message", {}).get("content", [])
            payload = next((b["toolUse"]["input"] for b in blocks if "toolUse" in b), None)
            text = next((b["text"] for b in blocks if "text" in b), "")

            log.info("%s vía %s: %d tokens de salida, USD %.5f",
                     request.purpose, model_id, completion_tokens, spent)

            yield Attempt(
                model_id=model_id,
                raw=text,
                payload=payload,
                # Bedrock usa max_tokens donde OpenRouter usa length: se normaliza.
                finish_reason="length" if body.get("stopReason") == "max_tokens"
                else (body.get("stopReason") or ""),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=int((time.monotonic() - started) * 1000),
                index=index,
                notes=list(notes),
            )

    def report_failure(self, model_id: str, kind: str) -> None:
        # No hay cooldown: la cadena es corta y fija, y un modelo pago no "se recupera"
        # con el tiempo como un endpoint free saturado.
        log.warning("Bedrock %s falló por %s", model_id, kind)

    def report_success(self, model_id: str) -> None:
        return None

    def status(self) -> dict:
        state = cost.status()
        return {
            "provider": self.name,
            "unit": "USD",
            "used": state["spent"],
            "limit": state["budget"],
            "remaining": state["remaining"],
            "exhausted": state["exhausted"],
            "reset_hours": state["reset_hours"],
            "detail": f"USD {state['remaining']:.2f} de {state['budget']:.2f} del día",
        }
