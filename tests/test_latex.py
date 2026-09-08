"""Escapado y compilación. El .tex se descarga, así que tiene que valer en Overleaf."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from apps.resume.compile import TectonicCompiler, ats_check, page_count, pdf_text
from apps.resume.render import escape_tex, escape_url, normalize_unicode, render_tex

CV = {
    "contact": {"full_name": "Ana Gómez Ríos", "email": "ana@example.com",
                "phone": "+595 981 123456", "location": "Asunción, Paraguay",
                "linkedin": "linkedin.com/in/anagomez"},
    "summary": "",
    "experience": [{
        "organization": "Acme & Co. S.A.", "title": "Backend Senior",
        "location": "Asunción", "dates": "2021 - Presente",
        "bullets": ["Procesé 12.000 transacciones con 99,95% de uptime",
                    "Reduje la latencia 75% usando C#, R&D y $USD"],
    }],
    "education": [{"institution": "UNA", "degree": "Ingeniería Informática",
                   "location": "San Lorenzo", "dates": "2014 - 2019", "details": []}],
    "extras": [],
    "skills": ["Python", "C++", "AWS"],
}


def test_escapa_todos_los_caracteres_especiales():
    assert escape_tex("100% & $5 #1 a_b {x} ~ ^ \\") == (
        r"100\% \& \$5 \#1 a\_b \{x\} \textasciitilde{} \textasciicircum{} \textbackslash{}"
    )


def test_el_backslash_se_escapa_primero():
    """Si no, los backslashes que introducen los demás escapes se re-escaparían."""
    assert escape_tex("a\\b") == r"a\textbackslash{}b"


def test_normaliza_tipografia_de_word():
    assert normalize_unicode("“hola” – mundo… • ítem") == '"hola" - mundo... - ítem'
    assert normalize_unicode("antes — después") == "antes --- después"


def test_descarta_emoji_que_ningun_motor_compone():
    assert "🚀" not in normalize_unicode("Crecimiento 🚀 sostenido")


def test_conserva_acentos_latinos():
    assert normalize_unicode("Asunción, Ñandutí") == "Asunción, Ñandutí"


def test_escape_url_agrega_esquema_y_protege_porcentajes():
    assert escape_url("linkedin.com/in/ana") == "https://linkedin.com/in/ana"
    assert escape_url("https://x.com/a%20b") == r"https://x.com/a\%20b"


def test_inyeccion_de_latex_queda_neutralizada():
    """El texto viene del usuario y del LLM: no puede ejecutar comandos."""
    tex = render_tex({**CV, "contact": {**CV["contact"],
                                        "full_name": r"\input{/etc/passwd}"}})
    assert r"\input{/etc/passwd}" not in tex
    assert r"\textbackslash{}input" in tex


def test_la_plantilla_no_usa_paquetes_prohibidos():
    """fontspec rompe pdflatex; fontawesome rompe la extracción de texto del ATS."""
    import re

    cargados = set(re.findall(r"\\usepackage(?:\[[^\]]*\])?\{([^}]+)\}", render_tex(CV)))
    prohibidos = {"fontspec", "fontawesome", "fontawesome5", "multicol", "tikz",
                  "graphicx", "moderncv", "altacv"}
    assert not (cargados & prohibidos), f"paquete prohibido: {cargados & prohibidos}"
    assert "geometry" in cargados and "hyperref" in cargados


def test_no_incluye_foto():
    tex = render_tex(CV)
    assert "includegraphics" not in tex


# --- los que necesitan un motor de LaTeX de verdad ---------------------------

tectonic = TectonicCompiler()
needs_tectonic = pytest.mark.skipif(not tectonic.available(), reason="tectonic no instalado")


@pytest.fixture(scope="module")
def compiled():
    workdir = Path(tempfile.mkdtemp())
    yield tectonic.compile(render_tex(CV), workdir), workdir
    shutil.rmtree(workdir, ignore_errors=True)


@needs_tectonic
def test_compila_y_da_una_pagina(compiled):
    pdf_path, _ = compiled
    assert pdf_path.exists() and page_count(pdf_path) == 1


@needs_tectonic
def test_el_ats_puede_leer_todo(compiled):
    """La prueba real de ATS: extraer el texto del PDF y buscar los datos."""
    pdf_path, _ = compiled
    assert ats_check(pdf_path, CV) == []


@needs_tectonic
def test_los_acentos_sobreviven_a_la_extraccion(compiled):
    """Sin cmap y sin fontenc, un ATS leería 'Asunci n' y el CV pierde keywords."""
    pdf_path, _ = compiled
    texto = pdf_text(pdf_path)
    for palabra in ["Ana Gómez Ríos", "Asunción", "Ingeniería Informática", "99,95%"]:
        assert palabra in texto


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex no instalado")
def test_el_mismo_tex_compila_con_pdflatex():
    """El .tex es descargable y Overleaf usa pdfLaTeX: tiene que compilar sin cambios."""
    workdir = Path(tempfile.mkdtemp())
    (workdir / "cv.tex").write_text(render_tex(CV), encoding="utf-8")
    result = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "cv.tex"],
        cwd=workdir, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout[-2000:]
    assert (workdir / "cv.pdf").exists()
    shutil.rmtree(workdir, ignore_errors=True)
