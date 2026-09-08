"""Extracción de texto de PDFs de diseño.

Un CV hecho en Canva o Figma se extrae en el orden del content stream, que no es el
orden en que se lee. Con un CV real de tres columnas eso hacía que los bullets de un
empleo terminaran asignados al otro: en el texto plano los dos cargos salían juntos y
las dos empresas juntas, lejos de sus bullets, y ningún modelo puede desambiguar eso.
"""



from apps.jobs.extract import (
    _columns,
    _join_line,
    _read_in_columns,
    clean_text,
    cv_hash,
    resolve_cv_text,
)


def frag(x, y, text, size=10.0):
    return {"x": x, "y": y, "text": text, "size": size}


# --- detección de columnas --------------------------------------------------


def test_separa_columnas_por_el_hueco_horizontal():
    fragments = [frag(10, 100, "izq"), frag(12, 120, "izq2"),
                 frag(400, 100, "der"), frag(402, 120, "der2")]
    columnas = _columns(fragments)
    assert len(columnas) == 2
    assert [f["text"] for f in columnas[0]] == ["izq", "izq2"]


def test_una_sola_columna_queda_intacta():
    fragments = [frag(10, 100, "a"), frag(12, 120, "b"), frag(14, 140, "c")]
    assert len(_columns(fragments)) == 1


# --- unión de renglones -----------------------------------------------------


def test_une_sin_espacio_los_fragmentos_pegados():
    """Canva emite la primera letra como fragmento aparte: sin esto el email sale
    partido como 'h ola@ejemplo.com'."""
    linea = _join_line([frag(1743, 531, "h", 41.7), frag(1766, 531, "ola@ejemplo.com", 41.7)])
    assert linea == "hola@ejemplo.com"


def test_une_con_espacio_los_fragmentos_separados():
    assert _join_line([frag(0, 10, "Mendoza,", 10), frag(200, 10, "Argentina", 10)]) == \
        "Mendoza, Argentina"


# --- orden de lectura -------------------------------------------------------


class PaginaFalsa:
    """Página que emite fragmentos posicionados, como hace el visitor de pypdf."""

    def __init__(self, fragments):
        self.fragments = fragments

    def extract_text(self, visitor_text=None, **kwargs):
        for f in self.fragments:
            # cm identidad: la posición ya viene en coordenadas de página.
            visitor_text(f["text"], [1, 0, 0, 1, 0, 0],
                         [1, 0, 0, 1, f["x"], f["y"]], {}, f["size"])
        return ""


def test_reconstruye_el_orden_de_lectura_de_un_cv_de_tres_columnas():
    """El caso real: el stream emite el segundo empleo antes que el primero, y los
    cargos separados de sus bullets."""
    pagina = PaginaFalsa([
        frag(1743, 100, "Formación Académica"),      # barra lateral, sale primero
        frag(544, 500, "Bullet del segundo empleo"),  # segundo empleo antes que el primero
        frag(544, 400, "COACH DEPORTIVO"),
        frag(544, 200, "PREPARADOR FÍSICO"),
        frag(544, 300, "Bullet del primer empleo"),
        frag(141, 150, "2020 - 2022"),                # columna de fechas
    ])
    lineas = _read_in_columns(pagina).splitlines()
    lineas = [line for line in lineas if line.strip()]

    # Cada cargo tiene que quedar antes de su propio bullet.
    assert lineas.index("PREPARADOR FÍSICO") < lineas.index("Bullet del primer empleo")
    assert lineas.index("COACH DEPORTIVO") < lineas.index("Bullet del segundo empleo")
    # Y el primer empleo antes que el segundo.
    assert lineas.index("PREPARADOR FÍSICO") < lineas.index("COACH DEPORTIVO")
    # La barra lateral va al final, no interrumpiendo la experiencia.
    assert lineas.index("Formación Académica") > lineas.index("COACH DEPORTIVO")


def test_descarta_el_fragmento_agregado_de_pypdf():
    """pypdf a veces emite además un fragmento con TODO el texto de la página; si se
    deja, duplica el contenido entero."""
    pagina = PaginaFalsa([
        frag(10, 100, "uno"), frag(10, 120, "dos"), frag(10, 140, "tres"),
        frag(10, 160, "cuatro"), frag(0, 200, "uno\ndos\ntres\ncuatro\ncinco"),
    ])
    assert _read_in_columns(pagina).count("uno") == 1


# --- ligaduras --------------------------------------------------------------


def test_normaliza_ligaduras():
    """Un ATS que busca 'planificación' no encuentra 'planiﬁcación' con U+FB01."""
    assert clean_text("planiﬁcación y perﬁles") == "planificación y perfiles"
    assert clean_text("deﬁnición ﬂuida") == "definición fluida"


# --- comportamiento general -------------------------------------------------


def test_el_texto_pegado_le_gana_al_archivo():
    texto = "Ana Gómez, desarrolladora backend con experiencia en Python. " * 5
    resultado, metodo = resolve_cv_text(texto, "cv.pdf", b"%PDF-falso")
    assert metodo == "pegado" and "Ana Gómez" in resultado


def test_el_hash_ignora_espaciado_y_mayusculas():
    """Es el multiplicador de cupo: el mismo CV reformateado no paga otro parse."""
    assert cv_hash("Ana Gomez\n\n  Backend  Dev") == cv_hash("ana gomez backend dev")


def test_pdf_de_una_columna_sigue_funcionando():
    """El reordenamiento no puede empeorar el caso simple."""
    from apps.resume.compile import TectonicCompiler
    from apps.resume.render import render_tex

    compiler = TectonicCompiler()
    if not compiler.available():
        import pytest

        pytest.skip("tectonic no instalado")

    import tempfile
    from pathlib import Path

    cv = {
        "contact": {"full_name": "Ana Gómez Ríos", "email": "ana@example.com",
                    "location": "Asunción"},
        "summary": "", "extras": [], "skills": ["Python"],
        "experience": [{
            "organization": "Acme", "title": "Dev", "location": "Asunción",
            "dates": "2020 - 2024",
            # Suficiente texto para superar MIN_TEXT_CHARS y no disparar el OCR.
            "bullets": [
                "Construí la API de pagos que procesa doce mil transacciones diarias",
                "Reduje la latencia del checkout usando caché distribuido en Redis",
                "Lideré la migración del monolito a servicios sin interrupciones",
                "Definí el pipeline de integración continua del equipo completo",
                "Mentoreé a cuatro desarrolladores junior en prácticas de código",
            ],
        }],
        "education": [{"institution": "UNA", "degree": "Ingeniería", "location": "Asunción",
                       "dates": "2014 - 2019", "details": []}],
    }
    workdir = Path(tempfile.mkdtemp())
    pdf = compiler.compile(render_tex(cv), workdir)
    texto, metodo = resolve_cv_text("", "cv.pdf", pdf.read_bytes())

    assert metodo == "pdf"
    for esperado in ["Ana Gómez Ríos", "Acme", "Construí la API de pagos", "UNA"]:
        assert esperado in texto, f"se perdió {esperado!r} al reordenar"
    # El cargo tiene que seguir junto a su empresa, no desplazado al final.
    assert abs(texto.index("Acme") - texto.index("Dev")) < 120

    import shutil

    shutil.rmtree(workdir, ignore_errors=True)


