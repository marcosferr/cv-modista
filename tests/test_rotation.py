"""Rotación de modelos y guard de cupo."""

import pytest

from apps.llm import quota, registry, store

POOL = [
    {"id": "a:free", "context": 512000, "max_output": 8000, "structured": True},
    {"id": "b:free", "context": 262144, "max_output": 8000, "structured": True},
    {"id": "c:free", "context": 131072, "max_output": 8000, "structured": False},
]


@pytest.fixture(autouse=True)
def _pool(monkeypatch):
    monkeypatch.setattr(registry, "sync_pool", lambda force=False: list(POOL))


def test_un_429_manda_el_modelo_a_cooldown_y_rota():
    primero = registry.candidates()[0]["id"]
    registry.report_failure(primero, "rate_limit")
    assert primero not in [m["id"] for m in registry.candidates()]
    assert registry.cooldown_remaining(primero) > 0


def test_el_cooldown_dura_segun_la_gravedad():
    """Un JSON irrecuperable aparta al modelo mucho más que un throttling pasajero."""
    registry.report_failure("a:free", "rate_limit")
    registry.report_failure("b:free", "bad_json")
    assert registry.cooldown_remaining("b:free") > registry.cooldown_remaining("a:free")


def test_un_exito_saca_del_cooldown():
    registry.report_failure("a:free", "rate_limit")
    registry.report_success("a:free")
    assert registry.is_healthy("a:free")


def test_prefiere_structured_output_pero_no_lo_exige():
    """Si ninguno con structured está sano se sigue con el resto: el prompt igual
    lleva el shape exacto como few-shot."""
    registry.report_failure("a:free", "bad_json")
    registry.report_failure("b:free", "bad_json")
    assert registry.candidates(need_structured=True)[0]["id"] == "c:free"


def test_si_todo_esta_en_cooldown_avisa_en_vez_de_llamar():
    for model in POOL:
        registry.report_failure(model["id"], "rate_limit")
    with pytest.raises(registry.NoModelAvailable):
        registry.candidates()


def test_la_rotacion_reparte_el_arranque():
    vistos = {registry.candidates()[0]["id"] for _ in range(9)}
    assert len(vistos) > 1, "el cursor round-robin no está rotando"


def test_el_contador_de_cupo_nunca_se_pasa(settings):
    settings.OPENROUTER_DAILY_QUOTA = 3
    store.set("llm:quota:limit", "3", ttl=60)
    assert [quota.reserve() for _ in range(4)] == [True, True, True, False]
    assert quota.used_today() == 3


def test_cupo_agotado_deja_el_contador_lleno_hasta_medianoche():
    quota.mark_exhausted()
    assert quota.remaining() == 0 and quota.reset_seconds() > 0


def test_release_devuelve_la_reserva():
    """Si la llamada nunca salió (fallo de conexión), no debe contar contra el cupo."""
    quota.reserve()
    usado = quota.used_today()
    quota.release()
    assert quota.used_today() == usado - 1


# --- contaminación con el ejemplo del prompt --------------------------------


def test_detecta_cuando_el_modelo_copia_el_ejemplo():
    """Caso real: un modelo de 2.6B devolvió los tres textos del few-shot tal cual.

    Pasa el schema sin problemas, así que ninguna validación de forma lo agarra, y el
    usuario se lleva un CV con el "Acerca de" de otra persona.
    """
    from apps.llm import prompts
    from apps.llm.client import _copied_from_example

    devuelto = {
        "about": "Construyo backends de pagos...",
        "recruiter_message": "Hola Juan, vi la búsqueda de Backend Engineer...",
        "headline": "Backend Engineer | Python & Django | Sistemas de pagos a escala",
    }
    copiados = _copied_from_example(devuelto, prompts.TAILOR_EXAMPLE, "CV real de Ana.")
    assert len(copiados) == 3


def test_no_marca_texto_que_tambien_esta_en_el_cv_del_usuario():
    """Falso positivo a evitar: coincidir con el ejemplo Y con el CV real no es copia."""
    from apps.llm import prompts
    from apps.llm.client import _copied_from_example

    frase = "Diseñé la API de pagos que procesa 12.000 transacciones diarias"
    assert _copied_from_example({"b": frase}, prompts.PARSE_EXAMPLE, f"EXPERIENCIA\n- {frase}") == []


def test_ignora_textos_cortos():
    from apps.llm.client import _copied_from_example

    assert _copied_from_example({"a": "Python"}, "Python es un lenguaje", "otro") == []


# --- heurística de tamaño de modelo -----------------------------------------


def test_reconoce_el_tamaño_desde_el_id():
    assert registry.size_hint("liquid/lfm-2.5-2.6b:free") == 2.6
    assert registry.size_hint("nvidia/nemotron-3-super-120b-a12b:free") == 120.0
    assert registry.size_hint("nex-agi/nex-n2.5-pro:free") is None


def test_los_modelos_diminutos_quedan_ultimos(monkeypatch):
    """Un 2.6B copia el ejemplo y recorta el CV sin criterio: último recurso, no primero."""
    pool = [
        {"id": "chico/x-2.6b:free", "context": 999999, "max_output": 8000,
         "structured": True, "tiny": True},
        {"id": "grande/y:free", "context": 100, "max_output": 8000,
         "structured": True, "tiny": False},
    ]
    monkeypatch.setattr(registry, "sync_pool", lambda force=False: list(pool))
    assert registry.candidates(need_structured=True)[0]["id"] == "grande/y:free"


def test_sin_dato_de_tamaño_se_asume_capaz():
    """La mayoría de los modelos buenos no ponen el tamaño en el id."""
    assert registry.is_tiny("dots-studio/dots-3-note-preview:free") is False
