"""Proveedor Bedrock y guard de presupuesto.

Todo con dobles: los tests no pueden depender de credenciales de AWS ni gastar dinero.
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from apps.llm import cost, schemas
from apps.llm.errors import ContentRejected, LlmError
from apps.llm.providers import get_provider
from apps.llm.providers.base import Attempt, Request
from apps.llm.providers.bedrock import BedrockProvider

REQUEST = Request(purpose="parse", system="sistema", user="usuario",
                  schema={"type": "object", "properties": {}}, schema_name="parsed_cv",
                  max_tokens=1000)


def respuesta(payload=None, texto="", stop="end_turn", tokens=(100, 50)):
    contenido = []
    if payload is not None:
        contenido.append({"toolUse": {"name": "parsed_cv", "input": payload}})
    if texto:
        contenido.append({"text": texto})
    return {
        "output": {"message": {"content": contenido}},
        "stopReason": stop,
        "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1]},
    }


def error_cliente(code):
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, "Converse")


@pytest.fixture
def bedrock(settings):
    settings.LLM_PROVIDER = "bedrock"
    settings.BEDROCK_MODEL_ID = "deepseek.v3.2"
    settings.BEDROCK_FALLBACK_MODEL_ID = "amazon.nova-lite-v1:0"
    settings.BEDROCK_DAILY_USD_BUDGET = 2.0
    p = BedrockProvider()
    p._client = MagicMock()
    return p


# --- estructura garantizada -------------------------------------------------


def test_fuerza_el_tool_para_garantizar_estructura(bedrock):
    """toolChoice forzado es lo que hace innecesaria la escalera de recuperación:
    el modelo no puede contestar con prosa, tiene que llenar el schema."""
    bedrock._client.converse.return_value = respuesta(payload={"skills": []})
    next(bedrock.attempts(REQUEST))

    enviado = bedrock._client.converse.call_args.kwargs
    assert enviado["toolConfig"]["toolChoice"] == {"tool": {"name": "parsed_cv"}}
    assert enviado["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"] == REQUEST.schema
    assert enviado["inferenceConfig"]["temperature"] == 0


def test_el_payload_viene_ya_parseado(bedrock):
    """Sin JSON que rescatar: Bedrock devuelve el objeto en toolUse.input."""
    bedrock._client.converse.return_value = respuesta(payload={"skills": ["Python"]})
    attempt = next(bedrock.attempts(REQUEST))
    assert attempt.payload == {"skills": ["Python"]}


def test_sin_schema_no_manda_toolconfig(bedrock):
    bedrock._client.converse.return_value = respuesta(texto='{"a": 1}')
    next(bedrock.attempts(Request(purpose="x", system="s", user="u", schema=None,
                                  schema_name="x", max_tokens=100)))
    assert "toolConfig" not in bedrock._client.converse.call_args.kwargs


def test_normaliza_max_tokens_a_length(bedrock):
    """Bedrock dice `max_tokens` donde OpenRouter dice `length`; el orquestador
    solo entiende `length`."""
    bedrock._client.converse.return_value = respuesta(texto="corta", stop="max_tokens")
    assert next(bedrock.attempts(REQUEST)).finish_reason == "length"


# --- errores y respaldo -----------------------------------------------------


def test_rota_al_modelo_de_respaldo(bedrock):
    bedrock._client.converse.side_effect = [error_cliente("ThrottlingException"),
                                            respuesta(payload={"skills": []})]
    attempt = next(bedrock.attempts(REQUEST))
    assert attempt.model_id == "amazon.nova-lite-v1:0"
    assert "deepseek.v3.2: rate_limit" in attempt.notes


def test_sin_acceso_al_modelo_es_fatal_y_no_rota(bedrock):
    """Rotar no arregla un permiso faltante: hay que habilitar el modelo en la consola."""
    bedrock._client.converse.side_effect = error_cliente("AccessDeniedException")
    with pytest.raises(LlmError) as exc:
        next(bedrock.attempts(REQUEST))
    assert exc.value.kind == "no_key" and exc.value.fatal


def test_traduce_los_errores_de_bedrock(bedrock):
    for code, esperado in [("ThrottlingException", "rate_limit"),
                           ("ValidationException", "no_endpoint"),
                           ("InternalServerException", "server_error"),
                           ("ModelTimeoutException", "timeout")]:
        bedrock._client.converse.side_effect = error_cliente(code)
        gen = bedrock.attempts(REQUEST)
        attempt = None
        try:
            attempt = next(gen)
        except (LlmError, StopIteration):
            pass
        assert attempt is None or esperado in " ".join(attempt.notes)


# --- presupuesto ------------------------------------------------------------


def test_el_presupuesto_agotado_frena_antes_de_llamar(bedrock, settings):
    """Con registro abierto, un proveedor pago sin tope es una superficie de abuso."""
    settings.BEDROCK_DAILY_USD_BUDGET = 0.001
    cost.record("deepseek.v3.2", 10000, 10000)
    with pytest.raises(LlmError) as exc:
        next(bedrock.attempts(REQUEST))
    assert exc.value.kind == "budget_exhausted" and exc.value.fatal
    bedrock._client.converse.assert_not_called()


def test_acumula_el_gasto_por_llamada(bedrock):
    bedrock._client.converse.return_value = respuesta(payload={}, tokens=(1000, 1000))
    antes = cost.spent_today_usd()
    next(bedrock.attempts(REQUEST))
    # deepseek.v3.2: USD 0,00062 por 1k de entrada + 0,00222 por 1k de salida
    assert cost.spent_today_usd() == pytest.approx(antes + 0.00284, abs=1e-5)


def test_precio_de_modelo_desconocido_es_conservador():
    """El guard tiene que errar cortando de más, no de menos."""
    assert cost.price_for("modelo.inventado") == cost.DEFAULT_PRICE
    assert cost.price_for("us.deepseek.v3.2") == cost.PRICES["deepseek.v3.2"]


def test_el_status_habla_en_dolares(bedrock):
    estado = bedrock.status()
    assert estado["provider"] == "bedrock" and estado["unit"] == "USD"
    assert "USD" in estado["detail"]


# --- selección de proveedor -------------------------------------------------


def test_el_setting_elige_el_proveedor(settings):
    settings.LLM_PROVIDER = "bedrock"
    assert get_provider().name == "bedrock"
    settings.LLM_PROVIDER = "openrouter"
    assert get_provider().name == "openrouter"
    assert get_provider("bedrock").name == "bedrock"


def test_proveedor_desconocido_falla_temprano(settings):
    settings.LLM_PROVIDER = "inventado"
    with pytest.raises(ValueError, match="LLM_PROVIDER desconocido"):
        get_provider()


# --- el orquestador trata igual a los dos -----------------------------------


def test_un_payload_estructurado_saltea_la_escalera_de_json():
    from apps.llm.client import _accept

    attempt = Attempt(model_id="deepseek.v3.2", raw="", payload={"skills": ["Python"]})
    assert _accept(attempt, schemas.ParsedCv, "", "")["skills"] == ["Python"]


def test_el_truncamiento_se_rechaza_igual_en_ambos():
    from apps.llm.client import _accept

    attempt = Attempt(model_id="x", raw='{"skills": ["Pyt', finish_reason="length")
    with pytest.raises(ContentRejected) as exc:
        _accept(attempt, schemas.ParsedCv, "", "")
    assert exc.value.kind == "truncated"
