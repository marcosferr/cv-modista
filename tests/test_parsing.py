"""La escalera de recuperación de JSON: el punto donde más rompen los free models."""

import pytest

from apps.llm.parsing import JsonRecoveryError, extract_json


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Claro, acá va:\n{"a": 1}\nEspero que sirva.', {"a": 1}),
        ("<think>razonando largo</think>\n{\"a\": 1}", {"a": 1}),
        ('{"a": 1,}', {"a": 1}),
        ('{“a”: 1}', {"a": 1}),
        ('[{"a": 1}]', {"a": 1}),
    ],
)
def test_recupera_json_de_respuestas_sucias(raw, expected):
    assert extract_json(raw) == expected


def test_recupera_json_truncado():
    """`finish_reason: length` es el fallo numero uno; lo que llego debe rescatarse."""
    truncado = '{"entries": [{"id": "exp0", "bullets": ["Lideré la migración de'
    recuperado = extract_json(truncado)
    assert recuperado["entries"][0]["id"] == "exp0"


def test_no_confunde_llaves_dentro_de_strings():
    raw = '{"text": "usá {llaves} adentro", "n": 2}'
    assert extract_json(raw) == {"text": "usá {llaves} adentro", "n": 2}


def test_levanta_si_no_hay_nada_rescatable():
    with pytest.raises(JsonRecoveryError):
        extract_json("no hay ningún objeto acá")
